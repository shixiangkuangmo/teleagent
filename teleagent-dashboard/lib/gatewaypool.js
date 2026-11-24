'use strict';
// 网关池：Windows 节点通过出站 WebSocket 长连接接入，dashboard 统一提供 OpenAI 兼容 /v1。
// 节点凭据与池 Key 都只存 sha256；明文只在创建时返回一次。
const crypto = require('crypto');
const path = require('path');
const { WebSocketServer } = require('ws');
const store = require('./jsonstore');

const DEFAULT_MODELS = ['chat-flagship', 'chat-pro', 'chat-flash', 'chat-lite', 'chat-nano'];
const MAX_V1_BODY = 8 * 1024 * 1024;
const MAX_WS_PAYLOAD = 16 * 1024 * 1024;
const NODE_OFFLINE_MS = 45 * 1000;
const START_TIMEOUT_MS = 60 * 1000;
const IDLE_TIMEOUT_MS = 90 * 1000;
const TOTAL_TIMEOUT_MS = 30 * 60 * 1000;
const RETAIN_DAYS = 30;
const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET,POST,OPTIONS',
  'access-control-allow-headers': 'authorization,content-type,x-api-key',
  'access-control-max-age': '86400',
};

function sha256(s) { return crypto.createHash('sha256').update(String(s)).digest('hex'); }
function safeEqualHex(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  return crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
}
function todayStr(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function newId(prefix) { return prefix + crypto.randomBytes(6).toString('hex'); }
function newToken(prefix) { return prefix + crypto.randomBytes(18).toString('base64url'); }
function zeroUsage() {
  return { req: 0, stream: 0, err: 0, tokensIn: 0, tokensOut: 0, estTokens: 0 };
}

// 读取请求体；超限时丢弃剩余数据并抛 413
function readRaw(req, limit) {
  return new Promise((resolve, reject) => {
    const declared = parseInt(req.headers['content-length'] || '0', 10);
    if (declared > limit) {
      req.resume();
      const e = new Error('payload too large');
      e.statusCode = 413;
      reject(e);
      return;
    }
    const chunks = [];
    let size = 0;
    let done = false;
    req.on('data', (c) => {
      if (done) return;
      size += c.length;
      if (size > limit) {
        done = true;
        chunks.length = 0;
        req.resume();
        const e = new Error('payload too large');
        e.statusCode = 413;
        reject(e);
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      if (done) return;
      done = true;
      resolve(Buffer.concat(chunks));
    });
    req.on('error', (e) => {
      if (done) return;
      done = true;
      reject(e);
    });
  });
}

class GatewayPool {
  constructor({ dataDir, log, notifier }) {
    this.dataDir = dataDir;
    this.log = log;
    this.notifier = notifier;
    this.nodesFile = path.join(dataDir, 'gateway-nodes.json');
    this.keysFile = path.join(dataDir, 'pool-keys.json');
    this.usageFile = path.join(dataDir, 'pool-usage.json');

    this.nodes = new Map();
    this.keys = new Map();
    this.runtime = new Map(); // id -> { rec, ws, lastSeen, hello, status, inflight, pending }
    this.rpmWindows = new Map();
    this.usage = { version: 1, days: {}, updatedAt: 0 };
    this.wss = null;
    this.server = null;
    this.timer = null;
    this.lastPrune = 0;
    this._load();
  }

  // ───────────────────── 持久化 ─────────────────────
  _load() {
    const nd = store.readJson(this.nodesFile, null);
    if (nd && Array.isArray(nd.nodes)) {
      for (const n of nd.nodes) {
        if (!n || !n.id || !n.tokenHash) continue;
        this.nodes.set(n.id, this._normNode(n));
      }
    }
    const kd = store.readJson(this.keysFile, null);
    if (kd && Array.isArray(kd.keys)) {
      for (const k of kd.keys) {
        if (!k || !k.id || !k.sha256) continue;
        this.keys.set(k.id, this._normKey(k));
      }
    }
    const ud = store.readJson(this.usageFile, null);
    if (ud && ud.days && typeof ud.days === 'object') {
      this.usage = { version: 1, days: ud.days, updatedAt: ud.updatedAt || 0 };
      this._pruneUsage();
    }
  }

  _normNode(n) {
    return {
      id: String(n.id),
      name: String(n.name || '未命名节点').slice(0, 48),
      tokenHash: String(n.tokenHash),
      tokenPrefix: String(n.tokenPrefix || ''),
      weight: clampInt(n.weight, 1, 100, 1),
      maxInflight: clampInt(n.maxInflight, 1, 64, 4),
      enabled: n.enabled !== false,
      createdAt: Number(n.createdAt) || Date.now(),
      updatedAt: Number(n.updatedAt) || Date.now(),
    };
  }

  _normKey(k) {
    const lim = k.limits && typeof k.limits === 'object' ? k.limits : {};
    return {
      id: String(k.id),
      name: String(k.name || '未命名 Key').slice(0, 48),
      keyPrefix: String(k.keyPrefix || ''),
      sha256: String(k.sha256),
      enabled: k.enabled !== false,
      limits: {
        rpm: clampInt(lim.rpm, 0, 100000, 0),
        dailyTokens: clampInt(lim.dailyTokens, 0, 1000000000, 0),
      },
      createdAt: Number(k.createdAt) || Date.now(),
      lastUsedAt: Number(k.lastUsedAt) || 0,
    };
  }

  _saveNodes() {
    store.saveSoon(this.nodesFile, { version: 1, nodes: [...this.nodes.values()] });
  }
  _saveKeys() {
    store.saveSoon(this.keysFile, { version: 1, keys: [...this.keys.values()] });
  }
  _saveUsage() {
    this.usage.updatedAt = Date.now();
    store.saveSoon(this.usageFile, { version: 1, days: this.usage.days, updatedAt: this.usage.updatedAt });
  }

  // ───────────────────── 生命周期 ─────────────────────
  start(httpServer) {
    this.server = httpServer;
    this.wss = new WebSocketServer({ noServer: true, maxPayload: MAX_WS_PAYLOAD });
    this._onUpgrade = (req, socket, head) => {
      let url;
      try { url = new URL(req.url, 'http://x'); } catch { socket.destroy(); return; }
      if (url.pathname !== '/api/pool/agent') { socket.destroy(); return; }
      const rec = this._authNode(req);
      if (!rec) {
        try { socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n'); } catch {}
        socket.destroy();
        return;
      }
      this.wss.handleUpgrade(req, socket, head, (ws) => this._onConnection(ws, rec));
    };
    httpServer.on('upgrade', this._onUpgrade);
    this.timer = setInterval(() => this._sweep(), 5000);
    if (this.timer.unref) this.timer.unref();
    this.log.info('Gateway', `网关池已启动（节点 ${this.nodes.size}，Key ${this.keys.size}）`);
  }

  stop() {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    if (this.server && this._onUpgrade) {
      try { this.server.removeListener('upgrade', this._onUpgrade); } catch {}
      this._onUpgrade = null;
    }
    for (const rt of this.runtime.values()) {
      try { rt.ws.close(4001, 'server stopping'); } catch {}
      for (const e of [...rt.pending.values()]) this._abortEntry(e, 'server stopping', 502);
    }
    this.runtime.clear();
    store.flushAll();
  }

  _authNode(req) {
    const auth = String(req.headers.authorization || '');
    let token = auth.toLowerCase().startsWith('bearer ') ? auth.slice(7).trim() : '';
    if (!token) token = String(req.headers['x-node-token'] || '').trim();
    if (!token || !token.startsWith('sk-node-')) return null;
    const h = sha256(token);
    for (const rec of this.nodes.values()) {
      if (!rec.enabled) continue;
      if (safeEqualHex(h, rec.tokenHash)) return rec;
    }
    return null;
  }

  _onConnection(ws, rec) {
    const prev = this.runtime.get(rec.id);
    const rt = {
      rec, ws, lastSeen: Date.now(), hello: null, status: null,
      inflight: 0, pending: new Map(),
    };
    this.runtime.set(rec.id, rt);
    if (prev) {
      for (const e of [...prev.pending.values()]) this._failEntry(e, 'node reconnected');
      try { prev.ws.close(4000, 'replaced'); } catch {}
    }
    ws.on('message', (data) => this._onMessage(rt, data));
    ws.on('close', () => this._onClose(rt));
    ws.on('error', (e) => this.log.warn('Gateway', `节点 ${rec.name || rec.id} 连接错误：${e.message}`));
  }

  _onClose(rt) {
    if (this.runtime.get(rt.rec.id) !== rt) return;
    for (const e of [...rt.pending.values()]) this._failEntry(e, 'node connection closed');
    this.runtime.delete(rt.rec.id);
    this.log.warn('Gateway', `节点离线 ${rt.rec.name || rt.rec.id}`);
    this._notifyOffline(rt);
  }

  _notifyOffline(rt) {
    if (!this.notifier) return;
    const title = '网关池节点离线';
    const body = `${rt.rec.name || rt.rec.id} 已断开，池会自动摘除该节点。`;
    Promise.resolve(this.notifier.event('pool-node-offline:' + rt.rec.id, title, body)).catch(() => {});
  }

  _onMessage(rt, data) {
    if (!data || data.length > 4 * 1024 * 1024) return;
    let msg;
    try { msg = JSON.parse(data.toString()); } catch { return; }
    if (!msg || typeof msg !== 'object') return;
    rt.lastSeen = Date.now();
    switch (msg.type) {
      case 'hello':
        rt.hello = {
          name: String(msg.name || ''),
          hostname: String(msg.hostname || ''),
          version: String(msg.version || ''),
          models: Array.isArray(msg.models) ? msg.models.map(String) : [],
          max_inflight: Number(msg.max_inflight) || 0,
        };
        this.log.info('Gateway', `节点上线 ${rt.rec.name || rt.rec.id}（${rt.hello.hostname || '-'}，v${rt.hello.version || '?'}）`);
        break;
      case 'status':
        rt.status = {
          upstream: msg.upstream && typeof msg.upstream === 'object' ? msg.upstream : null,
          inflight: Number(msg.inflight) || 0,
          totals: msg.totals && typeof msg.totals === 'object' ? msg.totals : null,
          models: Array.isArray(msg.models) ? msg.models.map(String) : [],
        };
        break;
      case 'chat_start': this._onChatStart(rt, msg); break;
      case 'chat_chunk': this._onChatChunk(rt, msg); break;
      case 'chat_end': this._onChatEnd(rt, msg); break;
      case 'chat_error': this._onChatError(rt, msg); break;
      default: break;
    }
  }

  _sweep() {
    const now = Date.now();
    for (const [id, rt] of [...this.runtime]) {
      const dead = !rt.ws || rt.ws.readyState !== 1;
      const stale = now - rt.lastSeen > NODE_OFFLINE_MS;
      if (!dead && !stale) continue;
      this.log.warn('Gateway', `节点离线 ${rt.rec.name || id}`);
      for (const e of [...rt.pending.values()]) this._failEntry(e, 'node offline');
      this.runtime.delete(id);
      try { rt.ws.terminate(); } catch {}
      this._notifyOffline(rt);
    }
    if (now - this.lastPrune > 60 * 1000) {
      this.lastPrune = now;
      this._pruneUsage();
    }
  }

  _pruneUsage() {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - RETAIN_DAYS);
    const c = todayStr(cutoff);
    for (const d of Object.keys(this.usage.days)) {
      if (d < c) delete this.usage.days[d];
    }
  }

  // ───────────────────── 节点/Key 管理 ─────────────────────
  createNode({ name, weight, maxInflight } = {}) {
    const token = newToken('sk-node-');
    const now = Date.now();
    const rec = {
      id: newId('n_'),
      name: String(name || '').trim().slice(0, 48) || '未命名节点',
      tokenHash: sha256(token),
      tokenPrefix: token.slice(0, 12),
      weight: clampInt(weight, 1, 100, 1),
      maxInflight: clampInt(maxInflight, 1, 64, 4),
      enabled: true,
      createdAt: now,
      updatedAt: now,
    };
    this.nodes.set(rec.id, rec);
    this._saveNodes();
    this.log.info('Gateway', `新建节点 ${rec.name}（${rec.id}）`);
    return { ok: true, node: this._nodeView(rec), token };
  }

  updateNode(id, patch = {}) {
    const rec = this.nodes.get(id);
    if (!rec) throw httpError('节点不存在', 404);
    if (patch.name != null) rec.name = String(patch.name).trim().slice(0, 48) || rec.name;
    if (patch.weight != null) rec.weight = clampInt(patch.weight, 1, 100, rec.weight);
    if (patch.maxInflight != null) rec.maxInflight = clampInt(patch.maxInflight, 1, 64, rec.maxInflight);
    if (typeof patch.enabled === 'boolean') rec.enabled = patch.enabled;
    let token = null;
    if (patch.rotateToken === true) {
      token = newToken('sk-node-');
      rec.tokenHash = sha256(token);
      rec.tokenPrefix = token.slice(0, 12);
      this.disconnectNode(id);
    }
    rec.updatedAt = Date.now();
    this._saveNodes();
    if (!rec.enabled) this.disconnectNode(id);
    return token ? { ok: true, node: this._nodeView(rec), token } : { ok: true, node: this._nodeView(rec) };
  }

  deleteNode(id) {
    const rec = this.nodes.get(id);
    if (!rec) throw httpError('节点不存在', 404);
    this.disconnectNode(id);
    this.nodes.delete(id);
    this._saveNodes();
    this.log.info('Gateway', `删除节点 ${rec.name}（${id}）`);
    return { ok: true };
  }

  disconnectNode(id) {
    const rt = this.runtime.get(id);
    if (!rt) return { ok: true, wasOnline: false };
    for (const e of [...rt.pending.values()]) this._failEntry(e, 'node disconnected');
    this.runtime.delete(id);
    try { rt.ws.close(4001, 'manual disconnect'); } catch {}
    return { ok: true, wasOnline: true };
  }

  createKey({ name, limits } = {}) {
    const key = newToken('sk-pool-');
    const now = Date.now();
    const rec = {
      id: newId('k_'),
      name: String(name || '').trim().slice(0, 48) || '未命名 Key',
      keyPrefix: key.slice(0, 12),
      sha256: sha256(key),
      enabled: true,
      limits: {
        rpm: clampInt(limits && limits.rpm, 0, 100000, 0),
        dailyTokens: clampInt(limits && limits.dailyTokens, 0, 1000000000, 0),
      },
      createdAt: now,
      lastUsedAt: 0,
    };
    this.keys.set(rec.id, rec);
    this._saveKeys();
    this.log.info('Gateway', `新建池 Key ${rec.name}（${rec.id}）`);
    return { ok: true, key: this._keyView(rec), token: key };
  }

  updateKey(id, patch = {}) {
    const rec = this.keys.get(id);
    if (!rec) throw httpError('Key 不存在', 404);
    if (patch.name != null) rec.name = String(patch.name).trim().slice(0, 48) || rec.name;
    if (typeof patch.enabled === 'boolean') rec.enabled = patch.enabled;
    if (patch.limits && typeof patch.limits === 'object') {
      rec.limits = {
        rpm: clampInt(patch.limits.rpm, 0, 100000, rec.limits.rpm),
        dailyTokens: clampInt(patch.limits.dailyTokens, 0, 1000000000, rec.limits.dailyTokens),
      };
    }
    this._saveKeys();
    return { ok: true, key: this._keyView(rec) };
  }

  deleteKey(id) {
    const rec = this.keys.get(id);
    if (!rec) throw httpError('Key 不存在', 404);
    this.keys.delete(id);
    this.rpmWindows.delete(id);
    this._saveKeys();
    this.log.info('Gateway', `删除池 Key ${rec.name}（${id}）`);
    return { ok: true };
  }

  listNodes() { return [...this.nodes.values()].map((r) => this._nodeView(r)); }
  listKeys() { return [...this.keys.values()].map((k) => this._keyView(k)); }

  _nodeView(rec) {
    const rt = this.runtime.get(rec.id);
    const hello = rt && rt.hello;
    const status = rt && rt.status;
    return {
      id: rec.id,
      name: rec.name,
      tokenPrefix: rec.tokenPrefix,
      weight: rec.weight,
      maxInflight: rec.maxInflight,
      enabled: rec.enabled,
      createdAt: rec.createdAt,
      updatedAt: rec.updatedAt,
      online: !!(rt && rt.ws && rt.ws.readyState === 1 && Date.now() - rt.lastSeen < NODE_OFFLINE_MS),
      lastSeen: rt ? rt.lastSeen : 0,
      inflight: rt ? rt.inflight : 0,
      hostname: hello ? hello.hostname : '',
      version: hello ? hello.version : '',
      models: (hello && hello.models) || (status && status.models) || [],
      upstream: (status && status.upstream) || null,
      totals: (status && status.totals) || null,
    };
  }

  _keyView(rec) {
    const day = this._day(false);
    const u = (day && day.keys[rec.id]) || zeroUsage();
    return {
      id: rec.id,
      name: rec.name,
      keyPrefix: rec.keyPrefix,
      enabled: rec.enabled,
      limits: rec.limits,
      createdAt: rec.createdAt,
      lastUsedAt: rec.lastUsedAt,
      today: {
        req: u.req, stream: u.stream, err: u.err,
        tokensIn: u.tokensIn, tokensOut: u.tokensOut,
      },
    };
  }

  overview() {
    const day = this._day(false) || { total: zeroUsage(), nodes: {}, keys: {}, models: {} };
    const nodes = this.listNodes();
    const keys = this.listKeys();
    return {
      ok: true,
      time: new Date().toISOString(),
      nodes,
      keys,
      usage: {
        date: todayStr(),
        total: day.total,
        nodes: nodes.map((n) => ({ id: n.id, name: n.name, ...(day.nodes[n.id] || zeroUsage()) })),
        keys: keys.map((k) => ({ id: k.id, name: k.name, ...(day.keys[k.id] || zeroUsage()) })),
        models: Object.entries(day.models).map(([model, s]) => ({ model, ...s })),
      },
      totals: {
        nodes: nodes.length,
        online: nodes.filter((n) => n.online).length,
        keys: keys.length,
        requests: day.total.req,
        tokens: day.total.tokensIn + day.total.tokensOut,
        errors: day.total.err,
      },
    };
  }

  // ───────────────────── 用量 ─────────────────────
  _day(create) {
    const d = todayStr();
    if (!this.usage.days[d] && create) {
      this.usage.days[d] = { total: zeroUsage(), nodes: {}, keys: {}, models: {} };
    }
    return this.usage.days[d] || null;
  }

  recordUsage({ nodeId, keyId, model, stream, err, inTokens, outTokens, est }) {
    const day = this._day(true);
    const pi = Math.max(0, Number(inTokens) || 0);
    const po = Math.max(0, Number(outTokens) || 0);
    const add = (o) => {
      o.req += 1;
      if (stream) o.stream += 1;
      if (err) o.err += 1;
      o.tokensIn += pi;
      o.tokensOut += po;
      if (est) o.estTokens += pi + po;
    };
    add(day.total);
    if (nodeId) { if (!day.nodes[nodeId]) day.nodes[nodeId] = zeroUsage(); add(day.nodes[nodeId]); }
    if (keyId) { if (!day.keys[keyId]) day.keys[keyId] = zeroUsage(); add(day.keys[keyId]); }
    const m = String(model || 'unknown');
    if (!day.models[m]) day.models[m] = zeroUsage();
    add(day.models[m]);
    this._saveUsage();
  }

  // ───────────────────── /v1 ─────────────────────
  async handleV1(req, res, url) {
    for (const [k, v] of Object.entries(CORS)) res.setHeader(k, v);
    const p = url.pathname;
    try {
      if (req.method === 'OPTIONS') {
        res.writeHead(204, CORS);
        res.end();
        return;
      }
      const auth = this._verifyKey(req);
      if (!auth.ok) {
        this._sendError(res, auth.code, auth.message, auth.type);
        return;
      }
      if (req.method === 'GET' && p === '/v1/models') {
        this._sendModels(res);
        return;
      }
      if (req.method === 'POST' && p === '/v1/chat/completions') {
        await this._handleChat(req, res, auth.key);
        return;
      }
      this._sendError(res, 404, 'not found', 'invalid_request_error');
    } catch (e) {
      this._sendError(res, e.statusCode || 500, e.message || 'server error', 'server_error');
    }
  }

  _verifyKey(req) {
    if (this.keys.size === 0) {
      return { ok: false, code: 401, message: 'no api key configured', type: 'invalid_api_key' };
    }
    const auth = String(req.headers.authorization || '');
    let raw = auth.toLowerCase().startsWith('bearer ') ? auth.slice(7).trim() : '';
    if (!raw) raw = String(req.headers['x-api-key'] || '').trim();
    if (!raw) return { ok: false, code: 401, message: 'missing api key', type: 'invalid_api_key' };
    const h = sha256(raw);
    let hit = null;
    for (const k of this.keys.values()) {
      if (k.enabled && safeEqualHex(h, k.sha256)) { hit = k; break; }
    }
    if (!hit) return { ok: false, code: 401, message: 'invalid api key', type: 'invalid_api_key' };

    const rpm = hit.limits.rpm;
    if (rpm > 0) {
      const now = Date.now();
      let win = this.rpmWindows.get(hit.id);
      if (!win) { win = []; this.rpmWindows.set(hit.id, win); }
      while (win.length && now - win[0] > 60000) win.shift();
      if (win.length >= rpm) return { ok: false, code: 429, message: 'rate limit exceeded', type: 'rate_limited' };
      win.push(now);
    }

    const daily = hit.limits.dailyTokens;
    if (daily > 0) {
      const day = this._day(false);
      const u = (day && day.keys[hit.id]) || null;
      const used = u ? u.tokensIn + u.tokensOut : 0;
      if (used >= daily) return { ok: false, code: 429, message: 'daily token limit exceeded', type: 'rate_limited' };
    }

    hit.lastUsedAt = Date.now();
    this._saveKeys();
    return { ok: true, key: hit };
  }

  _sendModels(res) {
    const set = new Set();
    for (const rt of this.runtime.values()) {
      if (!rt.rec.enabled || !rt.ws || rt.ws.readyState !== 1) continue;
      const models = (rt.hello && rt.hello.models) || (rt.status && rt.status.models) || [];
      for (const m of models) if (m) set.add(String(m));
    }
    const list = set.size ? [...set] : DEFAULT_MODELS;
    const now = Math.floor(Date.now() / 1000);
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', ...CORS });
    res.end(JSON.stringify({
      object: 'list',
      data: list.map((id) => ({ id, object: 'model', created: now, owned_by: 'teleagent' })),
    }));
  }

  _sendError(res, code, message, type) {
    try {
      if (res.headersSent) {
        res.end();
        return;
      }
      res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', ...CORS });
      res.end(JSON.stringify({ error: { message, type, code } }));
    } catch {}
  }

  async _handleChat(req, res, keyRec) {
    let raw;
    try {
      raw = await readRaw(req, MAX_V1_BODY);
    } catch (e) {
      this._sendError(res, 413, 'request body too large', 'payload_too_large');
      return;
    }
    let body;
    try { body = JSON.parse(raw.toString('utf8') || '{}'); } catch { body = null; }
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      this._sendError(res, 400, 'invalid json body', 'invalid_request_error');
      return;
    }
    const stream = !!body.stream;
    const upstreamBody = stream
      ? { ...body, stream: true, stream_options: { ...(body.stream_options || {}), include_usage: true } }
      : body;

    const node = this.pickNode();
    if (!node) {
      this._sendError(res, 502, 'no healthy node', 'no_healthy_node');
      return;
    }

    const entry = {
      reqId: crypto.randomBytes(8).toString('hex'),
      res,
      body: upstreamBody,
      stream,
      keyId: keyRec.id,
      model: String(body.model || ''),
      estIn: Math.round(raw.length / 4),
      started: false,
      status: 0,
      bufs: [],
      tail: '',
      streamBytes: 0,
      usageIn: 0,
      usageOut: 0,
      usageEst: false,
      attempt: 0,
      failed: new Set(),
      nodeId: null,
      done: false,
      startTimer: null,
      idleTimer: null,
      totalTimer: null,
    };

    res.on('close', () => {
      if (entry.done) return;
      this._cancelUpstream(entry);
      this._abortEntry(entry, 'client closed', 499);
    });

    this._dispatch(node, entry);
  }

  pickNode(exclude) {
    const list = [];
    for (const rt of this.runtime.values()) {
      const rec = rt.rec;
      if (!rec.enabled) continue;
      if (!rt.ws || rt.ws.readyState !== 1) continue;
      if (!rt.status || !rt.status.upstream || rt.status.upstream.ok !== true) continue;
      if (rt.inflight >= rec.maxInflight) continue;
      if (exclude && exclude.has(rec.id)) continue;
      list.push(rt);
    }
    if (!list.length) return null;
    list.sort((a, b) => (a.inflight - b.inflight) || (b.rec.weight - a.rec.weight));
    return list[0];
  }

  _dispatch(rt, entry) {
    entry.attempt += 1;
    entry.nodeId = rt.rec.id;
    entry.started = false;
    entry.bufs = [];
    entry.tail = '';
    entry.streamBytes = 0;
    rt.inflight += 1;
    rt.pending.set(entry.reqId, entry);
    entry.startTimer = setTimeout(() => {
      if (entry.done) return;
      this._cancelUpstream(entry);
      this._failEntry(entry, 'upstream start timeout');
    }, START_TIMEOUT_MS);
    entry.totalTimer = setTimeout(() => {
      if (entry.done) return;
      this._cancelUpstream(entry);
      this._abortEntry(entry, 'upstream total timeout', 504);
    }, TOTAL_TIMEOUT_MS);
    try {
      rt.ws.send(JSON.stringify({
        type: 'chat', reqId: entry.reqId, body: entry.body, stream: entry.stream,
      }));
    } catch (e) {
      this._failEntry(entry, 'send failed: ' + e.message);
    }
  }

  _detach(entry) {
    if (entry.startTimer) { clearTimeout(entry.startTimer); entry.startTimer = null; }
    if (entry.idleTimer) { clearTimeout(entry.idleTimer); entry.idleTimer = null; }
    if (entry.totalTimer) { clearTimeout(entry.totalTimer); entry.totalTimer = null; }
    const rt = entry.nodeId && this.runtime.get(entry.nodeId);
    if (rt && rt.pending.get(entry.reqId) === entry) {
      rt.pending.delete(entry.reqId);
      rt.inflight = Math.max(0, rt.inflight - 1);
    }
  }

  _touchIdle(entry) {
    if (entry.idleTimer) clearTimeout(entry.idleTimer);
    entry.idleTimer = setTimeout(() => {
      if (entry.done) return;
      this._cancelUpstream(entry);
      this._abortEntry(entry, 'upstream idle timeout', 504);
    }, IDLE_TIMEOUT_MS);
  }

  _cancelUpstream(entry) {
    const rt = entry.nodeId && this.runtime.get(entry.nodeId);
    if (rt && rt.ws && rt.ws.readyState === 1) {
      try { rt.ws.send(JSON.stringify({ type: 'chat_cancel', reqId: entry.reqId })); } catch {}
    }
  }

  _failEntry(entry, message) {
    if (entry.done) return;
    const tried = entry.nodeId;
    this._detach(entry);
    if (tried) entry.failed.add(tried);
    if (!entry.started && entry.attempt < 2) {
      const next = this.pickNode(entry.failed);
      if (next) {
        this.log.warn('Gateway', `节点 ${tried} 失败（${message}），切换到 ${next.rec.name || next.rec.id}`);
        this._dispatch(next, entry);
        return;
      }
    }
    if (!entry.started && !entry.res.headersSent) {
      this._sendError(entry.res, 502, message || 'no healthy node', 'no_healthy_node');
      this._finish(entry, true);
    } else {
      this._abortEntry(entry, message, 502);
    }
  }

  _abortEntry(entry, message, code) {
    if (entry.done) return;
    this._cancelUpstream(entry);
    this._detach(entry);
    if (!entry.res.headersSent) {
      this._sendError(entry.res, code || 502, message, code === 504 ? 'upstream_timeout' : 'upstream_error');
    } else {
      try { entry.res.end(); } catch {}
    }
    this._finish(entry, true);
  }

  _finish(entry, err) {
    if (entry.done) return;
    entry.done = true;
    this._detach(entry);
    this.recordUsage({
      nodeId: entry.nodeId,
      keyId: entry.keyId,
      model: entry.model,
      stream: entry.stream,
      err: !!err,
      inTokens: entry.usageIn,
      outTokens: entry.usageOut,
      est: entry.usageEst,
    });
  }

  // ───────────────────── 节点回传 ─────────────────────
  _onChatStart(rt, msg) {
    const e = rt.pending.get(msg.reqId);
    if (!e || e.done) return;
    if (e.startTimer) { clearTimeout(e.startTimer); e.startTimer = null; }
    e.started = true;
    e.status = Number(msg.status) || 200;
    if (e.stream && !e.res.headersSent) {
      e.res.writeHead(e.status, {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache',
        'x-accel-buffering': 'no',
        'x-pool-node': String(rt.rec.id),
      });
      try { e.res.flushHeaders(); } catch {}
    }
    this._touchIdle(e);
  }

  _onChatChunk(rt, msg) {
    const e = rt.pending.get(msg.reqId);
    if (!e || e.done) return;
    let buf;
    try { buf = Buffer.from(String(msg.data || ''), 'base64'); } catch { return; }
    if (!buf.length) return;
    this._touchIdle(e);
    e.streamBytes += buf.length;
    if (e.stream) {
      if (e.res.writableEnded) return;
      e.tail = (e.tail + buf.toString('utf8')).slice(-8192);
      const ok = e.res.write(buf);
      if (!ok && rt.ws && rt.ws._socket && !rt.ws._socket.destroyed) {
        try { rt.ws._socket.pause(); } catch {}
        e.res.once('drain', () => {
          try { if (!rt.ws._socket.destroyed) rt.ws._socket.resume(); } catch {}
        });
      }
    } else {
      e.bufs.push(buf);
    }
  }

  _onChatEnd(rt, msg) {
    const e = rt.pending.get(msg.reqId);
    if (!e || e.done) return;
    if (e.stream) {
      this._parseStreamUsage(e);
      if (!e.res.writableEnded) { try { e.res.end(); } catch {} }
    } else {
      const buf = Buffer.concat(e.bufs);
      this._parseBlockUsage(e, buf);
      if (!e.res.headersSent) {
        e.res.writeHead(e.status || 200, {
          'content-type': 'application/json; charset=utf-8',
          'x-pool-node': String(rt.rec.id),
        });
      }
      try { e.res.end(buf); } catch {}
    }
    this._finish(e, e.status >= 400);
  }

  _onChatError(rt, msg) {
    const e = rt.pending.get(msg.reqId);
    if (!e || e.done) return;
    this.log.warn('Gateway', `节点 ${rt.rec.name || rt.rec.id} 回传错误：${msg.message || ''}`);
    this._failEntry(e, String(msg.message || 'node error'));
  }

  _parseBlockUsage(e, buf) {
    try {
      const j = JSON.parse(buf.toString('utf8'));
      const u = j && j.usage;
      if (u) {
        e.usageIn = Number(u.prompt_tokens) || 0;
        e.usageOut = Number(u.completion_tokens) || 0;
        return;
      }
    } catch {}
    e.usageEst = true;
    e.usageIn = e.estIn || 0;
    e.usageOut = Math.round(buf.length / 4);
  }

  _parseStreamUsage(e) {
    let usage = null;
    const re = /"usage"\s*:\s*(\{[^{}]*\})/g;
    let m;
    while ((m = re.exec(e.tail))) {
      try { usage = JSON.parse(m[1]); } catch {}
    }
    if (usage) {
      e.usageIn = Number(usage.prompt_tokens) || 0;
      e.usageOut = Number(usage.completion_tokens) || 0;
      return;
    }
    e.usageEst = true;
    e.usageIn = e.estIn || 0;
    e.usageOut = Math.round(e.streamBytes / 4);
  }
}

function clampInt(v, min, max, def) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return def;
  return Math.min(max, Math.max(min, n));
}

function httpError(message, statusCode) {
  const e = new Error(message);
  e.statusCode = statusCode;
  return e;
}

module.exports = GatewayPool;
