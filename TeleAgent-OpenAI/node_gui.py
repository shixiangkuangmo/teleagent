# -*- coding: utf-8 -*-
"""
node_gui.py — TeleAgent 节点图形界面
====================================
一个窗口完成配置：填写面板地址与节点凭据，点「启动」后同时运行
本机上游网关与池代理（pool_agent）。

上游两种方式：
  1. teleagent2api（推荐）——自动拉起同目录的 teleagent2api.exe，Go 单跳直连
     TeleAgent 平台 API，不依赖 4397 客户端，流式输出不会中途断流；
  2. 内置网关 —— 运行 teleagent_gateway，需要本机 TeleAgent 客户端（4397）。

可打包成单个 exe（见 build-exe.bat），目标机器无需安装 Python。

配置保存在 exe 同目录的 pool-agent.json；该目录不可写时回退到
%APPDATA%\\TeleAgentNode\\pool-agent.json。
"""
import importlib
import json
import os
import queue
import secrets
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

APP_NAME = "TeleAgent 节点"
APP_VERSION = "1.1.0"
CONFIG_NAME = "pool-agent.json"
TEXT_ROWS = ("server", "node_id", "token", "name", "max_inflight",
             "gateway_url", "gateway_key", "max_prompt",
             "t2a_port", "t2a_admin_password")

