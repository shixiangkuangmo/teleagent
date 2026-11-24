// teleagent-dashboard — 多账号保活 / 积分 / 电源管理控制台
// 每个账号独立上下文（配置、密钥、JWT、保活轮、领取计划），启动错峰互不干扰。
// 鉴权：面板密码（TA_PANEL_PASSWORD 或 HASH）；平台账号密码以 AES-256-GCM 密文分账号落盘。
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

const Log = require('./lib/log');
const Notifier = require('./lib/notify');
const PanelAuth = require('./lib/panelauth');
const SysMon = require('./lib/sysmon');
const AccountManager = require('./lib/accounts');
const GatewayPool = require('./lib/gatewaypool');

const ROOT = __dirname;
const DATA_DIR = process.env.TA_DATA_DIR || path.join(ROOT, 'data');
const PORT = parseInt(process.env.PORT || '8590', 10);

fs.mkdirSync(DATA_DIR, { recursive: true });

const log = new Log(DATA_DIR);
const sysmon = new SysMon();
const config = loadGlobalConfig();
const notifier = new Notifier(config, log);
const panelAuth = new PanelAuth(config, log);
const manager = new AccountManager({
  dataDir: DATA_DIR,
  log,
  notifier,
  offline: process.env.TA_OFFLINE === '1',
});
const pool = new GatewayPool({ dataDir: DATA_DIR, log, notifier });

