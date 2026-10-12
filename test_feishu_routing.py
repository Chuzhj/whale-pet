# -*- coding: utf-8 -*-
"""v57 飞书路由的针对性冒烟：状态机 + 分流 + 回推。

用**假飞书桥**（不联网）驱动 `_on_feishu_message`，看它把消息派到了哪条管线、
回了什么话。重点验三件事：

1. 飞书说"闲聊"就是闲聊 —— 哪怕桌面工作台正开着 Harness 模式，
   也不能被劫成派活（`as_chat=True` 就是为这个加的）。
2. 派活有独立闸：没开闸时只回一句说明，绝不真派。
3. 结果回推到**发起的那条消息**上，推完就清 pending（不然下一次的结果会回错会话）。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)

import whale_pet as w

checks = []


def ck(name, cond):
    checks.append(bool(cond))
    print(("  OK   " if cond else "  FAIL ") + name)


# ---------- 一只真宠物（配置指向临时目录，别污染主人的聊天记录）----------
tmp = Path(tempfile.mkdtemp(prefix="whale_pet_feishu_test_"))
buckets = w.discover_assets(Path("assets"))
height = 340
awake_h = max(48, int(height * w.AWAKE_SCALE))
sleep_h = max(48, int(height * w.SLEEP_SCALE))
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
print("  宠物造好了（配置在临时目录 %s）" % tmp)


# ---------- 假飞书桥：把"回出去的每一句"记下来 ----------
class FakeFeishu:
    def __init__(self):
        self.replies = []

    def reply(self, chat_id, message_id, text):
        self.replies.append((chat_id, message_id, text))
        return True

    def last(self):
        return self.replies[-1][2] if self.replies else ""

    def clear(self):
        self.replies = []


fake = FakeFeishu()
pet._feishu = fake
pet._remote = None          # 别去碰远程那套


def feed(text, chat="oc_1", sender="ou_me", mid=None):
    """模拟收到一条飞书消息。mid 固定 = 方便断言"回的是哪条"。"""
    fake.clear()
    pet._on_feishu_message({"chat_id": chat, "message_id": mid or "om_1",
                            "chat_type": "p2p", "sender_open_id": sender,
                            "text": text})
    return fake.last()


print()
print("=== 1. 帮助 / 状态 ===")
ck("/help 回了帮助且提到 /harness", "/harness" in feed("/help"))
ck("帮助里带上了当前模式", "闲聊模式" in fake.last())
out = feed("/status")
ck("/status 报了版本和精力", w.APP_VERSION in out and "精力" in out)

print()
print("=== 2. 闲聊（没配 API key → 应当递同一句话）===")
pet.chat_ready = lambda: False
ck("没配钥匙时给的是同一句解释（chat_blocked 抽出来的那句）",
   "key" in feed("你好呀"))

# 配好钥匙，并且**把桌面切到 Harness 模式** —— 这是最容易串台的情形
pet.chat_ready = lambda: True
pet._harness_mode = True
sent = []
pet.start_chat = lambda text, as_chat=False: sent.append((text, as_chat))
feed("今天好累")
ck("闲聊走的是 start_chat 且带 as_chat=True（没被桌面 Harness 模式劫走）",
   sent and sent[-1] == ("今天好累", True))
ck("同时记下了 pending，知道结果该回哪条",
   # v61 起 _feishu_pending 是**分舱**结构（{"chat": …, "harness": …}）：
   # 闲聊与派活各占一格，谁后到都不覆盖对方（老的单槽写法已经修掉了）。
   # 这个测试当时没跟着改，一直在报 KeyError —— 2026-10-11 一并修正。
   bool(pet._feishu_pending) and bool(pet._feishu_pending.get("chat")))

# 回推结果
fake.clear()
pet._feishu_publish("done", "摸摸头，早点睡")
ck("结果回推到飞书", fake.last() == "摸摸头，早点睡")
ck("推完 pending 就清了（那一舱空了）",
   not (pet._feishu_pending or {}).get("chat"))
fake.clear()
pet._feishu_publish("done", "这句不该发出去")
ck("没有 pending 时不再乱推（本机聊天不会刷飞书屏）", fake.replies == [])
pet._harness_mode = False

print()
print("=== 3. 进工作模式 + 挑一段对话 ===")
out = feed("/harness")
ck("/harness 回了会话列表（或明确说没有）",
   ("序号" in out) or ("还没有任务对话" in out))
st = pet._feishu_session("oc_1")
ck("模式切到 harness", st["mode"] == "harness")
if st["rows"]:
    title = st["rows"][0]["title"]
    out = feed("1")
    ck("回 1 选上了第一段对话", st["target"] and st["target"]["id"] == st["rows"][0]["id"])
    ck("选完给了一句确认（带标题）", title in out)
    ck("选完就不再处于挑选态", st["choosing"] is False)
    # 数字 0 = 新对话
    feed("0")
    ck("回 0 = 开新对话（target 清空）", st["target"] is None)
    feed("/list")
    ck("/list 能重看列表", st["choosing"] is True)
    # 不在挑选态时，数字应该当普通聊天内容（前提：已经在闲聊模式）
    st["choosing"] = False
    st["mode"] = "chat"
    sent.clear()
    fake.clear()
    pet._on_feishu_message({"chat_id": "oc_1", "message_id": "om_9",
                            "chat_type": "p2p", "sender_open_id": "ou_me",
                            "text": "2"})
    ck("不在挑选态时，纯数字 2 被当聊天内容送出去（没被吃掉）",
       sent and sent[-1][0] == "2")
    # 清空"分舱"（v61 起 _feishu_pending 是 {"chat":…, "harness":…}）。
    # 旧写法是直接赋 None（单槽时代），那样后面 _feishu_task 里
    # `_feishu_pending.get(...)` 会 AttributeError —— 这个老测试一直没跑到那儿，
    # 2026-10-11 修正后才暴露出来。
    pet._feishu_pending["chat"] = None
    pet._feishu_pending["harness"] = None
else:
    print("       （这台机器上没有任务对话，跳过挑选相关的断言）")

print()
print("=== 4. 派活：闸门 + 派出去 ===")
pet._feishu_session("oc_1")["mode"] = "harness"   # 上一节末尾为了测数字把它切回闲聊了
pet.feishu_cfg = lambda: {"enabled": True, "app_id": "cli_x", "app_secret": "s",
                          "domain": "feishu", "allow_harness": False,
                          "allow_sender": ""}
out = feed("把测试跑一遍")
ck("没开派活闸时明确拒绝", "开关" in out)
ck("被拒时没有留下 pending（下一个结果不会回错会话）",
   not (pet._feishu_pending or {}).get("harness"))

# 开闸，并且用假桥替代真的 ACP（别真起子进程）
pet.feishu_cfg = lambda: {"enabled": True, "app_id": "cli_x",
                          "app_secret": "s", "domain": "feishu",
                          "allow_harness": True, "allow_sender": ""}
console_calls = []


class FakeConsole:
    def note(self, *a, **k):
        console_calls.append("note")

    def set_busy(self, v):
        console_calls.append("busy=%s" % v)

    def set_status(self, *a):
        pass

    def fail(self, *a):
        console_calls.append("fail")


class FakeHarnessBridge:
    """假桥。**必须模拟"start() 之后就忙起来"** —— 真桥就是这样：
    桌宠靠 busy() 判断"活有没有真的派出去"，不模拟的话会误判成没派成功。"""

    def __init__(self):
        self.started = []
        self._busy = False

    def busy(self):
        return self._busy

    def start(self, *a):
        self.started.append(a)
        self._busy = True
        return True


fh = FakeHarnessBridge()
pet._harness = fh                      # _harness_busy() 读的是这个字段
pet._harness_console = lambda: FakeConsole()
pet._harness_bridge = lambda: fh
fake.clear()
out = feed("统计一下代码行数")
ck("先回一句「接单」（3 秒内必须回，这是硬约束）", "干活" in out)
ck("真的把任务交给了 Harness 桥（带了 text/exe/cli/cwd/group/sid）",
   len(fh.started) == 1 and fh.started[0][0] == "统计一下代码行数"
   and len(fh.started[0]) == 6)
ck("主人那句进了工作台历史", "note" in console_calls)
ck("派活时记下了 pending 且带进度计数",
   bool((pet._feishu_pending or {}).get("harness"))
   and pet._feishu_pending["harness"].get("tools") == 0)

print()
print("=== 5. 派活进度与收尾 ===")
for _ in range(10):
    # 必须显式 for_kind="harness"：v61 起 tool 流水**只属于派活那一舱**，
    # 默认的 "chat" 会被直接忽略（这正是"闲聊不会因为派活刷屏"的实现）。
    pet._feishu_publish("tool", "读文件", for_kind="harness")
ck("跑过 10 个动作后报了一次进度", any("还在干" in r[2] for r in fake.replies))
ck("进度条数有上限（不会每步都刷）", len(fake.replies) <= 2)
fake.clear()
pet._feishu_publish("done", "跑完了，42 个测试全过", for_kind="harness")
ck("干完把正文回过去", fake.last() == "跑完了，42 个测试全过")
ck("收尾后 pending 清空", not (pet._feishu_pending or {}).get("harness"))

# 失败路径（先把假桥的"忙"放掉，模拟上一轮已经收尾）
fh._busy = False
feed("再跑一次")
fake.clear()
pet._feishu_publish("err", "没连上模型", for_kind="harness")
ck("失败也回一句人话", "岔子" in fake.last() and "没连上模型" in fake.last())
ck("失败后 pending 也清空", not (pet._feishu_pending or {}).get("harness"))

print()
print("=== 6. 白名单 ===")
pet.feishu_cfg = lambda: {"enabled": True, "app_id": "cli_x", "app_secret": "s",
                          "domain": "feishu", "allow_harness": True,
                          "allow_sender": "ou_boss"}
ck("白名单外的发送者被安静忽略（一个字都不回）",
   feed("帮我删库", sender="ou_stranger") == "" and fake.replies == [])
ck("白名单里的人照常", feed("/status", sender="ou_boss") != "")

print()
print("=== 7. 多会话状态互不干扰 ===")
# 注意：上面刚把白名单设成 ou_boss，这里必须用同一个身份，否则消息会被静默丢掉
feed("/harness", chat="oc_A", sender="ou_boss")
feed("/chat", chat="oc_B", sender="ou_boss")
stA = pet._feishu_session("oc_A")
stB = pet._feishu_session("oc_B")
ck("A 会话在工作模式", stA["mode"] == "harness")
ck("B 会话在闲聊模式（互不影响）", stB["mode"] == "chat")

print()
print("=== 8. 状态行：把「未启动」说成能照着做的话 ===")
# 这一段是主人问「未启动是什么意思」之后补的：
# 原来那一行只显示 status()["label"]，三个字没有任何信息量，
# 而且"保存/测试的结果"也写在同一行上、每 1.5 秒被刷新冲掉 ——
# 报错活不过 1.5 秒。现在状态归状态、反馈归反馈，各占一行。
pet._feishu = None
line = pet.feishu_status_line(enabled=False)
ck("没启用时，直接告诉他去勾哪个、点哪个按钮",
   "未启动" in line and "启用飞书接入" in line and "保存并应用" in line)
line = pet.feishu_status_line(enabled=True)
ck("启用了却没连起来时，让他重试并去看日志",
   "未启动" in line and "重试" in line and "日志" in line)


class FakeStatusBridge:
    """只负责 status() 的假桥。"""

    def __init__(self, **st):
        self._st = st

    def status(self):
        return self._st


pet._feishu = FakeStatusBridge(state="online", label="在线", online=True,
                               error="", uptime=120.0, last_event_ago=2.0)
line = pet.feishu_status_line()
ck("在线时带上「已连多久」", line.startswith("连接状态：在线") and "2 分钟" in line)

pet._feishu = FakeStatusBridge(state="offline", label="断线，正在重连…",
                               online=False, error="ConnectionClosed: gone",
                               uptime=9.0, last_event_ago=-1.0)
line = pet.feishu_status_line()
ck("断线时把真实错误一并摆出来（不再只给个标签）",
   "断线" in line and "ConnectionClosed" in line)

pet._feishu = FakeStatusBridge(state="connecting", label="连接中…",
                               online=False, error="", uptime=1.0,
                               last_event_ago=-1.0)
connecting_line = pet.feishu_status_line()
ck("连接中也有话说，且不硬凑「已连」",
   "连接中" in connecting_line and "已连" not in connecting_line)

print()
print("=== 9. 菜单入口 ===")
texts = [a.text() for a in pet.build_menu().actions()]
# 2026-10-11 起主人要求：飞书 / 远程 / API key 这三项**从菜单撤掉**，
# 全部收进「设置…」，菜单只留动作。所以这里改成验设置入口在、
# 而且那三项确实不再单独占菜单位（防止谁又加回来）。
ck("菜单里有「设置…」", any(t.startswith("设置") for t in texts))
ck("飞书/远程/钥匙不再单独占菜单位",
   not any(t.startswith(("飞书接入", "远程控制", "更换 API key", "设置 API key"))
           for t in texts))

shutil.rmtree(tmp, ignore_errors=True)
print()
print("结论:", "通过" if all(checks) else "有失败项（%d/%d）"
      % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
