# -*- coding: utf-8 -*-
"""窗口与交互：鼠标、吸附、右键菜单、托盘、提醒闹钟、退出清理。"""
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
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QSystemTrayIcon
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_sound import play as play_sound      # 音效（异步、失败即静音）
from pet_panels import build_alarm_window
from pet_store import (append_alarm, due_alarms, load_alarms, remove_alarm)
from pet_time import (format_alarm_time, note_matches)
from pet_ui import style_menu


class UiMixin:
    def mousePressEvent(self, event):
        # 点她一下就把留在桌面上的那句话收起来（主人的明确要求）
        self.dismiss_chat_bubble()
        if event.button() != Qt.LeftButton:
            return
        play_sound("press")   # 按压音（异步，绝不卡动画）
        self._press_global = event.globalPosition().toPoint()
        self._press_origin = self.frameGeometry().topLeft()
        self._moved = False

    def mouseDoubleClickEvent(self, event):
        """双击打开聊天窗口 —— **除了点在脚边那碗饭上**。

        注意单击的反应会先跑一遍（Qt 的顺序是 press→release→doubleclick→release），
        所以她是先弹一下、换个表情，再开窗口 —— 读起来就是"被戳了两下，抬头看你"，
        比人为去憋一个 400ms 的"也许是双击"延迟要自然得多。

        饭碗那一块必须单独放行：主人想在碗上连点几下喂她，结果第二次点击
        被 Qt 当成双击、聊天窗口弹了出来（实测报的）。这里吃到的那一下
        补上 `feed_bowl()`，手感才和"连点几口"一致。

        ⚠️ 那次双击后面**还跟着一个 release**（同一个"第二下"的收尾）。
        最早的实现没管它，于是碗上双击实际吃 3 口、身上双击会把同一句
        台词说两遍 —— 现在用 _skip_next_release 把它明确吃掉。
        """
        if event.button() == Qt.LeftButton:
            self.dismiss_chat_bubble()
            self._skip_next_release = True
            if self.bowl_hit(event.position()):
                self.feed_bowl()
                event.accept()
                return
            self.open_chat_window()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._press_global is None:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        # 6 像素的抖动容差：手抖一点点不该被判定成"拖拽"
        if not self._moved and delta.manhattanLength() < 6:
            return
        self._moved = True
        self._dragging = True
        self.move(self._press_origin + delta)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._skip_next_release:
            # 这是双击那次"第二下"的收尾 —— dblclick 已经处理过了
            # （喂饭 / 开窗口），不能再来一遍。
            self._skip_next_release = False
            self._press_global = None
            return
        if self._dragging:
            self._dragging = False
            self.home_y = self.y()
            self.snap_to_edge()
            play_sound("release")     # 松开音：拖完放她下来
            # 放下之后有概率嘟囔一句。不做成每次必说 ——
            # 每次都被评论一句会很吵，偶尔冒一句才像活的。
            if random.random() < 0.7:
                self.say(random.choice(LINES["drop"]))
        elif not self._moved:
            if self.bowl_hit(event.position()):
                # 点的是脚边那碗白饭（主人的要求）—— 这一下**只喂饭**，
                # 不弹她、不说话，否则"点饭"和"点她"的反应会叠在一起
                self.feed_bowl()
            elif self.sleeping():
                # 睡觉时左键**点不醒**（主人的明确要求）—— 只嘟囔一句继续睡。
                # 能结束睡眠的只有右键菜单「叫醒」和聊天窗口里开口说话。
                # 精力 <= 60 的"沉睡"连嘟囔都换个更含糊的池子 ——
                # 主人从反应上就能看出"她现在叫不醒"，不用去看能量条。
                self.bounce = 0.5
                self.say(random.choice(
                    LINES["deep_sleep"] if self.deep_sleep()
                    else LINES["sleep_grumble"]))
            else:
                self.bounce = 1.0       # 纯点击：弹一下
                self.flash()
                # 只说话、**不换表情**（主人的明确要求）—— 表情只通过
                # 右键菜单「做个表情」或对话里的心情变化来改变
                self.say(self.line_for_current())
        self._press_global = None

    def snap_to_edge(self, margin=24):
        """贴边：让**角色**（不是窗口）贴住屏幕边。

        ⚠️ 窗口比角色宽一截：左右各留了几十像素的透明边距（给呆毛/尾巴/饭碗的
        活动空间，`canvas_origin()` 里 `x = (窗口宽 - 立绘宽) // 2`）。
        以前拿**窗口**宽度去贴边，画面上她还离屏幕边 43~45px ——
        主人报的"右边自动吸附时离右侧边缘还有一定距离"就是这个。
        所以这里按立绘**实际落点**对齐：窗口该出屏幕就出屏幕（那部分本来就是透明的）。
        """
        screen = self.screen().availableGeometry()
        x, y = self.x(), self.y()
        ix, _iy, iw, _ih = self.canvas_origin()     # 立绘在窗口里的落点
        left_off = ix
        right_off = self.width() - (ix + iw)
        if abs((x + left_off) - screen.left()) < margin:
            x = screen.left() - left_off
        elif abs((x + iw + right_off) - screen.right()) < margin:
            x = screen.right() - iw - right_off
        y = max(screen.top(), min(y, screen.bottom() - self.height() + 1))
        self.move(x, y)
        self.home_y = y

    def place_at(self, x, y):
        self.move(x, y)
        self.home_y = y

    def build_menu(self):
        """造出右键菜单并返回（**不显示**）。

        和"显示"分开是有意的：菜单里每一项都改状态，而 `menu.exec()` 会
        阻塞在事件循环里，自动化测试根本拿不到它。拆成 build_menu() 之后，
        自检可以直接把菜单每一项 `trigger()` 一遍（"深睡时点遍所有菜单项，
        她还能不能睡着"就是这么验的 —— 主人报的那个 bug 正是菜单漏了一条路径）。
        """
        menu = QMenu(self)
        title = menu.addAction("鲸鲸 · %s" % APP_VERSION)
        title.setEnabled(False)
        menu.addSeparator()
        # 分组只靠**顺序 + 分隔线 + 留白**，不用 emoji 也不用图标：
        # 菜单是功能入口，花哨的小图标压不住整屏、还跟桌宠的干净画风打架
        # （主人 2026-10-11 明确要求去掉 emoji）。
        # 没配 key 时不藏起来：灰的说明照摆，旁边给一条**可点**的设置入口 ——
        # 这样第一次配置也不用去翻 json 文件
        if self.chat_ready():
            menu.addAction("和鲸鲸说句话…（双击她也可以）", self.open_chat_window)
            menu.addAction("清空聊天框", self.clear_chat_display)
            menu.addAction("查余额", self.show_balance)
            menu.addAction("小鲸鱼记账…", self.open_balance_panel)
            menu.addAction("查看提醒", self.show_alarms)
            menu.addAction("显示时钟", self.show_clock)
        else:
            hint = menu.addAction("和鲸鲸说句话（还没配钥匙）")
            hint.setEnabled(False)
            # 记账本不需要钥匙就能翻（只有"刷新余额"需要），所以照样给它入口
            menu.addAction("小鲸鱼记账…", self.open_balance_panel)
            menu.addAction("查看提醒", self.show_alarms)
            menu.addAction("显示时钟", self.show_clock)
        menu.addSeparator()
        # 派活走 Harness 的账号凭证，跟 API key 是两回事，所以不放进
        # chat_ready() 的分支 —— 没配 key 也能用。
        #
        # 「任务对话」不再单独占一项：它已经并进工作台（左边那栏就是它）。
        # 「切换对话」也不再需要菜单项：工作台里点左边就切。
        # 「接的是哪段」那行灰字也去掉了 —— 工作台标题栏里已经写着，
        # 菜单上再挂一行反而占地方。
        if HARNESS_AVAILABLE:
            menu.addAction(
                "退出 Harness 模式" if self._harness_mode
                else "派活给 Harness…", self.toggle_harness_mode)
        menu.addSeparator()
        if self.do_expr:
            expr_menu = menu.addMenu("做个表情")
            for name in self.expr_names:
                # 走 set_expression（"名字→图"的查表只有那一处），
                # 不再在这里自己拿 pixmap 直调 show_expression ——
                # 那样会绕过沉睡守卫，点了菜单就把睡着的她弄醒（主人报的）
                expr_menu.addAction(
                    name, lambda n=name: self.set_expression(n))
        # 「去睡觉」要有睡姿图（没有就置灰）。
        if self.sleep_pixmap is not None:
            menu.addAction("叫醒" if self.sleeping() else "去睡觉",
                           self.toggle_sleep)
        else:
            act = menu.addAction("去睡觉", lambda: None)
            act.setEnabled(False)
            act.setToolTip("没有睡姿图")
        menu.addAction("回到待机", self.back_to_idle)
        menu.addSeparator()
        # 音效设置**不再单列**（主人 2026-07-11 要求）：
        # 它已经是「设置…」里的一页，菜单只留动作、配置全归设置。
        # 「设置…」是唯一的配置入口：API key / 飞书 / 远程控制 / 音效 /
        # 桌宠大小 全在这一个窗口里。
        menu.addAction("设置…", self.open_settings)
        # 「隐藏」替代了旧的「退出」—— 退出只留在托盘菜单里：
        # 桌宠常驻，关窗口不该是结束程序的入口（主人明确要求）
        menu.addAction("隐藏", self.hide_into_tray)
        # 统一美术风格：白底 + 鲸鱼蓝描边 + 圆角 12 + 淡蓝选中，
        # 与气泡/聊天框/记账面板同一套（见 pet_ui.menu_style）
        return style_menu(menu)

    def contextMenuEvent(self, event):
        """右键菜单。

        不用再"先把气泡收起来"了：气泡已经不是永远置顶（见 build_bubble 的层级说明），
        Qt 的弹出菜单本来就在普通窗口之上，会自然盖住气泡。
        """
        self.build_menu().exec(event.globalPos())

    def moveEvent(self, event):
        """角色一动，气泡就得跟着 —— 否则拖到一半气泡留在原地。"""
        super().moveEvent(event)
        if self.bubble is not None and self.bubble.isVisible():
            self.bubble.reposition(self.char_rect(),
                                   self.screen().availableGeometry())

    def closeEvent(self, event):
        # 气泡、聊天窗、时钟窗、能量条都是独立窗口，父窗口关掉不会自动带走它们
        self.save_state()
        for extra in (self.bubble, self.chat_window, self.clock_window,
                      self.bars):
            if extra is not None:
                extra.close()
        super().closeEvent(event)

    def hide_into_tray(self):
        """右键「隐藏」：只藏界面，程序继续在托盘里活着。

        隐藏后屏幕上**不留任何东西** —— 台词挂在桌面上是幽灵（主人实测
        报过：隐藏了还在飘台词）。所以气泡也要收，桌面一个字都不留。
        "我躲进托盘了"这句改走**系统托盘通知**：该说给主人听的信息不丢，
        但不再占用桌面气泡。

        ⚠️ 每个附属窗口都**单独 try**：以前是裸循环 + 末尾统一 `self.hide()`，
        结果新壳门面少了个 `hide()` 就在中途抛异常，**后面那句 self.hide()
        永远执行不到** —— 表现就是"点隐藏没反应"，而且只在开过那个窗口之后
        才犯（主人 2026-07-11 报的"有时候"正是这个）。隐藏是主人的明确指令，
        任何一个附属窗口出问题都不该让它失败。
        """
        self._user_hidden = True
        self.dismiss_chat_bubble()
        if self.bubble is not None:
            try:
                self.bubble.hide()          # 气泡是独立窗口，得单独收
            except Exception as err:
                print("[隐藏] 收气泡失败：%s: %s" % (type(err).__name__, err),
                      file=sys.stderr)
        # 要收的窗口列全，而且**以 self._shell 为准**：
        # `self.chat_window` 只是它的一张"门面"，可能是 None（自检里就被清空过），
        # 只认它的话会出现"隐藏了但那个大窗口还杵在屏幕上"。
        # 设置/记账/提醒那几个对话框**不动** —— 主人可能正开着在用，
        # 隐藏桌宠不该把他的设置窗口也收走。
        for extra in (self.chat_window, self.clock_window, self.bars,
                      getattr(self, "_shell", None)):
            if extra is None:
                continue
            try:
                extra.hide()
            except Exception as err:
                # 继续藏 —— 单个附属窗口坏掉不能连累"隐藏"这件事本身
                print("[隐藏] 收 %s 失败：%s: %s"
                      % (type(extra).__name__, type(err).__name__, err),
                      file=sys.stderr)
        self.hide()
        try:
            if self.tray is not None:
                self.tray.showMessage(
                    "鲸鲸桌宠", "我躲进托盘里啦，左键点托盘图标就能找到我（右键是菜单）。",
                    QSystemTrayIcon.Information, 3000)
        except Exception:
            pass                        # 通知弹不出来不算错，不该中断隐藏

    def _on_tray_click(self, reason):
        """托盘图标被点击：左键（单击 / 双击）把桌宠请出来。

        只有 Trigger / DoubleClick 才处理，**Context 一律放行** ——
        右键的弹出菜单是 Qt 自己接管的，这里再多做动作会打架。

        已经开着的时候不再说"我回来啦"，只提到最前闪一下：Windows 上
        双击托盘图标会先发 Trigger 再发 DoubleClick，如果两次都走
        "请回来"的台词，就会连听两遍。
        """
        if reason not in (QSystemTrayIcon.Trigger,
                          QSystemTrayIcon.DoubleClick):
            return
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            self.flash()
        else:
            self.show_from_tray()

    def show_from_tray(self):
        """托盘「打开」：把藏起来的桌宠请回原位。"""
        self._user_hidden = False       # 主人主动叫回来的
        self.show()
        self.raise_()
        self.activateWindow()
        self.flash()
        self.say(random.choice(LINES["back"]))

    def quit_app(self):
        """托盘「退出」：真正的退出 —— 清干净所有窗口再结束事件循环。"""
        self.save_state()               # 先落盘，别让这一天白攒
        self.dismiss_chat_bubble()
        if self.tray is not None:
            self.tray.hide()
        for extra in (self.chat_window, self.clock_window, self.bubble,
                      self.bars):
            if extra is not None:
                extra.close()
        self.close()
        QApplication.instance().quit()

    def reset_position(self):
        screen = self.screen().availableGeometry()
        self.place_at(screen.right() - self.width() - 40,
                      screen.bottom() - self.height() - 40)

    def add_reminder_seconds(self, seconds, note):
        """按**秒**登记一个提醒，返回是否成功（秒级是刚需：主人说的
        "十秒后"不能被模型进位成 1 分钟 —— 那是实测踩到的 bug）。"""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return False
        note = (note or "").strip()
        if seconds <= 0 or not note:
            return False
        if len(self.reminders) >= MAX_REMINDERS:
            self.say("提醒排太多啦，我先顾手上的这些……")
            return False
        timer = QTimer(self)
        timer.setSingleShot(True)
        # 每条短提醒带一个 id，定时器回调只认它 —— 早先用 note 当身份，
        # 于是"两条都叫喝水的提醒"会一起被摘掉，而剩下的那个定时器
        # 还在跑（成了孤儿，到点照样弹）。
        rid = uuid.uuid4().hex[:8]
        timer.timeout.connect(lambda: self.fire_reminder(rid))
        timer.start(max(100, int(seconds * 1000)))
        self.reminders.append({"id": rid, "timer": timer, "note": note,
                               "when": datetime.now() + timedelta(seconds=seconds)})
        return True

    def add_reminder(self, minutes, note):
        """按分钟登记（保留给旧调用点和自检）。"""
        try:
            return self.add_reminder_seconds(float(minutes) * 60.0, note)
        except (TypeError, ValueError):
            return False

    def fire_reminder(self, key):
        """到点了：把桌宠叫出来认真提醒一句。

        key 优先当 id 找（定时器走这条路）；找不到就退回当 note 找 ——
        自检和外部调用点还在用"直接喂事项"的老写法，兼容它。

        隐藏状态下（托盘里）也要**自动弹出来** —— "到点提醒"如果因为
        桌宠藏着就看不见，这功能等于没有（主人的明确要求）。
        """
        hit = next((r for r in self.reminders if r.get("id") == key), None)
        if hit is None:
            hit = next((r for r in self.reminders if r["note"] == key), None)
        if hit is None:
            # 已经被取消/已经放过：什么都不做，但保留"直接喂 note"的
            # 老行为（自检里就是这么调的）
            note = str(key)
        else:
            note = hit["note"]
            self.reminders = [r for r in self.reminders if r is not hit]
            # 定时器要显式回收：QTimer(self) 的父对象是桌宠，
            # 只 stop 不 delete 的话每设一次提醒就多留一个 QObject。
            hit["timer"].stop()
            hit["timer"].deleteLater()
        if not self.isVisible():
            # 静默请回来 —— 走 show_from_tray 会先说"我回来啦"，
            # 和提醒语抢同一只气泡，白闪一下。
            # 提醒是**唯一的例外**：主人明确要求"藏起来了也得提醒我"，
            # 所以这里顺手清掉"是主人主动藏起来"的标记。
            self._user_hidden = False
            self.show()
            self.raise_()
        self.flash()
        self.bounce = 1.0
        play_sound("remind")          # 提醒音：和"叮铃——"那句台词一起响
        # 冒号句式对任何 note 都通顺；原来的"该「%s」了"在公司 note
        # 自带"该"字时会变成"该「该睡觉了」了"（实机看到的双份"该"）
        text = "叮铃——主人，约定的时间到啦：%s！" % note
        self.say(text, hold=0, max_w=BUBBLE_MAX_W_CHAT)   # 提醒常驻，点一下才收
        if self.chat_window is not None and self.chat_window.isVisible():
            self.chat_window.add_her(text)
        self.update()

    def add_alarm(self, when_dt, note):
        """登记一条长期提醒，返回是否成功。"""
        alarm_id = append_alarm(self._config_path, when_dt, note)
        if not alarm_id:
            return False
        self._arm_alarm(alarm_id, when_dt, note)
        return True

    def _arm_alarm(self, alarm_id, when_dt, note):
        """给一条提醒装上精确定时器（装得下的才装）。

        存的是 `{"id", "timer"}` 而不是裸 timer：主人**取消**这条提醒时，
        得能反过来找到它、把它 stop 掉 —— 否则文件里的条目删了，
        到点照样弹出来提醒（比没删更糟）。
        """
        delay_ms = (when_dt - datetime.now()).total_seconds() * 1000.0
        if delay_ms <= 0 or delay_ms > QTIMER_MAX_MS:
            return              # 已过期 / 太远 → 由每分钟检查器兜着
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self.fire_alarm(alarm_id, when_dt, note))
        timer.start(int(delay_ms))
        self._alarm_timers.append({"id": alarm_id, "timer": timer})

    def _disarm_alarm(self, alarm_id):
        """停掉某条长期提醒的精确定时器（取消/已播报时都要调）。

        见 _arm_alarm：留着它的话，文件里的条目删了、到点照样弹出来。
        顺手 deleteLater —— 以前只 stop 并摘出列表，QObject 会一直挂在
        桌宠名下（父对象还在），设得越多、攒得越多。
        """
        keep = []
        for item in self._alarm_timers:
            if item["id"] == alarm_id:
                item["timer"].stop()
                item["timer"].deleteLater()
            else:
                keep.append(item)
        self._alarm_timers = keep

    def cancel_reminders(self, key):
        """取消对得上的提醒，返回删掉几条。

        短期（内存 QTimer）和长期（`whale_pet_alarms.jsonl`）一起处理 ——
        主人说"取消"时不会区分自己当初设的是哪种。主人的要求是
        **必须真从记录里删掉**，不能只是"标记失效"：他去「查看提醒」时
        那条还挂在那儿，等于没取消。

        空关键词 = 全都要（"把提醒都取消了吧"）。
        """
        removed = 0
        # ① 短期提醒：先 stop 定时器（并回收），再摘出列表
        keep = []
        for r in self.reminders:
            if note_matches(r["note"], key):
                r["timer"].stop()
                r["timer"].deleteLater()
                removed += 1
            else:
                keep.append(r)
        self.reminders = keep

        # ② 长期提醒：从文件删条目 + 停掉它的精确定时器
        for a in load_alarms(self._config_path):
            if not note_matches(a["note"], key):
                continue
            remove_alarm(self._config_path, a["id"])
            self._disarm_alarm(a["id"])
            removed += 1
        return removed

    def load_persisted_alarms(self):
        """启动时把长期提醒读回来，返回未到期的条数。

        已经过期的不在这里处理 —— 由 check_alarms 统一捞（含关机期间
        错过的），补一句提醒总比永远不知道强。
        """
        now = datetime.now()
        count = 0
        for a in load_alarms(self._config_path):
            if a["when"] > now:
                self._arm_alarm(a["id"], a["when"], a["note"])
                count += 1
        return count

    def fire_alarm(self, alarm_id, when_dt, note):
        """长期提醒到点（精确定时器路径）：先从文件摘掉，再播报。"""
        if alarm_id:
            remove_alarm(self._config_path, alarm_id)
            # 自己也从定时器表里摘掉：这个单次定时器已经放过火了，
            # 不摘的话表会一直变长（而且主人取消时还会再匹配到它）。
            self._disarm_alarm(alarm_id)
        self._announce_alarm(note, when_dt, late=False)

    def check_alarms(self):
        """每分钟扫一遍：到期的（含关机期间错过的）捞出来补提醒。

        ⚠️ 播报后必须**顺手把它的精确定时器摘掉**。漏掉这一步会重复播报：
        QTimer 用的是单调时钟，电脑睡一觉跨过提醒时间后，墙钟上它已经过期
        （本函数先把条目从文件里删掉并播报），可那个定时器还剩着时间，
        醒来后照样再 fire_alarm 一次 —— 同一条提醒响两遍。
        """
        for a in due_alarms(self._config_path):
            self._disarm_alarm(a["id"])
            self._announce_alarm(a["note"], a["when"], late=True)

    def _announce_alarm(self, note, when_dt, late=False):
        """把一条长期提醒说出口（隐藏中也自动弹出来）。"""
        if not self.isVisible():
            self._user_hidden = False   # 提醒是例外：藏起来了也要弹出来
            self.show()
            self.raise_()
        self.flash()
        self.bounce = 1.0
        play_sound("remind")          # 长期提醒到点：同一个提醒音
        stamp = format_alarm_time(when_dt)
        if late:
            text = "主人，有一条提醒我错过了一会儿：%s（原定 %s）" % (note, stamp)
        else:
            text = "叮铃——主人，你让我提醒你：%s（原定 %s）" % (note, stamp)
        self.say(text, hold=0, max_w=BUBBLE_MAX_W_CHAT)
        if self.chat_window is not None and self.chat_window.isVisible():
            self.chat_window.add_her(text)
        self.update()

    def show_alarms(self):
        """右键菜单「查看提醒」：开一个提醒窗口，列出手上挂着的提醒。

        长期提醒看不见摸不着，得给主人一个"查一查"的地方 ——
        不然设没设上只能靠猜。而且**能查就该能删**：每条卡片自带「取消」，
        不用再原话告诉她自己取消（旧版只念一遍，看不见也删不掉）。
        真的什么都没有时照旧冒一句气泡，不开空窗口。
        """
        alarms = load_alarms(self._config_path)
        if not alarms and not self.reminders:
            self.say("现在没有挂着的提醒，随时可以让我记一个。")
            return
        items = [{"id": a.get("id"), "note": a.get("note"),
                  "when": a.get("when"), "kind": "长期"} for a in alarms]
        # 短期提醒（内存里的 QTimer）没有落盘 id，拿 note 当身份 ——
        # cancel_reminders 本来就支持按事项匹配（它要处理短期+长期两种）
        items += [{"id": None, "note": r.get("note"), "when": r.get("when"),
                   "kind": "短时"} for r in self.reminders]

        def on_cancel(item):
            """取消一条：交给 cancel_reminders（它同时管短期和长期），返回条数。"""
            return self.cancel_reminders(item.get("note") or "")

        def on_close():
            # 关掉就摘引用：下次打开重新建一份，免得拿着一份已经过期的时间表
            self._alarm_win = None

        self._alarm_win = build_alarm_window(self, items, on_cancel,
                                            on_close=on_close)
        self._alarm_win.win.show()
        self._alarm_win.win.raise_()
