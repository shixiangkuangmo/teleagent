"""
TeleAgent OpenAI 兼容网关 v2
============================
把本机 TeleAgent（4397 opencode server）包装成标准 OpenAI 接口。

v2 相对 v1 的关键修复
---------------------
1. **真流式**：prompt_async 异步提交 + 订阅 4397 的 /global/event (SSE)，
   把 message.part.delta 实时转发给客户端。v1 是「等上游完整回复再切块」，
   大请求（10万+ token）期间客户端零字节可收，会被判超时断连（WinError 10053）
   ——这就是「卡死」的根因。
2. **首字节探活**：流式连接建立后立刻发 role chunk，客户端立即收到响应头，不再干等。
3. **心跳保活**：等待上游期间每 3 秒发一个 SSE 注释行 `: keepalive`，防中间层超时。
4. **请求日志**：记录每个请求的耗时/token/状态，便于排查卡顿。
5. **key 提取不阻塞**：key 失效时后台重提，期间请求排队而不是全部卡在锁上。

启动：
    python teleagent_gateway.py                # 默认 127.0.0.1:8899
    python teleagent_gateway.py --port 9000

接口：
    GET  /v1/models              列出模型
    POST /v1/chat/completions    OpenAI 格式（流式；tools/response_format 走提示词桥接）
    可选：TG_API_KEY 开启 Bearer 鉴权，TG_MAX_BODY 限制请求体大小

依赖：teleagent_client.py 同目录
"""
import sys, os, json, time, argparse, threading, queue, re, hmac, uuid, socket
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teleagent_client import Client, _headers, BASE as UPSTREAM, SESSION_DIR  # noqa: E402
from tg_sanitize import sanitize_system, sanitize_any, residual_hits, suspicion_scan  # noqa: E402

DEFAULT_MODELS = ["chat-flagship", "chat-pro", "chat-flash", "chat-lite", "chat-nano"]
MODEL_DESC = {
    "chat-flagship": "旗舰 (最强推理)",
    "chat-pro": "智能",
    "chat-flash": "极速",
    "chat-lite": "轻量(默认)",
    "chat-nano": "轻速",
}

_state = {"client": None, "lock": threading.Lock()}
_log_lock = threading.Lock()

# 网关自身防护：请求体上限（防超大 body 打爆内存）与可选 API Key 校验。
# TG_API_KEY 未设置时保持原来的“本机免鉴权”行为；绑定到非 127.0.0.1 时会告警。
MAX_BODY_BYTES = int(os.environ.get("TG_MAX_BODY", str(8 * 1024 * 1024)))
API_KEY = os.environ.get("TG_API_KEY", "").strip()

# 运行统计与探活缓存：/pool/status 供节点代理采集，/health 只读缓存绝不阻塞。
GATEWAY_VERSION = "2.4.0"
STARTED_AT = time.time()
TG_NODE_NAME = os.environ.get("TG_NODE_NAME", "").strip()

_stats = {
    "inflight": 0, "requests": 0, "streams": 0, "errors": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "last_error_at": 0,
}
_stats_lock = threading.Lock()
_upstream = {"ok": None, "latency_ms": None, "at": 0, "key_set": False, "error": "", "state": "init"}


