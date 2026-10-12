# -*- coding: utf-8 -*-
"""说话：台词池、气泡、聊天管线、联网搜索、余额查询。"""
import argparse
import base64
import codecs
import html
import json
import math
import os
import queue
import random
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_bridges import (build_balance_query, build_chat, build_search)
from pet_net import (resolve_endpoint, search_query, wants_search)
from pet_panels import build_balance_panel, build_sound_dialog
from pet_ledger import (beijing_day as _bj_day, day_summary as _day_summary,
                        load as _ledger_load, observe as _ledger_observe,
                        peak_state as _peak_state, recent_days as _recent_days,
                        reconcile as _ledger_reconcile, save as _ledger_save)
from pet_sound import configure as sound_configure
from pet_sound import play as play_sound
from pet_sound import preview as sound_preview
from pet_ui import money_text as _money
from pet_ui import (COLOR_BLUE, COLOR_DEEP, DANGER, MUTED, OK_GREEN,
                    card_style, ghost_btn, hint, label, primary_btn, section)
from pet_store import (append_chat_log, append_memory, data_dir, load_config,
                       save_api_key, save_config_values)
from pet_tags import (bubble_preview, hide_mood_fragment, strip_reply_tags)
from pet_time import (cancel_from_user, format_alarm_time, format_seconds, parse_duration, reminder_from_user)
from pet_windows import (build_chat_window, build_clock_window, chat_font)


