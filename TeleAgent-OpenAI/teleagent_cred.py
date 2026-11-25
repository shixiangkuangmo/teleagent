# -*- coding: utf-8 -*-
"""
teleagent_cred.py — 从本机 TeleAgent 客户端数据目录自动提取平台凭证
====================================================================
节点要全自动接入 teleagent2api，就不能让用户去控制台手动加账号。
TeleAgent 桌面客户端登录后会把凭证落在：

    %USERPROFILE%\\.local\\share\\TeleAgent\\
        current-owner.json                    → 当前账号目录名
        app-auth\\device-meta.json             → deviceId / installId
        app-auth\\state.json                   → 旧版明文 token（兼容）
        users\\<owner>\\app-auth\\token.json   → token 或 encryptedToken
        Local State                           → os_crypt.encrypted_key

encryptedToken 是 Chromium os_crypt 的 v10 信封（AES-256-GCM），
主密钥本身用当前 Windows 用户的 DPAPI 保护。这里用 ctypes 调
CryptUnprotectData 解开主密钥，再用 cryptography 的 AESGCM 解 token，
与客户端自己的解密路径等价，不提权、不联网。
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import time


class CredError(RuntimeError):
    pass


def data_root():
    p = os.environ.get("TELEAGENT_DATA_DIR")
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".local", "share", "TeleAgent")


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(blob):
    buf = ctypes.create_string_buffer(blob, len(blob))
    pin = _BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    pout = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(pin), None, None, None, None, 0, ctypes.byref(pout))
    if not ok:
        raise CredError("DPAPI 解密失败（需要与客户端相同的 Windows 用户）")
    try:
        return ctypes.string_at(pout.pbData, pout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(pout.pbData)


def aes_gcm_decrypt(key, nonce, ct, tag):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise CredError("缺少 cryptography，无法解密 v10 token")
    return AESGCM(key).decrypt(nonce, ct + tag, None)


def _decrypt_v10(root, blob):
    local_state = os.path.join(root, "Local State")
    if not os.path.exists(local_state):
        raise CredError("缺少主密钥文件：%s" % local_state)
    enc = (_read_json(local_state).get("os_crypt") or {}).get("encrypted_key")
    if not enc:
        raise CredError("Local State 里没有 os_crypt.encrypted_key")
    key_raw = base64.b64decode(enc)
    if key_raw[:5] != b"DPAPI":
        raise CredError("encrypted_key 缺少 DPAPI 前缀")
    aes_key = dpapi_unprotect(key_raw[5:])
    nonce, tag, ct = blob[3:15], blob[-16:], blob[15:-16]
    return aes_gcm_decrypt(aes_key, nonce, ct, tag).decode("utf-8", "replace")


def _owner_dir(root):
    owner_file = os.path.join(root, "current-owner.json")
    if os.path.exists(owner_file):
        try:
            name = (_read_json(owner_file) or {}).get("ownerDir")
            if name:
                return name
        except Exception:
            pass
    users = os.path.join(root, "users")
    if not os.path.isdir(users):
        raise CredError("TeleAgent 用户目录不存在：%s" % users)
    dirs = [os.path.join(users, d) for d in os.listdir(users)
            if os.path.isdir(os.path.join(users, d))]
    if not dirs:
        raise CredError("TeleAgent users/ 为空，客户端还没登录过")
    return os.path.basename(max(dirs, key=os.path.getmtime))


def token_exp(token):
    """解析 JWT 的 exp（unix 秒）；解析不出来返回 0。"""
    try:
        part = token.split(".")[1].replace("-", "+").replace("_", "/")
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.b64decode(part)).get("exp") or 0)
    except Exception:
        return 0


def extract():
    """返回 {'token','device_id','install_id','exp'}；失败抛 CredError。"""
    root = data_root()
    if not os.path.isdir(root):
        raise CredError("未找到 TeleAgent 数据目录：%s（请先登录桌面客户端）" % root)

    device_id = install_id = ""
    meta_file = os.path.join(root, "app-auth", "device-meta.json")
    if os.path.exists(meta_file):
        try:
            meta = _read_json(meta_file) or {}
            device_id = meta.get("deviceId") or ""
            install_id = meta.get("installId") or ""
        except Exception:
            pass

    token = ""
    state_file = os.path.join(root, "app-auth", "state.json")
    if os.path.exists(state_file):
        try:
            st = _read_json(state_file) or {}
            token = st.get("token") or ""
            device_id = device_id or st.get("deviceId") or ""
            install_id = install_id or st.get("installId") or ""
        except Exception:
            pass

    if not token:
        tj_file = os.path.join(root, "users", _owner_dir(root), "app-auth", "token.json")
        if not os.path.exists(tj_file):
            raise CredError("未找到 token 文件：%s" % tj_file)
        tj = _read_json(tj_file) or {}
        token = tj.get("token") or ""
        if not token and tj.get("encryptedToken"):
            blob = base64.b64decode(tj["encryptedToken"])
            if len(blob) > 15 and blob[:3] == b"v10":
                token = _decrypt_v10(root, blob)
            else:
                token = dpapi_unprotect(blob).decode("utf-8", "replace")

    if not token:
        raise CredError("token 为空，请重新登录 TeleAgent 客户端")
    return {
        "token": token,
        "device_id": device_id,
        "install_id": install_id,
        "exp": token_exp(token),
    }


if __name__ == "__main__":
    c = extract()
    left = (c["exp"] - time.time()) / 86400 if c["exp"] else -1
    print("token : %s..." % c["token"][:24])
    print("device: %s" % c["device_id"])
    print("install: %s" % c["install_id"])
    print("expire: %s（剩余 %.1f 天）" % (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(c["exp"])) if c["exp"] else "未知", left))
