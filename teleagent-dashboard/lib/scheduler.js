// 调度模块：每日积分领取（纯 API）+ 补跑
'use strict';

const fs = require('fs');
const path = require('path');

class Scheduler {
  constructor({ config, log, api, ka, power, notifier }) {
    this.config = config;
    this.log = log;
    this.api = api;
    this.ka = ka;
    this.power = power;
    this.notifier = notifier || { event: async () => {} };
    this.state = loadState(path.join(config._file, '..', 'state.json'));
    this.timer = null;
    this.running = false;
    this.lastResult = this.state.last_result || null;
    this.nextAt = null;
  }

  status() {
    return {
      claim_time: this.config.points.claim_time,
      poll_minutes: this.config.points.poll_minutes || 10,
      next_claim_at: this.nextAt ? new Date(this.nextAt).toISOString() : null,
      next_claim_text: this.nextAt ? new Date(this.nextAt).toLocaleString('zh-CN') : null,
      last_result: this.lastResult,
      last_claim_date: this.state.last_claim_date || null,
      claiming: this.running,
      catchup_on_start: !!this.config.points.catchup_on_start,
    };
  }

  start() {
    this.scheduleNext();
    if (this.pollTimer) clearInterval(this.pollTimer); // 防重复注册：onToken 重复触发（自动重登/文件误判）会再次 start()
    // 认证活动轮询：每 poll_minutes 分钟带 JWT 查一次积分（确定的账号活动信号，兼作保活心跳）
    const pm = Math.max(2, parseInt(this.config.points.poll_minutes || '10', 10));
    this.pollTimer = setInterval(() => {
      this.api.refreshPoints().catch(() => {});
    }, pm * 60 * 1000);
    this.pollTimer.unref && this.pollTimer.unref();
    if (this.config.points.catchup_on_start && this.state.last_claim_date !== today()) {
      this.log.info('Scheduler', `启动补跑：今日（${today()}）尚未领取，3 秒后执行`);
      setTimeout(() => this.triggerClaim('catchup'), 3000);
    } else {
      this.log.info('Scheduler', `今日已领取过（${this.state.last_claim_date}），等待下一周期`);
    }
  }

  reschedule() {
    clearTimeout(this.timer);
    this.scheduleNext();
    this.log.info('Scheduler', `计划已更新：每日 ${this.config.points.claim_time} 领取`);
  }