def _stat_inc(key, n=1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def _stat_error():
    with _stats_lock:
        _stats["errors"] = _stats.get("errors", 0) + 1
        _stats["last_error_at"] = int(time.time())


def _stat_add_tokens(pi, po):
    with _stats_lock:
        _stats["prompt_tokens"] += int(pi or 0)
        _stats["completion_tokens"] += int(po or 0)


def log(msg):
    with _log_lock:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ─────────────────────── key 管理 ───────────────────────
def get_client(force_new=False):
    with _state["lock"]:
        if _state["client"] is None or force_new:
            log("提取 sessionKey ...")
            t0 = time.time()
            _state["client"] = Client()
            log("key 就绪 (%.1fs): %s..." % (time.time() - t0, _state["client"].key[:12]))
        return _state["client"]


def call_with_retry(method, path, body=None, timeout=300):
    c = get_client()
    st, raw = c.call(method, path, body, timeout=timeout)
    if st == 401:
        log("key 失效(401)，重新提取后重试")
        c = get_client(force_new=True)
        st, raw = c.call(method, path, body, timeout=timeout)
    return st, raw


# ─────────────────────── 探活（后台线程，状态接口只读缓存） ───────────────────────
def _probe_once():
    """每 15 秒探一次上游；没有 client 时在后台提取，期间状态保持 probing。"""
    global _upstream
    c = _state["client"]
    if c is None:
        _upstream = {"ok": None, "latency_ms": None, "at": int(time.time()),
                     "key_set": False, "error": "", "state": "probing"}
        try:
            c = get_client()
        except Exception as e:
            _upstream = {"ok": False, "latency_ms": None, "at": int(time.time()),
                         "key_set": False, "error": str(e), "state": "error"}
            return
    t0 = time.time()
    try:
        st, _ = c.call("GET", "/session", timeout=3)
        ms = int((time.time() - t0) * 1000)
        ok = st == 200
        _upstream = {"ok": ok, "latency_ms": ms, "at": int(time.time()),
                     "key_set": True, "error": "" if ok else "HTTP %s" % st,
                     "state": "ok" if ok else "error"}
    except Exception as e:
        _upstream = {"ok": False, "latency_ms": None, "at": int(time.time()),
                     "key_set": True, "error": str(e), "state": "error"}


_probe_stop = threading.Event()


def _probe_loop():
    while not _probe_stop.is_set():
        try:
            _probe_once()
        except Exception as e:
            log("探活异常: %s" % e)
        _probe_stop.wait(15)


def _status_payload(full):
    with _stats_lock:
        stats = dict(_stats)
    up = dict(_upstream)
    up["key_set"] = _state["client"] is not None
    now = time.time()
    out = {
        "ok": True,
        "version": GATEWAY_VERSION,
        "name": TG_NODE_NAME,
        "models": DEFAULT_MODELS,
        "time": int(now),
        "auth_required": bool(API_KEY),
    }
    if not full:
        return out
    out.update({
        "upstream": up,
        "inflight": stats["inflight"],
        "totals": {
            "requests": stats["requests"],
            "streams": stats["streams"],
            "errors": stats["errors"],
            "prompt_tokens": stats["prompt_tokens"],
            "completion_tokens": stats["completion_tokens"],
            "last_error_at": stats["last_error_at"],
        },
        "limits": {"max_body": MAX_BODY_BYTES, "max_prompt": MAX_PROMPT_CHARS},
        "hostname": socket.gethostname(),
        "uptime_sec": int(now - STARTED_AT),
        "pid": os.getpid(),
    })
    return out


# 上游处理能力有限：实测 2 万 token 约 1.7 秒返回；14 万字符（7-10 万 token）
# 会让上游 300~900 秒后返超时空。超过阈值就截断，保留开头（系统提示）与结尾（最近对话）。
MAX_PROMPT_CHARS = int(os.environ.get("TG_MAX_PROMPT", "40000"))

# 系统提示词处理模式（TG_SYSTEM）：
#   replace  默认。上游系统提示词整段丢弃，只发一句内置的干净角色设定。
#            宿主那份几万字的提示词里有大量"人设/拒绝对抗/灰色规则"章节，
#            逐行清洗永远漏，漏一行上游就回一句"这是注入我不采用"，所以直接不发。
#   strip    连内置设定也不发，只发对话内容。
#   filter   保留上游提示词，只用 tg_sanitize 做清洗（漏网风险自负）。
SYSTEM_MODE = os.environ.get("TG_SYSTEM", "replace").strip().lower()
if SYSTEM_MODE not in ("replace", "strip", "filter"):
    SYSTEM_MODE = "replace"
CLEAN_SYSTEM = os.environ.get(
    "TG_SYSTEM_TEXT",
    "你是运行在用户本机的 AI 助手，擅长编码、数据处理与办公文档任务。"
    "请用简体中文回答；代码放在带语言标注的代码块里；直接给结论，简洁、不说教。",
)
# 想用自己写的提示词（例如 dsh-purge 的 prompt-inject.md）就直接指过去，
# replace 模式会用它替代内置那句。
_SYSFILE = os.environ.get("TG_SYSTEM_FILE", "").strip()
SYSTEM_SOURCE = "内置"
if _SYSFILE and os.path.exists(_SYSFILE):
    try:
        with open(_SYSFILE, encoding="utf-8") as _f:
            _t = _f.read().strip()
        if _t:
            CLEAN_SYSTEM = _t
            SYSTEM_SOURCE = "文件 %s" % _SYSFILE
    except Exception as _e:
        SYSTEM_SOURCE = "文件读取失败(%s): %s" % (_SYSFILE, _e)
elif _SYSFILE:
    SYSTEM_SOURCE = "文件不存在: %s" % _SYSFILE

# 每次对话都会在 TeleAgent 后端建一个会话，这个会话会出现在客户端的历史列表里。
# 默认用完即删，客户端就看不到对话记录；设 TG_KEEP_HISTORY=1 可保留（调试用）。
KEEP_HISTORY = os.environ.get("TG_KEEP_HISTORY", "0") == "1"


def drop_session(sid, why=""):
    """删除本次产生的临时会话，避免在 TeleAgent 客户端留下对话记录。"""
    if KEEP_HISTORY or not sid:
        return
    try:
        st, _ = call_with_retry("DELETE", "/session/%s" % sid, timeout=20)
        log("已清理临时会话 %s (HTTP %s)%s" % (sid, st, (" " + why) if why else ""))
    except Exception as e:
        log("清理临时会话失败 %s: %s" % (sid, e))


def truncate_prompt(text, limit=None):
    limit = limit or MAX_PROMPT_CHARS
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = int(limit * 0.35)
    tail = limit - head
    cut = len(text) - limit
    return (text[:head]
            + "\n\n...[中间 %d 字符因超出通道处理能力已省略]...\n\n" % cut
            + text[-tail:]), True


def trim_messages(messages, max_chars):
    """按消息维度裁剪：保留全部 system，从最近往前保留对话，超长的工具输出单独截断。"""
    if not isinstance(messages, list) or not messages:
        return messages, False
    systems = [m for m in messages if m.get("role") in ("system", "developer")]
    others = [m for m in messages if m.get("role") not in ("system", "developer")]

    def clen(m):
        c = m.get("content")
        return len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))

    # 系统提示词的实际开销：replace/strip 模式下它根本不发给上游，不能占预算。
    # （踩过的坑：按原长计入，5 万字系统提示词一上来就把预算吃光，
    #  结果只留下最后一条消息——而宿主会把时间/环境上下文追加在末尾，
    #  于是真正的问题被裁掉，模型只看到一条时间元数据。）
    if SYSTEM_MODE == "filter":
        total = sum(len(sanitize_system(m.get("content") or "")[0]) for m in systems)
    else:
        total = len(CLEAN_SYSTEM) if (SYSTEM_MODE == "replace" and systems) else 0
    kept = []
    dropped = 0
    # 无论预算如何，最近 MIN_KEEP 条对话必须保留，保证用户当下这句话一定在
    MIN_KEEP = int(os.environ.get("TG_MIN_KEEP", "3"))
    for m in reversed(others):
        L = clen(m)
        # 单条工具结果过长 → 就地截断，避免一条撑爆
        if L > 6000:
            m2 = dict(m)
            c = m2.get("content")
            if isinstance(c, str):
                m2["content"] = c[:6000] + "\n...[单条内容过长已截断]"
            m = m2
            L = clen(m)
        if total + L > max_chars and len(kept) >= MIN_KEEP:
            dropped += 1
            continue
        kept.append(m)
        total += L

    if not dropped:
        return messages, False
    return systems + list(reversed(kept)), True


