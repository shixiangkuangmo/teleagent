// 多账号管理层：一个账号一套独立上下文（配置/密钥/JWT/状态/保活轮），互不干扰
//   data/accounts.json           账号索引 [{id,name,createdAt}]
//   data/accounts/<id>/config.json  该账号的 auth/keepalive/points/power
//   data/accounts/<id>/jwt.txt      该账号的 operationToken
//   data/accounts/<id>/.secret      该账号的密码加密密钥
//   data/accounts/<id>/state.json   该账号的积分领取状态
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const Login = require('./login');
const Auth = require('./auth');
const Api = require('./api');
const KeepAlive = require('./keepalive');
const Power = require('./power');
const Scheduler = require('./scheduler');

const STAGGER_MS = 4000; // 账号间启动错峰，避免几十个号同时打平台

function accDefaults() {
  return {
    token_file: '',
    auth: { account: '', passwordEnc: '', autoRelogin: true, reloginDays: 7 },
    keepalive: { enabled: true, mode: 'ticket', refreshSec: 300 },
    points: { claim_time: '08:05', catchup_on_start: true, poll_minutes: 10 },
    power: { restart_plan: { enabled: false, mode: 'daily', time: '04:30', weekday: 0 } },
    api_base: 'https://agent.teleai.com.cn/superCowork/sapi',
  };
}
function deepMerge(a, b) {
  const out = { ...a };
  if (!b || typeof b !== 'object') return out;
  for (const k of Object.keys(b)) {
    if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k]) && a[k] && typeof a[k] === 'object' && !Array.isArray(a[k])) {
      out[k] = deepMerge(a[k], b[k]);
    } else if (b[k] !== undefined) out[k] = b[k];
  }
  return out;
}

class AccountManager {
  constructor({ dataDir, log, notifier, offline }) {
    this.dataDir = dataDir;
    this.root = path.join(dataDir, 'accounts');
    this.indexFile = path.join(dataDir, 'accounts.json');
    this.log = log;
    this.notifier = notifier;
    this.offline = !!offline;
    this.ctxs = new Map();
    this.seq = 0;
    fs.mkdirSync(this.root, { recursive: true });
  }

  /* ---------- 索引 ---------- */
  loadIndex() {
    try {
      const j = JSON.parse(fs.readFileSync(this.indexFile, 'utf8'));
      if (Array.isArray(j.accounts)) return j.accounts;
    } catch {}
    return [];
  }
  saveIndex(list) {
    fs.writeFileSync(this.indexFile, JSON.stringify({ accounts: list }, null, 2));
  }

  /* ---------- 账号上下文 ---------- */
  scopedLog(name, id) {
    const tag = `${name || id}`;
    const L = this.log;
    return {
      info: (s, m) => L.info(`${tag}·${s}`, m),
      warn: (s, m) => L.warn(`${tag}·${s}`, m),
      error: (s, m) => L.error(`${tag}·${s}`, m),
      debug: (s, m) => (L.debug ? L.debug(`${tag}·${s}`, m) : null),
    };
  }

  loadAccountConfig(dir) {
    const fp = path.join(dir, 'config.json');
    let cur = {};
    try { cur = JSON.parse(fs.readFileSync(fp, 'utf8')); } catch {}
    const merged = deepMerge(accDefaults(), cur);
    fs.writeFileSync(fp, JSON.stringify(merged, null, 2));
    merged._file = fp;
    return merged;
  }

  buildCtx(meta) {
    const { id, name } = meta;
    const dir = path.join(this.root, id);
    fs.mkdirSync(dir, { recursive: true });
    const log = this.scopedLog(name, id);
    const config = this.loadAccountConfig(dir);

    const login = new Login(dir, log);
    const auth = new Auth(config, log, login);
    const api = new Api(auth, config, log);
    const ka = new KeepAlive(auth, config, log, this.notifier);
    const power = new Power(config, log, this.notifier, ka);
    const scheduler = new Scheduler({ config, log, api, ka, power, notifier: this.notifier });
    power.setBusyCheck(() => scheduler.running);

    const ctx = { id, name, dir, config, log, login, auth, api, ka, power, scheduler, svcTimer: null, started: false };

    auth.onToken(async (jwt) => {
      // 凭据更新必须无条件重新绑定：自动重登/外部刷新后如果直接 return，
      // Api/KeepAlive 会一直拿着旧 JWT（表现为积分刷新持续 401，且界面无异常）。
      api.bindToken(jwt);
      ka.bindToken(jwt);
      if (ctx.started) {
        log.info('Auth', `会话凭据已更新（有效期至 ${auth.expiryText()}）`);
        return;
      }
      ctx.started = true;                // 业务循环只启动一次
      log.info('Auth', `会话凭据已加载（有效期至 ${auth.expiryText()}），开始初始化`);
      if (this.offline) { log.warn('System', '离线模式，跳过平台连接'); return; }
      await api.refreshPoints().catch(() => {});
      // 积分任务先排程（首登/月度/每日），不等待云电脑领取
      scheduler.start();
      if (config.keepalive.enabled) {
        // 领取云电脑（无实例时 instance/create）+ 启动/换票，完成后再进入保活循环
        try {
          const ir = await ka.ensureInstance();
          log.info('KeepAlive', `云电脑已就绪（${ir.id}，${ir.created ? '本次新领取' : '已有实例'}）`);
        } catch (e) {
          log.warn('KeepAlive', `领取/启动云电脑失败：${e.message}`);
        }
        ka.start(jwt);
      }
      ka.pollServiceStatus().catch(() => {});
      if (ctx.svcTimer) clearInterval(ctx.svcTimer);
      ctx.svcTimer = setInterval(() => ka.pollServiceStatus().catch(() => {}), 10 * 60 * 1000);
      ctx.svcTimer.unref && ctx.svcTimer.unref();
    });

    return ctx;
  }