class ChatMixin:
    def say(self, text, hold=BUBBLE_HOLD, max_w=None):
        """冒一句台词。没开台词功能或气泡没造出来时，静默跳过。

        **桌宠隐藏时一律不说** —— 气泡是独立窗口，不挡住的话，隐藏后
        待机闲话还会继续飘在屏幕上（主人实测报的 bug）。守卫放在这里
        是根本解法：所有台词路径（闲话/点击/拖拽/回归语）一次覆盖，
        不用分别去堵。提醒（fire_reminder）会先把桌宠 show 出来再说话，
        所以不受影响。

        max_w 不透传的话，聊天回复那条更宽的气泡路径一走就 TypeError
        （实测踩过：参数只加在气泡上，忘了这一层）。
        默认一律回落到常规宽度，免得聊天用过的宽气泡把后面的台词也带宽。
        """
        if not self.do_talk or self.bubble is None or not text:
            return
        if not self.isVisible():
            return
        # 主人主动"隐藏"进托盘时，别再用气泡把她冒出来 —— 隐藏就该是隐藏。
        # 唯一的例外是提醒（fire_reminder / _announce_alarm 会先清掉这个标记）。
        if getattr(self, "_user_hidden", False):
            return
        # 气泡的置顶状态**跟着桌宠走**（主人 2026-07-11 的要求）：
        # 桌宠置顶 → 气泡也置顶；桌宠不置顶 → 气泡也不置顶（别的窗口能盖住它）。
        self.sync_bubble_topmost()
        # 不做避让（见 pet_windows.build_bubble 里那段层级说明）：
        # 气泡不再是永远置顶，别的程序窗口会正常盖住它。
        self.bubble.say(text, self.char_rect(),
                        self.screen().availableGeometry(), hold,
                        max_w or BUBBLE_MAX_W)

    def sync_bubble_topmost(self):
        """让气泡的置顶状态跟上桌宠当前的置顶状态。

        主人 2026-07-11 的要求：设置里勾了"一直最上层"，气泡也要在最上层；
        没勾，气泡就不该盖住别的程序窗口（窗口才是上层）。

        只在**状态真的不同**时才动窗口标志 —— 改标志会让窗口重建（闪一下），
        每次说话都改一遍会一直闪。
        """
        bubble = self.bubble
        if bubble is None:
            return
        try:
            want = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
            have = bool(bubble.windowFlags() & Qt.WindowStaysOnTopHint)
            if want == have:
                return
            flags = bubble.windowFlags()
            if want:
                flags |= Qt.WindowStaysOnTopHint
            else:
                flags &= ~Qt.WindowStaysOnTopHint
            was_visible = bubble.isVisible()
            bubble.setWindowFlags(flags)
            if was_visible:
                bubble.show()       # 改完标志要重新 show 才生效
                bubble.raise_()
        except Exception as err:
            print("[气泡] 同步置顶状态失败：%s: %s" % (type(err).__name__, err),
                  file=sys.stderr)

    def next_talk_gap(self):
        if self.talk_gap:
            return int(self.talk_gap * 1000)
        return int(random.uniform(TALK_GAP_MIN, TALK_GAP_MAX) * 1000)

    def idle_talk(self):
        """待机闲话。

        睡着时不插话（被吵醒很奇怪），拖拽中也不说（手正忙着），
        桌宠藏进托盘时更不说（人都不在屏幕上，冒台词就是幽灵）。
        但**定时器无论如何都要续上** —— 和眨眼踩过的坑同一个：
        一旦某条分支提前 return 忘了续期，闲话就永久停掉了。
        """
        if self.do_talk:
            if (self.isVisible() and not self.sleeping()
                    and not self._dragging and not self.chatting()):
                self.say(random.choice(LINES["idle"]))
            self.talk_timer.start(self.next_talk_gap())

    def line_for_current(self):
        """按**当前正画着哪张图**挑台词，而不是另存一个"现在是什么表情"的变量。

        表情各自有专属台词；没配到就落回通用池。
        """
        for i, pixmap in enumerate(self.expressions):
            if pixmap is self.pixmap:
                pool = EXPR_LINES.get(self.expr_names[i])
                return random.choice(pool or LINES["poke"])
        return random.choice(LINES["poke"])

    def talk_once(self):
        """右键菜单「说句话」：从闲聊池里随便挑一句。"""
        self.say(random.choice(LINES["chat"]))

    def chatting(self):
        """正在跟模型说话，或刚说完还挂在桌面上 —— 这时候不插待机闲话。

        状态从画面/客户端推出来，没有另存标志位：
        ① 请求在飞 → 看客户端；
        ② 她的话还挂在气泡上（要主人点一下或拖一下才收）→
           看**气泡是否还开着、装的正是那句**。
        第②条不能省 —— 少了它，待机闲话会把刚收到的回复顶掉（实测踩过）。
        """
        if self.chat is not None and self.chat.busy():
            return True
        return (bool(self._chat_last) and self.bubble is not None
                and self.bubble.isVisible()
                and self.bubble.text() == self._chat_last)

    def chat_ready(self):
        return self.chat is not None and self.chat.ready()

    def _ensure_chat_clients(self, config=None):
        """运行时把对话客户端补建出来并接好信号。

        启动时没配 key 的话，main() 里压根不建 chat/search ——
        所以第一次在界面里填 key 时要在这里补上。
        信号连接和 __init__ 里那两段是**同一套**，别改出两份分叉。
        """
        if self.chat is not None:
            return
        cfg = config if config is not None else load_config()
        self.chat = build_chat(None, cfg)
        self.chat.memory_context = self._memory_context    # 要求 4：带记忆上场
        self.chat.chunk.connect(self._on_chat_chunk)
        self.chat.done.connect(self._on_chat_done)
        self.chat.failed.connect(self._on_chat_failed)
        if str(cfg.get("search")) == "bing":
            self.search = build_search(None, cfg)
            self.search.found.connect(self._on_search_found)
            self.search.failed.connect(self._on_search_failed)

    def show_clock(self):
        """右键菜单「显示时钟」：与系统一致的日期时间，右上角叉号可关。"""
        if self.clock_window is None:
            self.clock_window = build_clock_window(self)
        self.clock_window.open_window(self.frameGeometry(),
                                      self.screen().availableGeometry())

    def set_api_key(self, key, path=None):
        """界面里换 key：落盘 + 热生效，返回是否成功。

        热生效的底气在 ChatClient.ask() —— Authorization 头是**每次请求
        现从 cfg 里取的**，所以改 cfg 就行，不用重建客户端、不用重启。
        之前没配过 key（客户端没建）就现建一套。
        """
        key = (key or "").strip()
        if not key:
            return False
        target = path or self._config_path     # 写回启动时加载的那份
        if not save_api_key(key, target):
            # 落盘失败（目录只读 / 文件被占用 / 盘满）：**不能**当成功。
            # 以前这里裸写且不看返回值，异常会从 Qt 槽里抛出来。
            return False
        if self.chat is None:
            self._ensure_chat_clients(load_config(target))
        else:
            self.chat.cfg["api_key"] = key
        return True

    def edit_api_key(self):
        """右键菜单「设置 / 更换 API key」：打开设置窗口并切到聊天那一页。"""
        return self.open_settings("chat")

    def _api_key_page(self):
        """「聊天与钥匙」页：现在用哪个接口 + 换钥匙（打码输入，保存即生效）。

        内容是原来那个独立小对话框搬过来的（打码输入、保存、回车即存、
        「算了」= 放弃改动），只是换成设置窗口里的一页：接口信息一张卡片、
        钥匙一张卡片。这样"我的钥匙配到哪去了"和"换一把"能在同一屏上看完。
        """
        from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLineEdit,
                                       QVBoxLayout, QWidget)
        from pet_ui import page_bg_style

        page = QWidget()
        page_bg_style(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        def card():
            box = QFrame()
            box.setObjectName("card")
            box.setStyleSheet(card_style(padding=12))
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(12, 10, 12, 10)
            box_lay.setSpacing(6)
            return box, box_lay

        # ---- 现在用哪个接口 ----
        # 原来只有"填 key"，主人看不到 key 到底配到哪个 base_url / 模型上，
        # 出问题只能翻 json；顺手把它摆出来，配错了当场能看出来。
        conf = load_config(self._config_path)
        base, model, native = resolve_endpoint(conf)
        info_card, info_lay = card()
        info_lay.addWidget(section("接口"))
        info_lay.addWidget(label(base or "（还没配接口地址）", 12))
        online = "开" if (native or str(conf.get("search")) == "bing") else "关"
        info_lay.addWidget(hint("模型 %s · 联网 %s · provider=%s"
                                % (model or "—", online,
                                   conf.get("provider") or "—")))
        info_lay.addWidget(hint("想换接口地址或模型，直接改配置里的 "
                                "base_url / model / provider。"))
        lay.addWidget(info_card)

        # ---- 钥匙 ----
        configured = self.chat is not None and bool(self.chat.cfg.get("api_key"))
        key_card, key_lay = card()
        key_lay.addWidget(section("API key"))
        key_lay.addWidget(hint("已配好钥匙，随时能换。" if configured
                               else "还没有钥匙，填一个我就能说话啦。"
                                    "保存后立刻生效，不用重启我。"))

        edit = QLineEdit(self.chat.cfg.get("api_key", "") if configured else "")
        edit.setFont(chat_font(12))
        edit.setPlaceholderText("sk-…")
        # 输入时打码：主人在录屏/旁边有人时填 key，不该整串露在屏幕上
        edit.setEchoMode(QLineEdit.Password)
        edit.setStyleSheet(
            "QLineEdit{background:#FFFFFF;border:1.5px solid %s;"
            "border-radius:10px;padding:7px 12px;color:%s;}"
            "QLineEdit:focus{border:1.5px solid %s;}"
            % (COLOR_BLUE, COLOR_DEEP, COLOR_DEEP))
        key_lay.addWidget(edit)
        key_lay.addWidget(hint("钥匙存在我旁边的 %s 里，用 Windows 账户加密"
                               "（DPAPI），不上传、不进日志。" % CONFIG_NAME))
        note = hint("")
        key_lay.addWidget(note)

        btns = QHBoxLayout()
        cancel = ghost_btn("算了")
        save = primary_btn("保存")
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(save)
        key_lay.addLayout(btns)
        lay.addWidget(key_card)
        lay.addStretch(1)

        def do_save():
            if self.set_api_key(edit.text()):
                note.setStyleSheet("color:%s;background:transparent;" % OK_GREEN)
                note.setText("钥匙收好啦，现在就能跟我说话了。")
                self.say("钥匙收好啦，现在就能跟我说话了～")
            else:
                note.setStyleSheet("color:%s;background:transparent;" % DANGER)
                note.setText("钥匙还没填进去：要么是空的，要么配置写不进去"
                             "（目录只读或被占用？）")

        def do_cancel():
            # 「算了」= 放弃这次改动，把输入框拉回当前生效的那把钥匙
            saved = self.chat.cfg.get("api_key", "") if self.chat is not None else ""
            edit.setText(saved)
            note.setStyleSheet("color:%s;background:transparent;" % MUTED)
            note.setText("没改，还是原来那把。")

        save.clicked.connect(do_save)
        cancel.clicked.connect(do_cancel)
        edit.returnPressed.connect(do_save)
        return page

    def open_chat_window(self):
        """双击鲸鲸（或右键菜单）打开对话窗口。

        **Harness 模式下开的是工作台**（和「派活给 Harness…」同一个窗口）：
        那时候说的话是任务，不是闲聊，界面上得看得出来。
        不分流的话就是一个错位 —— 窗口长得像聊天框，发出去却是派活
        （start_chat 已被重定向到 _harness_send），主人会以为她在陪聊。

        分流必须放在 chat_ready() **之前**：派活走的是 Harness 的账号凭证，
        跟聊天那把 API key 毫无关系，没配 key 也该能派活。
        """
        if self._harness_mode:
            self._harness_console().open_window()
            return
        if not self.chat_ready():
            # 第一次配置不用翻 json —— 直接把钥匙框递到主人面前
            self.say("我还没拿到钥匙……先给我一把吧？")
            self.edit_api_key()
            return
        if self.chat_window is None:
            self._shell_window()          # 聊天与派活共用一个窗口（pet_shell）
        self.chat_window.open_window(self.char_rect(),
                                     self.screen().availableGeometry())

    def dismiss_chat_bubble(self):
        """把她留在桌面上的那句话收起来。点一下或拖一下都算。"""
        self._chat_last = ""
        if self.bubble is not None:
            self.bubble.hide()

    def refresh_balance_quiet(self):
        """静默取一次余额：**只记账、只看预警**，不冒泡念数字。

        为什么要有静默版：「今日已用」是靠余额观测推出来的，
        不常观测就只是一段段的缺口（挂件那边 60 秒自动刷一次，同一个道理）。
        冒泡报数留给主人手动点 —— 自动刷还每次冒一句会很吵。
        """
        if not self.chat_ready():
            return False
        self._balance_quiet = True
        try:
            return self.show_balance()
        finally:
            self._balance_quiet = False

    def start_balance_watch(self, minutes=BALANCE_REFRESH_MIN):
        """按配置起"自动记一笔余额"的定时器（0 或负数 = 关）。返回是否起了。"""
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = float(BALANCE_REFRESH_MIN)
        self._balance_timer.stop()
        if minutes <= 0:
            return False
        self._balance_timer.setInterval(int(minutes * 60000))
        self._balance_timer.start()
        return True

    def show_balance(self):
        """查余额：只读接口，不花 token。**顺手记一笔账**（越查越准）。

        请求走 QNetworkAccessManager 异步，绝不卡界面；
        查成功之后由 `_on_balance_snapshot` 记账、说多行气泡、刷面板、看预警。
        只有 DeepSeek 公开了余额端点 —— 其他厂商如实告知，不假装查。
        """
        if not self.chat_ready():
            if not self._balance_quiet:
                self.say("还没配钥匙呢，查不了钱包……")
            return False
        if "deepseek" not in self.chat.base_url.lower():
            if not self._balance_quiet:
                self.say("这家厂商没公开查余额的门，只有 DeepSeek 家能查……")
            return False
        if self._balance is None:
            self._balance = build_balance_query(self)
            self._balance.done.connect(self._on_balance)
            self._balance.snapshot.connect(self._on_balance_snapshot)
        if self._balance.busy():
            return False
        started = self._balance.query(self.chat.cfg.get("api_key", ""),
                                      self.chat.base_url,
                                      float(self.chat.cfg.get("timeout", 30)))
        if started and not self._balance_quiet:
            self.say("（让鲸鲸数数饭钱……）", hold=0)
        return bool(started)

    def _on_balance(self, ok, text):
        """只在**失败**时说话（静默刷新时连失败也不吵）。"""
        if not ok and not self._balance_quiet:
            self.say(text)

    # ---------------------------------------------------------- 记账
    def _balance_ledger(self):
        """现读记账本 —— 主人在面板里刚校正过，这里要立刻看到。"""
        return _ledger_load(self._config_path)

    def _on_balance_snapshot(self, snap):
        """一次成功的余额观测：记账 → 气泡 → 面板 → 预警。"""
        if not snap:
            return
        self._last_balance = dict(snap)
        summary = None
        try:
            ledger = self._balance_ledger()
            summary = _ledger_observe(ledger, snap.get("total", 0),
                                      at=snap.get("at"),
                                      currency=snap.get("currency"))
            if not _ledger_save(self._config_path, ledger):
                print("[记账] 记账本写不进去，这次观测没落盘", file=sys.stderr)
        except Exception as err:                      # 记账坏了也不能影响说话
            print("[记账] 记录失败：%s: %s" % (type(err).__name__, err),
                  file=sys.stderr)
        warning = self._balance_warning(snap, summary)   # 只算一次（里面会放提示音）
        if self._balance_quiet:
            # 自动观测：不念数字，但**预警该说就说**（不然余额预警就废了）
            if warning:
                self.say(warning)
        else:
            self.say(self._balance_bubble_text(snap, summary, warning))
        if self._balance_panel is not None and self._balance_panel.isVisible():
            self._balance_panel.refresh()

    def _balance_bubble_text(self, snap, summary, warning=None):
        """余额气泡：**多行**，余额 / 今日已用 / 峰谷一眼看完。

        （借挂件那套"一个泡泡里塞几个模块"的做法：与其让主人点开面板，
          不如把最常用的三个数字直接摆在气泡里。）
        warning 由调用方传进来：算预警会放提示音，绝不能算两次。
        """
        cur = snap.get("currency") or "CNY"
        total = snap.get("total")
        # 排版（主人 2026-10-11 定的口径）：**不用 emoji**（"太土"），靠行首对齐
        # 与缩进来分区；每行都用「标签 值」的同一句式，扫一眼就能对上号。
        lines = ["饭钱　%s" % _money(total, cur)]
        granted = float(snap.get("granted") or 0)
        if granted > 0:
            lines.append("赠送　%s" % _money(granted, cur))
        if summary:
            tag = "（部分数据）" if summary.get("partial") else ""
            if summary.get("corrected"):
                tag += "（已校正）"
            lines.append("今日已用　%s%s"
                         % (_money(summary.get("amount"), cur), tag))
        pk = _peak_state()
        # 谷价时把原因也说出来（"周末全天谷价"这种比干说一个"谷价"有用得多），
        # 但**压成一行**：原来那句"回到峰价 还有 22 小时 50 分"有 28 个字，
        # 在 224px 的气泡里要折两行 —— 用 HH:MM 的紧凑写法就能一行放下
        # （remain_sec 本来就是秒数，没必要非说成"小时/分"）。
        secs = max(0, int(pk.get("remain_sec") or 0))
        when = "%d:%02d" % (secs // 3600, (secs % 3600) // 60)
        lines.append("峰谷　%s · %s %s"
                     % (pk["label"].replace("时段", ""), pk["next_label"], when))
        if warning is None:
            warning = self._balance_warning(snap, summary)
        if warning:
            lines.append(warning)
        return "\n".join(lines)

    def _balance_warning(self, snap, summary):
        """余额预警 / 今日预算（借挂件的提醒思路；0 = 关，默认关）。"""
        try:
            conf = load_config(self._config_path)
            below = float(conf.get("balance_warn_below") or 0)
            above = float(conf.get("budget_warn_above") or 0)
        except (TypeError, ValueError, OSError):
            return ""
        cur = snap.get("currency") or "CNY"
        total = float(snap.get("total") or 0)
        if below > 0 and total < below:
            play_sound("remind")
            return "⚠️ 饭钱低于你设的警戒线 %s 了" % _money(below, cur)
        if above > 0 and summary and float(summary.get("amount") or 0) > above:
            play_sound("remind")
            return "⚠️ 今天已用超过预算 %s" % _money(above, cur)
        return ""

    def open_balance_panel(self):
        """右键菜单「小鲸鱼记账」：余额 / 今日已用 / 峰谷 / 逐日明细。"""
        if self._balance_panel is None:
            self._balance_panel = build_balance_panel(
                self, self._balance_state, self.show_balance,
                self._reconcile_today, self._save_balance_warn)
        self._balance_panel.refresh()
        self._balance_panel.show()
        self._balance_panel.raise_()
        play_sound("press")

    def _balance_state(self):
        """面板需要的全部数据（记账逻辑不放进面板，面板只管画）。"""
        ledger = self._balance_ledger()
        today = _day_summary(ledger, _bj_day())
        snap = dict(self._last_balance) if self._last_balance else {}
        if not snap and today:
            # 没查过余额（比如刚开机）：用记账本最后一行当展示值，
            # 充值与赠金拆不出来，就都算充值 —— 面板会写"还没查到"
            snap = {"total": today["current"], "currency": today["currency"],
                    "granted": 0.0, "recharge": today["current"],
                    "at": today["last_at"], "from_ledger": True}
        if snap and not snap.get("from_ledger"):
            granted = float(snap.get("granted") or 0)
            snap["recharge"] = max(0.0, float(snap.get("total") or 0) - granted)
        note = ""
        if self._balance is not None and self._balance.busy():
            note = "正在查余额…"
        elif snap.get("from_ledger"):
            note = "这是记账本里的最后一个余额，点「刷新余额」取最新。"
        # 把"多久自动记一笔"写在面板上：记账准不准全看这个间隔，
        # 主人得能一眼看见它现在是几分钟一次（也才知道去哪改）
        try:
            minutes = float(load_config(self._config_path).get(
                "balance_refresh_min", BALANCE_REFRESH_MIN))
        except (TypeError, ValueError, OSError):
            minutes = float(BALANCE_REFRESH_MIN)
        auto = ("自动观测：每 %g 分钟一次" % minutes if minutes > 0
                else "自动观测：已关（只有点「刷新余额」时才记）")
        note = (note + " · " + auto) if note else auto
        # 两条预警线（余额低于 / 今日超过），面板上能直接改
        try:
            conf = load_config(self._config_path)
            warn_below = float(conf.get("balance_warn_below") or 0)
            warn_above = float(conf.get("budget_warn_above") or 0)
        except (TypeError, ValueError, OSError):
            warn_below = warn_above = 0.0
        return {"balance": snap or None, "today": today,
                "recent": _recent_days(ledger, BALANCE_RECENT_DAYS),
                "peak": _peak_state(), "note": note,
                "warn": {"below": warn_below, "above": warn_above}}

    def _save_balance_warn(self, below, above):
        """面板上的两条预警线写回配置（0 = 不提醒）。"""
        return bool(save_config_values(
            {"balance_warn_below": float(below or 0),
             "budget_warn_above": float(above or 0)},
            self._config_path))

    def _reconcile_today(self, credits, other_debits):
        """面板的「余额校正」：重算今天的消费；算不通返回 None。"""
        ledger = self._balance_ledger()
        summary = _ledger_reconcile(ledger, _bj_day(), credits, other_debits)
        if summary is None:
            return None
        if not _ledger_save(self._config_path, ledger):
            return None
        return summary

    # ---------------------------------------------------------- 音效设置
    def open_sound_dialog(self):
        if self._sound_dialog is None:
            self._sound_dialog = build_sound_dialog(
                self, self._sound_cfg, self._save_sound_cfg,
                sound_preview, self._apply_sound_cfg)
        self._sound_dialog.reload()
        self._sound_dialog.show()
        self._sound_dialog.raise_()

    def _sound_cfg(self):
        conf = load_config(self._config_path)
        events = conf.get("sound_events")
        try:
            volume = int(conf.get("sound_volume", 70))
        except (TypeError, ValueError):
            volume = 70
        return {"enabled": bool(conf.get("sound_enabled", True)),
                "volume": max(0, min(100, volume)),
                "events": dict(events) if isinstance(events, dict) else {}}

    def _sound_summary(self):
        """菜单那行显示的摘要：开没开、多大声（挂件的"入口行"做法）。"""
        try:
            cfg = self._sound_cfg()
        except Exception:
            return "读不到配置"
        if not cfg.get("enabled"):
            return "已关闭"
        return "开 · %d%%" % cfg.get("volume", 70)

    def _apply_sound_cfg(self, cfg):
        """立刻生效（试听要用当前音量，所以不等保存）。"""
        sound_configure(cfg.get("enabled"), cfg.get("volume"),
                        cfg.get("events"),
                        data_dir(self._config_path) / SOUND_CACHE_DIRNAME)

    def _save_sound_cfg(self, cfg):
        ok = bool(save_config_values(
            {"sound_enabled": bool(cfg.get("enabled")),
             "sound_volume": int(cfg.get("volume", 70)),
             "sound_events": dict(cfg.get("events") or {})},
            self._config_path))
        if ok:
            self._apply_sound_cfg(cfg)
        return ok

    def chat_blocked(self):
        """现在不能闲聊吗？返回 (一句人话的原因, 要不要在聊天框里留痕)。

        抽成一处是因为飞书/远程也要给主人**同一句解释**。各写一套的话，
        过两天两边的说法就分叉了 —— 改一个字都得想起改两处。
        """
        if not self.chat_ready():          # 没配 key 时别让 --ask 之类把她打崩
            return "我还不会说话……主人先把我那个 key 填上吧。", False
        if self.deep_sleep():
            # 沉睡（精力 <= 60）：问也问不出东西，只含糊应一声。
            # 三条边界都要守住：
            # ① **不叫醒她**（主人的规则：任何手段都叫不醒）；
            # ② **不花 token**（压根不发给模型）；
            # ③ **不扣精力** —— 这不算"一问一答"。否则越困越被扣，
            #    就成了"刚睡着就被叫起来扣血"的死循环。
            # 注意：**到点的提醒不受影响** —— fire_reminder / _announce_alarm
            # 走的是另一条路径，根本不经过这里（主人明确要求睡觉也要能提醒）。
            return random.choice(LINES["deep_sleep_reply"]), True
        return None, False

    def start_chat(self, text, as_chat=False):
        """发一句话给模型。需要联网查的话，先搜索再问。

        as_chat=True = "无论如何按闲聊走"。飞书那边有自己的模式判断，
        不能被桌面工作台的状态劫成派活 —— 否则主人在飞书上想聊两句，
        家里那台电脑突然开始改文件。反过来，桌面双击进来的仍然是原行为。
        """
        if self._harness_mode and not as_chat:
            # Harness 模式：这句话不是聊天，是**派活** —— 真的会改文件 / 跑命令。
            # 分流放在最上面，别让下面的"配 key"守卫把它拦住（派活不需要那个 key）。
            return self._harness_send(text)
        reason, echo = self.chat_blocked()
        if reason:
            if echo:
                self.dismiss_chat_bubble()
                if self.chat_window is not None:
                    self.chat_window.add_me(text)
                    self.chat_window.add_her(reason)
            self.say(reason)
            return
        if self.sleeping():
            # 主人文档要求 3：睡觉只能被右键「叫醒」或**对话框里说话**结束。
            # 主人在聊天窗口开口 = 叫醒，静默醒（回复本身就是她说的话）
            self.wake_up(quiet=True)
        self._last_question = text         # 提醒的本地兜底/纠偏要用原话
        append_chat_log(self._config_path, "主人", text)     # 要求 4：对话日志
        self.dismiss_chat_bubble()
        self._chat_buffer = ""
        if self.chat_window is not None:
            self.chat_window.add_me(text)
            self.chat_window.begin_reply()
            self.chat_window.set_busy(True)
        self.say("（让鲸鲸想想……）", hold=0)     # hold=0 = 一直开着，等回复

        mode = str(self.chat.cfg.get("search", "off"))
        if self.search is not None and mode == "bing" and wants_search(text):
            self._pending_question = text
            self.say("（等我去翻一下网页……）", hold=0)
            if not self.search.search(search_query(text)):
                self._pending_question = None
                self._send_to_model(text, None)
        else:
            self._send_to_model(text, None)

    def _send_to_model(self, text, digest):
        if self.chat is None or not self.chat.ask(text, digest):
            self._fail_reply("我这儿正忙着呢，等我说完这句。")

    def _on_search_found(self, digest):
        question, self._pending_question = self._pending_question, None
        if not question:
            return
        if not digest:
            # 搜不到不算错误，但也不能假装搜过了 —— 说一句再凭印象答
            self.say("（网上没翻到啥，我抬头想一下……）", hold=0)
        self._send_to_model(question, digest or None)

    def _on_search_failed(self, message):
        question, self._pending_question = self._pending_question, None
        self.say("（%s 我先凭印象说……）" % message, hold=0)
        if question:
            self._send_to_model(question, None)

    def _on_chat_chunk(self, piece):
        """流式增量：每来一小块就重画一次气泡，天然的打字机效果。

        走的是 bubble.render()（不是 say()）—— 它不碰收起定时器、
        尺寸没变也不动窗口，所以每秒几十次也不会闪。

        **只处理闲聊那条流**：派活的增量走 _on_harness_chunk，两条流
        各接各的槽（以前共用一个槽、靠 _harness_mode 分派，模式一切
        正文就串台）。
        """
        self._chat_buffer += piece
        self._remote_publish("chunk", piece)   # 远程页面的打字机
        shown = hide_mood_fragment(self._chat_buffer)
        if self.chat_window is not None:
            self.chat_window.stream_full(shown)
        if self.bubble is not None:
            self.bubble.render(bubble_preview(shown),
                               self.char_rect(),
                               self.screen().availableGeometry(),
                               max_w=BUBBLE_MAX_W_CHAT)

    def _on_chat_done(self, answer):
        self._chat_buffer = ""
        # 一问一答：精力 -1（主人的规则）。放在"真拿到回复"这一侧，
        # 请求失败不算 —— 失败了主人也没问出东西，不该还扣她的力气。
        self.spend_energy(ENERGY_PER_CHAT)
        self.update_bars()
        if self.chat_window is not None:
            self.chat_window.end_reply()
            self.chat_window.set_busy(False)
        # 统一剥掉末尾的所有标记（mood / memory / remind，顺序无关）
        text, mood, memory_note, reminder, alarm, cancel = \
            strip_reply_tags(answer)
        self._remote_publish("done", text)     # 远程页面：流式收尾成整段
        self._feishu_publish("done", text)     # 飞书那边：等这条等很久了
        if memory_note:
            # 要求 4：模型判定为长期内容的，写进长期记忆日志（只增不删）
            append_memory(self._config_path, memory_note)
            if self.chat is not None:
                # 立刻进入她自己的记忆 —— 不用等下次重启
                extra = "- %s（%s）" % (memory_note,
                                       datetime.now().strftime(TIME_FMT))
                if self.chat.memory_context:
                    self.chat.memory_context += "\n" + extra
                else:
                    self.chat.memory_context = (
                        "【长期记忆（主人告诉过你的事，说话时要体现）】\n" + extra)
        if reminder:
            # 主人要的定时提醒：登记一个 QTimer，到点自动弹出提醒。
            # **用户原话优先**：他说"十秒"而模型写成 1 分钟时，以他为准 ——
            # 实测模型会把几十秒进位成 1 分钟（remind:1），主人等 10 秒
            # 什么都等不到。差得离谱（>2 倍或 <半数）就纠正。
            minutes, note = reminder
            seconds = minutes * 60.0
            user_seconds = parse_duration(self._last_question or "")
            if user_seconds and (seconds > user_seconds * 2
                                 or seconds < user_seconds * 0.5):
                seconds, note = user_seconds, note
            if self.add_reminder_seconds(seconds, note):
                # 回执：让主人**立刻看到**提醒设上了、几秒后响，
                # 不用干等着猜（"没反应"的焦虑多半来自没有回执）
                text += "\n（已定好：%s后提醒你「%s」）" % (
                    format_seconds(seconds), note)
        elif self._last_question:
            # 模型没给标记，但主人明显是在要求提醒 → 本地兜底登记
            fallback = reminder_from_user(self._last_question)
            if fallback and self.add_reminder_seconds(*fallback):
                text += "\n（已定好：%s后提醒你「%s」）" % (
                    format_seconds(fallback[0]), fallback[1])
        if alarm:
            # 长期提醒（"十月二十号早上提醒我跑步"）：存进提醒文件 +
            # 装上定时器。**独立文件**，不受对话日志三天清理的影响
            when_dt, note = alarm
            if self.add_alarm(when_dt, note):
                text += "\n（已记下：%s 提醒你「%s」，到期前不会丢）" % (
                    format_alarm_time(when_dt), note)
            else:
                # 登记失败要说清楚（时间过去了 / 排太满），不能静默
                text += "\n（这条没记上：时间已经过了，或者提醒排太满了）"
        if cancel is not None:
            # 主人要取消提醒（模型给了 [cancel:关键词]，**空串 = 全都取消**）。
            # 短期定时器和长期提醒一起删 —— 主人的要求是"必须从日志里删掉"，
            # 不能只是标记失效（那样他去「查看提醒」还看得见，等于没取消）。
            gone = self.cancel_reminders(cancel)
            if gone:
                text += "\n（好，%d 条提醒我给你划掉了）" % gone
            else:
                text += "\n（没找到对得上的提醒，是不是已经删过了？）"
        elif self._last_question:
            # 模型没给标记，但主人明显在要求取消 → 本地兜底。
            # 这里只在**真的删到了**才回话，免得他随口提到"提醒"时
            # 也冒一句"没找到"。
            key = cancel_from_user(self._last_question)
            if key is not None:
                gone = self.cancel_reminders(key)
                if gone:
                    text += "\n（好，%d 条提醒我给你划掉了）" % gone
        append_chat_log(self._config_path, "鲸鲸", text)
        # 顺序不能反：先按心情切画面，气泡才按**最终**的 char_rect 摆位。
        # 反过来的话，睡姿（横躺、视觉顶端差 70px）摆完气泡再换站姿，气泡就飘了。
        self.apply_mood(mood)
        self._show_reply(text)

    def _on_chat_failed(self, message):
        self._chat_buffer = ""
        self._remote_publish("err", message)
        self._feishu_publish("err", message)
        self._fail_reply(message)

    def _show_reply(self, text):
        """把回复挂到气泡上。

        hold=0 表示**不定时收** —— 主人明确要求她的话一直留着，
        要收只能靠 dismiss_chat_bubble()（点击或拖动她）。
        """
        preview = bubble_preview(text) or "嗯……我没什么想说的。"
        self._chat_last = preview
        self.say(preview, hold=0, max_w=BUBBLE_MAX_W_CHAT)

    def _fail_reply(self, message):
        self._chat_last = ""
        self._chat_buffer = ""
        if self.chat_window is not None:
            self.chat_window.fail_reply(message)
            self.chat_window.set_busy(False)
        self.say(message)

    def schedule_ask(self, text, delay=ASK_DELAY):
        """启动后自动问一句（--ask 用）。

        特意做成**方法**而不是直接在 main() 里写 QTimer.singleShot ——
        直接在 main() 里写，一来 main() 里没导入 QTimer 就会 NameError，
        二来自检根本够不着这条路径。踩过一次：打包成 windowed 后异常被吞掉，
        进程照常活着，只是"什么都没发生"，查了半天。
        放进类里，用的是 build_widget 作用域里那个 QTimer，自检也能直接调。
        """
        if text:
            QTimer.singleShot(int(delay * 1000), lambda: self.start_chat(text))

    def clear_chat_display(self):
        """右键菜单「清空聊天框」：**只擦界面**，记忆和上下文原样保留。

        旧版叫「忘掉刚才聊的」还顺带清了对话上下文 —— 主人明确要求改掉：
        清空聊天框 ≠ 忘掉她。长期记忆文件、对话日志、模型上下文、
        记忆注入全都不碰，擦的只是聊天窗口里的字。
        """
        self.dismiss_chat_bubble()
        if self.chat_window is not None:
            self.chat_window.clear()
        self.say("聊天框擦干净啦，主人的事我都好好记着呢。")


