// 鉴权模块：凭据来源 = 页面「账号登录」（lib/login）→ JWT 落盘 data/jwt.txt
// 旧的本机客户端 token.json 解密链路已移除（异地部署工具无需客户端，统一登录获取）
// 自动续期：登录态剩余不足 reloginDays 天时自动重登（需保存密码）
'use strict';

const fs = require('fs');
const path = require('path');

class Auth {
  constructor(config, log, login) {
    this.config = config;
    this.log = log;
    this.login = login || null;      // lib/login 实例（账号密码登录）
    this.jwt = null;
    this.jwtExp = null;
    this.listeners = [];
    this.lastTokenMtime = 0;
    this.timer = null;
    this.error = null;
    this.lastReloginCheck = 0;
    this.relogging = false;
  }

  onToken(fn) { this.listeners.push(fn); }

  jwtPath() { return this.login ? this.login.jwtPath() : path.join(this.config._file, '..', 'jwt.txt'); }

  status() {
    const a = this.config.auth || {};
    return {
      ok: !!this.jwt,
      expires_at: this.jwtExp ? new Date(this.jwtExp * 1000).toISOString() : null,
      expires_text: this.expiryText(),
      days_left: this.jwtExp ? Math.max(0, ((this.jwtExp * 1000 - Date.now()) / 86400000)).toFixed(1) : null,
      source: this.jwt ? (this.config.token_file ? 'jwt-file' : 'none') : 'none',
      account_masked: this.maskAccount(a.account || ''),
      has_password: !!a.passwordEnc,
      auto_relogin: !!a.autoRelogin,
      relogin_days: a.reloginDays || 7,
      relogin_check_text: a.autoRelogin && a.account && a.passwordEnc
        ? `剩 ${a.reloginDays || 7} 天时自动重登` : '未配置',
      error: this.error,
    };
  }

  maskAccount(acc) {
    if (!acc) return '';
    if (/^\d{11}$/.test(acc)) return acc.slice(0, 3) + '****' + acc.slice(7);
    return acc.length > 4 ? acc.slice(0, 2) + '***' + acc.slice(-1) : acc;
  }

  saveConfig() {
    try {
      fs.writeFileSync(this.config._file, JSON.stringify({ ...this.config, _file: undefined }, null, 2));
    } catch (e) {
      this.log.error('Auth', `配置落盘失败: ${e.message}`);
    }
  }