def _dump_prompt(tag, text):
    """TG_DUMP=1 时把系统提示词落盘，便于按真实内容校准清洗规则。"""
    if os.environ.get("TG_DUMP") != "1":
        return
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt-dump-%s.txt" % tag)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def messages_to_text(messages):
    if not isinstance(messages, list):
        return str(messages)
    if len(messages) == 1 and messages[0].get("role") == "user":
        c = messages[0].get("content")
        if not isinstance(c, str):
            c = json.dumps(c, ensure_ascii=False)
        orig = c
        c, meta_cut = sanitize_any(c)
        if meta_cut:
            log("已剔除用户消息中的宿主元信息（%d -> %d 字符）" % (len(orig), len(c)))
        return c if c.strip() else orig
    lines = []
    sys_sent = False
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content is None:
            # OpenAI 的 assistant 工具调用消息 content 为 null，这里还原成可读文本
            tcs = m.get("tool_calls") or []
            content = json.dumps([
                {"name": (tc.get("function") or {}).get("name"),
                 "arguments": (tc.get("function") or {}).get("arguments")}
                for tc in tcs
            ], ensure_ascii=False) if tcs else ""
        if isinstance(content, str):
            if role in ("system", "developer"):
                _dump_prompt("raw", content)
                # developer 是 OpenAI 新规范里的"应用/用户规则"，不是平台元话术。
                # replace 模式下也不要整段丢弃，清洗后保留，否则用户自己的规则会失效。
                mode = "filter" if (role == "developer" and SYSTEM_MODE == "replace") else SYSTEM_MODE
                if mode == "strip":
                    if not sys_sent:
                        log("系统提示词已丢弃（strip 模式，%d 字符不发给上游）" % len(content))
                        sys_sent = True
                    continue
                if mode == "replace":
                    if not sys_sent:
                        log("系统提示词已替换（%d 字符 -> 内置 %d 字符）"
                            % (len(content), len(CLEAN_SYSTEM)))
                        sys_sent = True
                        content = CLEAN_SYSTEM
                    else:
                        continue
                else:
                    cleaned, changed = sanitize_system(content)
                    _dump_prompt("clean", cleaned)
                    if changed:
                        log("系统提示词已清洗: %d -> %d 字符（去掉上游会当作注入的人设/越狱话术）"
                            % (len(content), len(cleaned)))
                        left = residual_hits(cleaned)
                        if left:
                            log("清洗后仍有可疑模式 %d 处: %s" % (len(left), ", ".join(left[:4])))
                        scan = suspicion_scan(cleaned)
                        if scan:
                            log("残留可疑词统计(类别:次数): %s"
                                % ", ".join("%s:%d" % (k, v) for k, v in sorted(scan.items())))
                    content = cleaned
            else:
                orig_len = len(content)
                content, meta_cut = sanitize_any(content)
                if meta_cut and not content.strip():
                    # 整条都是宿主追加的元信息（运行时快照/技能清单），丢掉，别让上游读成注入
                    log("丢弃纯元信息消息（%d 字符）" % orig_len)
                    continue
                if meta_cut:
                    log("已剔除用户消息中的宿主元信息（%d -> %d 字符）" % (orig_len, len(content)))
        if role in ("system", "developer"):
            # 系统设定不加"系统:"前缀——上游会把"系统: xxx"当成伪造系统消息的注入
            # （实测：加了前缀它就回"那段以「系统:」开头的人设设定我不会采用"）
            lines.append(content)
            continue
        label = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(role, role)
        lines.append("%s: %s" % (label, content))
    return "\n\n".join(lines)


