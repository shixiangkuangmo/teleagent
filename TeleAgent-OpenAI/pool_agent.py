"""
pool_agent.py — 把本机 TeleAgent 网关接入 dashboard 的 API 池
=============================================================
节点通常在 NAT 后，dashboard 无法主动访问，因此本程序主动出站建立 WebSocket
长连接（ws/wss），把本地网关状态上报给面板，并转发面板下发的聊天请求。

零第三方依赖：WebSocket 客户端手写（握手 / 掩码帧 / 64 位长度 / ping-pong / close），
TLS 用标准库 ssl，本地网关用 http.client。

启动示例：
    python pool_agent.py --server wss://panel.example.com/api/pool/agent \
        --node-id n_xxxx --token sk-node-xxxx \
        --gateway-url http://127.0.0.1:8899 --gateway-key <TG_API_KEY>

也可以用 --config pool-agent.json 从文件读取（见 pool-agent.example.json）。

环境变量（同名优先于配置文件，低于命令行）：POOL_SERVER、POOL_NODE_ID、POOL_TOKEN、
POOL_GATEWAY_URL、POOL_GATEWAY_KEY（缺省回退 TG_API_KEY）、POOL_NAME、POOL_MAX_INFLIGHT、
POOL_INSECURE。
"""
import argparse
import base64
import http.client
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AGENT_VERSION = "1.0.0"
DEFAULT_MODELS = ["chat-flagship", "chat-pro", "chat-flash", "chat-lite", "chat-nano"]
MAX_FRAME = 32 * 1024 * 1024


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class WSClient:
    """最小 WebSocket 客户端：只支持 JSON 文本帧、二进制、ping/pong/close。"""

    def __init__(self, url, token, insecure=False, timeout=15):
        self.url = url
        self.token = token
        self.insecure = insecure
        self.timeout = timeout
        self.sock = None
        self.send_lock = threading.Lock()
        self._buf = bytearray()
        self.closed = False

    def connect(self):
        u = urllib.parse.urlsplit(self.url)
        if u.scheme not in ("ws", "wss"):
            raise RuntimeError("server 必须是 ws:// 或 wss:// 地址")
        secure = u.scheme == "wss"
        host = u.hostname
        if not host:
            raise RuntimeError("server 地址缺少主机名")
        port = u.port or (443 if secure else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            ctx = ssl.create_default_context()
            if self.insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if port in (80, 443) else "%s:%d" % (host, port)
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Authorization: Bearer %s\r\n"
            "\r\n"
        ) % (path, host_header, key, self.token)
        raw.sendall(req.encode("utf-8"))

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                raise RuntimeError("握手失败：连接被关闭")
            buf += chunk
            if len(buf) > 65536:
                raise RuntimeError("握手失败：响应头过大")
        head, rest = buf.split(b"\r\n\r\n", 1)
        line = head.decode("latin1", "replace").split("\r\n", 1)[0]
        if " 101 " not in line:
            raise RuntimeError("握手失败：%s" % line)
        self.sock = raw
        self.sock.settimeout(None)
        self._buf = bytearray(rest)
        self.closed = False

    # ---------- 帧 ----------
    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_frame(self):
        b1, b2 = self._read_exact(2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._read_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._read_exact(8))[0]
        if ln > MAX_FRAME:
            raise RuntimeError("收到超大帧：%d" % ln)
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(ln) if ln else b""
        if masked and data:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return bool(b1 & 0x80), opcode, data

    def send_frame(self, opcode, payload=b""):
        if not self.sock:
            raise ConnectionError("未连接")
        header = bytearray()
        header.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj):
        self.send_frame(1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def send_ping(self):
        self.send_frame(9, b"")

    def close(self):
        self.closed = True
        try:
            self.send_frame(8, b"")
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def recv_messages(self, on_message):
        frag = b""
        while True:
            fin, opcode, data = self.recv_frame()
            if opcode == 8:
                raise ConnectionError("服务端关闭连接")
            if opcode == 9:
                try:
                    self.send_frame(10, data)
                except Exception:
                    raise ConnectionError("回 pong 失败")
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                frag = data
            elif opcode == 0:
                frag += data
            else:
                continue
            if fin:
                payload = frag
                frag = b""
                if payload:
                    on_message(payload)


class Agent:
    def __init__(self, cfg):
        self.server = cfg["server"]
        self.node_id = cfg["node_id"]
        self.token = cfg["token"]
        self.gateway_url = cfg["gateway_url"].rstrip("/")
        self.gateway_key = cfg.get("gateway_key") or ""
        self.name = cfg.get("name") or socket.gethostname()
        self.max_inflight = int(cfg.get("max_inflight") or 4)
        self.insecure = bool(cfg.get("insecure"))

        self.ws = None
        self.lock = threading.Lock()
        self.inflight = set()
        self.conns = {}
        self.stopping = False

    # ---------- 本地网关 ----------
    def _gateway_conn(self, timeout=5):
        u = urllib.parse.urlsplit(self.gateway_url)
        secure = u.scheme == "https"
        cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        conn = cls(u.hostname, u.port or (443 if secure else 80), timeout=timeout)
        base = u.path.rstrip("/")
        return conn, base

    def _auth_headers(self):
        return {"Authorization": "Bearer " + self.gateway_key} if self.gateway_key else {}

    def _get_json(self, path, timeout=5):
        conn, base = self._gateway_conn(timeout=timeout)
        try:
            conn.request("GET", base + path, headers=self._auth_headers())
            resp = conn.getresponse()
            data = resp.read()
            try:
                obj = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                obj = None
            return resp.status, obj
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def fetch_status(self):
        st, obj = self._get_json("/pool/status")
        if st != 404:
            return st, obj
        # 标准 OpenAI 网关（teleagent2api）没有 /pool/status，
        # 退回用 /v1/models 探活，并伪造同形状的状态给面板。
        t0 = time.time()
        st2, obj2 = self._get_json("/v1/models")
        ms = int((time.time() - t0) * 1000)
        models = DEFAULT_MODELS
        if st2 == 200 and isinstance(obj2, dict):
            got = [m.get("id") for m in (obj2.get("data") or [])
                   if isinstance(m, dict) and m.get("id")]
            if got:
                models = got
            up = {"ok": True, "latency_ms": ms, "key_set": bool(self.gateway_key),
                  "state": "ok", "error": ""}
        else:
            up = {"ok": False, "latency_ms": ms, "key_set": bool(self.gateway_key),
                  "state": "error", "error": "models HTTP %s" % st2}
        with self.lock:
            inflight = len(self.inflight)
        return 200, {"ok": True, "models": models, "upstream": up,
                     "inflight": inflight, "totals": {}}

    # ---------- 连接 ----------
    def run(self):
        delay = 1
        while not self.stopping:
            ws = WSClient(self.server, self.token, insecure=self.insecure)
            try:
                ws.connect()
                self.ws = ws
                delay = 1
                log("已连接 %s" % self.server)
                self._send_hello(ws)
                threading.Thread(target=self._status_loop, args=(ws,), daemon=True).start()
                threading.Thread(target=self._ping_loop, args=(ws,), daemon=True).start()
                ws.recv_messages(self._on_message)
                raise ConnectionError("连接结束")
            except KeyboardInterrupt:
                self.stopping = True
            except Exception as e:
                log("连接断开：%s（%d 秒后重连）" % (e, delay))
            finally:
                self.ws = None
                ws.close()
                self._close_all_conns()
            if self.stopping:
                break
            time.sleep(delay)
            delay = min(30, delay * 2)

    def _send_hello(self, ws):
        models = DEFAULT_MODELS
        try:
            st, obj = self.fetch_status()
            if st == 200 and isinstance(obj, dict) and obj.get("models"):
                models = obj["models"]
        except Exception:
            pass
        ws.send_json({
            "type": "hello",
            "node_id": self.node_id,
            "name": self.name,
            "hostname": socket.gethostname(),
            "version": AGENT_VERSION,
            "models": models,
            "max_inflight": self.max_inflight,
        })

    def _status_loop(self, ws):
        while self.ws is ws and not self.stopping:
            try:
                st, obj = self.fetch_status()
                if st == 200 and isinstance(obj, dict):
                    msg = {
                        "type": "status",
                        "upstream": obj.get("upstream") or {"ok": None},
                        "inflight": obj.get("inflight", 0),
                        "totals": obj.get("totals") or {},
                        "models": obj.get("models") or DEFAULT_MODELS,
                    }
                else:
                    msg = {"type": "status", "upstream": {
                        "ok": False, "error": "gateway HTTP %s" % st}}
                ws.send_json(msg)
            except Exception as e:
                try:
                    ws.send_json({"type": "status", "upstream": {
                        "ok": False, "error": "gateway unreachable: %s" % e}})
                except Exception:
                    return
            for _ in range(15):
                if self.ws is not ws or self.stopping:
                    return
                time.sleep(1)

    def _ping_loop(self, ws):
        while self.ws is ws and not self.stopping:
            for _ in range(20):
                if self.ws is not ws or self.stopping:
                    return
                time.sleep(1)
            try:
                ws.send_ping()
            except Exception:
                return

    def _on_message(self, payload):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        t = msg.get("type")
        if t == "chat":
            threading.Thread(target=self._do_chat, args=(msg,), daemon=True).start()
        elif t == "chat_cancel":
            self._cancel(msg.get("reqId"))

    def _cancel(self, req_id):
        with self.lock:
            conn = self.conns.get(req_id)
        if not conn:
            return
        s = getattr(conn, "sock", None)
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    def _close_all_conns(self):
        with self.lock:
            conns = list(self.conns.values())
            self.conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    def stop(self):
        """停止代理：置停止标志并关闭连接，run() 会在当前循环后退出。"""
        self.stopping = True
        ws = self.ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    # ---------- 聊天转发 ----------
    def _do_chat(self, msg):
        req_id = msg.get("reqId")
        body = msg.get("body") or {}
        if not req_id:
            return
        with self.lock:
            if len(self.inflight) >= self.max_inflight:
                self.inflight.discard(req_id)
                busy = True
            else:
                self.inflight.add(req_id)
                busy = False
        if busy:
            self._send({"type": "chat_error", "reqId": req_id, "message": "busy"})
            return

        conn = None
        try:
            conn, base = self._gateway_conn(timeout=1800)
            with self.lock:
                self.conns[req_id] = conn
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", "Content-Length": str(len(data))}
            headers.update(self._auth_headers())
            conn.request("POST", base + "/v1/chat/completions", body=data, headers=headers)
            resp = conn.getresponse()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            self._send({"type": "chat_start", "reqId": req_id,
                        "status": resp.status, "headers": hdrs})
            ctype = hdrs.get("content-type", "")
            if "text/event-stream" in ctype:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self._send_chunk(req_id, line)
            else:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self._send_chunk(req_id, chunk)
            self._send({"type": "chat_end", "reqId": req_id})
        except Exception as e:
            self._send({"type": "chat_error", "reqId": req_id, "message": str(e)})
        finally:
            with self.lock:
                self.inflight.discard(req_id)
                self.conns.pop(req_id, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _send_chunk(self, req_id, data):
        self._send({
            "type": "chat_chunk",
            "reqId": req_id,
            "data": base64.b64encode(data).decode("ascii"),
        })

    def _send(self, obj):
        ws = self.ws
        if not ws:
            raise ConnectionError("未连接")
        ws.send_json(obj)


def _arg_or_env(value, env_key, default=None):
    if value:
        return value
    v = os.environ.get(env_key)
    return v if v else default


def parse_config(argv):
    ap = argparse.ArgumentParser(description="TeleAgent 网关池节点代理")
    ap.add_argument("--config", help="JSON 配置文件路径（字段见 pool-agent.example.json）")
    ap.add_argument("--server", help="ws(s)://<panel>/api/pool/agent")
    ap.add_argument("--node-id", help="面板里创建的节点 ID")
    ap.add_argument("--token", help="节点 Token（sk-node-...）")
    ap.add_argument("--gateway-url", help="本地网关地址，默认 http://127.0.0.1:8899")
    ap.add_argument("--gateway-key", help="本地网关 TG_API_KEY（未设置可留空）")
    ap.add_argument("--name", help="节点显示名，默认主机名")
    ap.add_argument("--max-inflight", type=int, help="本节点最大并发，默认 4")
    ap.add_argument("--insecure", action="store_true", help="wss 跳过证书校验（仅调试用）")
    args = ap.parse_args(argv)

    file_cfg = {}
    if args.config:
        try:
            with open(args.config, encoding="utf-8") as f:
                file_cfg = json.load(f)
        except Exception as e:
            raise SystemExit("读取配置文件失败：%s" % e)

    def pick(cli, file_key, env_key, default=None):
        if cli:
            return cli
        if env_key and os.environ.get(env_key):
            return os.environ[env_key]
        if file_cfg.get(file_key) not in (None, ""):
            return file_cfg[file_key]
        return default

    cfg = {
        "server": pick(args.server, "server", "POOL_SERVER"),
        "node_id": pick(args.node_id, "node_id", "POOL_NODE_ID"),
        "token": pick(args.token, "token", "POOL_TOKEN"),
        "gateway_url": pick(args.gateway_url, "gateway_url", "POOL_GATEWAY_URL",
                            "http://127.0.0.1:8899"),
        "gateway_key": pick(args.gateway_key, "gateway_key", "POOL_GATEWAY_KEY",
                            os.environ.get("TG_API_KEY", "")),
        "name": pick(args.name, "name", "POOL_NAME", socket.gethostname()),
        "max_inflight": int(pick(args.max_inflight, "max_inflight", "POOL_MAX_INFLIGHT", 4)),
        "insecure": bool(args.insecure) or bool(file_cfg.get("insecure"))
                   or os.environ.get("POOL_INSECURE") == "1",
    }
    missing = [k for k in ("server", "node_id", "token") if not cfg.get(k)]
    if missing:
        raise SystemExit("缺少必填配置：%s（可用命令行、环境变量或 --config 提供）"
                         % ", ".join(missing))
    return cfg


def main():
    cfg = parse_config(sys.argv[1:])
    log("节点 %s → %s（本地网关 %s，最大并发 %d）" % (
        cfg["node_id"], cfg["server"], cfg["gateway_url"], cfg["max_inflight"]))
    agent = Agent(cfg)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stopping = True
        log("已停止")


if __name__ == "__main__":
    main()
