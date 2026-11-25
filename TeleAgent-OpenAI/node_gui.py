# -*- coding: utf-8 -*-
"""
node_gui.py — TeleAgent 节点图形界面
====================================
一个窗口完成配置：填写面板地址与节点凭据，点「启动」后同时运行
本机 OpenAI 网关（teleagent_gateway）和池代理（pool_agent）。

可打包成单个 exe（见 build-exe.bat），目标机器无需安装 Python。

配置保存在 exe 同目录的 pool-agent.json；该目录不可写时回退到
%APPDATA%\\TeleAgentNode\\pool-agent.json。
"""
import importlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import pool_agent
import teleagent_cred

APP_NAME = "TeleAgent 节点"
APP_VERSION = "1.2.0"
CONFIG_NAME = "pool-agent.json"
TEXT_ROWS = ("server", "node_id", "token", "name", "max_inflight",
             "gateway_url", "gateway_key", "max_prompt")

# 上游方式：tg2api = 直连平台 chat 接口（无 agent、无原生工具，推荐）
#           embedded = 本机 4397 opencode 网关（会自己跑工具，不推荐）
UPSTREAM_LABELS = {
    "tg2api": "teleagent2api（直连平台，推荐）",
    "embedded": "内置网关（4397，agent 模式）",
}
LABEL_TO_UPSTREAM = {v: k for k, v in UPSTREAM_LABELS.items()}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    """优先与 exe 同目录；无写权限时回退到 %APPDATA%。"""
    p = os.path.join(app_dir(), CONFIG_NAME)
    if os.path.exists(p):
        return p
    try:
        probe = os.path.join(app_dir(), ".ta-write-test")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
        return p
    except OSError:
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "TeleAgentNode")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, CONFIG_NAME)


def default_config():
    return {
        "server": "",
        "node_id": "",
        "token": "",
        "name": socket.gethostname(),
        "max_inflight": 4,
        "gateway_url": "http://127.0.0.1:8899",
        "gateway_key": "",
        "upstream": "tg2api",
        "auto_gateway": True,
        "max_prompt": 40000,
        "insecure": False,
    }


def load_config(path):
    cfg = default_config()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in cfg:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


