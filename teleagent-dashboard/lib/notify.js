// 通知模块：企业微信机器人 webhook + 事件冷却去重（参考 teleagent-keepalive notify.js）
'use strict';

class Notifier {
  constructor(config, log) {
    this.config = config; // { notify: { enabled, wecomWebhook, cooldownMin } }
    this.log = log;
    this.lastSent = {};
  }

  cfg() {
    const n = this.config.notify || {};
    return { enabled: n.enabled !== false, wecomWebhook: n.wecomWebhook || '', cooldownMin: n.cooldownMin || 30 };
  }

  async send(title, content) {
    const c = this.cfg();
    if (!c.enabled || !c.wecomWebhook) return { ok: false, skipped: true };
    try {
      const res = await fetch(c.wecomWebhook, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ msgtype: 'text', text: { content: `【TeleAgent面板】${title}\n${content}` } }),
        signal: AbortSignal.timeout(10000),
      });
      const j = await res.json().catch(() => ({}));
      if (j.errcode === 0) return { ok: true };
      this.log.warn('Notify', `推送失败 errcode=${j.errcode} ${j.errmsg || ''}`);
      return { ok: false, error: j.errmsg };
    } catch (e) {
      this.log.warn('Notify', `推送异常: ${e.message}`);
      return { ok: false, error: e.message };
    }
  }

  async event(key, title, content, cooldownMin) {
    const c = this.cfg();
    const cd = (cooldownMin ?? c.cooldownMin) * 60000;
    const now = Date.now();
    if (this.lastSent[key] && now - this.lastSent[key] < cd) return { ok: false, throttled: true };
    this.lastSent[key] = now;
    const r = await this.send(title, content);
    if (r.ok) this.log.info('Notify', `已推送：${title}`);
    return r;
  }

  reset(key) {
    if (key) delete this.lastSent[key];
    else this.lastSent = {};
  }
}

module.exports = Notifier;
