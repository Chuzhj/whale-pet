# -*- coding: utf-8 -*-
"""dsh_acp_client —— 让外部 Python 程序（鲸鲸桌宠）通过官方 ACP 通道驱动 DeepSeek Harness。

为什么是这条路
--------------
DSH 的 HTTP API 用"签名 cookie + 拒绝 Authorization 头"把任意本地进程挡在外面（防 DNS
rebinding / 跨站）。但它同时给自动化留了一扇正门：`dsh --profile acp`，即
Agent Client Protocol（ACP v1）—— 官方文档明说适用于 "scripted controllers"，且
`authenticate` **无需鉴权**。本模块就是那扇门的 Python 客户端。

线格式
------
一行一帧的 JSON-RPC 2.0（DSH 文档原文："Stdout is reserved for newline-delimited ACP
JSON-RPC frames"）。子进程的 stdout 只允许出现协议帧，所以它的 stderr 单独收走，
绝不混进来。也因此：**调用方不要把 print 指到这个子进程的 stdout**。

进程启动
--------
不走 `dsh.cmd`（.cmd 要经 cmd.exe，Windows 上从 Python 直接 spawn 有引号转义的坑），
而是照抄 dsh.cmd 的内容直接调 Electron：

    set ELECTRON_RUN_AS_NODE=1
    DeepSeek Harness.exe --expose-internals <app.asar>/dsh/.../cli.js --profile acp

Electron 以 Node 模式运行时自带 asar 支持，所以它能读到 app.asar 里的 cli.js。

线程模型
--------
一个读线程常驻解析 stdout，把帧分成三类分派：
  1. 有 id 无 method  → 我们发出去的请求的响应 → 唤醒等它的 Future
  2. 有 method 无 id  → 通知（session/update 等）→ 交给 on_update 回调
  3. 有 method 有 id  → **服务端反向请求**（如 session/request_permission）→ 必须回复，
     否则 agent 会一直等。默认策略是"拒绝"（最安全），可传 on_permission 覆盖。

调用方拿到的是同步阻塞的 request()，内部用 concurrent.futures.Future 排队。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- 默认路径
# DSH 安装目录：优先读环境变量 DSH_APP_DIR，否则按当前用户的标准安装位置推断。
# 以前这里写死了某一台开发机的绝对路径，换台机器就找不到。
DSH_APP_DIR = Path(os.environ.get("DSH_APP_DIR")
                   or Path.home() / "AppData" / "Local" / "Programs" / "DeepSeek Harness")
DSH_EXE = DSH_APP_DIR / "DeepSeek Harness.exe"
DSH_CLI_JS = (DSH_APP_DIR / "resources" / "app.asar" / "dsh" / "node_modules"
              / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js")
DSH_CMD = (DSH_APP_DIR / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd")

PROTOCOL_VERSION = 1          # ACP v1

# JSON-RPC 标准错误码（回复我们不认识的服务端反向请求时用）
RPC_METHOD_NOT_FOUND = -32601


class AcpError(Exception):
    """服务端返回的 JSON-RPC error，或本客户端自身的协议错误。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__("ACP %s: %s" % (code, message))
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------- 内容块
def text_block(text: str) -> dict:
    """ACP 的 ContentBlock（text 形态）。session/prompt 的 prompt 是一个块列表。"""
    return {"type": "text", "text": text}


def update_kind(update: dict) -> str:
    """从 session/update 的 update 里取 sessionUpdate 种类，缺省给个可读的。"""
    if not isinstance(update, dict):
        return "unknown"
    return update.get("sessionUpdate") or update.get("kind") or "unknown"