  scheduleNext(minGapMs = 0) {
    clearTimeout(this.timer);
    const [h, m] = String(this.config.points.claim_time || '08:05').split(':').map((x) => parseInt(x, 10));
    const now = new Date();
    const next = new Date(now);
    next.setHours(h || 8, m || 5, 0, 0);
    if (next <= now) next.setDate(next.getDate() + 1);
    // 领取刚跑完又遇到「距今不足 minGapMs」的今日同一时刻（定时器毫秒截断可能提前零点几秒触发）→ 跳到明天，防止 1 秒内连跑两次
    else if (next.getTime() - Date.now() <= minGapMs) next.setDate(next.getDate() + 1);
    this.nextAt = next.getTime();
    const wait = this.nextAt - Date.now();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.triggerClaim('scheduled'), wait);
    this.timer.unref && this.timer.unref();
    this.log.info('Scheduler', `下一次领取：${next.toLocaleString('zh-CN')}`);
  }

  async triggerClaim(source) {
    if (this.running) return { ok: false, error: '领取任务执行中' };
    this.running = true;
    this.log.info('Claim', `开始领取（触发：${source}，通道：API）`);
    try {
      const r = await this.claimViaApi();
      this.lastResult = { ...r, source, at: new Date().toISOString() };
      this.state.last_result = this.lastResult;
      if (r.status === 'claimed' || r.status === 'already_claimed') {
        this.state.last_claim_date = today();
      }
      saveState(path.join(this.config._file, '..', 'state.json'), this.state);
      this.log.info('Claim', `结果：${r.status}${r.balance ? '，余额 ' + r.balance : ''}`);
      if (r.status === 'claimed' && this.notifier) {
        const list = (r.claimed || []).map((x) => `${x.name}${x.points ? ' +' + x.points : ''}`).join('、');
        this.notifier.event('claim', '积分已领取', `本次领取：${list || '任务积分'}\n余额：${r.balance || '未知'}\n时间：${new Date().toLocaleString('zh-CN')}`, 60);
      }
      // 领取后刷新积分状态
      setTimeout(() => this.api.refreshPoints().catch(() => {}), 3000);
      return { ok: true, ...r };
    } catch (e) {
      this.log.error('Claim', `异常: ${e.message}`);
      if (this.notifier) {
        this.notifier.event('claim-fail', '积分领取异常', `原因：${e.message}`, 60);
      }
      return { ok: false, error: e.message };
    } finally {
      this.running = false;
      // 领取后重排：30 秒内即将到达的「今日同一时刻」直接跳到明天（防止边界提前触发导致 1 秒内连领两次）
      if (source !== 'manual') this.scheduleNext(30 * 1000);
    }
  }

  // API 领取：任务列表（首登 3000 / 月度 3000）→ 每日 100 → 刷新余额
  async claimViaApi() {
    const claimed = [];
    const errors = [];

    // 1. 一次性/月度任务：checkTask 列表里逐项领取
    let tasks = [];
    try {
      tasks = await this.api.listTasks();
    } catch (e) {
      this.log.warn('Claim', `查询任务列表失败（${e.message}）`);
    }
    for (const t of tasks) {
      const code = t && t.task_code;
      // daily_login 走下面单独的每日判态逻辑；0 积分活动任务不在这里领
      if (!code || code === 'daily_login' || t.claimed || !(Number(t.points_amount) > 0)) continue;
      try {
        const r = await this.api.claimTask(code);
        if (r && (r.code === 200 || r.code === 0)) {
          const pts = Number(t.points_amount) || 0;
          claimed.push({ code, name: t.task_name || code, points: pts });
          this.log.info('Claim', `任务 ${code}（${t.task_name || ''}）领取成功${pts ? '，+' + pts + ' 积分' : ''}`);
        } else if (r && /已(经)?领|already|重复/.test(String(r.msg || ''))) {
          this.log.info('Claim', `任务 ${code} 已领取（${r.msg}）`);
        } else {
          errors.push(`${code}: ${JSON.stringify(r).slice(0, 100)}`);
        }
      } catch (e) {
        errors.push(`${code}: ${e.message}`);
      }
    }

    // 2. 每日登录积分（100）：checkDayTask 判态后领取
    try {
      const dt = await this.api.dayTaskRaw();
      const act = (dt && dt.data && dt.data.activity_data || []).find((a) => (a.jump_url || '').includes('checkTask'));
      if (!(act && act.action_status === 'completed')) {
        const r = await this.api.claimDailyLogin();
        if (r && (r.code === 200 || r.code === 0)) {
          // data 为空数组表示本次没有可领额度（今日已领/已自动发放）
          const noop = Array.isArray(r.data) && r.data.length === 0;
          if (!noop) claimed.push({ code: 'daily_login', name: '每日登录积分', points: 100 });
          this.log.info('Claim', noop ? '每日积分今日已领（接口返回空）' : '每日登录积分领取成功，+100 积分');
        } else if (r && /已(经)?领|already|重复/.test(String(r.msg || ''))) {
          this.log.info('Claim', `每日积分今日已领（${r.msg}）`);
        } else {
          errors.push('daily_login: ' + JSON.stringify(r).slice(0, 100));
        }
      }
    } catch (e) {
      errors.push('daily_login: ' + e.message);
    }

    await this.api.refreshPoints().catch(() => {});
    const balance = this.api.pointsSnapshot().balance;

    if (!claimed.length && !errors.length) return { status: 'already_claimed', balance };
    if (!claimed.length && errors.length) {
      // 全部失败：若服务端提示已领过，仍按已领取处理，避免定时任务反复重试
      const txt = errors.join(' | ');
      if (/已(经)?领|already|重复/.test(txt)) return { status: 'already_claimed', balance, detail: errors };
      throw new Error('领取失败：' + txt.slice(0, 200));
    }
    return { status: 'claimed', balance, claimed, errors };
  }
}

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function loadState(fp) {
  try { return JSON.parse(fs.readFileSync(fp, 'utf8')); } catch { return {}; }
}
function saveState(fp, s) {
  try { fs.writeFileSync(fp, JSON.stringify(s, null, 2)); } catch {}
}

module.exports = Scheduler;
