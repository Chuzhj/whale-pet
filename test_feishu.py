# -*- coding: utf-8 -*-
"""pet_feishu.py 的验证：纯函数 + 协议互操作 + 本地假飞书端到端。

三件事值得说清楚：

1. **帧编解码的"标准答案"是官方 protobuf**。本模块没用 lark-oapi，
   但测试里把它的 `pbbp2_pb2` 拉出来当基准：我编它解、它编我解，
   两边字段必须一致。这样"我手写的 protobuf 对不对"就不靠嘴说。
2. **本地起一个假飞书**（真 WebSocket 服务器），按真协议推事件帧、
   收 ack 帧 —— 除了"没真连上 open.feishu.cn"，整条链都是真的。
3. 官方包太重（import 一次 10~56 秒），所以用"冒充父包"的办法只加载
   pb 那一小块，顺带也验证了 `pet_feishu` 自己没有偷偷依赖它。
"""
import json
import os
import sys
import threading
import time
import types

sys.path.insert(0, ".")

checks = []


def ck(name, cond):
    checks.append(bool(cond))
    print(("  OK   " if cond else "  FAIL ") + name)


import pet_feishu as pf

# ---------------------------------------------------------------------------
print("=== 1. 依赖探测：import 本模块不能把重家伙拉进来 ===")
ck("available() 为真（websockets 在）", pf.available())
ck("没有把官方 lark_oapi 拉进 sys.modules", "lark_oapi" not in sys.modules)
ck("missing_reason() 在有依赖时为空", pf.missing_reason() == "")

# 官方 protobuf 的"标准答案"（冒充父包，跳过它那串重量级 __init__）
ROOT = None
for p in sys.path:
    if os.path.isfile(os.path.join(p, "lark_oapi", "__init__.py")):
        ROOT = os.path.join(p, "lark_oapi")
        break
ck("找得到官方 SDK 目录（当基准用）", bool(ROOT))
if ROOT is None:
    # 这一份测试是拿官方 protobuf 当**基准**对拍我们自己手写的编解码。
    # 缺了基准，后面的对拍无从谈起 —— 但那是**环境缺依赖**，不是代码坏了，
    # 所以明确说清楚并用退出码 2 区分（以前这里会直接 TypeError 崩掉，
    # 看起来像"测试挂了"）。
    print()
    print("！！ 没找到官方 lark_oapi：它是本测试的帧格式基准。")
    print("   它随 DSH 打包在 app.asar 里，请在 DSH 的 Node/桌面环境里跑，")
    print("   或把 lark_oapi 目录放进 sys.path 后再跑。")
    print("   本次：%d 项检查，%d 项通过（**基准缺失，不计为通过**）"
          % (len(checks), sum(checks)))
    sys.exit(2)
for name, path in (("lark_oapi", ROOT),
                   ("lark_oapi.ws", os.path.join(ROOT, "ws")),
                   ("lark_oapi.ws.pb", os.path.join(ROOT, "ws", "pb"))):
    m = types.ModuleType(name)
    m.__path__ = [path]
    sys.modules[name] = m
t0 = time.time()
from lark_oapi.ws.pb.pbbp2_pb2 import Frame as OfficialFrame   # noqa: E402
print("       加载官方 pb 当基准：%.2f 秒" % (time.time() - t0))
ck("官方基准可用", OfficialFrame is not None)

# ---------------------------------------------------------------------------
print()
print("=== 2. parse_command ===")
for raw, want in [
    ("/help", ("cmd", "help", None)),
    ("帮助", ("cmd", "help", None)),
    ("/harness", ("cmd", "harness", None)),
    ("工作模式", ("cmd", "harness", None)),
    ("/exit", ("cmd", "chat", None)),
    ("/list", ("cmd", "list", None)),
    ("/new", ("cmd", "new", None)),
    ("状态", ("cmd", "status", None)),
    ("3", ("cmd", "pick", 3)),
    ("/12", ("cmd", "pick", 12)),
    ("  2  ", ("cmd", "pick", 2)),
    ("帮我跑一下测试", ("text", "帮我跑一下测试", None)),
    ("", ("text", "", None)),
    ("1234567890123", ("text", "1234567890123", None)),
]:
    got = pf.parse_command(raw)
    ck("parse(%r) -> %r" % (raw, got), got == want)