  saveAccountConfig(ctx) {
    const out = { ...ctx.config };
    delete out._file;
    fs.writeFileSync(ctx.config._file, JSON.stringify(out, null, 2));
  }

  // 保活开关：运行时状态与 config 保持同一份真源，并立即落盘
  setKeepalive(id, enabled) {
    const ctx = this.get(id);
    if (!ctx) throw new Error('账号不存在');
    const r = ctx.ka.setEnabled(typeof enabled === 'boolean' ? enabled : !ctx.ka.enabled);
    this.saveAccountConfig(ctx);
    return r;
  }

  async startCtx(ctx, delayMs) {
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    try { await ctx.auth.init(); }
    catch (e) { ctx.log.error('Auth', `初始化失败: ${e.message}`); }
  }

  /* ---------- 生命周期 ---------- */
  async init() {
    const idx = this.loadIndex();
    if (!idx.length) this.migrateLegacy();
    const list = this.loadIndex();
    let i = 0;
    for (const meta of list) {
      const ctx = this.buildCtx(meta);
      this.ctxs.set(meta.id, ctx);
      this.startCtx(ctx, i * STAGGER_MS).catch(() => {});
      i++;
    }
    if (list.length) this.log.info('System', `已加载 ${list.length} 个账号，错峰启动中`);
    return list.length;
  }

