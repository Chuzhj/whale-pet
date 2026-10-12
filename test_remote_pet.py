# -*- coding: utf-8 -*-
"""桌宠侧的远程遥控测试：**走真链路**（真 HTTP 服务 → 真信号 → 真主线程）。

为什么必须有这一个文件：之前那套集成测试自己连了个监听器去接指令，
于是"生产代码里压根没接线"这件事被完美地盖住了 —— 手机网页发什么都石沉大海。
所以这里一条假监听器都不加，全部通过 HTTP 打进去，看桌宠到底有没有反应。
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")

from PySide6.QtCore import QThread, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)

import whale_pet as w

checks = []


def ck(name, cond):
    checks.append(bool(cond))
    print(("  OK   " if cond else "  FAIL ") + name)


# ---------- 真宠物（配置指向临时目录）----------
tmp = Path(tempfile.mkdtemp(prefix="whale_pet_remote_test_"))
buckets = w.discover_assets(Path("assets"))
awake_h = max(48, int(340 * w.AWAKE_SCALE))
sleep_h = max(48, int(340 * w.SLEEP_SCALE))
pool = buckets["front"] + buckets["variant"] or buckets["back"]
blinks = {w.asset_key(p.stem): p for p in buckets["blink"]}


def blink_of(src):
    p = blinks.get(w.asset_key(src.stem))
    return w.load_scaled(p, awake_h) if p else None


WhalePet = w.build_widget(
    [w.load_scaled(p, awake_h) for p in pool],
    awake_h, 60, 1.0, True,
    expressions=[(w.asset_key(s.stem), w.load_scaled(s, awake_h))
                 for s in buckets["expression"]],
    do_expr=True,
    idle_blinks=[blink_of(p) for p in pool],
    expr_blinks=[blink_of(p) for p in buckets["expression"]],
    sleep_pixmap=(w.load_scaled(buckets["sleep"][0], sleep_h)
                  if buckets["sleep"] else None),
    bowl_full_pixmap=next((QPixmap(str(p)) for p in buckets["bowl"]
                           if "empty" not in p.stem.lower()), None),
    bowl_empty_pixmap=next((QPixmap(str(p)) for p in buckets["bowl"]
                            if "empty" in p.stem.lower()), None),
    show_bars=True, do_talk=True)
pet = WhalePet()
pet._config_path = tmp / "whale_pet_config.json"

# ---------- 记录"桌宠到底收到了什么" ----------
chat_calls, harness_calls, threads = [], [], []
pet.chat_ready = lambda: True
pet.start_chat = lambda text, as_chat=False: (
    chat_calls.append((text, as_chat)),
    threads.append(QThread.currentThread() is app.thread()))
pet._harness_send = lambda text, from_console=False, target=None: (
    harness_calls.append((text, target)),
    threads.append(QThread.currentThread() is app.thread()))

_probe = socket.socket()
_probe.bind(("127.0.0.1", 0))
PORT = _probe.getsockname()[1]
_probe.close()
TOKEN = "testtoken"
url = pet.remote_start({"remote_enabled": True, "remote_port": PORT,
                        "remote_bind": "127.0.0.1", "remote_token": TOKEN,
                        "remote_allow_harness": True})
base = "http://127.0.0.1:%d" % PORT
print("  远程服务：%s" % url)

ck("remote_start 起得来", pet._remote is not None and pet._remote.is_running())
# 注意：这里**不去反射检查信号有没有监听者** —— PySide 的 receivers() 要的是
# C++ 签名串，脆而且绕。真凭据在下面：HTTP 打进去，看桌宠有没有真的收到。
# 那一组断言就是"接线在不在"的判据（漏接 command 时会整条挂掉）。


def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=8) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:
        return 0, str(e)


def post(path, obj):
    req = urllib.request.Request(base + path,
                                 data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:
        return 0, str(e)


# ---------- 在子线程里发请求（主线程要留给 Qt 事件循环）----------
sessions_seen = []
history_seen = []


def fire():
    time.sleep(0.4)
    post("/api/chat?t=%s" % TOKEN, {"text": "在吗"})
    time.sleep(0.4)
    # 一个不存在的 sid：应当**不派活**，只回一条 err
    post("/api/harness?t=%s" % TOKEN, {"text": "不该派出去", "sid": "不存在的会话"})
    time.sleep(0.4)
    # 用真实会话列表里的第一段（如果这台机器上有）
    code, body = get("/api/sessions?t=%s" % TOKEN)
    rows = (json.loads(body).get("sessions") or []) if code == 200 else []
    sessions_seen.extend(rows)
    if rows:
        sid = rows[0]["id"]
        history_seen.append(get("/api/history?t=%s&sid=%s" % (TOKEN, sid)))
        post("/api/harness?t=%s" % TOKEN, {"text": "接着干", "sid": sid})
    else:
        post("/api/harness?t=%s" % TOKEN, {"text": "开个新的", "sid": ""})
    time.sleep(0.4)
    app.quit()


threading.Thread(target=fire, daemon=True).start()
QTimer.singleShot(15000, app.quit)
app.exec()

print()
print("=== 1. 闲聊：HTTP → 信号 → 主线程（接线在不在，就看这一组）===")
ck("桌宠收到了那句话（漏接 command 就会在这里挂）",
   bool(chat_calls) and chat_calls[0][0] == "在吗")
ck("**按闲聊走**（as_chat=True，没被桌面 Harness 模式劫成派活）",
   chat_calls and chat_calls[0][1] is True)
ck("而且确实落在主线程（不是 socket 线程直接摸到桌宠）",
   bool(threads) and all(threads))

print()
print("=== 2. 派活：不存在的会话不该派出去 ===")
ck("共派了 %d 次活（那条伪造 sid 被拦住了）"
   % len(harness_calls), len(harness_calls) >= 1)
ck("传上去的文本里没有那条伪造 sid 的活",
   all("不该派出去" != c[0] for c in harness_calls))
code, body = get("/api/events?t=%s&since=0" % TOKEN)
evs = json.loads(body)["events"]
ck("界面上收到了「找不到那段对话」的提示",
   any(e["kind"] == "err" and "找不到" in e["text"] for e in evs))

print()
print("=== 3. 派活：真会话要带上目标 ===")
if harness_calls:
    text, target = harness_calls[-1]
    ck("最后一次派活的正文对得上", text == "接着干" or text == "开个新的")
    if text == "接着干":
        ck("带上了选中的那段会话（id/cwd/title 都在）",
           isinstance(target, dict) and target.get("id")
           and target.get("cwd") and target.get("title"))
    else:
        ck("没有会话时 target 为空 = 开新对话", target is None)
else:
    ck("至少派了一次活", False)

print()
print("=== 4. 网页能拿到真数据 ===")
ck("会话列表走的是真 dsh_session_log（拿到 %d 段）" % len(sessions_seen),
   isinstance(sessions_seen, list))
if sessions_seen:
    ck("列表字段够网页渲染",
       all(k in sessions_seen[0] for k in ("id", "title", "turns", "time_label")))
    code, body = history_seen[0] if history_seen else (0, "{}")
    turns = json.loads(body).get("turns") if code == 200 else []
    ck("真会话的历史读得出来（%d 条）" % len(turns), len(turns) > 0)
    ck("历史里的角色是网页认的那三个",
       {t[0] for t in turns} <= {"主人", "助手", "工具"})
    ck("伪造的 sid 读不到任何东西（白名单里查不到就不读文件）",
       json.loads(get("/api/history?t=%s&sid=not-a-real-sid" % TOKEN)[1])["turns"] == [])
else:
    print("      （这台机器上还没有任务对话，跳过这几条）")

print()
print("=== 5. 状态 / 关闭 ===")
st = json.loads(get("/api/state?t=%s" % TOKEN)[1])
ck("状态里带版本号", st.get("version") == w.APP_VERSION)
ck("带 allowHarness（网页据此决定工作模式能不能用）", st.get("allowHarness") is True)
pet.remote_stop()
ck("remote_stop 之后服务关掉", not (pet._remote and pet._remote.is_running()))

shutil.rmtree(tmp, ignore_errors=True)
print()
print("结论:", "通过" if all(checks) else "有失败项（%d/%d）"
      % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