# ---------------------------------------------------------------------------
print()
print("=== 3. split_text ===")
ck("短文本不切", pf.split_text("你好") == ["你好"])
ck("空文本返回空表", pf.split_text("   ") == [])
long_text = "\n".join("第%d行内容" % i for i in range(400))
parts = pf.split_text(long_text)
ck("长文本被切开（%d 片）" % len(parts), len(parts) > 1)
ck("每片都不超限", all(len(p) <= pf.CHUNK_LIMIT for p in parts))
ck("内容没丢（拼回去等于原文）", "\n".join(parts) == long_text)
hard = "x" * (pf.CHUNK_LIMIT * 2 + 10)
hp = pf.split_text(hard)
ck("超长单行硬切（%d 片）" % len(hp), len(hp) == 3 and "".join(hp) == hard)

# ---------------------------------------------------------------------------
print()
print("=== 4. 事件归一化（v2 信封）===")
payload = {
    "schema": "2.0",
    "header": {"event_id": "e1", "event_type": "im.message.receive_v1",
               "create_time": "1760000000000", "token": "tk",
               "app_id": "cli_x", "tenant_key": "t1"},
    "event": {
        "sender": {"sender_id": {"open_id": "ou_me", "union_id": "on_me"},
                   "sender_type": "user", "tenant_key": "t1"},
        "message": {"message_id": "om_1", "chat_id": "oc_9",
                    "chat_type": "group", "message_type": "text",
                    "content": json.dumps({"text": "@_user_1  帮我看看这个  "}),
                    "mentions": [{"key": "@_user_1", "name": "鲸鲸",
                                  "id": {"open_id": "ou_bot"}}]},
    },
}
etype, ev = pf.normalize_payload(json.dumps(payload, ensure_ascii=False))
ck("event_type 从 header 里取到了", etype == "im.message.receive_v1")
ck("会话/消息 id 提出来了", ev["chat_id"] == "oc_9" and ev["message_id"] == "om_1")
ck("群里 @ 的占位符被抹掉", "@_user_1" not in ev["text"])
ck("正文首尾空白被去掉", ev["text"] == "帮我看看这个")
ck("发送者 open_id 提出来了", ev["sender_open_id"] == "ou_me")
ck("chat_type / mention 名字都对", ev["chat_type"] == "group"
   and ev["mention_names"] == ["鲸鲸"])
ck("payload 是坏 JSON 时不炸", pf.normalize_payload("{不是 json") == ("", None))
ck("没订阅的事件也能解析出类型", pf.normalize_payload(json.dumps(
    {"schema": "2.0", "header": {"event_type": "im.chat.updated_v1"},
     "event": {}}))[0] == "im.chat.updated_v1")

# ---------------------------------------------------------------------------
print()
print("=== 5. 帧编解码 vs 官方 protobuf（双向对拍）===")
raw = pf.encode_frame(seq=1234567890123, log_id=7, service=42, method=1,
                      headers=[("type", "event"), ("message_id", "om_x"),
                               ("trace_id", "tr"), ("sum", "1"), ("seq", "0")],
                      payload=b'{"hello":"world"}')
off = OfficialFrame()
off.ParseFromString(raw)
hdrs = {h.key: h.value for h in off.headers}
ck("我编 → 官方解：SeqID（大数，验多字节 varint）", off.SeqID == 1234567890123)
ck("我编 → 官方解：service/method", off.service == 42 and off.method == 1)
ck("我编 → 官方解：headers 条数与内容",
   len(off.headers) == 5 and hdrs["type"] == "event" and hdrs["message_id"] == "om_x")
ck("我编 → 官方解：payload", off.payload == b'{"hello":"world"}')
ck("我编的帧过得了 proto2 的 required 检查（1~4 号字段一个不缺）",
   off.IsInitialized())

mine = pf.decode_frame(raw)
ck("我自己解自己：字段全对",
   mine["seq"] == 1234567890123 and mine["service"] == 42
   and mine["method"] == 1 and mine["payload"] == b'{"hello":"world"}'
   and pf.header_of(mine, "trace_id") == "tr")

off2 = OfficialFrame()
off2.SeqID = 99
off2.LogID = 3          # proto2 required：官方侧序列化也要求它
off2.service = 5
off2.method = 1
for k, v in (("type", "event"), ("biz_rt", "12")):
    h = off2.headers.add()
    h.key = k
    h.value = v
off2.payload = "中文负载".encode("utf-8")
bytes2 = off2.SerializeToString()
mine2 = pf.decode_frame(bytes2)
ck("官方编 → 我解：数值字段", mine2["seq"] == 99 and mine2["service"] == 5)
ck("官方编 → 我解：header 字典", pf.header_of(mine2, "type") == "event"
   and pf.header_of(mine2, "biz_rt") == "12")
ck("官方编 → 我解：中文 payload", mine2["payload"].decode("utf-8") == "中文负载")

ping = pf.encode_frame(service=7, method=0, headers=[("type", "ping")])
po = OfficialFrame()
po.ParseFromString(ping)
ck("心跳帧：官方解出来是 CONTROL + type=ping",
   po.method == 0 and {h.key: h.value for h in po.headers}.get("type") == "ping")