function loadGlobalConfig() {
  const def = {
    notify: { enabled: false, wecomWebhook: '', cooldownMin: 30 },
    api_base: 'https://agent.teleai.com.cn/superCowork/sapi',
  };
  const fp = path.join(DATA_DIR, 'config.json');
  let cur = {};
  try { cur = JSON.parse(fs.readFileSync(fp, 'utf8')); } catch {}
  const merged = deepMerge(def, cur);
  merged._file = fp;
  return merged;
}
function saveGlobalConfig() {
  const out = { ...config };
  delete out._file;
  fs.writeFileSync(config._file, JSON.stringify(out, null, 2));
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

function statusPayload() {
  const accounts = manager.summaries();
  const num = (v) => parseFloat(v) || 0;
  return {
    time: new Date().toISOString(),
    system: sysmon.snapshot(),
    accounts,
    totals: {
      accounts: accounts.length,
      online: accounts.filter((a) => a.keepalive.state === 'online').length,
      warn: accounts.filter((a) => a.health === 'warn').length,
      error: accounts.filter((a) => a.health === 'err').length,
      points: Math.round(accounts.reduce((s, a) => s + num(a.points.balance), 0) * 100) / 100,
      days_min: accounts.length ? Math.min(...accounts.map((a) => num(a.auth.days_left) || 999)) : null,
    },
    notify: { enabled: !!config.notify.enabled, has_webhook: !!config.notify.wecomWebhook },
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const p = url.pathname;

  if (req.method === 'GET' && (p === '/login' || p === '/login.html')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
    res.end(fs.readFileSync(path.join(ROOT, 'public', 'login.html')));
    return;
  }
  if (req.method === 'POST' && p === '/api/panel/login') {
    let b;
    try { b = await readJson(req); } catch (e) { return json(res, { ok: false, error: e.message }, e.statusCode || 400); }
    const r = panelAuth.verifyLogin(b.user, b.password);
    if (!r.ok) {
      log.warn('PanelAuth', `面板登录失败：${r.error}`);
      return json(res, { ok: false, error: r.error }, 401);
    }
    res.setHeader('set-cookie', panelAuth.issueCookie(req));
    log.info('PanelAuth', '面板登录成功');
    json(res, { ok: true });
    return;
  }
  if (req.method === 'POST' && p === '/api/panel/logout') {
    res.setHeader('set-cookie', panelAuth.clearCookie(req));
    json(res, { ok: true });
    return;
  }

  // 池接口走独立鉴权（池 API Key），必须在面板 Cookie 门禁之前
  if (p === '/v1' || p.startsWith('/v1/')) {
    pool.handleV1(req, res, url);
    return;
  }

  if (!panelAuth.check(req, res)) return;

  try {
    if (req.method === 'GET' && (p === '/' || p === '/index.html')) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
      res.end(fs.readFileSync(path.join(ROOT, 'public', 'index.html')));
      return;
    }

    if (req.method === 'GET' && p === '/api/status') return json(res, statusPayload());
    if (req.method === 'GET' && p === '/api/logs') return json(res, { logs: log.tail(200).slice().reverse() });

    if (p === '/api/accounts') {
      if (req.method === 'GET') return json(res, { accounts: manager.summaries() });
      if (req.method === 'POST') {
        const b = await readJson(req);
        try {
          const r = await manager.create({
            account: b.account, password: b.password, name: b.name, remember: b.remember !== false,
          });
          return json(res, { ok: true, id: r.id, login: r.login, account: r.summary });
        } catch (e) {
          log.error('Accounts', `新增账号失败：${e.message}`);
          return json(res, { ok: false, error: e.message }, 400);
        }
      }
    }

    if (req.method === 'POST' && p === '/api/batch') {
      const b = await readJson(req);
      try {
        const r = await manager.batch(String(b.action || ''), Array.isArray(b.ids) ? b.ids : null);
        return json(res, { ok: true, ...r });
      } catch (e) {
        return json(res, { ok: false, error: e.message }, 400);
      }
    }

    if (req.method === 'POST' && p === '/api/accounts/bulk') {
      const b = await readJson(req);
      const lines = String(b.text || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
      const results = [];
      for (const line of lines) {
        const parts = line.split(/[\s,\uFF0C\t]+/).filter(Boolean);
        if (parts.length < 2) { results.push({ line, ok: false, error: '格式应为「账号 密码」' }); continue; }
        const [account, password] = parts;
        const name = parts[2] || '';
        try {
          const r = await manager.create({ account, password, name, remember: true });
          results.push({ line, ok: true, id: r.id, name: r.summary && r.summary.name });
        } catch (e) {
          results.push({ line, ok: false, error: e.message });
        }
      }
      return json(res, { ok: true, results });
    }

    const ma = p.match(/^\/api\/accounts\/([^/]+)(?:\/(.*))?$/);
    if (ma) {
      const id = decodeURIComponent(ma[1]);
      const rest = ma[2] || '';
      const ctx = manager.get(id);
      if (!ctx) return json(res, { ok: false, error: '账号不存在' }, 404);

      if (req.method === 'GET' && !rest) return json(res, manager.summary(ctx));
      if (req.method === 'DELETE' && !rest) {
        await manager.remove(id);
        return json(res, { ok: true });
      }

      if (req.method === 'POST') {
        try {
          switch (rest) {
            case 'keepalive/toggle': {
              const b = await readJson(req).catch(() => ({}));
              const t = manager.setKeepalive(id, typeof b.enabled === 'boolean' ? b.enabled : !ctx.ka.enabled);
              return json(res, { ok: true, enabled: t.enabled });
            }
            case 'keepalive/reconnect': return json(res, { ok: true, ...(await ctx.ka.reconnect()) });
            case 'desktop/claim': return json(res, { ok: true, ...(await ctx.ka.ensureInstance()) });
            case 'desktop/open': {
              const u = await ctx.ka.freshConnectUrl();
              return json(res, { ok: true, url: u });
            }
            case 'points/refresh': return json(res, { ok: true, ...(await ctx.api.refreshPoints()) });
            case 'points/claim': return json(res, await ctx.scheduler.triggerClaim('manual'));
            case 'power/restart': {
              if (!ctx.auth.status().ok) return json(res, { ok: false, error: '未登录' }, 400);
              return json(res, await ctx.ka.platformRestart());
            }
            case 'power/rebuild': {
              if (String(url.searchParams.get('confirm') || '') !== 'RESET') {
                return json(res, { ok: false, error: '缺少确认参数（confirm=RESET）' }, 400);
              }
              if (!ctx.auth.status().ok) return json(res, { ok: false, error: '未登录' }, 400);
              return json(res, await ctx.ka.platformRebuild());
            }
            case 'power/cancel': {
              if (!ctx.auth.status().ok) return json(res, { ok: false, error: '未登录' }, 400);
              return json(res, await ctx.ka.platformCancel());
            }
            case 'service-status': {
              const ss = await ctx.ka.pollServiceStatus();
              return json(res, { ok: true, status: ss });
            }
            case 'relogin': return json(res, await ctx.auth.reloginSaved());
            case 'logout': return json(res, await ctx.auth.clearLogin());
            case 'login': {
              const b = await readJson(req);
              if (!b.account || !b.password) return json(res, { ok: false, error: '账号和密码都要填' }, 400);
              const r = await ctx.auth.loginAndStore(b.account, b.password, { remember: b.remember !== false });
              return json(res, r);
            }
            case 'config': {
              const b = await readJson(req);
              applyAccountConfig(ctx, b);
              return json(res, { ok: true });
            }
            default: break;
          }
        } catch (e) {
          ctx.log.error('Action', `${rest} 失败: ${e.message}`);
          return json(res, { ok: false, error: e.message }, 500);
        }
      }
      return json(res, { ok: false, error: '未知操作' }, 404);
    }

    // ---------- 网关池管理 ----------
    if (req.method === 'GET' && p === '/api/gateway/overview') return json(res, pool.overview());

    if (p === '/api/gateway/nodes') {
      if (req.method === 'GET') return json(res, { nodes: pool.listNodes() });
      if (req.method === 'POST') {
        const b = await readJson(req);
        try { return json(res, pool.createNode(b)); } catch (e) { return json(res, { ok: false, error: e.message }, e.statusCode || 400); }
      }
    }
    const mn = p.match(/^\/api\/gateway\/nodes\/([^/]+)(?:\/(disconnect))?$/);
    if (mn) {
      const id = decodeURIComponent(mn[1]);
      try {
        if (req.method === 'POST' && mn[2] === 'disconnect') return json(res, pool.disconnectNode(id));
        if (req.method === 'PATCH' && !mn[2]) {
          const b = await readJson(req);
          return json(res, pool.updateNode(id, b));
        }
        if (req.method === 'DELETE' && !mn[2]) return json(res, pool.deleteNode(id));
      } catch (e) { return json(res, { ok: false, error: e.message }, e.statusCode || 400); }
    }

    if (p === '/api/gateway/keys') {
      if (req.method === 'GET') return json(res, { keys: pool.listKeys() });
      if (req.method === 'POST') {
        const b = await readJson(req);
        try { return json(res, pool.createKey(b)); } catch (e) { return json(res, { ok: false, error: e.message }, e.statusCode || 400); }
      }
    }
    const mk = p.match(/^\/api\/gateway\/keys\/([^/]+)$/);
    if (mk) {
      const id = decodeURIComponent(mk[1]);
      try {
        if (req.method === 'PATCH') {
          const b = await readJson(req);
          return json(res, pool.updateKey(id, b));
        }
        if (req.method === 'DELETE') return json(res, pool.deleteKey(id));
      } catch (e) { return json(res, { ok: false, error: e.message }, e.statusCode || 400); }
    }

    if (req.method === 'GET' && p === '/api/gateway/usage') {
      return json(res, pool.overview().usage);
    }

    if (req.method === 'POST' && p === '/api/notify') {
      const patch = await readJson(req);
      config.notify = deepMerge(config.notify || {}, patch);
      saveGlobalConfig();
      notifier.reset();
      return json(res, { ok: true, notify: { enabled: config.notify.enabled, has_webhook: !!config.notify.wecomWebhook } });
    }
    if (req.method === 'POST' && p === '/api/notify/test') {
      const r = await notifier.send('测试通知', `来自 teleagent-dashboard\n时间：${new Date().toLocaleString('zh-CN')}`);
      return json(res, r.ok ? { ok: true } : { ok: false, error: r.error || '未配置 webhook' }, r.ok ? 200 : 400);
    }

    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'not_found' }));
  } catch (e) {
    log.error('System', `请求异常 ${p}: ${e.message}`);
    json(res, { error: String((e && e.message) || e) }, e.statusCode || 500);
  }
});

