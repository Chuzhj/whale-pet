# -*- coding: utf-8 -*-
"""pet_remote —— 给鲸鲸桌宠开一个手机/别的电脑能用的遥控口。

形态：桌宠进程里跑一个**极小的 HTTP 服务**（标准库 http.server，不开线程池外的依赖），
手机或局域网里的电脑用浏览器打开一个带 token 的地址，就能跟她聊天、喂饭、
叫她睡觉/起床、看她状态，以及（可选）给 Harness 派活。

为什么是"桌宠自己当服务器"：
  * 不依赖任何云/中转 —— 没有账号、没有第三方看到你的对话；
  * 手机上什么都不用装 —— 有浏览器就行；
  * 跨网段（不在同一局域网）时不归程序管：推荐挂 Tailscale/VPN 后仍用局域网地址，
    程序本身绝不主动打洞（那是把控制权暴露到公网，责任太重）。

安全模型（照抄 DSH 的思路，浓缩成四条）：
  1. **token 即身份**：所有请求（含页面本身）都要带 token，没有就不给。
     第一次打开靠 ?t=<token>；页面加载时顺手把 token 种进 **HttpOnly Cookie**，
     之后刷新/书签/Cookie 都在，地址栏里那份会被 JS 立刻抹掉（history.replaceState）
     —— token 不必也不该长期留在浏览器历史和书签里。
     token 用 secrets 生成，存在桌宠自己的配置里，不写死在代码。
  2. **默认只听 127.0.0.1**：手机要连必须显式把绑定地址改成 0.0.0.0（局域网）。
     信任栅栏只防"别的局域网设备瞎扫"，不建立身份 —— 身份就是 token。
  3. **Host 只认 IP 字面量**：防 DNS rebinding —— 恶意网页把自己的域名解析到本机
     LAN IP，再用免预检的简单请求打这个口，浏览器会替它带上 Cookie。正常访问
     （127.0.0.1 / localhost / 局域网 IP）Host 都是 IP 字面量，所以不误伤。
  4. **危险能力单独上闸**：远程派活 = 在这台电脑上执行代码，所以有独立开关
     allow_harness（默认关），服务端直接 403，不靠前端隐藏。

线程模型：本模块**完全不碰 Qt**。HTTP 处理发生在 socket 线程里，绝不能直接摸窗口；
要进主线程的动作通过 emit_fn(dict) 交给桌宠 —— 那是它提供的 Qt 信号发射器，
跨线程 emit 会自动排队到主线程（Qt 的标准跨线程套路）。状态读取反过来：
state_fn 也是桌宠给的，只读几个数字和布尔，不碰任何 QWidget，所以在 socket
线程里调用是安全的。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

MAX_BODY = 8 * 1024          # 请求体上限：聊天输入用不了这么大，防滥而已
EVENTS_KEEP = 400            # 内存里留多少条事件（页面轮询用，重启即清零）
POLL_HINT_MS = 1200          # 页面轮询间隔（只是给前端看的提示）

COOKIE_NAME = "whale_remote"     # 页面加载时种下的令牌 Cookie 名
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 天：手机书签一个月不用重扫地址
# Host 头只认 IPv4 字面量（外加 localhost / [v6]），见 Handler._host_ok
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def gen_token() -> str:
    """远程访问令牌。urlsafe 格式，能直接放进 URL。"""
    return secrets.token_urlsafe(16)


class RemoteServer:
    """一个可启停的遥控 HTTP 服务。协议归它，界面归桌宠。"""

    def __init__(self, emit_fn, state_fn, token: str,
                 port: int = 8765, bind: str = "127.0.0.1",
                 allow_harness: bool = False,
                 sessions_fn=None, history_fn=None,
                 on_log=None):
        """
        emit_fn(dict)   —— 把"要进主线程的动作"交给桌宠（Qt 信号发射器）
        state_fn()      —— 读桌宠当前状态，返回 dict（只读数字/布尔，不碰 UI）
        sessions_fn()   —— 列 Harness 的任务对话，返回 [{id,title,cwd,turns,time_label}]
        history_fn(sid) —— 读某段任务对话的正文，返回 [[角色, 正文], ...]
                           两个都只读文件、不碰 Qt，所以能在 socket 线程里直接调。
        allow_harness   —— 远程派活的总闸（False 时 /api/harness 一律 403）
        """
        self._emit = emit_fn
        self._state = state_fn
        self._sessions_fn = sessions_fn or (lambda: [])
        self._history_fn = history_fn or (lambda sid: [])
        self.token = token
        self.port = int(port)
        self.bind = bind
        self.allow_harness = bool(allow_harness)
        self._on_log = on_log or (lambda s: None)

        self._events = []            # [{seq, kind, text, ts}]
        self._seq = 0
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None

    # ------------------------------------------------------------ 事件
    def publish(self, kind: str, text: str = "") -> None:
        """往事件流里放一条（桌宠在主线程里调，页面轮询取走）。

        事件是"只进不出"的环形日志：页面带着 seq 来增量拉，
        掉线重连也不丢 —— 比维护"每个客户端一份"的队列简单得多，
        而这个服务只服务主人一个人，单订阅者假设是成立的。
        """
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": kind,
                                 "text": text, "ts": time.time()})
            if len(self._events) > EVENTS_KEEP:
                del self._events[:len(self._events) - EVENTS_KEEP]

    def _events_since(self, since: int):
        with self._lock:
            return [e for e in self._events if e["seq"] > since], self._seq

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._httpd is not None:
            return
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):      # 静音默认的 stderr 噪音
                server._on_log("http: " + fmt % args)

            def _client_token(self) -> str:
                """请求里带的令牌：先看 query（第一次打开就靠它），再看 Cookie。

                Cookie 是页面加载时种下的 —— 有了它，手机上的书签/刷新
                不必再带 t，地址栏里那份也能被页面自己抹掉。
                """
                q = parse_qs(urlparse(self.path).query)
                tok = (q.get("t") or [""])[0]
                if tok:
                    return tok
                for part in (self.headers.get("Cookie") or "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == COOKIE_NAME:
                        return unquote(value)
                return ""

            def _host_ok(self) -> bool:
                """Host 头必须是 IP 字面量（或 localhost）—— 防 DNS rebinding。

                没有这道闸的话：手机/电脑上打开一个恶意网页，它把自己的域名
                解析到本机 LAN IP，再用 text/plain 的简单请求（免预检）打这个
                端口，浏览器会照常带上 Cookie，那个网页就等于拿到了遥控权。
                正常用法里 Host 只会是 127.0.0.1 / localhost / 本机局域网 IP，
                全都是 IP 字面量，所以这里不会误伤。
                """
                host = (self.headers.get("Host") or "").strip()
                if not host:
                    return False
                if host.startswith("["):                 # [::1]:8765
                    return True
                name = host.rsplit(":", 1)[0] if ":" in host else host
                return name.lower() == "localhost" or bool(_IPV4_RE.match(name))

            def _authed(self) -> bool:
                if not self._host_ok():
                    server._on_log("remote: 拒绝 Host=%r（只认 IP 字面量）"
                                   % (self.headers.get("Host") or ""))
                    return False
                # 定长比较，别让"token 对不对"从响应时间上漏出去
                try:
                    return secrets.compare_digest(self._client_token(),
                                                  server.token)
                except TypeError:                        # 非 ASCII 的脏输入
                    return False

            def _send(self, code: int, body: bytes, ctype: str,
                      extra=None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # 不加缓存：状态和事件都是活的
                self.send_header("Cache-Control", "no-store")
                for key, value in (extra or ()):
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self):
                path = urlparse(self.path).path
                if not self._authed():
                    self._json(401, {"error": "token 不对"})
                    return
                if path == "/":
                    # 页面加载时顺手种下令牌 Cookie：之后手机上的书签/刷新不必
                    # 再带 t，浏览器历史里那份也会被页面自己的 replaceState 抹掉。
                    # HttpOnly：页面脚本读不到它，XSS 也偷不走。
                    self._send(200, PAGE.encode("utf-8"),
                               "text/html; charset=utf-8",
                               extra=[("Set-Cookie",
                                       "%s=%s; Path=/; HttpOnly; SameSite=Lax; "
                                       "Max-Age=%d"
                                       % (COOKIE_NAME, server.token,
                                          COOKIE_MAX_AGE))])
                elif path == "/api/state":
                    try:
                        st = server._state() or {}
                    except Exception as err:
                        st = {"error": str(err)}
                    st["allowHarness"] = server.allow_harness
                    st["pollMs"] = POLL_HINT_MS
                    self._json(200, st)
                elif path == "/api/events":
                    q = parse_qs(urlparse(self.path).query)
                    try:
                        since = int(q.get("since", ["0"])[0])
                    except ValueError:
                        since = 0
                    events, seq = server._events_since(since)
                    self._json(200, {"events": events, "seq": seq})
                elif path == "/api/sessions":
                    # 只读：列 Harness 的任务对话（数据源是会话日志文件）
                    try:
                        rows = server._sessions_fn() or []
                    except Exception as err:
                        self._json(200, {"sessions": [], "error": str(err)})
                        return
                    self._json(200, {"sessions": rows})
                elif path == "/api/history":
                    # 只读：某一段任务对话的正文。
                    # sid 是**由桌宠拿去白名单里查**的（它扫真实目录），
                    # 所以这里不用防路径穿越 —— 传什么进来都只能命中已存在的会话。
                    q = parse_qs(urlparse(self.path).query)
                    sid = (q.get("sid", [""])[0] or "").strip()
                    if not sid:
                        self._json(400, {"error": "缺少 sid"})
                        return
                    try:
                        turns = server._history_fn(sid) or []
                    except Exception as err:
                        self._json(200, {"turns": [], "error": str(err)})
                        return
                    self._json(200, {"turns": turns})
                else:
                    self._json(404, {"error": "no such route"})

            def do_POST(self):
                path = urlparse(self.path).path
                if not self._authed():
                    self._json(401, {"error": "token 不对"})
                    return
                if path not in ("/api/chat", "/api/command", "/api/harness"):
                    self._json(404, {"error": "no such route"})
                    return
                if path == "/api/harness" and not server.allow_harness:
                    # 危险能力在**服务端**上闸：前端藏按钮没有意义
                    self._json(403, {"error": "远程派活没开（桌宠配置 remote_allow_harness）"})
                    return
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                if n <= 0 or n > MAX_BODY:
                    self._json(400, {"error": "空的或者太长"})
                    return
                try:
                    payload = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    self._json(400, {"error": "不是 JSON"})
                    return
                text = str(payload.get("text") or "").strip()
                action = str(payload.get("action") or "").strip()
                if path == "/api/chat":
                    if not text:
                        self._json(400, {"error": "想说什么就写呀"})
                        return
                    server._emit({"action": "chat", "text": text[:2000]})
                elif path == "/api/harness":
                    if not text:
                        self._json(400, {"error": "要派什么活写一下嘛"})
                        return
                    # sid 空 = 开一段新对话；非空 = 接着那一段说
                    sid = str(payload.get("sid") or "").strip()
                    server._emit({"action": "harness", "text": text[:2000],
                                  "sid": sid})
                else:
                    if action not in ("feed", "sleep", "wake", "talk"):
                        self._json(400, {"error": "没这个动作"})
                        return
                    server._emit({"action": action})
                self._json(202, {"ok": True})

        self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="pet-remote")
        self._thread.start()
        self._on_log("remote: listening on %s:%d" % (self.bind, self.port))

    def stop(self, timeout: float = 5.0) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._httpd = None
        self._thread = None

    def is_running(self) -> bool:
        return self._httpd is not None

    def url(self) -> str:
        """给主人看的完整地址（**带 token**）。

        只该出现在设置框里（复制到手机用）—— 终端日志请用 masked_url()。
        绑定 0.0.0.0 时用本机局域网 IP 拼出来。
        """
        host = self.bind
        if host in ("0.0.0.0", "::"):
            host = _lan_ip()
        return "http://%s:%d/?t=%s" % (host, self.port, self.token)

    def masked_url(self) -> str:
        """打码版地址：能看出"开在哪儿"，但 token 不外泄。

        终端输出会进日志、会被截图，而 token 是唯一身份凭证 ——
        不该跟着地址一起躺在那些地方。要完整地址去设置框里复制。
        """
        tok = self.token
        masked = (tok[:4] + "****" + tok[-2:]) if len(tok) > 8 else "****"
        return self.url().replace(tok, masked)


def _lan_ip() -> str:
    """局域网 IP（不开 socket 出去，用一个 UDP connect 探路由——不发包）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # 不会真的发包，只让系统选路由
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- 内嵌页面
# 单文件页面：无构建、无 CDN、无框架 —— 手机浏览器打开就能用。
# token 从 ?t= 读一次（并立刻从地址栏抹掉），之后靠服务端种的 Cookie 认证。
PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>鲸鲸 · 远程</title>
<style>
 body{margin:0;font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      background:#EEF3FA;color:#2B3A67;}
 header{background:#2B3A67;color:#fff;padding:10px 14px;font-weight:600;
        display:flex;justify-content:space-between;align-items:center;}
 header small{font-weight:400;opacity:.75;}
 #bars{padding:8px 14px;background:#E2E9F4;font-size:12px;}
 .bar{height:8px;border-radius:4px;background:#C7D4E8;margin:4px 0 8px;overflow:hidden;}
 .bar i{display:block;height:100%;background:#4A90D9;}
 #chat{padding:12px 14px 170px;max-width:640px;margin:0 auto;}
 .row{margin:8px 0;display:flex;}
 .row.me{justify-content:flex-end;}
 .bubble{max-width:78%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;
         word-break:break-word;}
 .me .bubble{background:#4A90D9;color:#fff;border-bottom-right-radius:4px;}
 .her .bubble{background:#fff;border:1px solid #C7D4E8;border-bottom-left-radius:4px;}
 .tool .bubble{background:#EDF1F4;color:#5F6B7A;font-size:12px;}
 .err .bubble{background:#FDECEA;color:#B4483C;border:1px solid #E9B3AC;}
 #dock{position:fixed;left:0;right:0;bottom:0;background:#fff;
       border-top:1px solid #D8E2F0;box-shadow:0 -2px 8px rgba(43,58,103,.06);}
 .drow{display:flex;gap:8px;padding:7px 10px;align-items:center;}
 #moderow,#actrow{border-bottom:1px solid #EFF3F9;}
 .mbtn{border:1px solid #C7D4E8;background:#fff;color:#2B3A67;border-radius:14px;
       padding:5px 14px;font-size:13px;white-space:nowrap;}
 .mbtn.on{background:#2B3A67;border-color:#2B3A67;color:#fff;}
 #ses{flex:1;min-width:0;border:1px solid #C7D4E8;border-radius:10px;padding:5px 8px;
      font-size:13px;background:#fff;color:#2B3A67;}
 #actrow{justify-content:center;gap:6px;}
 #actrow button{border:1px solid #C7D4E8;border-radius:14px;padding:5px 11px;
                background:#fff;color:#2B3A67;font-size:12px;}
 #ir{display:flex;gap:8px;padding:8px 10px;}
 #t{flex:1;min-width:0;border:1.5px solid #C7D4E8;border-radius:12px;padding:9px 12px;
    font-size:15px;}
 #sendbtn{border:none;border-radius:12px;padding:0 18px;background:#4A90D9;color:#fff;
          font-size:15px;}
 #hint{padding:0 12px 7px;font-size:12px;color:#5F6B7A;}
 #hint.warn{color:#B4483C;}
 .tip{font-size:12px;color:#5F6B7A;}
</style></head><body>
<header>🐋 鲸鲸 <small id="st">…</small></header>
<div id="bars">
  精力<div class="bar"><i id="e" style="width:0%"></i></div>
  饱食<div class="bar"><i id="s" style="width:0%"></i></div>
</div>
<div id="chat"></div>
<div id="dock">
  <div class="drow" id="moderow">
    <button class="mbtn on" id="mchat" onclick="setMode('chat')">闲聊</button>
    <button class="mbtn" id="mwork" onclick="setMode('work')">工作</button>
    <select id="ses" onchange="pick(this.value)" style="display:none"></select>
    <button class="mbtn" id="btnrefresh" onclick="loadSessions(1)"
            style="display:none">刷新</button>
  </div>
  <div class="drow" id="actrow">
    <button onclick="cmd('feed')">喂饭</button>
    <button onclick="cmd('sleep')">去睡觉</button>
    <button onclick="cmd('wake')">叫醒</button>
    <button onclick="cmd('talk')">说句话</button>
  </div>
  <div class="drow" id="ir">
    <input id="t" placeholder="和鲸鲸说点什么…" autocomplete="off">
    <button id="sendbtn" onclick="send()">发送</button>
  </div>
  <div id="hint"></div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
// 地址栏里的 token 用完就抹掉：服务端已经在页面加载时种下 HttpOnly Cookie，
// 之后的请求靠 Cookie 认证，历史记录/书签里不必（也不该）留着那串令牌。
// T 留在内存里，方便"Cookie 被清了"的老浏览器继续用 query 方式工作。
if (T) history.replaceState(null, '', location.pathname);
let seq = 0, stream = null, mode = 'chat', sid = '', allowH = false;
const chat = document.getElementById('chat');
const $ = id => document.getElementById(id);
function api(p){ return p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T); }
function setHint(text, warn){
  const h = $('hint'); h.textContent = text || '';
  h.className = warn ? 'warn' : '';
}
function add(cls, text){
  const row = document.createElement('div');
  row.className = 'row ' + cls;
  const b = document.createElement('div');
  b.className = 'bubble'; b.textContent = text;
  row.appendChild(b); chat.appendChild(row);
  window.scrollTo(0, document.body.scrollHeight);
  return b;
}
function streamUp(text){
  if (!stream) stream = add('her', '');
  stream.textContent += text;
  window.scrollTo(0, document.body.scrollHeight);
}
function streamEnd(text){
  if (stream && text) stream.textContent = text;
  stream = null;
}
// 换模式/换会话时清屏：这一块显示的就是"当前这段对话"
function clearView(){ chat.innerHTML = ''; stream = null; }
function roleClass(role){
  if (role === '主人') return 'me';
  if (role === '助手') return 'her';
  return 'tool';
}
function setMode(m){
  mode = m;
  $('mchat').className = 'mbtn' + (m === 'chat' ? ' on' : '');
  $('mwork').className = 'mbtn' + (m === 'work' ? ' on' : '');
  const w = (m === 'work');
  $('ses').style.display = w ? '' : 'none';
  $('btnrefresh').style.display = w ? '' : 'none';
  $('t').placeholder = w ? '在这段任务里接着说…' : '和鲸鲸说点什么…';
  clearView();
  if (w){
    setHint(allowH ? '挑一段任务对话接着干；选「＋ 开一段新对话」就是新任务。'
                   : '远程派活没开：在那台电脑上右键鲸鲸 → 远程控制… → 勾上「允许远程派活」。',
            !allowH);
    loadSessions(0);
  } else {
    setHint('');
  }
}
async function loadSessions(keep){
  try{
    const r = await (await fetch(api('/api/sessions'))).json();
    const rows = r.sessions || [];
    const sel = $('ses');
    const want = keep ? sid : '';
    sel.innerHTML = '';
    const o0 = document.createElement('option');
    o0.value = ''; o0.textContent = '＋ 开一段新对话';
    sel.appendChild(o0);
    rows.forEach((s, i) => {
      const o = document.createElement('option');
      o.value = s.id;
      o.textContent = '对话' + (i + 1) + ' · ' + s.title + '（' + s.turns + ' 轮）';
      sel.appendChild(o);
    });
    sel.value = want;
    if (r.error) setHint('读会话列表失败：' + r.error, true);
    if (want) loadHistory(want);
  }catch(e){ setHint('列会话失败（网络不通？）', true); }
}
async function pick(v){
  sid = v;
  clearView();
  if (!v){ setHint('好，下一条就开一段新任务。'); return; }
  loadHistory(v);
}
async function loadHistory(id){
  try{
    const r = await (await fetch(api('/api/history?sid=' + encodeURIComponent(id)))).json();
    if (id !== sid) return;            // 拉的这一会儿主人可能又换了会话
    clearView();
    const turns = r.turns || [];
    if (!turns.length) add('tool', '（这段还没有内容）');
    for (const t of turns) add(roleClass(t[0]), t[1]);
    if (r.error) add('err', '读历史失败：' + r.error);
  }catch(e){ add('err', '读历史失败（网络不通？）'); }
}
async function send(){
  const el = $('t');
  const v = el.value.trim(); if (!v) return;
  el.value = '';
  if (mode === 'work'){
    if (!allowH){ add('err', '远程派活没开（在那台电脑上打开开关才能用）'); return; }
    add('me', v);
    await fetch(api('/api/harness'), {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:v, sid:sid})});
  } else {
    add('me', v);
    await fetch(api('/api/chat'), {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:v})});
  }
}
async function cmd(a){
  await fetch(api('/api/command'), {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:a})});
}
async function refreshState(){
  try{
    const s = await (await fetch(api('/api/state'))).json();
    $('e').style.width =
      Math.max(0, Math.min(100, (s.energy || 0) / (s.energyMax || 200) * 100)) + '%';
    $('s').style.width =
      Math.max(0, Math.min(100, (s.satiety || 0) / (s.satietyMax || 200) * 100)) + '%';
    const st = s.sleeping ? (s.deep ? '沉睡中' : '睡觉中') : (s.busy ? '干活中' : '醒着');
    $('st').textContent = st + ' · ' + (s.version || '');
    const was = allowH;
    allowH = !!s.allowHarness;
    if (!was && allowH && mode === 'work') setHint('挑一段任务对话接着干。');
  }catch(e){ $('st').textContent = '连不上（token 或网络变了？）'; }
}
async function poll(){
  try{
    const r = await (await fetch(api('/api/events') + '&since=' + seq)).json();
    seq = r.seq || seq;
    for (const e of r.events || []){
      if (e.kind === 'chunk') streamUp(e.text);
      else if (e.kind === 'done'){
        streamEnd(e.text);
        // 工作模式下这一轮干完了：重读历史，让新内容从日志里"权威地"回来
        if (mode === 'work' && sid) setTimeout(() => loadSessions(1), 400);
      }
      else if (e.kind === 'tool') add('tool', e.text);
      else if (e.kind === 'err') add('err', e.text);
      else if (e.kind === 'say') { streamEnd(''); add('her', e.text); }
    }
  }catch(e){}
}
$('t').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
setMode('chat');
refreshState(); poll(); setInterval(poll, 1500); setInterval(refreshState, 5000);
</script>
</body></html>
"""