  // 老版本单账号数据迁移成第一个账号（保留已登录的 jwt，不用重登）
  migrateLegacy() {
    const legacyCfgPath = path.join(this.dataDir, 'config.json');
    const legacyJwt = path.join(this.dataDir, 'jwt.txt');
    let cfg = null;
    try { cfg = JSON.parse(fs.readFileSync(legacyCfgPath, 'utf8')); } catch {}
    const hasJwt = fs.existsSync(legacyJwt);
    if (!cfg && !hasJwt) return;
    const account = (cfg && cfg.auth && cfg.auth.account) || 'default';
    const id = this.newId(account);
    const dir = path.join(this.root, id);
    fs.mkdirSync(dir, { recursive: true });
    if (cfg) fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(cfg, null, 2));
    if (hasJwt) fs.copyFileSync(legacyJwt, path.join(dir, 'jwt.txt'));
    for (const f of ['.secret', 'state.json']) {
      const src = path.join(this.dataDir, f);
      if (fs.existsSync(src)) fs.copyFileSync(src, path.join(dir, f));
    }
    this.saveIndex([{ id, name: account, createdAt: new Date().toISOString(), migrated: true }]);
    this.log.info('System', `已把旧版单账号数据迁移为账号「${account}」（${id}）`);
  }

  newId(name) {
    const slug = String(name || 'acc').toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 10) || 'acc';
    return `${slug}${Date.now().toString(36).slice(-5)}${(this.seq++ % 36).toString(36)}`;
  }

  /* ---------- 增删查 ---------- */
  get(id) { return this.ctxs.get(id) || null; }

  list() { return [...this.ctxs.values()]; }

  // 新建账号：立刻登录一次拿 JWT，失败则回滚（不留下空壳）
  async create({ account, password, name, remember = true }) {
    account = String(account || '').trim();
    if (!account || !password) throw new Error('账号和密码都要填');
    const dup = this.list().find((c) => (c.config.auth.account || '').toLowerCase() === account.toLowerCase());
    if (dup) throw new Error(`账号 ${account} 已存在`);

    const id = this.newId(account);
    const meta = { id, name: String(name || account).trim(), createdAt: new Date().toISOString() };
    const ctx = this.buildCtx(meta);
    this.ctxs.set(id, ctx);
    try {
      const r = await ctx.auth.loginAndStore(account, password, { remember });
      const idx = this.loadIndex();
      idx.push(meta);
      this.saveIndex(idx);
      // onToken 回调通常已把业务循环拉起来了，这里只兜底一次，避免重复注册定时器/重复领取
      if (!ctx.started) {
        ctx.started = true;
        ctx.api.bindToken(ctx.auth.jwt);
        ctx.ka.bindToken(ctx.auth.jwt);
        if (!this.offline && ctx.config.keepalive.enabled) {
          try { await ctx.ka.ensureInstance(); } catch (e) { ctx.log.warn('KeepAlive', `领取/启动云电脑失败：${e.message}`); }
          ctx.ka.start(ctx.auth.jwt);
        }
        await ctx.api.refreshPoints().catch(() => {});
        ctx.scheduler.start();
      }
      ctx.log.info('System', `账号已接入（登录成功，凭据有效至 ${r.expires_text}）`);
      return { id, summary: this.summary(ctx), login: r };
    } catch (e) {
      this.ctxs.delete(id);
      try { fs.rmSync(ctx.dir, { recursive: true, force: true }); } catch {}
      throw e;
    }
  }

  async remove(id) {
    const ctx = this.get(id);
    if (!ctx) throw new Error('账号不存在');
    try { ctx.ka.stop(); } catch {}
    try { ctx.scheduler.stop && ctx.scheduler.stop(); } catch {}
    if (ctx.svcTimer) clearInterval(ctx.svcTimer);
    this.ctxs.delete(id);
    this.saveIndex(this.loadIndex().filter((a) => a.id !== id));
    try { fs.rmSync(ctx.dir, { recursive: true, force: true }); } catch {}
    this.log.info('System', `已删除账号 ${ctx.name}（${id}）`);
    return true;
  }

  /* ---------- 状态 ---------- */
  summary(ctx) {
    const a = ctx.auth.status();
    const k = ctx.ka.status();
    const p = ctx.api.pointsSnapshot();
    const pw = ctx.power.status();
    const ss = k.service_status || {};
    const sc = ctx.scheduler.status();
    // 综合健康度：凭据 + 实例 + 平台服务
    let health = 'ok';
    if (!a.ok || k.state === 'error' || ss.error) health = 'err';
    else if (k.state !== 'online' || k.instance_status === 3) health = 'warn';
    return {
      id: ctx.id,
      name: ctx.name,
      account: ctx.config.auth.account || '',
      account_masked: a.account_masked,
      health,
      enabled: !!ctx.ka.enabled,
      auth: {
        ok: a.ok, expires_text: a.expires_text, days_left: a.days_left,
        source: a.source, has_password: a.has_password, relogin_check_text: a.relogin_check_text,
        error: a.error,
      },
      keepalive: {
        enabled: !!ctx.ka.enabled,
        state: k.state,
        instance_id: k.instance_id,
        instance_status: k.instance_status,
        instance_status_text: k.instance_status_text,
        last_ticket_text: k.last_ticket_text,
        cycles: k.cycles, heartbeats: k.heartbeats, healed: k.healed,
        refresh_sec: ctx.config.keepalive.refreshSec,
        service_ok: !!ss.ok,
        last_error: k.last_error || '',
      },
      points: { balance: p.balance, plan: p.plan, error: p.error },
      power: { restart_plan_text: pw.restart_plan_text, restart_plan: pw.restart_plan },
      scheduler: { claim_time: sc.claim_time, next_claim_text: sc.next_claim_text, claiming: sc.claiming, last_claim_date: sc.last_claim_date },
    };
  }

  summaries() { return this.list().map((c) => this.summary(c)); }

  /* ---------- 批量 ---------- */
  async batch(action, ids) {
    const ALLOWED = ['reconnect', 'toggle', 'claim', 'relogin'];
    if (!ALLOWED.includes(action)) throw new Error('未知批量操作: ' + action);
    const targets = (ids && ids.length ? ids : this.list().map((c) => c.id))
      .map((id) => this.get(id)).filter(Boolean);
    const out = [];
    for (const ctx of targets) {
      try {
        let r = { ok: true };
        if (action === 'reconnect') r = await ctx.ka.reconnect();
        else if (action === 'toggle') { const t = ctx.ka.toggle(); this.saveAccountConfig(ctx); r = { ok: true, enabled: t.enabled }; }
        else if (action === 'claim') r = await ctx.scheduler.triggerClaim('manual');
        else if (action === 'relogin') r = await ctx.auth.reloginSaved();
        else throw new Error('未知批量操作: ' + action);
        out.push({ id: ctx.id, name: ctx.name, ...r });
      } catch (e) {
        out.push({ id: ctx.id, name: ctx.name, ok: false, error: e.message });
      }
    }
    return { action, total: out.length, results: out };
  }

  stopAll() {
    for (const ctx of this.list()) {
      try { ctx.ka.stop(); } catch {}
      if (ctx.svcTimer) clearInterval(ctx.svcTimer);
    }
  }
}

module.exports = AccountManager;
