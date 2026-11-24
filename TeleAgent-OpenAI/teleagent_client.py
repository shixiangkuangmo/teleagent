"""
TeleAgent 本地通道客户端
========================
通过本机 4397 的 opencode server 调用天翼云 LLM（消耗 TeleAgent 账号积分）。

前置：TeleAgent 必须处于运行状态（4397 在监听）。

用法：
    python teleagent_client.py key                  # 从内存提取当前 sessionKey
    python teleagent_client.py chat "你好"           # 单轮对话（自动提取 key）
    python teleagent_client.py chat "你好" --model chat-flagship
    python teleagent_client.py models               # 列出可用模型
    python teleagent_client.py sessions             # 列出会话

作为库使用：
    from teleagent_client import Client
    c = Client()                       # 自动提取 key
    c = Client(key="...")              # 指定 key
    print(c.chat("写个快排"))
    print(c.chat("继续", session_id=c.session_id))
"""
import sys, io, os, json, time, hmac, hashlib, base64, secrets
import urllib.request, urllib.error, ctypes
from ctypes import wintypes as wt

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:4397"
SIGN_VERSION = "local-v1"

# 隐身：客户端的会话列表只拉根会话（GET /session?roots=true），
# 把我们的临时会话挂到一个不存在的父 ID 下，它就永远不在列表里出现。
ORPHAN_PARENT = "ses_0000000000000000orphan00"
SESSION_DIR = os.environ.get("TG_SESSION_DIR", r"C:\Windows\Temp\tg-gw")


# ─────────────────────────── 签名 ───────────────────────────
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _headers(key: str, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(12)
    # 注意：分隔符是换行 \n —— 不是空格
    payload = "\n".join([SIGN_VERSION, method.upper(), path, ts, nonce])
    sig = _b64url(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    return {
        "X-SA-Sign-Version": SIGN_VERSION,
        "X-SA-Timestamp": ts,
        "X-SA-Nonce": nonce,
        "X-SA-Signature": sig,
        "Accept": "application/json",
    }


# ─────────────────────── 从内存提取 key ───────────────────────
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
_k32.ReadProcessMemory.restype = wt.BOOL
_k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class _MBI64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("_a1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_a2", wt.DWORD)]


_READ = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80, 0x10}


def _all_pids(image="TeleAgent.exe"):
    """枚举同名进程（纯 ctypes，不依赖 PowerShell）。

    打包版 Python（embeddable）里 subprocess 调 powershell 会拿到空输出，
    所以这里改用 Toolhelp32 快照，任一台机器都能跑。
    """
    TH32CS_SNAPPROCESS = 0x00000002

    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PE32)]
    _k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PE32)]

    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        return []
    pe = _PE32()
    pe.dwSize = ctypes.sizeof(_PE32)
    out = []
    try:
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == image.lower():
                out.append(int(pe.th32ProcessID))
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _k32.CloseHandle(snap)
    return out


def _exe_path(pid):
    """取进程映像路径，用来区分 Electron 主程序与运行时里的同名副本。"""
    h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        _k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                    ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        _k32.CloseHandle(h)
    return ""