  expiryText() {
    if (!this.jwtExp) return '未知';
    const d = new Date(this.jwtExp * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  async init() {
    // 启动时若已有登录落盘的 JWT（data/jwt.txt）则直接加载（含 Docker 重启恢复）
    // 注意：加载后必须记录 mtime，否则 60s 后 pollTokenFile 会把同一文件误判为「更新」，
    // 重复触发 onToken → scheduler.start() 再注册一个积分轮询定时器（积分日志刷屏的根源）
    const startupPath = this.config.token_file && fs.existsSync(this.config.token_file) ? this.config.token_file : this.jwtPath();
    if (startupPath && fs.existsSync(startupPath)) {
      const raw = fs.readFileSync(startupPath, 'utf8').trim();
      this.lastTokenMtime = fs.statSync(startupPath).mtimeMs;
      this.acceptJwt(raw.split(/\r?\n/)[0].trim(), 'jwt-file');
    } else {
      this.error = '待登录：请在「🔑 账号登录」输入手机号+密码获取凭据';
      this.log.warn('Auth', this.error);
    }
    // 每分钟：检查 JWT 文件更新 + 登录态自动续期
    this.timer = setInterval(() => {
      this.pollTokenFile().catch(() => {});
      this.ensureFresh().catch(() => {});
    }, 60 * 1000);
    this.timer.unref();
  }

  acceptJwt(jwt, source) {
    this.jwt = jwt;
    this.error = null;
    try {
      const p = JSON.parse(Buffer.from(jwt.split('.')[1], 'base64').toString('utf8'));
      this.jwtExp = p.exp || null;
    } catch { this.jwtExp = null; }
    this.log.info('Auth', `JWT 已加载（来源 ${source}，长度 ${jwt.length}，过期 ${this.expiryText()}）`);
    for (const fn of this.listeners) {
      try { fn(jwt); } catch (e) { this.log.error('Auth', `回调失败: ${e.message}`); }
    }
  }

  // ---------- 账号密码登录（唯一凭据通道） ----------
  // 登录成功 → JWT 写 data/jwt.txt；remember=true 时保存账号密码（供自动重登）
  async loginAndStore(account, password, opts = {}) {
    if (!this.login) throw new Error('登录模块未初始化');
    const remember = opts.remember !== false;
    const r = await this.login.loginByPassword(String(account).trim(), password);
    const jwt = r.operationToken;

    const jp = this.login.jwtPath();
    fs.writeFileSync(jp, jwt + '\n', { mode: 0o600 });
    this.lastTokenMtime = fs.statSync(jp).mtimeMs;

    if (remember) {
      this.config.auth = Object.assign({}, this.config.auth, {
        account: String(account).trim(),
        passwordEnc: this.login.encryptPassword(password),
      });
    }
    this.config.token_file = jp;
    this.saveConfig();

    this.acceptJwt(jwt, 'password-login');
    return {
      ok: true,
      expires_at: this.status().expires_at,
      expires_text: this.expiryText(),
      days_left: this.status().days_left,
    };
  }

  // 用已保存的账号密码立即重登（手动续期）
  async reloginSaved() {
    const a = this.config.auth || {};
    if (!a.account || !a.passwordEnc) throw new Error('未保存账号密码，请先登录');
    if (this.relogging) throw new Error('重登进行中');
    this.relogging = true;
    try {
      const password = this.login.decryptPassword(a.passwordEnc);
      return await this.loginAndStore(a.account, password, { remember: false });
    } finally {
      this.relogging = false;
    }
  }

  // 登录态保鲜：每 12h 检查一次，剩余天数不足 reloginDays 且配置了账号密码 → 自动重登
  async ensureFresh() {
    if (!this.login) return;
    const now = Date.now();
    if (now - this.lastReloginCheck < 12 * 3600 * 1000) return;
    this.lastReloginCheck = now;
    const a = this.config.auth || {};
    if (!a.autoRelogin || !a.account || !a.passwordEnc) return;
    const daysLeft = this.jwtExp ? (this.jwtExp * 1000 - now) / 86400000 : -1;
    if (daysLeft > Number(a.reloginDays || 7)) return;
    if (this.relogging) return;
    this.relogging = true;
    try {
      const password = this.login.decryptPassword(a.passwordEnc);
      await this.loginAndStore(a.account, password, { remember: false });
      this.log.info('Auth', '自动重登成功，登录态已刷新');
    } catch (e) {
      this.log.error('Auth', `自动重登失败: ${e.message}`);
    } finally {
      this.relogging = false;
    }
  }

  // 清除登录凭据（data/jwt.txt + 已存账号密码），回到待登录状态
  async clearLogin() {
    const a = this.config.auth || {};
    try { fs.unlinkSync(this.jwtPath()); } catch {}
    this.config.token_file = '';
    this.config.auth = { account: '', passwordEnc: '', autoRelogin: a.autoRelogin, reloginDays: a.reloginDays };
    this.saveConfig();
    this.lastTokenMtime = 0;
    this.jwt = null;
    this.jwtExp = null;
    this.error = '已清除登录凭据，请重新登录';
    this.log.info('Auth', '已清除登录凭据');
    return { ok: true };
  }

  async pollTokenFile() {
    const target = this.config.token_file && fs.existsSync(this.config.token_file)
      ? this.config.token_file : this.jwtPath();
    if (!target || !fs.existsSync(target)) return;
    const mtime = fs.statSync(target).mtimeMs;
    if (mtime <= this.lastTokenMtime) return;
    this.log.info('Auth', '检测到 JWT 文件更新，正在重新加载…');
    try {
      const raw = fs.readFileSync(target, 'utf8').trim().split(/\r?\n/)[0].trim();
      this.lastTokenMtime = mtime;
      this.acceptJwt(raw, 'jwt-file');
    } catch (e) {
      this.log.error('Auth', `JWT 文件重载失败: ${e.message}`);
    }
  }
}

module.exports = Auth;