class QueueWriter:
    """把 print 输出转到 GUI 日志框。"""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.cfg_path = config_path()
        self.cfg = load_config(self.cfg_path)
        self.logs = queue.Queue()
        self.agent = None
        self.gateway_srv = None
        self.gateway_mod = None
        self.upstream_proc = None
        self.upstream_token = ""
        self.running = False
        self.vars = {}

        self._build_ui()
        self._redirect_stdout()
        self.root.after(120, self._drain_logs)
        self.root.after(1000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("配置文件：%s" % self.cfg_path)
        self._log("填好面板地址、节点 ID 和 Token 后点「启动」。")

    # ───────────────────── 界面 ─────────────────────
    def _build_ui(self):
        self.root.title("%s v%s" % (APP_NAME, APP_VERSION))
        self.root.geometry("780x660")
        self.root.minsize(700, 560)

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # 面板连接
        box1 = ttk.LabelFrame(outer, text=" 面板连接 ", padding=10)
        box1.pack(fill="x")
        box1.columnconfigure(1, weight=1)

        self._row(box1, 0, "服务器地址", "server", "wss://面板域名/api/pool/agent")
        self._row(box1, 1, "节点 ID", "node_id", "面板「网关池」里创建节点后得到，n_...")
        self._row(box1, 2, "节点 Token", "token", "sk-node-...（只显示一次）", show="*")
        self._row(box1, 3, "节点名称", "name", "显示在面板里的名字")
        self._row(box1, 4, "最大并发", "max_inflight", "同时处理的请求数，默认 4")

        # 本地网关
        box2 = ttk.LabelFrame(outer, text=" 本地网关 ", padding=10)
        box2.pack(fill="x", pady=(10, 0))
        box2.columnconfigure(1, weight=1)

        ttk.Label(box2, text="上游方式").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=3)
        up = self.cfg.get("upstream") or "tg2api"
        self.vars["upstream"] = tk.StringVar(value=UPSTREAM_LABELS.get(up, up))
        self.cmb_upstream = ttk.Combobox(box2, textvariable=self.vars["upstream"],
                                         state="readonly",
                                         values=list(UPSTREAM_LABELS.values()))
        self.cmb_upstream.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(box2, text="直连平台：网关地址/Key 由程序自动写入",
                  foreground="#888").grid(row=0, column=2, sticky="w", padx=(8, 0), pady=3)

        self._row(box2, 1, "网关地址", "gateway_url", "http://127.0.0.1:8899")
        self._row(box2, 2, "网关 API Key", "gateway_key",
                  "即 TG_API_KEY；面板转发到本机网关时使用，可留空", show="*")
        self._row(box2, 3, "Prompt 上限", "max_prompt", "字符数，默认 40000")

        opts = ttk.Frame(box2)
        opts.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.vars["auto_gateway"] = tk.BooleanVar(value=bool(self.cfg.get("auto_gateway", True)))
        self.chk_auto = ttk.Checkbutton(opts, text="自动启动本地网关（TeleAgent 客户端需已运行）",
                                        variable=self.vars["auto_gateway"])
        self.chk_auto.pack(side="left")
        self.vars["insecure"] = tk.BooleanVar(value=bool(self.cfg.get("insecure", False)))
        self.chk_insecure = ttk.Checkbutton(opts, text="跳过 wss 证书校验（仅调试）",
                                            variable=self.vars["insecure"])
        self.chk_insecure.pack(side="left", padx=(18, 0))

        # 按钮
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=10)
        self.btn_save = ttk.Button(bar, text="保存配置", command=self._save_clicked)
        self.btn_start = ttk.Button(bar, text="启动", command=self._start)
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop, state="disabled")
        self.btn_test = ttk.Button(bar, text="测试网关", command=self._test_gateway)
        self.btn_clear = ttk.Button(bar, text="清空日志", command=self._clear_log)
        self.btn_save.pack(side="left")
        self.btn_start.pack(side="left", padx=(8, 0))
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_test.pack(side="left", padx=(8, 0))
        self.btn_clear.pack(side="right")

        self.link_gh = tk.Label(bar, text="★ GitHub 项目主页", fg="#2563eb", cursor="hand2")
        self.link_gh.pack(side="left", padx=(16, 0))
        self.link_gh.bind("<Button-1>", lambda e: webbrowser.open(
            "https://github.com/shixiangkuangmo/teleagent"))

        self.status = tk.Label(bar, text="● 未启动", fg="#8a8a8a")
        self.status.pack(side="right", padx=12)

        # 日志
        logbox = ttk.LabelFrame(outer, text=" 运行日志 ", padding=6)
        logbox.pack(fill="both", expand=True)
        self.txt = scrolledtext.ScrolledText(logbox, height=16, state="disabled",
                                             font=("Consolas", 9), wrap="word")
        self.txt.pack(fill="both", expand=True)

    def _row(self, parent, row, label, key, hint, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=3)
        var = tk.StringVar(value=str(self.cfg.get(key, "") or ""))
        ent = ttk.Entry(parent, textvariable=var, show=show or "")
        ent.grid(row=row, column=1, sticky="ew", pady=3)
        self.vars[key] = var
        self.vars["#" + key] = ent
        if hint:
            ttk.Label(parent, text=hint, foreground="#888").grid(
                row=row, column=2, sticky="w", padx=(8, 0), pady=3)
        return ent

    def _redirect_stdout(self):
        w = QueueWriter(self.logs)
        sys.stdout = w
        sys.stderr = w

    # ───────────────────── 配置 ─────────────────────
    def _collect(self):
        def txt(k):
            return self.vars[k].get().strip()

        def num(k, default, lo, hi):
            try:
                v = int(float(txt(k)))
            except Exception:
                v = default
            return max(lo, min(hi, v))

        cfg = {
            "server": txt("server"),
            "node_id": txt("node_id"),
            "token": txt("token"),
            "name": txt("name") or socket.gethostname(),
            "max_inflight": num("max_inflight", 4, 1, 64),
            "gateway_url": txt("gateway_url") or "http://127.0.0.1:8899",
            "gateway_key": txt("gateway_key"),
            "upstream": LABEL_TO_UPSTREAM.get(txt("upstream"), "tg2api"),
            "auto_gateway": bool(self.vars["auto_gateway"].get()),
            "max_prompt": num("max_prompt", 40000, 1000, 5000000),
            "insecure": bool(self.vars["insecure"].get()),
        }
        missing = [n for n, k in (("服务器地址", "server"), ("节点 ID", "node_id"),
                                  ("节点 Token", "token")) if not cfg[k]]
        if missing:
            raise ValueError("请填写：%s" % "、".join(missing))
        if not cfg["server"].startswith(("ws://", "wss://")):
            raise ValueError("服务器地址必须以 ws:// 或 wss:// 开头")
        if cfg["upstream"] != "tg2api" and not cfg["gateway_url"].startswith(("http://", "https://")):
            raise ValueError("网关地址必须以 http:// 或 https:// 开头")
        return cfg

    def _write_config(self, cfg):
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg_path)

    def _save_clicked(self):
        try:
            cfg = self._collect()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        try:
            self._write_config(cfg)
        except OSError as e:
            messagebox.showerror(APP_NAME, "保存配置失败：%s" % e)
            return
        self.cfg = cfg
        self._log("配置已保存：%s" % self.cfg_path)

    # ───────────────────── 启停 ─────────────────────
    def _start(self):
        if self.running:
            return
        try:
            cfg = self._collect()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        try:
            self._write_config(cfg)
        except OSError as e:
            self._log("保存配置失败（不影响本次运行）：%s" % e)
        self.cfg = cfg

        if cfg["upstream"] == "tg2api":
            if not self._start_upstream(cfg):
                return
        elif cfg["auto_gateway"]:
            if not self._start_gateway(cfg):
                return
        self._start_agent(cfg)
        self.running = True
        self._set_controls(False)
        self._set_status("启动中…", "#b58900")
        if cfg["upstream"] == "tg2api":
            threading.Thread(target=self._cred_loop, daemon=True).start()

    def _start_gateway(self, cfg):
        u = urllib.parse.urlsplit(cfg["gateway_url"])
        host = (u.hostname or "127.0.0.1").lower()
        port = u.port or (443 if u.scheme == "https" else 80)
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._log("网关地址不是本机，自动启动改为监听 127.0.0.1:%d" % port)
        if not self._port_4397():
            self._log("提示：本机 4397 未监听，TeleAgent 客户端可能没启动，"
                      "网关暂时无法提取 key（启动客户端后会自动重试）。")

        os.environ["TG_API_KEY"] = cfg["gateway_key"]
        os.environ["TG_MAX_PROMPT"] = str(cfg["max_prompt"])
        os.environ["TG_NODE_NAME"] = cfg["name"]
        try:
            mod = self._load_gateway()
            srv = mod.create_gateway("127.0.0.1", port, prewarm=False)
        except OSError as e:
            messagebox.showerror(
                APP_NAME,
                "启动本地网关失败（端口 %d 可能已被占用）：%s\n\n"
                "如果本机已有网关在运行，请取消勾选「自动启动本地网关」。" % (port, e))
            return False
        except Exception as e:
            messagebox.showerror(APP_NAME, "启动本地网关失败：%s" % e)
            return False
        self.gateway_mod = mod
        self.gateway_srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._log("本地网关已启动：http://127.0.0.1:%d/v1" % port)
        return True

    def _start_upstream(self, cfg):
        """启动 teleagent2api，并用自动提取的账号凭证播种账号池。"""
        exe = os.path.join(app_dir(), "teleagent2api.exe")
        if not os.path.exists(exe):
            messagebox.showerror(APP_NAME, "缺少 teleagent2api.exe，请与节点 exe 放在同一目录。")
            return False
        try:
            cred = teleagent_cred.extract()
        except Exception as e:
            messagebox.showerror(
                APP_NAME, "自动提取 TeleAgent 账号失败：%s\n\n"
                "请确认桌面客户端已登录过（凭证保存在 %%USERPROFILE%%\\.local\\share\\TeleAgent）。" % e)
            return False
        exp = cred.get("exp") or 0
        self._log("已自动提取账号凭证：deviceId=%s token 剩余 %s" % (
            (cred["device_id"] or "未知")[:8],
            ("%.1f 天" % ((exp - time.time()) / 86400)) if exp else "未知"))

        data_dir = os.path.join(os.environ.get("LOCALAPPDATA") or app_dir(),
                                "TeleAgentNode", "tg2api")
        os.makedirs(data_dir, exist_ok=True)
        acct_file = os.path.join(data_dir, "teleagent-accounts.json")
        state_file = os.path.join(data_dir, "teleagent-token-state.json")
        for p in (acct_file, state_file):
            try:
                # 每次启动都重新播种，保证用的是最新 token；
                # state 文件优先级高于环境变量，留着会盖掉新 token
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

        api_key = cfg["gateway_key"] or "sk-node-local"
        env = dict(os.environ)
        env.update({
            "TELEAGENT_TOKEN": cred["token"],
            "TELEAGENT_DEVICE_ID": cred["device_id"],
            "TELEAGENT_INSTALL_ID": cred["install_id"],
            "TELEAGENT2API_LISTEN": "127.0.0.1:10000",
            "TELEAGENT_TIMEOUT": "1800s",
            "TELEAGENT_REASONING_MODE": "reasoning_content",
            "TELEAGENT_ACCOUNTS_FILE": acct_file,
            "TELEAGENT_KEYS_FILE": os.path.join(data_dir, "teleagent-keys.json"),
            "TELEAGENT_STATE_FILE": state_file,
            "ADMIN_PASSWORD": os.urandom(9).hex(),
            "API_KEY": api_key,
            "TELEAGENT_LOG_LEVEL": "info",
            "TELEAGENT_LOG_FORMAT": "text",
        })
        flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
        try:
            self.upstream_proc = subprocess.Popen(
                [exe], cwd=app_dir(), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
        except OSError as e:
            messagebox.showerror(APP_NAME, "启动 teleagent2api 失败：%s" % e)
            return False

        ok = False
        for _ in range(60):
            if self.upstream_proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:10000/health", timeout=1) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.2)
        if not ok:
            code = self.upstream_proc.poll()
            self._stop_upstream()
            messagebox.showerror(
                APP_NAME, "teleagent2api 启动失败（退出码 %s）。\n"
                "常见原因：10000 端口被占用，或 token 已失效。" % code)
            return False

        self.upstream_token = cred["token"]
        self._log("teleagent2api 已就绪：http://127.0.0.1:10000/v1（账号已自动配置）")
        cfg["gateway_url"] = "http://127.0.0.1:10000"
        cfg["gateway_key"] = api_key
        return True

    def _stop_upstream(self):
        proc, self.upstream_proc = self.upstream_proc, None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._log("teleagent2api 已停止")

    def _cred_loop(self):
        """每 30 分钟重读一次凭证，token 变了就重启上游（JWT 会过期）。"""
        while self.running:
            time.sleep(1800)
            if not self.running:
                return
            try:
                cred = teleagent_cred.extract()
            except Exception:
                continue
            if cred["token"] and cred["token"] != self.upstream_token:
                self._log("检测到账号 token 已更新，重启 teleagent2api…")
                self._stop_upstream()
                cfg = dict(self.cfg)
                self._start_upstream(cfg)

    def _start_agent(self, cfg):
        self.agent = pool_agent.Agent({
            "server": cfg["server"],
            "node_id": cfg["node_id"],
            "token": cfg["token"],
            "gateway_url": cfg["gateway_url"],
            "gateway_key": cfg["gateway_key"],
            "name": cfg["name"],
            "max_inflight": cfg["max_inflight"],
            "insecure": cfg["insecure"],
        })
        threading.Thread(target=self._run_agent, args=(self.agent,), daemon=True).start()

    def _run_agent(self, agent):
        try:
            agent.run()
        except Exception:
            self._log(traceback.format_exc())
        self._log("节点代理已停止")

    def _load_gateway(self):
        """重新加载网关模块，让最新填写的 API Key / prompt 上限立即生效。"""
        sys.modules.pop("teleagent_gateway", None)
        return importlib.import_module("teleagent_gateway")

    def _stop(self):
        if not self.running:
            return
        self._log("正在停止…")
        if self.agent:
            try:
                self.agent.stop()
            except Exception:
                pass
            self.agent = None
        if self.gateway_srv:
            srv, mod = self.gateway_srv, self.gateway_mod
            self.gateway_srv = None
            self.gateway_mod = None
            threading.Thread(target=mod.stop_gateway, args=(srv,), daemon=True).start()
            self._log("本地网关已停止")
        self._stop_upstream()
        self.running = False
        self._set_controls(True)
        self._set_status("已停止", "#8a8a8a")

    def _on_close(self):
        if self.running and not messagebox.askyesno(APP_NAME, "节点正在运行，确定退出？"):
            return
        self._stop()
        self.root.destroy()

    # ───────────────────── 状态 / 日志 ─────────────────────
    def _tick(self):
        if self.running:
            connected = bool(self.agent and getattr(self.agent, "ws", None))
            if connected:
                self._set_status("● 节点已连接面板", "#1f9d55")
            else:
                self._set_status("● 连接中（自动重试）…", "#b58900")
        self.root.after(1000, self._tick)

    def _set_status(self, text, color):
        self.status.configure(text=text, fg=color)

    def _set_controls(self, enabled):
        state = "normal" if enabled else "disabled"
        for k in TEXT_ROWS:
            self.vars["#" + k].configure(state=state)
        self.chk_auto.configure(state=state)
        self.chk_insecure.configure(state=state)
        self.cmb_upstream.configure(state="readonly" if enabled else "disabled")
        self.btn_save.configure(state=state)
        self.btn_start.configure(state=state)
        self.btn_stop.configure(state="disabled" if enabled else "normal")

    def _log(self, msg):
        self.logs.put(str(msg).rstrip("\r\n") + "\n")

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _drain_logs(self):
        buf = []
        try:
            for _ in range(300):
                buf.append(self.logs.get_nowait())
        except queue.Empty:
            pass
        if buf:
            self.txt.configure(state="normal")
            self.txt.insert("end", "".join(buf))
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > 3000:
                self.txt.delete("1.0", "%d.0" % (lines - 2000))
            self.txt.see("end")
            self.txt.configure(state="disabled")
        self.root.after(120, self._drain_logs)

    # ───────────────────── 测试 ─────────────────────
    def _test_gateway(self):
        try:
            cfg = self._collect()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return

        def work():
            url = cfg["gateway_url"].rstrip("/") + "/pool/status"
            headers = {}
            if cfg["gateway_key"]:
                headers["Authorization"] = "Bearer " + cfg["gateway_key"]
            req = urllib.request.Request(url, headers=headers)
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    body = r.read(300).decode("utf-8", "replace")
                self._log("测试网关 %s → HTTP %s（%.0f ms）" % (url, r.status, (time.time() - t0) * 1000))
                self._log("返回：%s" % body)
            except urllib.error.HTTPError as e:
                self._log("测试网关 %s → HTTP %s（检查网关 API Key 是否一致）" % (url, e.code))
            except Exception as e:
                self._log("测试网关失败：%s" % e)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _port_4397():
        try:
            s = socket.create_connection(("127.0.0.1", 4397), timeout=0.5)
            s.close()
            return True
        except OSError:
            return False


def main():
    # PyInstaller --noconsole 下标准流可能是 None，先兜底，避免 print 崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    root = tk.Tk()
    try:
        root.iconname(APP_NAME)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
