'use strict';
// 与 lib/login.js 完全一致的 AES-256-GCM 密文格式：
//   v1:base64(iv):base64(ciphertext):base64(authTag)
// 密钥文件 data/.secret（32 字节 hex，0600）。两个模块共用同一份密钥。
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const b64 = (b) => Buffer.from(b).toString('base64');

function loadSecret(dir) {
  const f = path.join(dir, '.secret');
  try {
    return Buffer.from(fs.readFileSync(f, 'utf8').trim(), 'hex');
  } catch {
    const k = crypto.randomBytes(32);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(f, k.toString('hex'), { mode: 0o600 });
    return k;
  }
}

function encrypt(plain, dir) {
  if (!plain) return '';
  const key = loadSecret(dir);
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([c.update(String(plain), 'utf8'), c.final()]);
  return ['v1', b64(iv), b64(ct), b64(c.getAuthTag())].join(':');
}

function decrypt(blob, dir) {
  if (!blob) return '';
  const [ver, ivB, ctB, tagB] = String(blob).split(':');
  if (ver !== 'v1') return '';
  const key = loadSecret(dir);
  const d = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(ivB, 'base64'));
  d.setAuthTag(Buffer.from(tagB, 'base64'));
  return Buffer.concat([d.update(Buffer.from(ctB, 'base64')), d.final()]).toString('utf8');
}

// 展示用：只保留前后少量字符
function mask(s) {
  const v = String(s || '');
  if (v.length <= 10) return v ? v.slice(0, 3) + '***' : '';
  return v.slice(0, 7) + '…' + v.slice(-4);
}

module.exports = { loadSecret, encrypt, decrypt, mask };