def _pids_via_wmi(image="TeleAgent.exe"):
    """用 WMI 补全进程列表。

    受限环境（AppContainer / 作业对象 / 服务会话）里 Toolhelp 与 EnumProcesses
    只能看到一小部分进程，看不到 TeleAgent；WMI 由 WmiPrvSE 执行，能看全。
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
             "Select-Object -ExpandProperty ProcessId" % image],
            timeout=25, text=True, errors="replace")
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def find_backend_pid(port=4397):
    """按监听端口找后端进程 PID（netstat 走 TCP 表，不受进程枚举限制影响）。"""
    import subprocess
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                      timeout=20, text=True, errors="replace")
    except Exception:
        return None
    needle = ":%d" % port
    for line in out.splitlines():
        if needle not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) >= 5:
            tail = parts[-1]
            if tail.isdigit():
                return int(tail)
    return None


def find_main_pid():
    """找最像 Electron 主程序的 TeleAgent 进程（优先 Program Files 下那个）。"""
    pids = _all_pids()
    if not pids:
        pids = _pids_via_wmi()
    if not pids:
        return None
    for pid in pids:
        p = _exe_path(pid)
        if p and "\\program files\\" in p.lower():
            return pid
    return pids[0]


def _candidates(pid, budget=300):
    import re
    A8 = re.compile(rb"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])")
    A16 = re.compile(rb"(?:(?:[A-Za-z0-9_-])\x00){43}")
    h = _k32.OpenProcess(0x0410, False, pid)
    if not h:
        return []
    mbi = _MBI64(); addr = 0x10000; t0 = time.time()
    found = {}
    while addr < 0x7FFFFFFEFFFF and time.time() - t0 < budget:
        if not _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize
        if mbi.State == 0x1000 and mbi.Protect in _READ and mbi.Type != 0x1000000 \
                and 0 < size <= 256 * 1024 * 1024:
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            if _k32.ReadProcessMemory(h, ctypes.c_void_p(mbi.BaseAddress), buf, size, ctypes.byref(got)):
                d = buf.raw[:got.value]
                for m in A8.finditer(d):
                    found.setdefault(m.group(1).decode(), 1)
                for m in A16.finditer(d):
                    found.setdefault(m.group().decode("utf-16-le", "replace"), 1)
        addr = mbi.BaseAddress + size
    _k32.CloseHandle(h)
    return list(found.keys())


_KEY_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".session_key")


def _cached_key():
    """读本地缓存并验证是否仍有效。"""
    try:
        if os.path.exists(_KEY_CACHE):
            k = open(_KEY_CACHE, "r", encoding="utf-8").read().strip()
            if k and len(k) >= 40 and _raw(k, "GET", "/session", timeout=3)[0] == 200:
                return k
    except Exception:
        pass
    return None


def extract_session_key(pid=None, verbose=True):
    """先试本地缓存，失效再扫主进程内存。"""
    hit = _cached_key()
    if hit:
        if verbose:
            print("使用缓存 key: %s..." % hit[:12])
        return hit

    if pid:
        pids = [pid]
    else:
        pids = []
        bp = find_backend_pid()            # 4297→4397 监听者就是持有 key 的后端，最准
        if bp:
            pids.append(bp)
        for p in _all_pids():              # 其余同名进程作为补充
            if p not in pids:
                pids.append(p)
        if len(pids) < 5:                  # 受限环境用 WMI 补全
            for p in _pids_via_wmi():
                if p not in pids:
                    pids.append(p)
        pids.sort(key=lambda p: p != bp)
    if not pids:
        raise RuntimeError("找不到 TeleAgent 进程，先启动 TeleAgent")
    cands = []
    for p in pids:
        if verbose:
            print("扫描进程 %d 内存..." % p)
        cands.extend(_candidates(p))
    if verbose:
        print("候选 %d 个，逐个验证..." % len(cands))
    for i, c in enumerate(cands):
        st, _ = _raw(c, "GET", "/session", timeout=4)
        if st == 200:
            if verbose:
                print("命中: %s" % c)
            try:
                open(_KEY_CACHE, "w", encoding="utf-8").write(c)
            except Exception:
                pass
            return c
        if verbose and i and i % 500 == 0:
            print("  %d/%d" % (i, len(cands)))
    raise RuntimeError("没找到 sessionKey（候选 %d 个）" % len(cands))


# ─────────────────────────── HTTP ───────────────────────────
def _raw(key, method, path, body=None, timeout=300):
    headers = _headers(key, method, path)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


class Client:
    def __init__(self, key=None, auto=True):
        self.key = key or (extract_session_key(verbose=False) if auto else None)
        if not self.key:
            raise RuntimeError("需要 sessionKey")
        self.session_id = None

    def call(self, method, path, body=None, timeout=300):
        return _raw(self.key, method, path, body, timeout)

    def config(self):
        st, raw = self.call("GET", "/config")
        return json.loads(raw) if st == 200 else None

    def models(self):
        cfg = self.config() or {}
        out = []
        for prov, pdata in (cfg.get("provider") or {}).items():
            for mid in (pdata.get("models") or {}):
                out.append("%s/%s" % (prov, mid))
        return out

    def sessions(self):
        st, raw = self.call("GET", "/session")
        return json.loads(raw) if st == 200 else []

    def new_session(self, title="api", hidden=True):
        """hidden=True 时建“孤儿会话”：挂在假父 ID 下 + 独立目录，
        客户端列表（roots=true）看不到，也不会占用你自己的工作空间。"""
        body = {"title": title}
        if hidden:
            body["parentID"] = ORPHAN_PARENT
            body["directory"] = SESSION_DIR
            body["workspaceID"] = "tg_gw"
        st, raw = self.call("POST", "/session", body)
        if st != 200:
            raise RuntimeError("建会话失败: %s %s" % (st, raw[:200]))
        self.session_id = json.loads(raw)["id"]
        return self.session_id

    def chat(self, text, session_id=None, provider="NewApi", model="chat-lite", timeout=600):
        """发一条消息，返回回复文本。"""
        sid = session_id or self.session_id or self.new_session()
        body = {
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": provider, "modelID": model},
        }
        st, raw = self.call("POST", "/session/%s/message" % sid, body, timeout=timeout)
        if st != 200:
            raise RuntimeError("调用失败: %s %s" % (st, raw[:300]))
        j = json.loads(raw)
        data = j.get("data", {})
        info = data.get("info", {})
        parts = data.get("parts") or []
        reply = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        usage = info.get("tokens") or {}
        self.last_usage = usage
        return reply, sid, usage


# ─────────────────────────── CLI ───────────────────────────
def _main():
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd = args[0]
    rest = args[1:]

    if cmd == "key":
        print(extract_session_key())
        return

    if cmd == "models":
        c = Client()
        for m in c.models():
            print(" ", m)
        return

    if cmd == "sessions":
        c = Client()
        for s in c.sessions():
            print("  %s  %s" % (s.get("id"), (s.get("title") or "")[:50]))
        return

    if cmd == "chat":
        if not rest:
            print("用法: chat <文本> [--model chat-lite] [--session ses_xxx]")
            return
        text = rest[0]
        model = "chat-lite"
        sid = None
        if "--model" in rest:
            model = rest[rest.index("--model") + 1]
        if "--session" in rest:
            sid = rest[rest.index("--session") + 1]
        c = Client()
        reply, used_sid, usage = c.chat(text, session_id=sid, model=model)
        print(reply)
        print("\n[session=%s model=%s tokens=%s]" % (used_sid, model, usage), file=sys.stderr)
        return

    print("未知命令:", cmd)
    print(__doc__)


if __name__ == "__main__":
    _main()