# ─────────────────── tools / function calling 桥接 ───────────────────
# 4397 通道只能发纯文本，没有原生 function calling。这里用一段 JSON 协议桥接：
# 需要调用工具时，模型只输出 {"tool_calls":[{"name":...,"arguments":{...}}]}，
# 网关解析后转成标准 OpenAI tool_calls。模型不按协议输出时按普通文本返回（安全降级）。
def upstream_tools_off():
    """关掉 opencode 上游自带的原生工具。

    4397 本身是个 agent，模型能看到 read/bash/edit 等原生工具定义。不关的话
    它会自己「读文件、跑命令」，工具调用不经过 OpenAI tool_calls，Trae 只能
    看到说到一半就没下文。opencode 的 prompt 接口支持 tools 开关，未知名字
    会被忽略，传 false 是安全的。
    """
    names = ("bash", "edit", "write", "read", "glob", "grep", "list", "patch",
             "task", "todowrite", "todoread", "webfetch", "websearch", "lsp",
             "multiedit", "skill")
    return {n: False for n in names}


def build_tool_prompt(tools, tool_choice):
    if tool_choice == "none":
        return ""
    specs = []
    # 宿主（Trae / WorkBuddy 等）会把整篇技能文档塞进工具描述，动辄数万字。
    # 全部透传会挤掉用户真正的问题，还会让模型把工具说明当成任务复述。
    # 这里只保留名称、截断后的简述和参数 schema。
    MAX_TOOLS = 32
    MAX_DESC = 240
    for t in (tools if isinstance(tools, list) else []):
        fn = t.get("function") if isinstance(t, dict) and t.get("type", "function") == "function" else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        desc = (fn.get("description") or "").strip()
        if len(desc) > MAX_DESC:
            desc = desc[:MAX_DESC] + "…"
        specs.append({
            "name": fn["name"],
            "description": desc,
            "parameters": fn.get("parameters") or {},
        })
        if len(specs) >= MAX_TOOLS:
            break
    if not specs:
        return ""
    force = ""
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            force = "本轮必须调用工具 %s，不要直接回答。\n" % name
    elif tool_choice == "required":
        force = "本轮必须调用至少一个工具，不要直接回答。\n"
    return (
        "\n\n[工具调用协议]\n"
        "下面只是可用工具的定义，不是用户任务；不要复述或解释这段协议。\n"
        "你可以调用下列本机工具。需要调用时，只输出一行 JSON，"
        "不要输出解释、Markdown 或代码围栏：\n"
        '{"tool_calls":[{"name":"工具名","arguments":{"参数名":值}}]}\n'
        "arguments 必须符合对应工具的 JSON Schema。不需要调用工具时，正常回答用户。\n"
        "你没有内置工具，不要尝试自己读取文件、执行命令或调用任何原生工具；"
        "所有工具调用只能走上面的 JSON 协议。\n"
        + force +
        "工具列表（JSON）：\n" + json.dumps(specs, ensure_ascii=False)
    )


def parse_tool_calls(text):
    """从模型回复里提取 tool_calls；不合法/未调用时返回 None。"""
    if not isinstance(text, str) or "tool_calls" not in text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # 上游会把正文切碎，偶尔把开头的 {" 丢掉，留下 tool_calls":[...
    # 补回缺的花括号和引号再解析，否则整段 JSON 会被当正文显示给用户
    if s.startswith('tool_calls"') or s.startswith("tool_calls'"):
        s = '{"' + s
    dec = json.JSONDecoder()
    idx = 0
    while True:
        i = s.find("{", idx)
        if i < 0:
            return None
        try:
            obj, end = dec.raw_decode(s[i:])
        except Exception:
            idx = i + 1
            continue
        idx = i + end
        calls = obj.get("tool_calls") if isinstance(obj, dict) else None
        if not isinstance(calls, list):
            continue
        out = []
        for c in calls:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") if isinstance(c.get("function"), dict) else {}
            name = c.get("name") or fn.get("name")
            args = c.get("arguments", fn.get("arguments"))
            if not name:
                continue
            if not isinstance(args, str):
                args = json.dumps(args if args is not None else {}, ensure_ascii=False)
            out.append({"name": name, "arguments": args})
        if out:
            return out
    return None