function applyAccountConfig(ctx, patch) {
  const c = ctx.config;
  if (patch.keepalive && typeof patch.keepalive === 'object') {
    if (patch.keepalive.refreshSec != null) {
      c.keepalive.refreshSec = Math.max(60, parseInt(patch.keepalive.refreshSec, 10) || 300);
      ctx.ka.refreshSec = c.keepalive.refreshSec;
    }
    if (typeof patch.keepalive.enabled === 'boolean') {
      // 同时同步运行时状态与 config.keepalive.enabled，避免两处开关打架
      ctx.ka.setEnabled(patch.keepalive.enabled);
    }
  }
  if (patch.points && typeof patch.points === 'object') {
    if (/^\d{2}:\d{2}$/.test(String(patch.points.claim_time || ''))) {
      c.points.claim_time = patch.points.claim_time;
      ctx.scheduler.reschedule();
    }
  }
  if (patch.power && patch.power.restart_plan && typeof patch.power.restart_plan === 'object') {
    const rp = patch.power.restart_plan;
    const wd = Number(rp.weekday);
    c.power = Object.assign({}, c.power || {}, {
      restart_plan: {
        enabled: !!rp.enabled,
        mode: rp.mode === 'weekly' ? 'weekly' : 'daily',
        time: /^\d{2}:\d{2}$/.test(String(rp.time)) ? String(rp.time) : '04:30',
        weekday: Number.isInteger(wd) && wd >= 0 && wd <= 6 ? wd : 0,
      },
    });
    ctx.power.applyPlan(c.power.restart_plan);
  }
  if (patch.auth && typeof patch.auth === 'object') {
    if (typeof patch.auth.autoRelogin === 'boolean') c.auth.autoRelogin = patch.auth.autoRelogin;
    if (patch.auth.reloginDays != null) c.auth.reloginDays = Math.max(1, parseInt(patch.auth.reloginDays, 10) || 7);
  }
  const out = { ...c };
  delete out._file;
  fs.writeFileSync(c._file, JSON.stringify(out, null, 2));
  ctx.log.info('Config', '配置已更新');
}