ck("心跳帧里带着 service_id", po.service == 7)
ck("解一个空字节流不炸", pf.decode_frame(b"")["method"] == 0)
ck("header 取值有默认值", pf.header_of({"headers": []}, "没有的key") == "")

# ---------------------------------------------------------------------------
print()
print("=== 6. 没填凭据 / 假凭据的降级 ===")
c = pf.FeishuClient("", "", on_event=lambda d: None)
ck("start() 直接拒绝", c.start() is False)
ck("状态标 error 且有人话", c.status()["state"] == "error"
   and "App ID" in c.status()["error"])
ck("标签是中文字", c.status()["label"] == "出错了")
ck("没连接不假装在线", c.is_online() is False)
ck("stop() 空操作不炸", c.stop() is True)

try:
    ok, msg = pf.check_credentials("cli_这不是真的", "假secret", timeout=15)
    ck("check_credentials 返回 (False, 人话) 且不抛", ok is False and bool(msg))
    print("       飞书原话：%s" % msg[:110])
except Exception as err:
    ck("check_credentials 不该抛异常：%s" % err, False)

# ---------------------------------------------------------------------------
print()
print("=== 7. 本地假飞书端到端 ===")
import websockets.sync.server as wss   # noqa: E402
import socket                          # noqa: E402

outbox = []          # 主线程要塞给客户端的帧
got_frames = []      # 假飞书收到的帧（用官方 pb 解）
conn_objs = []       # 建立过的连接（用来掐断，验重连）
lock = threading.Lock()
server_stop = threading.Event()


def evt_frame(text, mid, chat="oc_1", etype="im.message.receive_v1",
              sum_=1, seq=0, service=7):
    body = json.dumps({
        "schema": "2.0",
        "header": {"event_id": "e_" + mid, "event_type": etype,
                   "create_time": str(int(time.time() * 1000)),
                   "token": "t", "app_id": "cli_fake", "tenant_key": "tk"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_me"}, "sender_type": "user"},
            "message": {"message_id": mid, "chat_id": chat, "chat_type": "p2p",
                        "message_type": "text",
                        "content": json.dumps({"text": text},
                                              ensure_ascii=False),
                        "mentions": []},
        }}, ensure_ascii=False)
    f = OfficialFrame()
    f.SeqID = 1
    f.LogID = 2
    f.service = service
    f.method = 1
    for k, v in (("type", "event"), ("message_id", mid), ("trace_id", "tr1"),
                 ("sum", str(sum_)), ("seq", str(seq))):
        h = f.headers.add()
        h.key = k
        h.value = v
    f.payload = body.encode("utf-8")
    return f.SerializeToString()


def control_frame(kind="ping", service=7):
    f = OfficialFrame()
    f.SeqID = 0
    f.LogID = 0
    f.service = service
    f.method = 0
    h = f.headers.add()
    h.key = "type"
    h.value = kind
    return f.SerializeToString()


def handler(conn):
    with lock:
        conn_objs.append(conn)
    try:
        while not server_stop.is_set():
            with lock:
                pending, outbox[:] = list(outbox), []
            for item in pending:
                conn.send(item)
            try:
                raw = conn.recv(timeout=0.1)
            except TimeoutError:
                continue
            except Exception:
                break
            f = OfficialFrame()
            f.ParseFromString(raw)
            with lock:
                got_frames.append(f)
    except Exception:
        pass


# 先抢一个空闲端口再交给服务器（Server 对象上拿不到端口）
_probe = socket.socket()
_probe.bind(("127.0.0.1", 0))
port = _probe.getsockname()[1]
_probe.close()
srv = wss.serve(handler, "127.0.0.1", port, ping_interval=None)
srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
srv_thread.start()
print("       假飞书跑在 ws://127.0.0.1:%d" % port)

events = []
client = pf.FeishuClient("cli_fake", "secret_fake",
                         on_event=events.append, on_log=lambda s: None)
# 把"取连接地址"换成指向本地假飞书（其余协议逻辑一个字不改）
client._discover = lambda: setattr(client, "_conn_url",
                                   "ws://127.0.0.1:%d/ws" % port) or \
    setattr(client, "_service_id", 7)
client._ping_interval = 1          # 心跳改快，好在测试里看到
client._reconnect_interval = 1
client._reconnect_nonce = 1
ck("start() 受理", client.start() is True)

deadline = time.time() + 15
while time.time() < deadline and not client.is_online():
    time.sleep(0.2)
ck("连上了（状态变在线）", client.is_online())
ck("状态标签是中文「在线」", client.status()["label"] == "在线")