def _openai_tool_calls(calls):
    return [{
        "id": "call_%s" % uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": c["name"], "arguments": c["arguments"]},
    } for c in calls]


# ─────────────────────── HTTP ───────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _authorized(self):
        """TG_API_KEY 未设置时放行（仅本机使用）；设置后校验 Bearer / x-api-key。"""
        if not API_KEY:
            return True
        auth = self.headers.get("Authorization") or ""
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not key:
            key = (self.headers.get("x-api-key") or "").strip()
        if key and hmac.compare_digest(key, API_KEY):
            return True
        self._json(401, {"error": {"message": "invalid api key", "type": "invalid_request_error"}})
        return False

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            # 先丢掉请求体再回 413，避免客户端还在发送时只看到连接重置
            if n <= MAX_BODY_BYTES * 4:
                try:
                    self.rfile.read(n)
                except Exception:
                    pass
            self._json(413, {"error": {
                "message": "request body too large (limit %d bytes)" % MAX_BODY_BYTES,
                "type": "invalid_request_error"}})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return
            now = int(time.time())
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "teleagent",
                 "description": MODEL_DESC.get(m, "")} for m in DEFAULT_MODELS]})
            return
        if path == "/pool/status":
            # 网关默认只监听 127.0.0.1；无 TG_API_KEY 时也返回完整状态，
            # 否则节点代理上报不了 upstream，池会一直把该节点判为不可用。
            if API_KEY and not self._authorized():
                return
            self._json(200, _status_payload(True))
            return
        if path == "/health":
            up = dict(_upstream)
            if up.get("ok") is True:
                code, status = 200, "ok"
            elif up.get("ok") is False:
                code, status = 503, "error"
            else:
                code, status = 200, "probing"
            self._json(code, {"status": status, "upstream": up})
            return
        self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return

        body = self._read_body()
        if body is None:
            return
        if not self._authorized():
            return
        if not isinstance(body, dict):
            self._json(400, {"error": {"message": "body must be a JSON object",
                                       "type": "invalid_request_error"}})
            return
        _stat_inc("requests")
        _stat_inc("inflight")
        if bool(body.get("stream")):
            _stat_inc("streams")
        model = body.get("model") or "chat-lite"
        stream = bool(body.get("stream"))
        raw_msgs = body.get("messages") or []
        msgs, msgs_cut = trim_messages(raw_msgs, MAX_PROMPT_CHARS)
        text = messages_to_text(msgs)
        tools = body.get("tools") or []
        tool_prompt = build_tool_prompt(tools, body.get("tool_choice"))
        if tool_prompt:
            text += tool_prompt
        rf = body.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            text += "\n\n[输出格式] 只输出一个合法 JSON 对象，不要输出 Markdown 代码围栏或额外说明。"
        text, text_cut = truncate_prompt(text)
        if msgs_cut or text_cut:
            log("%s prompt 超限已裁剪: %d 条消息 -> %d 条，%d 字符"
                % (model, len(raw_msgs), len(msgs), len(text)))
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        t_req = time.time()
        log("%s 请求 %s prompt=%d 字符 stream=%s%s" % (
            model, self.path.split("?")[0], len(text), "是" if stream else "否",
            (" tools=%d" % len(tools)) if tools else ""))

        sid = None
        try:
            c = get_client()
            sid = c.new_session(title="gw")
            if stream:
                self._do_stream(sid, model, text, want_usage, t_req)
            else:
                self._do_block(sid, model, text, t_req)
        except Exception as e:
            log("请求异常: %s" % e)
            _stat_error()
            try:
                self._json(500, {"error": {"message": str(e), "type": "server_error"}})
            except Exception:
                pass
        finally:
            _stat_inc("inflight", -1)
            # 无论成功、失败还是客户端中断，都把临时会话删掉
            drop_session(sid)

    # ── 非流式：等完整回复 ──
    def _do_block(self, sid, model, text, t_req):
        st, raw = call_with_retry("POST", "/session/%s/message" % sid, {
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": "NewApi", "modelID": model},
            "tools": upstream_tools_off(),
        })
        el = time.time() - t_req
        if st != 200:
            log("%s 非流式失败 HTTP %s (%.1fs)" % (model, st, el))
            _stat_error()
            self._json(500, {"error": {
                "message": "upstream %s: %s" % (st, raw[:300].decode("utf-8", "replace")),
                "type": "upstream_error"}})
            return
        j = json.loads(raw)
        data = j.get("data", {})
        info = data.get("info", {})
        parts = data.get("parts") or []
        reply = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        usage = info.get("tokens") or {}
        pi, po = usage.get("input", 0), usage.get("output", 0)
        _stat_add_tokens(pi, po)
        calls = parse_tool_calls(reply)
        if calls:
            log("%s 非流式完成（tool_calls %d 个）in=%s out=%s (%.1fs)"
                % (model, len(calls), pi, po, el))
            message = {"role": "assistant", "content": None,
                       "tool_calls": _openai_tool_calls(calls)}
            finish = "tool_calls"
        else:
            log("%s 非流式完成 in=%s out=%s (%.1fs)" % (model, pi, po, el))
            message = {"role": "assistant", "content": reply}
            finish = "stop"
        self._json(200, {
            "id": "chatcmpl-%s" % sid, "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": pi, "completion_tokens": po,
                      "total_tokens": pi + po},
        })

    # ── 真流式：prompt_async 异步提交 + /global/event 实时取增量 ──
    def _do_stream(self, sid, model, text, want_usage, t_req):
        c = get_client()
        q = queue.Queue()
        stop = threading.Event()

        def open_stream(path):
            h = _headers(c.key, "GET", path)
            h["Accept"] = "text/event-stream"
            req = urllib.request.Request(UPSTREAM + path, headers=h)
            return urllib.request.urlopen(req, timeout=1800)

        def listener():
            try:
                try:
                    r = open_stream("/global/event")
                except Exception as e1:
                    log("订阅 /global/event 失败(%s)，回退 /event" % e1)
                    r = open_stream("/event")
                with r:
                    while not stop.is_set():
                        line = r.readline()
                        if not line:
                            break
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        # /global/event 外层是 {"payload": {...}}；/event 直接给事件本身
                        payload = ev.get("payload")
                        q.put(payload if isinstance(payload, dict) else ev)
            except Exception as e:
                q.put({"_error": str(e)})

        # 先订阅，再提交消息：否则最前面的增量会漏
        threading.Thread(target=listener, daemon=True).start()
        time.sleep(0.25)

        cid = "chatcmpl-%s" % sid
        created = int(time.time())
        log("%s 流式开始 (prompt≈%d 字符)" % (model, len(text)))

        # 立刻回响应头 + role chunk，客户端马上有反馈
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except Exception:
            return

        dead = threading.Event()

        def emit(obj):
            if dead.is_set():
                return False
            try:
                self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
                return True
            except Exception:
                dead.set()
                return False

        def chunk(delta, fin=None):
            # 必须 return：调用处用 `if not chunk(...)` 判断连接是否还活着
            return emit({"id": cid, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]})

        def send_tail(u):
            u = u or {}
            if want_usage and u:
                emit({"id": cid, "object": "chat.completion.chunk", "created": created,
                      "model": model, "choices": [],
                      "usage": {"prompt_tokens": u.get("input", 0),
                                "completion_tokens": u.get("output", 0),
                                "total_tokens": u.get("input", 0) + u.get("output", 0)}})
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            # 收尾关连接：有些客户端读到 EOF 才认为流结束，挂着 keep-alive 会让它干等
            try:
                self.close_connection = True
            except Exception:
                pass
            if u:
                _stat_add_tokens(u.get("input", 0), u.get("output", 0))

        chunk({"role": "assistant"})

        # 异步提交：上游立刻返回 204，正文全部靠事件流推过来。原来那条同步
        # /session/{sid}/message 一挂就是几十分钟，期间没有任何正文增量，
        # 客户端判 idle 直接断流——这就是「思考到一半停住」的根因。
        try:
            st, raw = c.call("POST", "/session/%s/prompt_async" % sid, {
                "sessionID": sid,
                "parts": [{"type": "text", "text": text}],
                "directory": SESSION_DIR,
                "model": {"providerID": "NewApi", "modelID": model},
                # 关键：关掉上游原生工具，工具调用只能走网关的 JSON 协议返回给客户端
                "tools": upstream_tools_off(),
            }, timeout=15)
        except Exception as e:
            st, raw = None, str(e).encode()

        if st not in (200, 204):
            detail = raw[:300].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            log("%s prompt_async 失败 HTTP %s %s" % (model, st, detail))
            _stat_error()
            chunk({"content": "\n[上游 %s: %s]" % (st, detail)}, "stop")
            send_tail({})
            return

        text_pids = set()
        msg_ids = set()
        part_types = {}
        pending = {}
        ev_count = {}
        content_acc = []
        usage = {}
        got_any = False
        error_msg = ""
        first_token_at = [None]
        completed_at = [0.0]
        last_beat = time.time()
        last_ev = time.time()

        while True:
            if dead.is_set():
                break
            try:
                ev = q.get(timeout=0.3)
            except queue.Empty:
                ev = None

            if ev is None:
                now = time.time()
                # 缓存超过 1 秒还没等到 part.updated：类型未知，按正文放出，
                # 宁可混一点也不丢字（正文缺开头会让 tool_calls JSON 解析失败）
                if pending:
                    for p, ent in list(pending.items()):
                        if now - ent[0] >= 1.0:
                            del pending[p]
                            for dly in ent[1]:
                                content_acc.append(dly)
                                chunk({"content": dly})
                # Trae 的 stream_idle_timeout 只认 data: 帧，SSE 注释行算静默，
                # 所以用零宽空格 content 做心跳：客户端视为有进度，正文不可见。
                if now - last_beat >= 8.0:
                    if not chunk({"content": "\u200b"}):
                        break
                    last_beat = now
                # 单条 assistant 消息完成 ≠ 整轮结束：模型可能刚发起工具调用，
                # 后面还有工具结果 + 新消息。给 3 秒宽限，期间有新事件就继续。
                if completed_at[0] and now - completed_at[0] > 3.0:
                    break
                if now - t_req > 1800:
                    error_msg = "总超时"
                    log("%s 流式总超时" % model)
                    break
                if now - last_ev > 150:
                    log("%s 事件流 %.0fs 无事件，判定结束" % (model, now - last_ev))
                    break
                continue

            if "_error" in ev:
                error_msg = str(ev["_error"])
                log("事件流错误: %s" % error_msg)
                break

            last_ev = time.time()
            et = ev.get("type") or "?"
            ev_count[et] = ev_count.get(et, 0) + 1
            pr = ev.get("properties") or {}
            ssid = pr.get("sessionID")
            if ssid and ssid != sid:
                continue

            if et == "message.updated":
                info = pr.get("info") or {}
                if info.get("role") == "assistant":
                    if info.get("id"):
                        msg_ids.add(info["id"])
                    tk = info.get("tokens") or {}
                    if tk:
                        usage = tk
                    if info.get("error") or info.get("finish") == "error":
                        error_msg = "上游返回错误"
                    if (info.get("time") or {}).get("completed"):
                        # 不直接结束：工具调用后还会有后续消息
                        completed_at[0] = time.time()
            elif et == "message.part.updated":
                part = pr.get("part") or {}
                ptype = part.get("type") or ""
                pid = part.get("id")
                if pid:
                    part_types[pid] = ptype
                    ent = pending.pop(pid, None)
                    if ent and ptype not in ("tool", "tool-call", "tool-invocation",
                                             "patch", "snapshot"):
                        for dly in ent[1]:
                            if ptype == "text":
                                content_acc.append(dly)
                                chunk({"content": dly})
                            else:
                                chunk({"reasoning_content": dly})
                if ptype == "text" and pid:
                    mid = part.get("messageID") or pr.get("messageID")
                    if not msg_ids or mid in msg_ids:
                        text_pids.add(pid)
                elif ptype in ("tool", "tool-call", "tool-invocation"):
                    log("%s 上游仍在执行原生工具 %s（opencode.json 未生效，需要重启 TeleAgent 客户端）"
                        % (model, part.get("name") or part.get("tool") or "?"))
                elif ptype == "step-finish":
                    tk = part.get("tokens") or {}
                    if tk and not usage:
                        usage = tk
                if part.get("error"):
                    error_msg = "上游返回错误"
            elif et == "message.part.delta":
                pid = pr.get("partID")
                d = pr.get("delta") or ""
                if not d:
                    continue
                ptype = part_types.get(pid, "")
                if ptype in ("tool", "tool-call", "tool-invocation",
                             "step-start", "step-finish", "patch", "snapshot"):
                    continue
                if first_token_at[0] is None:
                    first_token_at[0] = time.time() - t_req
                    log("%s 首字 %.1fs" % (model, first_token_at[0]))
                last_beat = last_ev
                completed_at[0] = 0.0
                if not ptype and pid:
                    # part.updated 可能晚于第一批 delta：先按 part 缓存，
                    # 等类型确定了再归到 content / reasoning_content，
                    # 否则开头几个字符会被错判丢弃（tool_calls JSON 就解析不出来）
                    ent = pending.get(pid)
                    if ent is None:
                        pending[pid] = [time.time(), [d]]
                    else:
                        ent[1].append(d)
                    continue
                # 正文 = part 类型为 text；reasoning/thinking 一律走 reasoning_content，
                # 不能当正文，否则客户端会把模型内心独白渲染成回复。
                if ptype == "text" or (not pid and pr.get("field") == "text"):
                    content_acc.append(d)
                    if not chunk({"content": d}):
                        break
                else:
                    if not chunk({"reasoning_content": d}):
                        break
            elif et == "session.status":
                status = pr.get("status") or {}
                if status.get("type") == "idle":
                    break
            elif et == "session.error" or et.startswith("permission."):
                # 上游工具审批等交互我们不参与，记下来便于排查
                log("%s 上游事件 %s: %s" % (model, et,
                                            json.dumps(pr, ensure_ascii=False)[:300]))

        stop.set()
        # 收尾：还没等到类型判定的增量按正文补发，避免丢字
        for ent in pending.values():
            for dly in ent[1]:
                content_acc.append(dly)
                chunk({"content": dly})
        el = time.time() - t_req

        if ev_count:
            pts = {}
            for t in part_types.values():
                pts[t] = pts.get(t, 0) + 1
            log("%s 事件统计: %s | part: %s" % (
                model,
                ",".join("%s=%d" % (k, v) for k, v in sorted(ev_count.items())),
                ",".join("%s=%d" % (k, v) for k, v in sorted(pts.items())) or "无"))

        full = "".join(content_acc)
        calls = parse_tool_calls(full) if full else None
        if calls:
            wire = _openai_tool_calls(calls)
            for i, tc in enumerate(wire):
                tc["index"] = i
            chunk({"tool_calls": wire})
            chunk({}, "tool_calls")
            got_any = True
        elif error_msg:
            chunk({"content": "\n[网关错误: %s]" % error_msg}, "stop")
        else:
            chunk({}, "stop")
            if full:
                got_any = True

        send_tail(usage)
        if error_msg:
            _stat_error()
        log("%s 流式完成 in=%s out=%s 首字=%s 总耗时=%.1fs%s" % (
            model, usage.get("input", 0), usage.get("output", 0),
            ("%.1fs" % first_token_at[0]) if first_token_at[0] else "无",
            el, "" if got_any else " [无内容]"))


