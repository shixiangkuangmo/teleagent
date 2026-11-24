// 日志模块：内存环形 + 落盘 JSONL（按天滚动，自动清理 14 天前旧文件）
'use strict';

const fs = require('fs');
const path = require('path');
const RETAIN_DAYS = 14;

// 本地时间戳（勿用 toISOString —— 那是 UTC，会和实际时间差时区）
function stamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function todayStamp() { return stamp().slice(0, 10); }

class Log {
  constructor(dataDir) {
    this.dir = dataDir;
    this.logDir = path.join(dataDir, 'logs');
    fs.mkdirSync(this.logDir, { recursive: true });
    this.file = this.filePathFor(todayStamp());
    this.ring = [];
    this.seq = 0;
    this.cleanupOldFiles();
    // 每小时检查：跨天滚动文件 + 清理旧文件（进程长期运行场景）
    this.timer = setInterval(() => {
      const today = todayStamp();
      if (this.file !== this.filePathFor(today)) {
        this.file = this.filePathFor(today);
      }
      this.cleanupOldFiles();
    }, 3600 * 1000);
    if (this.timer.unref) this.timer.unref();
  }

  filePathFor(day) { return path.join(this.logDir, `dashboard-${day}.jsonl`); }

  // 删除 RETAIN_DAYS 天前的日志文件（按文件名日期判断，非 mtime —— 防系统时间漂移误删）
  cleanupOldFiles() {
    try {
      const cutoff = Date.now() - RETAIN_DAYS * 86400 * 1000;
      const files = fs.readdirSync(this.logDir).filter((f) => /^dashboard-\d{4}-\d{2}-\d{2}\.jsonl$/.test(f));
      for (const f of files) {
        const m = f.match(/(\d{4})-(\d{2})-(\d{2})/);
        if (!m) continue;
        const fileDate = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime();
        if (fileDate < cutoff) {
          try { fs.unlinkSync(path.join(this.logDir, f)); } catch {}
        }
      }
    } catch {}
  }

  _push(source, message, level) {
    const e = {
      id: ++this.seq,
      timestamp: stamp(),
      source, message, level,
    };
    this.ring.push(e);
    if (this.ring.length > 500) this.ring.splice(0, this.ring.length - 500);
    // 跨天即时兜底（小时定时器之外的保险）
    const today = todayStamp();
    const expected = this.filePathFor(today);
    if (this.file !== expected) {
      this.file = expected;
      this.cleanupOldFiles();
    }
    try {
      fs.appendFileSync(this.file, JSON.stringify(e) + '\n');
    } catch {}
    return e;
  }

  info(source, message) { return this._push(source, message, 'info'); }
  warn(source, message) { return this._push(source, message, 'warning'); }
  error(source, message) { return this._push(source, message, 'error'); }
  debug(source, message) { return this._push(source, message, 'debug'); }

  tail(n = 200) { return this.ring.slice(-n).reverse(); }
}

module.exports = Log;