def find_option_value(session_info: dict, config_id: str, group: str = "") -> Optional[str]:
    """在 session/new / set_config_option 的返回里挑一个"可原样传回"的 value。

    想用主人登录的 DeepSeek 账号（不另配 API key）就选 group="deepseek-account"；
    那条路由走的是桌面应用同一套账号凭证。返回的是不透明字符串，直接喂给
    set_config_option 即可。
    """
    for opt in session_info.get("configOptions") or []:
        if opt.get("id") != config_id:
            continue
        for g in opt.get("options") or []:
            if group and (g.get("group") or "") != group:
                continue
            for o in g.get("options") or []:
                if o.get("value") is not None:
                    return o["value"]
    return None


# ---------------------------------------------------------------- 客户端
class AcpClient:
    """一个 ACP 连接 = 一个 `dsh --profile acp` 子进程，可同时挂多个 session。"""

    def __init__(self,
                 on_update: Optional[Callable[[str, dict], None]] = None,
                 on_permission: Optional[Callable[[dict], dict]] = None,
                 on_server_request: Optional[Callable[[str, dict], Any]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 profile: str = "acp",
                 exe: Path = DSH_EXE,
                 cli_js: Path = DSH_CLI_JS,
                 startup_timeout: float = 90.0):
        self._on_update = on_update
        self._on_permission = on_permission
        self._on_server_request = on_server_request
        self._on_log = on_log or (lambda s: None)
        self._profile = profile
        self._exe = Path(exe)
        self._cli_js = Path(cli_js)
        self._startup_timeout = startup_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._closed = False

    # ---------------------------------------------------------- 日志
    def log(self, msg: str) -> None:
        self._on_log("[acp] " + msg)

    # ---------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("ACP 客户端已经启动")
        if not self._exe.exists():
            raise FileNotFoundError("找不到 DeepSeek Harness.exe：%s" % self._exe)

        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"        # Electron 当 Node 用，这是 dsh.cmd 的原话
        cmd = [str(self._exe), "--expose-internals", str(self._cli_js),
               "--profile", self._profile]
        self.log("启动: %s" % " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._read_loop, daemon=True, name="acp-read").start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="acp-stderr")
        self._stderr_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """关 stdin（协议约定：EOF = 有界、成功的关停），等它自己退。"""
        if self._proc is None or self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=timeout)
            self.log("子进程已退出，returncode=%s" % self._proc.returncode)
        except subprocess.TimeoutExpired:
            self.log("子进程 %ss 内未退出，改用 terminate" % timeout)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(AcpError(-1, "ACP 客户端已关闭"))
            self._pending.clear()

    # ---------------------------------------------------------- 底层收发
    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in iter(self._proc.stderr.readline, ""):
            line = raw.rstrip()
            if line:
                self.log("stderr: " + line)

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in iter(self._proc.stdout.readline, ""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError as err:
                self.log("非 JSON 帧，丢弃：%r (%s)" % (line[:200], err))
                continue
            try:
                self._dispatch(msg)
            except Exception as err:                  # 回调抛错不能带垮读线程
                self.log("分派帧时出错：%r" % (err,))
        self.log("stdout 已关闭")
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AcpError(-1, "ACP 子进程关闭了 stdout"))
        self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        has_id = "id" in msg
        if method is None and has_id:                 # 我们请求的响应
            fut = self._pending.pop(msg["id"], None)
            if fut is None:
                self.log("收到无主响应 id=%r，丢弃" % (msg.get("id"),))
                return
            if "error" in msg:
                e = msg["error"] or {}
                fut.set_exception(AcpError(e.get("code", -1),
                                           e.get("message", "unknown"),
                                           e.get("data")))
            else:
                fut.set_result(msg.get("result"))
            return

        if method is None:
            self.log("无法理解的帧：%s" % json.dumps(msg)[:200])
            return

        if has_id:                                    # 服务端反向请求，必须回
            self._handle_server_request(msg)
            return

        # 通知
        if method == "session/update":
            params = msg.get("params") or {}
            sid = params.get("sessionId", "")
            update = params.get("update") or {}
            if self._on_update:
                self._on_update(sid, update)
            return
        self.log("忽略通知 method=%s" % method)

    def _handle_server_request(self, msg: dict) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]
        self.log("服务端请求 %s (id=%s)" % (method, rid))

        if method == "session/request_permission":
            result = self._default_permission(params)
        elif self._on_server_request:
            try:
                result = self._on_server_request(method, params)
            except Exception as err:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": RPC_METHOD_NOT_FOUND, "message": str(err)}})
                return
        else:
            # 不认识又不接：回标准错误，让 agent 立刻失败而不是干等
            self._send({"jsonrpc": "2.0", "id": rid, "error":
                        {"code": RPC_METHOD_NOT_FOUND,
                         "message": "client does not implement %s" % method}})
            return

        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _default_permission(self, params: dict) -> dict:
        """默认权限策略：挑第一个 reject_* 选项（最安全）；没有就取消这次请求。

        ACP 的 PermissionOption.kind 有 allow_once / allow_always / reject_once /
        reject_always。桌宠不该替主人放行危险操作，所以默认拒绝；
        要放行请传 on_permission 自己决定。
        """
        if self._on_permission:
            return self._on_permission(params)
        options = params.get("options") or []
        for opt in options:
            if str(opt.get("kind", "")).startswith("reject"):
                return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}

    def _send(self, msg: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("ACP 子进程未启动")
        frame = json.dumps(msg, ensure_ascii=False)
        self._proc.stdin.write(frame + "\n")
        self._proc.stdin.flush()

    def _next_request_id(self) -> int:
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def request(self, method: str, params: dict, timeout: float = 120.0) -> Any:
        """发一个请求并阻塞等响应。超时抛 TimeoutError，不取消远端（ACP 无此语义）。"""
        if self._proc is None:
            raise RuntimeError("先调用 start()")
        rid = self._next_request_id()
        fut: Future = Future()
        with self._pending_lock:
            self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            # 超时的请求从登记表里摘掉，避免以后同一 id 的迟到响应落进新请求
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---------------------------------------------------------- ACP 方法
    def initialize(self, timeout: float | None = None) -> dict:
        """握手。必须是第一个调用。返回 protocolVersion / agentCapabilities / authMethods。"""
        return self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                # 我们不提供文件系统回调，也不接终端 —— 都声明成没有，
                # agent 就不会反过来找我们要这些能力。
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        }, timeout=timeout or self._startup_timeout)

    def authenticate(self, method_id: str = "") -> Any:
        """DSH 的 ACP 无需鉴权（initialize 返回的 authMethods 是空列表）。

        实测差异：标准 ACP 的 authenticate 参数叫 `method`，DSH 的 zod 校验要的是
        `methodId` —— 传 `method` 会报 Invalid params。authMethods 为空时传空串即可。
        """
        return self.request("authenticate", {"methodId": method_id}, timeout=30)

    def new_session(self, cwd: str, mcp_servers: Optional[list] = None) -> dict:
        """开一个新会话。cwd 必须是绝对路径，会作为这个 agent 的工作目录。"""
        return self.request("session/new", {
            "cwd": str(Path(cwd).resolve()),
            "mcpServers": mcp_servers or [],
        }, timeout=180)

    def list_sessions(self, cursor: str = "", cwd: str = "") -> dict:
        """DSH 扩展：跨重启列持久会话（newest-first 分页）。"""
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        if cwd:
            params["cwd"] = str(Path(cwd).resolve())
        return self.request("session/list", params, timeout=60)

    def resume_session(self, session_id: str, cwd: str,
                       mcp_servers: Optional[list] = None) -> dict:
        """接续一个持久会话。不会重放历史（文档原话）。"""
        return self.request("session/resume", {
            "sessionId": session_id,
            "cwd": str(Path(cwd).resolve()),
            "mcpServers": mcp_servers or [],
        }, timeout=180)

    def set_config_option(self, session_id: str, config_id: str, value: str) -> dict:
        """改会话的 model / reasoning_effort，返回**完整的**最终配置状态。

        坑（实测）：select 的 value 在线上是**不透明字符串** —— DSH 把路由数组
        JSON 编码成串再发（currentValue 就是这种串）。所以要么原样回传
        options 里给的 value，要么用 find_option_value() 取，别自己拼数组，
        否则 zod 校验直接 Invalid params。
        """
        return self.request("session/set_config_option", {
            "sessionId": session_id, "configId": config_id, "value": value,
        }, timeout=60)

    def prompt(self, session_id: str, text: str,
               timeout: float = 600.0) -> dict:
        """发一段文本，阻塞到这一轮结束，返回 {"stopReason": ...}。

        注意：每个会话同时只允许一个在途 prompt（文档原话），
        并发发第二个会被拒或排队失败。
        """
        return self.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [text_block(text)],
        }, timeout=timeout)

    def cancel(self, session_id: str) -> None:
        """取消当前在途的 prompt / 自主工作。这是通知，没有响应。"""
        self.notify("session/cancel", {"sessionId": session_id})

    def close_session(self, session_id: str, timeout: float = 60.0) -> Any:
        """关会话：静默取消 → 排空更新 → 持久化落盘 → 释放。持久状态仍可 resume。"""
        return self.request("session/close", {"sessionId": session_id}, timeout=timeout)