def _opencode_config_paths():
    """opencode 会加载的项目/全局配置路径（项目级 + 用户级都写一份）。"""
    home = os.path.expanduser("~")
    paths = [
        os.path.join(SESSION_DIR, "opencode.json"),
        os.path.join(home, ".config", "opencode", "opencode.json"),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, "opencode", "opencode.json"))
    return paths


def ensure_upstream_no_tools():
    """把上游原生工具关掉。

    这个版本的 4397 不认请求体里的 tools 开关，只能走 opencode 配置。
    项目级配置（会话目录）不一定被加载，所以用户级 ~/.config/opencode
    也写一份——节点机器上的 TeleAgent 本来就只当模型后端用，关掉它
    自己的 read/bash/edit 正是我们想要的效果。已有配置会被保留，只
    覆盖 tools 里的对应项。
    """
    tools = upstream_tools_off()
    for p in _opencode_config_paths():
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            cfg = {}
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception:
                    # 已有配置不是合法 JSON 就别动，免得把用户的东西写坏
                    log("跳过 %s：已有内容不是合法 JSON，需手动处理" % p)
                    continue
                if not isinstance(cfg, dict):
                    continue
            cur = cfg.get("tools")
            cur = cur if isinstance(cur, dict) else {}
            merged = dict(cur)
            merged.update(tools)
            if merged == cur and "tools" in cfg:
                continue
            cfg["$schema"] = "https://opencode.ai/config.json"
            cfg["tools"] = merged
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            log("已更新 %s：关闭上游原生工具（重启 TeleAgent 客户端后生效）" % p)
        except Exception as e:
            log("写入 %s 失败: %s" % (p, e))