const MAX_BODY_BYTES = 1024 * 1024;       // 1MB，面板接口正常请求远小于此
const MAX_DRAIN_BYTES = 32 * 1024 * 1024; // 超限时最多丢弃这么多，避免客户端还在发送时收不到 413
async function readJson(req) {
  const declared = parseInt(req.headers['content-length'] || '0', 10);
  if (declared > MAX_BODY_BYTES) {
    // 先把剩余请求体读掉再回 413，否则直接断连会让部分客户端只看到连接重置
    let drained = 0;
    try {
      for await (const c of req) {
        drained += c.length;
        if (drained > MAX_DRAIN_BYTES) break;
      }
    } catch {}
    const e = new Error('请求体过大');
    e.statusCode = 413;
    throw e;
  }
  let body = '';
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > MAX_BODY_BYTES) {
      const e = new Error('请求体过大');
      e.statusCode = 413;
      throw e;
    }
    body += c;
  }
  try { return JSON.parse(body || '{}'); } catch { return {}; }
}
function json(res, obj, code = 200) {
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(JSON.stringify(obj));
}

const HOST = process.env.TA_HOST || '127.0.0.1';
server.listen(PORT, HOST, async () => {
  pool.start(server);
  const shown = HOST === '0.0.0.0' ? '127.0.0.1（已监听全部网卡）' : HOST;
  log.info('System', `teleagent-dashboard 已就绪: http://${shown}:${PORT}`);
  if (panelAuth.enabled) log.info('System', `面板登录保护已开启（用户 ${panelAuth.user}）`);
  else log.warn('System', '面板登录保护未开启——仅建议本机使用');
  const n = await manager.init().catch((e) => { log.error('System', `账号加载失败: ${e.message}`); return 0; });
  log.info('System', `账号数：${n}`);
});

process.on('SIGINT', () => { try { pool.stop(); } catch {} try { manager.stopAll(); } catch {} process.exit(0); });
process.on('SIGTERM', () => { try { pool.stop(); } catch {} try { manager.stopAll(); } catch {} process.exit(0); });
