# -*- coding: utf-8 -*-
"""pet_remote.py 的协议层测试（不需要 Qt，也不起桌宠）。

覆盖：鉴权、页面、状态、事件流、以及新加的「列任务 / 读历史」两个只读接口，
还有远程派活的两段式闸门（服务端 403，且**不许悄悄派出去**）。
"""
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")

import pet_remote as pr

checks = []


def ck(name, cond):
    checks.append(bool(cond))
    print(("  OK   " if cond else "  FAIL ") + name)


# ---- 造一个有内容的假会话列表和历史 ----
SESSIONS = [
    {"id": "s-1", "title": "跨会话记忆能否共享", "cwd": r"C:\ws", "turns": 3,
     "time_label": "今天 12:30"},
    {"id": "s-2", "title": "把测试跑一遍", "cwd": r"C:\ws2", "turns": 1,
     "time_label": "昨天 09:01"},
]
HISTORY = {"s-1": [["主人", "记忆能共享吗"], ["助手", "默认不能。"],
                   ["工具", "pwsh({\"command\": \"ls\"})"]],
           "s-2": [["主人", "跑测试"]]}
asked = []


def sessions_fn():
    asked.append("sessions")
    return SESSIONS


def history_fn(sid):
    asked.append("history:" + sid)
    return HISTORY.get(sid, [])


emitted = []
srv = pr.RemoteServer(emit_fn=emitted.append,
                      state_fn=lambda: {"energy": 150, "energyMax": 200,
                                        "satiety": 40, "satietyMax": 200,
                                        "sleeping": False, "deep": False,
                                        "busy": True, "version": "v59"},
                      token="tok123", port=0, bind="127.0.0.1",
                      allow_harness=False,
                      sessions_fn=sessions_fn, history_fn=history_fn)
# port=0 会拿随机端口；但 url() 里存的是传进去的 port，所以要自己记真实端口
srv.start()
port = srv._httpd.server_address[1]
base = "http://127.0.0.1:%d" % port
print("  假服务跑在 %s" % base)


def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=6) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def post(path, obj):
    req = urllib.request.Request(base + path,
                                 data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


print()
print("=== 1. 鉴权 ===")
ck("无 token → 401", get("/api/state")[0] == 401)
ck("token 错 → 401", get("/api/state?t=bad")[0] == 401)
ck("页面本身也要 token", get("/")[0] == 401)

print()
print("=== 2. 页面 ===")
code, page = get("/?t=tok123")
ck("页面 200", code == 200)
ck("页面是鲸鲸的远程页", "鲸鲸" in page and "远程" in page)
ck("页面有「闲聊 / 工作」两档", "setMode('chat')" in page and "setMode('work')" in page)
ck("页面有会话下拉与历史渲染", 'id="ses"' in page and "loadHistory" in page)
ck("页面不再有旧的独立派活框（已并进工作模式）",
   'id="harness"' not in page and "function harness(" not in page)
ck("页面用 sid 派活", "sid:sid" in page)

print()
print("=== 3. 状态 ===")
st = json.loads(get("/api/state?t=tok123")[1])
ck("读得到精力/饱食", st["energy"] == 150 and st["satiety"] == 40)
ck("带上了 allowHarness 给前端做显示", st["allowHarness"] is False)
ck("带上了轮询间隔提示", st["pollMs"] == pr.POLL_HINT_MS)

print()
print("=== 4. 任务列表（新接口）===")
d = json.loads(get("/api/sessions?t=tok123")[1])
ck("sessions_fn 被调了", "sessions" in asked)
ck("两条会话都回来了", len(d["sessions"]) == 2)
ck("字段够网页用（id/title/turns/cwd/time_label）",
   all(k in d["sessions"][0] for k in ("id", "title", "turns", "cwd", "time_label")))
ck("没有把本机路径 log_path 漏给浏览器", "log_path" not in d["sessions"][0])

print()
print("=== 5. 历史（新接口）===")
d = json.loads(get("/api/history?t=tok123&sid=s-1")[1])
ck("history_fn 收到了正确的 sid", "history:s-1" in asked)
ck("三条记录回来了（含工具行）", len(d["turns"]) == 3
   and d["turns"][0] == ["主人", "记忆能共享吗"])
ck("角色字符串是网页认的那三个", {t[0] for t in d["turns"]} <= {"主人", "助手", "工具"})
ck("缺 sid → 400", get("/api/history?t=tok123")[0] == 400)
ck("不存在的 sid → 空表（不报错、也不去读文件）",
   json.loads(get("/api/history?t=tok123&sid=../etc/passwd")[1])["turns"] == [])
ck("伪造的 sid 只在白名单里查过、没触发读取",
   "history:../etc/passwd" in asked)

print()
print("=== 6. 事件流 ===")
srv.publish("say", "测试一下")
ev = json.loads(get("/api/events?t=tok123&since=0")[1])
ck("取得到刚推的事件", any(e["kind"] == "say" for e in ev["events"]))
since = ev["seq"]
ck("since 之后没有旧事件", json.loads(
    get("/api/events?t=tok123&since=%d" % since)[1])["events"] == [])

print()
print("=== 7. 指令 ===")
emitted.clear()
ck("聊天 202", post("/api/chat?t=tok123", {"text": "在吗"})[0] == 202)
ck("聊天指令进了主线程队列（带 text）",
   emitted == [{"action": "chat", "text": "在吗"}])
emitted.clear()
ck("空文本 → 400", post("/api/chat?t=tok123", {"text": "  "})[0] == 400)
ck("空文本什么都没发出去", emitted == [])
ck("非法动作 → 400", post("/api/command?t=tok123", {"action": "hack"})[0] == 400)
emitted.clear()
ck("合法动作 202", post("/api/command?t=tok123", {"action": "feed"})[0] == 202)
ck("动作进了队列", emitted == [{"action": "feed"}])

print()
print("=== 8. 派活闸（服务端 403，且不许悄悄派出去）===")
emitted.clear()
code, body = post("/api/harness?t=tok123", {"text": "把测试跑一遍", "sid": "s-1"})
ck("没开闸时 403", code == 403)
ck("**被拒时一个指令都没发出去**", emitted == [])
ck("错误信息指向该去开哪个开关", "allow_harness" in body)

srv.allow_harness = True          # 开闸（不改配置，直接改内存里的闸）
emitted.clear()
ck("开闸后 202", post("/api/harness?t=tok123",
                      {"text": "把测试跑一遍", "sid": "s-1"})[0] == 202)
ck("指令带上了选中的那段会话", emitted == [{"action": "harness",
                                            "text": "把测试跑一遍", "sid": "s-1"}])
emitted.clear()
post("/api/harness?t=tok123", {"text": "开个新的"})
ck("不选会话时 sid 是空串（= 新开一段）",
   emitted == [{"action": "harness", "text": "开个新的", "sid": ""}])

print()
print("=== 9. 杂项 ===")
ck("不存在的路由 → 404", get("/api/nope?t=tok123")[0] == 404)
ck("POST 到不存在的路由 → 404", post("/api/nope?t=tok123", {})[0] == 404)
ck("空的 POST body → 400", post("/api/chat?t=tok123", {})[0] == 400)
ck("url() 拼得出地址", srv.url().startswith("http://127.0.0.1:")
   and "t=tok123" in srv.url())
srv.stop()
ck("stop() 之后服务关了", not srv.is_running())

print()
print("结论:", "通过" if all(checks) else "有失败项（%d/%d）"
      % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
