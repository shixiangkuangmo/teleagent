'use strict';
// 面板登录：会话 Cookie + Basic 双通道
//   - 页面/浏览器：登录页 POST /api/panel/login → 签名 Cookie（HttpOnly, SameSite=Strict, 7 天）
//   - API/脚本：Authorization: Basic（照旧支持）
// 启用方式（任选其一）：
//   1) env TA_PANEL_USER（默认 admin）+ TA_PANEL_PASSWORD
//   2) env TA_PANEL_PASSWORD_HASH —— sha256("user:password") 的 64 位 hex
//   3) data/config.json -> { "panel": { "pass_hash": "<同上 hex>" } }
// 三者都未配置 = 不启用（本机 127.0.0.1 模式默认）。
// Cookie 密钥由 pass_hash 派生：密码不变则重启后会话仍有效，改密码则全部失效。
const crypto = require('crypto');

const COOKIE_NAME = 'ta_panel';
const COOKIE_TTL_MS = 7 * 24 * 3600 * 1000; // 7 天
// 暴力破解节流：10 分钟内连续失败 N 次后锁定该窗口剩余时间
const FAIL_WINDOW_MS = 10 * 60 * 1000;
const FAIL_LIMIT = 8;

function sha256(s) { return crypto.createHash('sha256').update(s).digest('hex'); }
function hmacHex(key, msg) { return crypto.createHmac('sha256', key).update(msg).digest('hex'); }
function safeEq(a, b) {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

class PanelAuth {
  constructor(config, log) {
    this.log = log;
    this.user = String(process.env.TA_PANEL_USER || 'admin').trim() || 'admin';
    let hash = String(process.env.TA_PANEL_PASSWORD_HASH || '').trim().toLowerCase();
    const pass = String(process.env.TA_PANEL_PASSWORD || '');
    if (!hash && pass) hash = sha256(this.user + ':' + pass);
    if (!hash && config && config.panel && config.panel.pass_hash) {
      hash = String(config.panel.pass_hash).trim().toLowerCase();
    }
    this.enabled = /^[0-9a-f]{64}$/.test(hash);
    this.hash = this.enabled ? hash : '';
    this.showHint = process.env.TA_PANEL_HINT === '1';
    // Cookie 签名密钥：派生自 pass_hash（内存中），不额外落盘
    this._cookieKey = crypto.createHash('sha256').update('ta-panel-cookie-v1:' + (this.hash || 'none')).digest();
    this._fails = [];
  }

  // ---------- 失败节流（登录表单与 Basic 共用，防暴力破解） ----------
  _pruneFails() {
    const now = Date.now();
    this._fails = this._fails.filter((t) => now - t < FAIL_WINDOW_MS);
  }

  lockRemainMin() {
    this._pruneFails();
    if (this._fails.length < FAIL_LIMIT) return 0;
    return Math.max(1, Math.ceil((FAIL_WINDOW_MS - (Date.now() - this._fails[0])) / 60000));
  }

  _recordFail() { this._fails.push(Date.now()); }

  // ---------- 凭证校验（登录表单用，带节流） ----------
  verifyLogin(user, password) {
    if (!this.enabled) return { ok: true };
    const wait = this.lockRemainMin();
    if (wait) return { ok: false, error: `尝试次数过多，约 ${wait} 分钟后再试` };
    const pair = String(user || '') + ':' + String(password || '');
    let ok = false;
    if (pair.length <= 256) {
      ok = safeEq(sha256(pair), this.hash);
    }
    if (ok) { this._fails = []; return { ok: true }; }
    this._recordFail();
    return { ok: false, error: '用户名或密码不对' };
  }

  // ---------- Cookie ----------
  issueCookie(req) {
    return this._cookie(req, Date.now() + COOKIE_TTL_MS, Math.floor(COOKIE_TTL_MS / 1000));
  }
  clearCookie(req) {
    return this._cookie(req, 0, 0);
  }
  _cookie(req, exp, maxAge) {
    const parts = [
      `${COOKIE_NAME}=${exp}.${hmacHex(this._cookieKey, String(exp))}`,
      'Path=/', 'HttpOnly', 'SameSite=Strict', `Max-Age=${maxAge}`,
    ];
    // 经 HTTPS（直连 https 或 CF Tunnel 的 x-forwarded-proto）时加 Secure
    const fwd = String((req.headers && req.headers['x-forwarded-proto']) || '').toLowerCase();
    if (fwd === 'https' || (req.socket && req.socket.encrypted)) parts.push('Secure');
    return parts.join('; ');
  }
  _verifyCookie(req) {
    const raw = String(req.headers.cookie || '');
    const m = new RegExp(COOKIE_NAME + '=([0-9]+)\\.([0-9a-f]{64})').exec(raw);
    if (!m) return false;
    const exp = m[1];
    if (Date.now() > Number(exp)) return false;
    return safeEq(m[2], hmacHex(this._cookieKey, exp));
  }

  // ---------- 请求门禁 ----------
  // 未启用放行；Basic 或 Cookie 任一通过放行。
  // 页面请求未认证 → 302 登录页；API 请求未认证 → 401（不带 WWW-Authenticate，避免浏览器弹原生框）
  check(req, res) {
    if (!this.enabled) return true;
    if (this._verify(req) || this._verifyCookie(req)) return true;
    const accept = String(req.headers.accept || '');
    const wantsHtml = /text\/html/.test(accept) || /application\/xhtml/.test(accept);
    const isPage = (req.method === 'GET' || req.method === 'HEAD') && !/api\//.test(req.url) && wantsHtml;
    if (isPage) {
      res.writeHead(302, {
        'cache-control': 'no-store',
        location: '/login' + (this.showHint ? `?hint=1&user=${encodeURIComponent(this.user)}` : ''),
      });
      res.end();
    } else {
      res.writeHead(401, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
      res.end(JSON.stringify({ ok: false, error: 'unauthorized' }));
    }
    return false;
  }

  _verify(req) {
    const m = /^Basic ([A-Za-z0-9+/=]+)$/.exec(String(req.headers.authorization || ''));
    if (!m) return false;
    if (this.lockRemainMin()) return false;
    let pair = '';
    try { pair = Buffer.from(m[1], 'base64').toString('utf8'); } catch { return false; }
    if (!pair || pair.length > 256) { this._recordFail(); return false; }
    const ok = safeEq(sha256(pair), this.hash);
    if (!ok) this._recordFail();
    return ok;
  }
}

module.exports = PanelAuth;