# 上游两种形态：teleagent2api（Go 单跳直连平台，推荐）与内置网关（依赖 4397 客户端）
MODE_T2A_LABEL = "teleagent2api（直连平台，推荐）"
MODE_GW_LABEL = "内置网关（TeleAgent 客户端 4397）"
MODE_LABELS = {"teleagent2api": MODE_T2A_LABEL, "gateway": MODE_GW_LABEL}


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
        "upstream_mode": "teleagent2api",
        "gateway_url": "http://127.0.0.1:10000",
        "gateway_key": "",
        "auto_gateway": True,
        "max_prompt": 40000,
        "t2a_port": 10000,
        "t2a_admin_password": "",
        "t2a_exe": "",
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
        self.t2a_proc = None
        self.running = False
        self.vars = {}

        self._build_ui()
        self._redirect_stdout()
        self.root.after(120, self._drain_logs)
        self.root.after(1000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("配置文件：%s" % self.cfg_path)
        self._log("填好面板地址、节点 ID 和 Token 后点「启动」。")
        self._log("默认上游 teleagent2api：请把 teleagent2api.exe 放在本程序同一目录。")

    # ───────────────────── 界面 ─────────────────────
    def _build_ui(self):
        self.root.title("%s v%s" % (APP_NAME, APP_VERSION))
        self.root.geometry("800x720")
        self.root.minsize(720, 620)

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

        # 上游网关
        box2 = ttk.LabelFrame(outer, text=" 上游网关 ", padding=10)
        box2.pack(fill="x", pady=(10, 0))
        box2.columnconfigure(1, weight=1)

        ttk.Label(box2, text="上游方式").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=3)
        self.var_mode = tk.StringVar(
            value=MODE_LABELS.get(str(self.cfg.get("upstream_mode") or "teleagent2api"),
                                  MODE_T2A_LABEL))
        self.cmb_mode = ttk.Combobox(box2, textvariable=self.var_mode, state="readonly",
                                     values=(MODE_T2A_LABEL, MODE_GW_LABEL))
        self.cmb_mode.grid(row=0, column=1, sticky="ew", pady=3)
        self.cmb_mode.bind("<<ComboboxSelected>>", self._on_mode_change)
        ttk.Label(box2, text="直连平台更稳；内置网关需要 TeleAgent 客户端在运行",
                  foreground="#888").grid(row=0, column=2, sticky="w", padx=(8, 0), pady=3)

        self._row(box2, 1, "网关地址", "gateway_url", "teleagent2api 默认 http://127.0.0.1:10000")
        self._row(box2, 2, "网关 API Key", "gateway_key",
                  "节点→本机网关的密钥；留空自动生成", show="*")
        self._row(box2, 3, "网关端口", "t2a_port", "teleagent2api 监听端口，默认 10000")
        self._row(box2, 4, "控制台密码", "t2a_admin_password",
                  "登录 http://127.0.0.1:10000/ui 添加账号用，建议设置", show="*")
        self._row(box2, 5, "Prompt 上限", "max_prompt", "字符数，默认 40000（仅内置网关生效）")

        opts = ttk.Frame(box2)
        opts.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.vars["auto_gateway"] = tk.BooleanVar(value=bool(self.cfg.get("auto_gateway", True)))
        self.chk_auto = ttk.Checkbutton(opts, text="自动启动本地网关（仅内置网关方式）",
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
        self.btn_console = ttk.Button(bar, text="打开上游控制台", command=self._open_console)
        self.btn_clear = ttk.Button(bar, text="清空日志", command=self._clear_log)
        self.btn_save.pack(side="left")
        self.btn_start.pack(side="left", padx=(8, 0))
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_test.pack(side="left", padx=(8, 0))
        self.btn_console.pack(side="left", padx=(8, 0))
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
    def _mode(self):
        return "gateway" if self.var_mode.get() == MODE_GW_LABEL else "teleagent2api"

    def _on_mode_change(self, _evt=None):
        port = self._int(self.vars["t2a_port"].get(), 10000)
        cur = self.vars["gateway_url"].get().strip()
        if self._mode() == "teleagent2api":
            if not cur or "8899" in cur or ":10000" in cur:
                self.vars["gateway_url"].set("http://127.0.0.1:%d" % port)
            self._log("上游方式：teleagent2api（启动时自动拉起 teleagent2api.exe）")
        else:
            if not cur or ":10000" in cur:
                self.vars["gateway_url"].set("http://127.0.0.1:8899")
            self._log("上游方式：内置网关（需要 TeleAgent 客户端在运行）")

    @staticmethod
    def _int(value, default):
        try:
            return int(float(str(value).strip()))
        except Exception:
            return default

    def _collect(self):
        def txt(k):
            return self.vars[k].get().strip()

        def num(k, default, lo, hi):
            try:
                v = int(float(txt(k)))
            except Exception:
                v = default
            return max(lo, min(hi, v))

        mode = self._mode()
        port = num("t2a_port", 10000, 1, 65535)
        gw_url = txt("gateway_url")
        if mode == "teleagent2api" and (not gw_url or gw_url == "http://127.0.0.1:8899"):
            gw_url = "http://127.0.0.1:%d" % port
        cfg = {
            "server": txt("server"),
            "node_id": txt("node_id"),
            "token": txt("token"),
            "name": txt("name") or socket.gethostname(),
            "max_inflight": num("max_inflight", 4, 1, 64),
            "upstream_mode": mode,
            "gateway_url": gw_url or "http://127.0.0.1:8899",
            "gateway_key": txt("gateway_key"),
            "auto_gateway": bool(self.vars["auto_gateway"].get()),
            "max_prompt": num("max_prompt", 40000, 1000, 5000000),
            "t2a_port": port,
            "t2a_admin_password": txt("t2a_admin_password"),
            "t2a_exe": self.cfg.get("t2a_exe", ""),
            "insecure": bool(self.vars["insecure"].get()),
        }
        missing = [n for n, k in (("服务器地址", "server"), ("节点 ID", "node_id"),
                                  ("节点 Token", "token")) if not cfg[k]]
        if missing:
            raise ValueError("请填写：%s" % "、".join(missing))
        if not cfg["server"].startswith(("ws://", "wss://")):
            raise ValueError("服务器地址必须以 ws:// 或 wss:// 开头")
        if not cfg["gateway_url"].startswith(("http://", "https://")):
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
        self.cfg = cfg
        if cfg["upstream_mode"] == "teleagent2api":
            if not self._start_t2a(cfg):
                return
        elif cfg["auto_gateway"]:
            if not self._start_gateway(cfg):
                return
        try:
            self._write_config(cfg)
        except OSError as e:
            self._log("保存配置失败（不影响本次运行）：%s" % e)
        self._start_agent(cfg)
        self.running = True
        self._set_controls(False)
        self._set_status("启动中…", "#b58900")

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

    # ───────────────── teleagent2api 子进程 ─────────────────
    def _start_t2a(self, cfg):
        """拉起同目录的 teleagent2api.exe，并等它健康检查通过。"""
        exe = cfg.get("t2a_exe") or os.path.join(app_dir(), "teleagent2api.exe")
        if not os.path.isfile(exe):
            messagebox.showerror(
                APP_NAME,
                "找不到 teleagent2api.exe：\n%s\n\n"
                "请把它和本程序放在同一目录；或改用「内置网关」上游方式。" % exe)
            return False
        port = cfg["t2a_port"]
        if self._port_open(port):
            self._log("端口 %d 已有服务在监听，直接复用（不再重复启动）。" % port)
            return True

        key = cfg.get("gateway_key") or ""
        if not key:
            key = "sk-t2a-" + secrets.token_hex(12)
            self.vars["gateway_key"].set(key)
            cfg["gateway_key"] = key
            self._log("已自动生成网关 API Key（面板外无需配置）")

        data_dir = os.path.dirname(self.cfg_path) or app_dir()
        conf_path = os.path.join(data_dir, "teleagent2api-config.json")
        conf = {
            "listen": "127.0.0.1:%d" % port,
            "apiKey": key,
            "timeout": "1800s",
            "reasoningMode": "reasoning_content",
            "logLevel": "info",
            "logFormat": "text",
            "retryCount": 1,
        }
        try:
            with open(conf_path, "w", encoding="utf-8") as f:
                json.dump(conf, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror(APP_NAME, "写 teleagent2api 配置失败：%s" % e)
            return False

        env = dict(os.environ)
        env["TELEAGENT_CONFIG"] = conf_path
        admin = cfg.get("t2a_admin_password") or ""
        if not admin:
            admin = secrets.token_hex(6)
            self.vars["t2a_admin_password"].set(admin)
            cfg["t2a_admin_password"] = admin
            self._log("已自动生成控制台密码：%s（登录 /ui 添加账号用）" % admin)
        env["ADMIN_PASSWORD"] = admin

        log_path = os.path.join(data_dir, "teleagent2api.log")
        try:
            logf = open(log_path, "ab", buffering=0)
        except OSError:
            logf = subprocess.DEVNULL
        creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            self.t2a_proc = subprocess.Popen(
                [exe], cwd=data_dir, env=env, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT, creationflags=creation)
        except OSError as e:
            messagebox.showerror(APP_NAME, "启动 teleagent2api 失败：%s" % e)
            return False
        self._log("teleagent2api 已启动（PID %d，端口 %d）"
                  % (self.t2a_proc.pid, port))

        if not self._wait_health(port, 25):
            self._log("teleagent2api 健康检查失败，日志末尾（%s）：" % log_path)
            for line in self._tail(log_path, 20):
                self._log("  " + line)
            self._stop_t2a()
            messagebox.showerror(APP_NAME,
                                 "teleagent2api 没能正常启动，详情见运行日志。")
            return False
        self._log("teleagent2api 就绪：http://127.0.0.1:%d/ui（首次请在此添加账号）" % port)
        return True

    def _stop_t2a(self):
        p = self.t2a_proc
        self.t2a_proc = None
        if not p:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        except Exception:
            pass
        self._log("teleagent2api 已停止")

    def _wait_health(self, port, seconds):
        url = "http://127.0.0.1:%d/health" % port
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.t2a_proc and self.t2a_proc.poll() is not None:
                self._log("teleagent2api 进程已退出（返回码 %s）" % self.t2a_proc.returncode)
                return False
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    @staticmethod
    def _tail(path, n):
        try:
            with open(path, "rb") as f:
                lines = f.read().decode("utf-8", "replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

    @staticmethod
    def _port_open(port):
        try:
            s = socket.create_connection(("127.0.0.1", int(port)), timeout=0.5)
            s.close()
            return True
        except OSError:
            return False

    def _open_console(self):
        if self._mode() == "teleagent2api":
            port = self._int(self.vars["t2a_port"].get(), 10000)
            url = "http://127.0.0.1:%d/ui" % port
        else:
            url = self.vars["gateway_url"].get().strip().rstrip("/") or "http://127.0.0.1:8899"
        webbrowser.open(url)
        self._log("已在浏览器打开 %s" % url)

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
        self._stop_t2a()
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
        self.cmb_mode.configure(state="readonly" if enabled else "disabled")
        self.chk_auto.configure(state=state)
        self.chk_insecure.configure(state=state)
        self.btn_save.configure(state=state)
        self.btn_start.configure(state=state)
        self.btn_console.configure(state=state)
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
            base = cfg["gateway_url"].rstrip("/")
            headers = {}
            if cfg["gateway_key"]:
                headers["Authorization"] = "Bearer " + cfg["gateway_key"]
            # teleagent2api 没有 /pool/status，直连网关才有；标准网关用 /v1/models
            paths = (("/v1/models",) if cfg["upstream_mode"] == "teleagent2api"
                     else ("/pool/status", "/v1/models"))
            for path in paths:
                url = base + path
                req = urllib.request.Request(url, headers=headers)
                t0 = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        body = r.read(300).decode("utf-8", "replace")
                    self._log("测试网关 %s → HTTP %s（%.0f ms）"
                              % (url, r.status, (time.time() - t0) * 1000))
                    self._log("返回：%s" % body)
                    return
                except urllib.error.HTTPError as e:
                    if e.code in (301, 302, 404) and path != paths[-1]:
                        continue
                    self._log("测试网关 %s → HTTP %s（检查网关 API Key 是否一致）" % (url, e.code))
                    return
                except Exception as e:
                    self._log("测试网关失败：%s" % e)
                    return

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
