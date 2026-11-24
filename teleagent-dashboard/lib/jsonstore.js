'use strict';
// 轻量 JSON 持久化：读取容错 + 原子写（先写 .tmp 再 rename）+ 防抖落盘
const fs = require('fs');
const path = require('path');

const queues = new Map(); // fp -> { obj, timer }

function readJson(fp, def) {
  try {
    return JSON.parse(fs.readFileSync(fp, 'utf8'));
  } catch {
    return def;
  }
}

function writeJsonAtomic(fp, obj) {
  const dir = path.dirname(fp);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = fp + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2));
  fs.renameSync(tmp, fp);
}

// 2 秒内的连续写入合并为一次；进程退出前调用 flushAll 保证不丢
function saveSoon(fp, obj, delay = 2000) {
  const cur = queues.get(fp);
  if (cur) {
    cur.obj = obj;
    return;
  }
  const rec = { obj, timer: null };
  rec.timer = setTimeout(() => {
    queues.delete(fp);
    try { writeJsonAtomic(fp, rec.obj); } catch {}
  }, delay);
  if (rec.timer.unref) rec.timer.unref();
  queues.set(fp, rec);
}

function flushAll() {
  for (const [fp, rec] of queues) {
    try {
      clearTimeout(rec.timer);
      writeJsonAtomic(fp, rec.obj);
    } catch {}
  }
  queues.clear();
}

module.exports = { readJson, writeJsonAtomic, saveSoon, flushAll };