# ---------------------------------------------------------------- 便捷封装
def default_client(on_update=None, on_permission=None, on_log=None) -> AcpClient:
    return AcpClient(on_update=on_update, on_permission=on_permission,
                     on_log=on_log)


# ---------------------------------------------------------------- 自检
def _selftest() -> int:
    """离线自检：不起子进程，只验本模块自己的纯逻辑。"""
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + name)
        ok = ok and bool(cond)

    print("[自检] 内容块")
    b = text_block("hi")
    check("text_block 形状", b == {"type": "text", "text": "hi"})

    print("[自检] JSON-RPC 帧编解码")
    msg = {"jsonrpc": "2.0", "id": 7, "method": "session/prompt",
           "params": {"sessionId": "s", "prompt": [text_block("你好\n世界")]}}
    frame = json.dumps(msg, ensure_ascii=False)
    check("帧是单行（含 \\n 的文本不会破帧）", "\n" not in frame)
    check("往返后 params 一致", json.loads(frame)["params"]["prompt"][0]["text"] == "你好\n世界")

    print("[自检] 权限默认策略")
    c = AcpClient()
    rej = c._default_permission({"options": [
        {"optionId": "a", "kind": "allow_once"},
        {"optionId": "b", "kind": "reject_once"},
    ]})
    check("默认选 reject_*", rej == {"outcome": {"outcome": "selected", "optionId": "b"}})
    can = c._default_permission({"options": [{"optionId": "a", "kind": "allow_once"}]})
    check("没有 reject 就取消", can == {"outcome": {"outcome": "cancelled"}})

    print("[自检] update 种类解析")
    check("agent_message_chunk", update_kind({"sessionUpdate": "agent_message_chunk"})
          == "agent_message_chunk")
    check("缺字段给 unknown", update_kind({}) == "unknown")

    print("[自检] 可执行文件存在")
    check("DeepSeek Harness.exe", DSH_EXE.exists())
    # cli.js 在 app.asar 里，os.path.exists 对 asar 内部路径**恒为 False**，
    # 所以这里不能断言存在。以前写成 `DSH_CLI_JS.exists() or True` ——
    # 那是个永远通过的断言，等于没有（自检会给人"查过了"的错觉）。
    # 现在改成如实打印路径：对不上时人一眼能看出来。
    print("      cli.js（asar 内，exists 恒为 False 属正常）：%s" % DSH_CLI_JS)

    print("[自检] %s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 命令行
def _fmt_sessions(res: dict) -> str:
    rows = res.get("sessions") or []
    if not rows:
        return "（没有持久会话）"
    lines = []
    for i, s in enumerate(rows, 1):
        sid = s.get("sessionId") or s.get("id") or "?"
        title = s.get("title") or s.get("displayName") or ""
        cwd = s.get("cwd") or ""
        when = s.get("updatedAt") or s.get("timestamp") or ""
        lines.append("%2d. %s  %s" % (i, sid, title))
        extra = "    " + " / ".join(x for x in (cwd, when) if x)
        if extra.strip():
            lines.append(extra)
    if res.get("nextCursor"):
        lines.append("… 还有更多（nextCursor=%s）" % res["nextCursor"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="鲸鲸桌宠的 DSH ACP 客户端")
    ap.add_argument("--selftest", action="store_true", help="离线自检，不启动子进程")
    ap.add_argument("--list", action="store_true", help="列出持久会话")
    ap.add_argument("--ask", metavar="文本", help="完整链路：握手→开会话→发 prompt→收更新→关闭")
    ap.add_argument("--cwd", default=os.getcwd(), help="会话工作目录（默认当前目录）")
    ap.add_argument("--group", default="deepseek-account",
                    help="优先选哪组模型路由（默认 deepseek-account＝主人登录的账号，不用另配 key）")
    ap.add_argument("--verbose", action="store_true", help="打印协议日志")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    updates: list[dict] = []

    def on_update(sid: str, update: dict) -> None:
        kind = update_kind(update)
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content") or {}
            chunk = content.get("text", "") if isinstance(content, dict) else ""
            sys.stdout.write(chunk)
            sys.stdout.flush()
            return
        updates.append(update)
        print("  … %s" % kind)

    def on_log(msg: str) -> None:
        if args.verbose:
            print(msg, file=sys.stderr)

    cli = AcpClient(on_update=on_update, on_log=on_log)
    try:
        cli.start()
        info = cli.initialize()
        print("协议版本: %s" % info.get("protocolVersion"))
        caps = info.get("agentCapabilities") or {}
        print("agent 能力: loadSession=%s  promptCapabilities=%s"
              % (caps.get("loadSession"), bool(caps.get("promptCapabilities"))))
        cli.authenticate()

        if args.list:
            print(_fmt_sessions(cli.list_sessions()))
            return 0

        # 默认（无参数）：跑一条最小链路，证明端到端通
        cwd = str(Path(args.cwd).resolve())
        print("工作目录: %s" % cwd)
        sess = cli.new_session(cwd=cwd)
        sid = sess.get("sessionId")
        print("会话已开: %s" % sid)

        # 默认路由 deepseek-official 需要 API key；主人登录的账号走 deepseek-account，
        # 沿用桌面应用同一套凭证，所以这里优先切过去（切不动就照原样跑，把错误交给上层）。
        value = find_option_value(sess, "model", args.group)
        if value:
            cli.set_config_option(sid, "model", value)
            print("模型路由: %s" % value)
        else:
            print("（没找到 %s 组的路由，用默认 %s）"
                  % (args.group, json.dumps(sess.get("configOptions"), ensure_ascii=False)[:120]))

        text = args.ask or "请只回复四个字：链路畅通。不要调用任何工具。"
        print("--- prompt: %s" % text)
        t0 = time.time()
        result = cli.prompt(sid, text)
        print("\n--- 结束 stopReason=%s（%.1fs，收到 %d 条非文本更新）"
              % (result.get("stopReason"), time.time() - t0, len(updates)))

        cli.close_session(sid)
        print("会话已关闭")
        return 0
    except AcpError as err:
        print("ACP 错误: code=%s %s" % (err.code, err.message), file=sys.stderr)
        if err.data is not None:
            print("data=%s" % json.dumps(err.data, ensure_ascii=False)[:500], file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断", file=sys.stderr)
        return 130
    finally:
        cli.stop()


if __name__ == "__main__":
    sys.exit(main())