# --- 正常一条消息 ---
with lock:
    outbox.append(evt_frame("你好呀", "om_a1"))
deadline = time.time() + 10
while time.time() < deadline and not events:
    time.sleep(0.1)
ck("事件送达上层", len(events) == 1 and events[0]["text"] == "你好呀")
if events:
    ck("事件字段完整", events[0]["chat_id"] == "oc_1"
       and events[0]["sender_open_id"] == "ou_me"
       and events[0]["message_type"] == "text")

# --- ack 要合法 ---
deadline = time.time() + 10
while time.time() < deadline:
    with lock:
        acks = [f for f in got_frames if f.method == 1]
    if acks:
        break
    time.sleep(0.1)
ck("收到了 ack", bool(acks))
if acks:
    a = acks[0]
    hd = {h.key: h.value for h in a.headers}
    ck("ack 的 payload 是 {\"code\":200}", json.loads(a.payload.decode())["code"] == 200)
    ck("ack 原样带回了 message_id", hd.get("message_id") == "om_a1")
    ck("ack 补了 biz_rt", "biz_rt" in hd)
    ck("ack 回了同一个 seq/service", a.SeqID == 1 and a.service == 7)

# --- 重推去重 ---
with lock:
    outbox.append(evt_frame("你好呀", "om_a1"))
time.sleep(2.5)
ck("飞书重推被去重（上层只收到一次）", len(events) == 1)

# --- 分片合包 ---
mid = "om_frag"
full = {"text": "把这句话拆成两帧发过来"}
body = json.dumps({
    "schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
    "event": {"sender": {"sender_id": {"open_id": "ou_me"}, "sender_type": "user"},
              "message": {"message_id": mid, "chat_id": "oc_1",
                          "chat_type": "p2p", "message_type": "text",
                          "content": json.dumps(full, ensure_ascii=False)}}},
    ensure_ascii=False).encode("utf-8")
half = len(body) // 2
for i, part in enumerate((body[:half], body[half:])):
    f = OfficialFrame()
    f.SeqID = 5
    f.LogID = 6
    f.service = 7
    f.method = 1
    for k, v in (("type", "event"), ("message_id", mid), ("trace_id", "t"),
                 ("sum", "2"), ("seq", str(i))):
        h = f.headers.add()
        h.key = k
        h.value = v
    f.payload = part
    with lock:
        outbox.append(f.SerializeToString())
deadline = time.time() + 10
while time.time() < deadline and len(events) < 2:
    time.sleep(0.1)
ck("分片消息被正确拼起来", len(events) == 2
   and events[1]["text"] == "把这句话拆成两帧发过来")

# --- 未订阅事件被忽略 ---
with lock:
    outbox.append(evt_frame("无关事件", "om_other", etype="im.chat.updated_v1"))
time.sleep(2.0)
ck("未订阅的事件不往上层送", len(events) == 2)

# --- 收到 ping 控制帧不该炸 ---
with lock:
    outbox.append(control_frame("ping"))
time.sleep(1.5)
ck("收到 ping 控制帧后连接还在", client.is_online())

# --- 我们自己要发心跳 ---
with lock:
    pings = [f for f in got_frames if f.method == 0]
ck("客户端按间隔发了协议级心跳（%d 个）" % len(pings), len(pings) >= 1)
if pings:
    hd = {h.key: h.value for h in pings[0].headers}
    ck("心跳帧 type=ping 且带 service_id",
       hd.get("type") == "ping" and pings[0].service == 7)

# --- 断线要能自己重连 ---
# 从**服务器侧**把连接掐断（srv.shutdown() 只停 accept，不会断已有的连接，
# 第一版测试就是栽在这个假设上）。
with lock:
    before = len(conn_objs)
    victims = list(conn_objs)
for c in victims:
    try:
        c.close()
    except Exception:
        pass
deadline = time.time() + 20
while time.time() < deadline:
    with lock:
        now = len(conn_objs)
    if now > before and client.is_online():
        break
    time.sleep(0.2)
with lock:
    after = len(conn_objs)
print("       掐断前连接数=%d，掐断后=%d，online=%s" % (before, after,
                                                     client.is_online()))
ck("被掐断后自己重连上了（服务器收到新连接）", after > before)
ck("重连后状态回到在线", client.is_online())
ck("重连后状态标签还是「在线」", client.status()["label"] == "在线")

try:
    srv.shutdown()
except Exception:
    pass

t0 = time.time()
ck("stop() 能收干净", client.stop() is True)
dt = time.time() - t0
ck("stop() 够快（%.1fs）" % dt, dt < 9)

server_stop.set()
print()
print("结论:", "通过" if all(checks) else "有失败项（%d/%d）"
      % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