def probe_upstream_config():
    """读一次上游 /config，确认 tools 配置到底有没有被加载。"""
    try:
        c = get_client()
        st, raw = c.call("GET", "/config", None, timeout=8)
        if st != 200:
            log("[config] 上游 /config HTTP %s" % st)
            return
        obj = json.loads(raw.decode("utf-8", "replace"))
        tools = obj.get("tools") if isinstance(obj, dict) else None
        off = sum(1 for v in (tools or {}).values() if v is False)
        log("[config] 上游 tools: %s（已关闭 %d 项）" % (
            "未配置" if tools is None else "已加载", off))
    except Exception as e:
        log("[config] 读取上游 /config 失败: %s" % e)


def create_gateway(host="127.0.0.1", port=8899, prewarm=True):
    """创建网关 HTTP 服务并启动探活线程；调用方负责 serve_forever。

    图形界面（node_gui.py）用它把网关内嵌到同一个进程里。
    """
    ensure_upstream_no_tools()
    if prewarm:
        try:
            get_client()
        except Exception as e:
            log("预提取失败（请求时会重试）: %s" % e)

    _probe_stop.clear()
    threading.Thread(target=_probe_loop, daemon=True).start()
    threading.Thread(target=probe_upstream_config, daemon=True).start()

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log("已启动 http://%s:%d/v1（v%s）" % (host, port, GATEWAY_VERSION))
    log("模型: %s" % ", ".join(DEFAULT_MODELS))
    log("系统提示词模式: %s（replace/strip/filter），生效来源: %s，%d 字符" % (SYSTEM_MODE, SYSTEM_SOURCE, len(CLEAN_SYSTEM)))
    log("鉴权: %s；请求体上限: %d 字节" % ("TG_API_KEY 已启用" if API_KEY else "未启用（仅建议本机使用）", MAX_BODY_BYTES))
    if host not in ("127.0.0.1", "localhost", "::1") and not API_KEY:
        log("警告: 监听 %s 且未设置 TG_API_KEY，同网段任何人都能消耗账号积分" % host)
    return srv


def stop_gateway(srv=None):
    """停止探活线程并关闭监听（在非 serve_forever 线程调用）。"""
    _probe_stop.set()
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-prewarm", action="store_true")
    args = ap.parse_args()

    srv = create_gateway(args.host, args.port, prewarm=not args.no_prewarm)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        stop_gateway(srv)


if __name__ == "__main__":
    main()
