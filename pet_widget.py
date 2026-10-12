# -*- coding: utf-8 -*-
"""桌宠窗口本体：能量条 + WhalePet（由各 mixin 组装）+ build_widget 工厂。

WhalePet 自身只剩「窗口骨架」和 __init__；画面、养成、说话、对外通道、交互
分别在 pet_mixin_anim / life / chat / link / ui 里。
"""
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
from PySide6.QtGui import QColor
from PySide6.QtCore import QElapsedTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPainterPath
from PySide6.QtGui import QPen
from PySide6.QtGui import QPixmap
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QSystemTrayIcon
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_mixin_anim import (AnimMixin)
from pet_mixin_chat import (ChatMixin)
from pet_mixin_life import (LifeMixin)
from pet_mixin_link import (LinkMixin)
from pet_mixin_ui import (UiMixin)
from pet_ui import style_menu
from pet_windows import (build_bubble)


class EnergyBars(QWidget):
    """浮在桌宠**头顶**的两条能量条（纯展示，不接收任何鼠标事件）。

    三个刻意的选择：
    - **鼠标整块穿透**（Qt.WindowTransparentForInput）：它悬在角色头顶，
      要是吃鼠标，点那片区域就没反应了；
    - **parent 指向宠物窗口**：Windows 会让被拥有的窗口压在拥有者之上，
      省掉跟宠物抢层级的麻烦，也让两条窗一起被收进托盘；
    - **另开一个窗口，而不是把宠物窗口撑大**：宠物窗口的尺寸被
      reset_position / snap_to_edge 依赖，已经逐项验收过；撑大会连带改掉
      落点，还会在顶上留一块"透明但吃鼠标"的死区。
    """

    def __init__(self, pet):
        super().__init__(pet)
        self.pet = pet
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint
            | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._energy = ENERGY_MAX
        self._satiety = SATIETY_MAX
        self._deep = False
        # 宽度跟着**角色本身**的宽度走，不跟窗口 —— 窗口比角色宽一圈，
        # 按窗口算出来的条会比她的头还宽一截，像戴了顶大帽子。
        # 用待机第一帧定一次就够（条宽不该随睡姿/表情跳来跳去）。
        span_l, span_r = pet.alpha_span(pet.frames[0])
        self.resize(max(120, int((span_r - span_l + 1) * 1.06)),
                    BAR_H * 2 + BAR_GAP)
        # 跟随：宠物被拖动 / 复位 / 藏进托盘，条都要跟上。
        # 用定时器而不是重写 moveEvent —— 位置来源只有一处，不会漏。
        self._follow_timer = QTimer(self)
        self._follow_timer.timeout.connect(self.follow)
        self._follow_timer.start(120)
        self.follow()

    def set_values(self, energy, satiety, deep=False):
        """数值变了才重画（30fps 调过来，多数帧是同一个整数，别白重绘）。"""
        key = (round(energy), round(satiety), bool(deep))
        if key == (round(self._energy), round(self._satiety), self._deep):
            return
        self._energy, self._satiety, self._deep = energy, satiety, bool(deep)
        self.update()

    def follow(self):
        """贴着宠物窗口上沿，**水平对齐角色本身**的中心。

        不能拿窗口中心当基准：素材四周的透明留白左右不一样宽（尾巴那一侧
        更宽），窗口居中算出来的条会明显偏左。canvas_origin() 给的是底图的
        实际绘制矩形，再配合 alpha_span() 扫出来的"真·内容左右边界"，
        条才能正对着她头顶 —— 跟气泡用 alpha_top 定高度是同一个道理。
        """
        pet = self.pet
        ox, _oy, ow, _oh = pet.canvas_origin()
        k = ow / float(max(1, pet.pixmap.width()))
        left, right = pet.alpha_span(pet.pixmap)
        cl = pet.x() + ox + int(left * k)
        cr = pet.x() + ox + int(right * k)
        x = (cl + cr) // 2 - self.width() // 2
        y = pet.y() - self.height() - BAR_PAD
        screen = pet.screen()
        if screen is not None and y < screen.availableGeometry().top():
            y = pet.y() + pet.height() + BAR_PAD
        self.move(x, y)
        if pet.isVisible() and not self.isVisible():
            self.show()
        elif not pet.isVisible() and self.isVisible():
            self.hide()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = painter.font()
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        self._draw_bar(painter, 0, self._energy,
                       BAR_ENERGY_LOW if self._deep else BAR_ENERGY_COLOR,
                       "精力")
        self._draw_bar(painter, BAR_H + BAR_GAP, self._satiety,
                       BAR_SATIETY_COLOR, "饱食")
        painter.end()

    def _draw_bar(self, painter, top, value, color, label):
        w = self.width()
        # 底：半透明白 + 一圈浅边。只画白底的话，桌面本身是浅色时整条就
        # "看不见"，只剩一段填充色浮在那儿。
        painter.setPen(QPen(QColor(196, 208, 228, 180), 1.0))
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRoundedRect(QRectF(0.5, top + 0.5,
                                       w - 1.0, BAR_H - 1.0), 4.0, 4.0)
        ratio = max(0.0, min(1.0, float(value) / ENERGY_MAX))
        if ratio > 0.002:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(
                QRectF(1.0, top + 1.0,
                       max(2.0, (w - 2.0) * ratio), BAR_H - 2.0), 3.0, 3.0)
        # 字压在"填充"和"底"的交界上：填充满时整块在深色上、空时整块在
        # 白底上，单色字怎么选都会有一头看不清。所以走**白描边 + 深色填充**，
        # 而且必须分两步画 —— QPainter.drawPath 是"先填充后描边"，
        # 一步画完的话白边正好压在字上，深色被盖掉，字会淡得看不清
        # （放大特写里一眼就露馅，正常尺寸下反而不容易发现）。
        text = "%s %d" % (label, int(round(value)))
        font = painter.font()
        metrics = painter.fontMetrics()
        tx = (w - metrics.horizontalAdvance(text)) / 2.0
        ty = top + (BAR_H + metrics.ascent() - metrics.descent()) / 2.0
        path = QPainterPath()
        path.addText(tx, ty, font, text)
        painter.setPen(QPen(QColor(255, 255, 255, 235), 2.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(BAR_TEXT_COLOR))
        painter.drawPath(path)


class CoreMixin:
    def __init__(self, frames, height, fps, opacity, on_top,
                 expressions=(), do_expr=True,
                 idle_blinks=None, expr_blinks=None, sleep_pixmap=None,
                 ahoge_pixmap=None, tail_pixmap=None,
                 bowl_full_pixmap=None, bowl_empty_pixmap=None,
                 do_talk=True, talk_gap=None,
                 chat=None, search=None, show_bars=SHOW_ENERGY_BARS,
                 parent=None):
        """建窗口。素材与开关全部显式传进来（以前靠 build_widget 的闭包，
        那样这个方法就搬不出那个函数了）。"""
        super().__init__(parent)

        flags = Qt.FramelessWindowHint | Qt.Tool
        if on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        # WA_TranslucentBackground 是"真·逐像素 alpha"：
        # 素材哪里透明，窗口那里就真的透明，可以穿透看到桌面。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self.frames = frames            # 待机轮换池
        self.index = 0
        self.pixmap = self.frames[0]

        # 点击表情：expressions 是 [(名字, pixmap)] 的列表
        self.expr_names = [name for name, _ in expressions]
        self.expressions = [pix for _, pix in expressions]
        self.do_expr = do_expr and bool(self.expressions)
        self._expr_cursor = 0       # 点击轮换游标：每点一次前进一个

        # 窗口得同时装得下站姿和趴睡（趴睡更宽），所以取所有素材的最大尺寸
        every = list(self.frames) + [p for _, p in expressions]
        every += [p for p in (idle_blinks or []) if p is not None]
        every += [p for p in (expr_blinks or []) if p is not None]
        if sleep_pixmap is not None:
            every.append(sleep_pixmap)
        self.base_w = max(p.width() for p in every)
        self.base_h = max(p.height() for p in every)
        self.setFixedSize(int(self.base_w * WINDOW_MARGIN),
                          int(self.base_h * WINDOW_MARGIN))

        self.fps = max(5, min(120, fps))
        self.base_opacity = max(0.2, min(1.0, opacity))
        self.setWindowOpacity(self.base_opacity)

        # 动画状态
        self.bounce = 0.0           # 点击弹跳，指数衰减到 0
        self.home_y = 0             # 位置基准（拖拽 / 复位时更新）

        # 眨眼：每张"睁眼图"配一张闭眼帧，按下标一一对应。
        # 特例：happy 的配对帧是"睁眼笑"，因为开心表情本身就是闭眼笑。
        self.idle_blinks = list(idle_blinks or [None] * len(self.frames))
        self.expr_blinks = list(expr_blinks or [None] * len(self.expressions))
        self._blink_token = 0
        self._blink_target = None   # 本次眨眼贴上的是哪张
        self._return_pixmap = None  # 眨完要回到哪张

        # 睡觉状态。注意这里**不存布尔标志** ——
        # "睡没睡"一律由 sleeping() 从当前画面推导，避免标志与画面失同步。
        self.sleep_pixmap = sleep_pixmap
        self._breath_phase = 0.0    # 呼吸相位（弧度）

        # 呆毛层（原尺寸，绘制时按显示比例缩放）
        self.ahoge_pixmap = ahoge_pixmap
        self._ahoge_phase = 0.0
        self._ahoge_angle = 0.0

        # 帧间真实耗时（见 _tick）。QElapsedTimer 走单调时钟，
        # 不受系统时间被 NTP/手动改动的影响。
        self._clock = QElapsedTimer()
        self._clock.start()

        # 尾巴层（同样原尺寸不预缩放，绘制时按同一比例换算才对得上）
        self.tail_pixmap = tail_pixmap
        self._tail_phase = 0.0
        self._tail_angle = 0.0

        # 白饭（主人的要求）：她左脚边的一碗白饭。
        # 和呆毛/尾巴不同，白饭不摆动、每帧都是同一张，所以**预缩放到显示尺寸**
        # （省掉每帧重采样）。换算用底图的 k = 显示宽 / 画布宽，和另两层同一套口径。
        # ⚠️ 换算逻辑抽在 `_fit_bowl()` 里，构造与"改大小"两处共用 ——
        # 各写一份的话，改大小那条路会漏掉这一步、饭碗直接按画布原图显示，
        # 变成一个巨大的碗（主人 2026-07-11 报的）。
        self.bowl_full_pixmap = self._fit_bowl(bowl_full_pixmap)
        self.bowl_empty_pixmap = self._fit_bowl(bowl_empty_pixmap)
        self._bowl_hop = 0.0        # 跳一下的进度 0→1；0 = 没在跳
        self._rice_left = BOWL_RICE_CLICKS   # 这碗还能点几次
        self._refill_timer = QTimer(self)
        self._refill_timer.setSingleShot(True)
        self._refill_timer.timeout.connect(self.refill_bowl)

        # 交互状态
        self._press_global = None
        self._press_origin = None
        self._moved = False
        self._dragging = False
        # 双击的收尾那一下要跳过（Qt 的序列是 press→release→dblclick→release，
        # 中间的 release 已经算过"第一下"了，见 mouseDoubleClickEvent）
        self._skip_next_release = False

        # 台词气泡：独立窗口，鼠标点击穿透（原因见 build_bubble）
        self.do_talk = do_talk
        self.talk_gap = talk_gap if talk_gap and talk_gap > 0 else None
        self.bubble = build_bubble(self) if do_talk else None
        self._alpha_top_cache = {}      # 各张图的"内容顶端"缓存
        self._alpha_span_cache = {}     # 各张图的"内容左右边界"缓存（能量条对齐用）
        self.talk_timer = QTimer(self)
        self.talk_timer.setSingleShot(True)
        self.talk_timer.timeout.connect(self.idle_talk)
        if do_talk:
            self.talk_timer.start(int(FIRST_TALK_DELAY * 1000))

        # 和大模型说话。chat / search 由 main 按配置造好传进来，
        # 没配 key 时是 None —— 整个功能干净地关掉，不留半死不活的中间态。
        self.chat = chat
        self.search = search
        self.clock_window = None      # 右键「显示时钟」才建
        self._balance = None          # 右键「查余额」才建
        self._last_balance = None     # 最近一次成功的余额快照（记账 + 面板展示用）
        self._balance_panel = None    # 右键「小鲸鱼记账」才建（持引用防 GC）
        self._sound_dialog = None     # 右键「音效…」才建（同上）
        self._balance_quiet = False   # True = 这一轮是自动观测（不冒泡念数字）
        self._balance_timer = QTimer(self)   # 自动观测余额的节拍（main 里按配置起）
        self.reminders = []           # 挂着的定时提醒（模型 [remind:] 标记登记）
        self._last_question = None    # 最近一次提问的原话（提醒兜底/纠偏要用）
        self._alarm_timers = []       # 长期提醒的精确定时器（持引用防 GC）
        self._alarm_watch = None      # 每分钟巡检定时器（main 里启动）
        # 启动时实际加载的那份配置。界面里改 key 必须**写回这份**，
        # 而不是无脑写默认路径 —— 主人用 --config 指了别的文件时，
        # 写错地方等于"改了没生效"（实测踩过：真 key 被写进了源码目录）
        self._config_path = None
        self._key_dialog = None
        self._memory_context = None   # 要求 4：长期记忆 + 最近对话的注入文本

        # 精力 / 饱食度（隐藏数值）。这里先给满值 —— 真正"上次退出时的值"
        # 由 main 在设好 _config_path 之后调 load_state() 读回来：
        # 此刻还不知道主人有没有用 --config 换目录，读错了就白读。
        self._energy, self._satiety = ENERGY_MAX, SATIETY_MAX
        self._steal_left = 0          # 这轮偷吃还剩几碗（0 = 没在偷吃）
        # 状态表就绪开关：main 调过 load_state() 之后才允许写盘 ——
        # 否则构造阶段（_config_path 还是 None）的 sleep()/wake_up()
        # 会往默认路径乱写文件。
        self._state_ready = False
        self._state_timer = QTimer(self)
        self._state_timer.timeout.connect(self.save_state)
        self._state_timer.start(STATE_SAVE_MS)
        # 头顶那两条能量条：主人要先看效果，满意后再把 SHOW_ENERGY_BARS
        # 改成 False（或启动时加 --no-bars）就彻底不出现了。
        self.bars = EnergyBars(self) if show_bars else None
        self.chat_window = None       # 双击才建，免得白占一个窗口
        self._harness_win = None      # 「派活给 Harness…」才建的**工作台**窗口
        self._shell = None            # 聊天/工作共用的那个窗口（pet_shell，懒建）
        self._harness_mode = False    # 是不是在"给 Harness 派活"（决定事件往哪个窗口走）
        self._harness_target = None   # 这次接哪段旧对话（None = 新开一段）
        self._harness = None          # 派活桥（首次派活才建，免得白起一个进程）
        self._harness_usage_pct = -1  # 上次播报 token 用的百分比（防刷屏）
        self._remote = None           # 远程遥控桥（菜单里开服务才建）
        self._remote_dialog = None    # 同上，持引用防 GC
        self._feishu = None           # 飞书桥（开了才建）
        self._feishu_dialog = None    # 同上，持引用防 GC
        self._feishu_chats = {}       # 每条飞书会话各自的状态（模式/接哪段）
        self._pet_height = None       # 当前角色高度（main 里设；设置窗口据此算比例）
        self._settings_win = None     # 设置窗口（持引用防 GC）
        self._alarm_win = None        # 「查看提醒」窗口（同上）
        # 「主人主动藏进托盘」的标记：藏起来之后**自动冒泡/闲话一律不再打扰**
        # （只有提醒例外，见 fire_reminder）。主人报过"点了隐藏却还在冒台词"。
        self._user_hidden = False
        # 正在等结果的两类请求**分舱**存：闲聊和派活各一格。
        # 以前是单槽，谁后到谁覆盖 —— 派活还没干完时来一句闲聊，
        # 干完的结果就回给了闲聊那条消息，两边都错。
        # 2026-10-11 又加两舱：media=照片下载中、shot=截图中。它们和上面两格
        # 是同样的道理 —— 下载照片时主人再发一句闲聊，不能让两边互相顶掉，
        # 也不能因为"这两格没定义"就 KeyError（写死两格时踩过这种假"没反应"）。
        self._feishu_pending = {"chat": None, "harness": None,
                                "media": None, "shot": None}
        self._feishu_log = []         # 飞书的连接日志，设置窗口里显示
        # 最近一次说话的飞书会话 id（--doctor 的图片自测要拿它当收件人；
        # 收到消息时更新并落盘，见 LinkMixin._feishu_remember_chat）
        self._feishu_last_chat = ""
        self._pending_question = None
        self._chat_buffer = ""
        self._chat_last = ""          # 现在停在气泡上的那句回复（截断后的）
        if chat is not None:
            chat.chunk.connect(self._on_chat_chunk)
            chat.done.connect(self._on_chat_done)
            chat.failed.connect(self._on_chat_failed)
        if search is not None:
            search.found.connect(self._on_search_found)
            search.failed.connect(self._on_search_failed)
        # 托盘图标（主人的要求）：桌宠可以隐藏，但托盘里永远有她的
        # 影子 —— 隐藏 ≠ 消失，「打开」从托盘请回，「退出」才真正走。        # 图标用正面立绘缩到 64px、垫一层浅色圆底 —— 托盘里小,得有辨识度。
        tray_pixmap = QPixmap(64, 64)
        tray_pixmap.fill(Qt.transparent)
        painter = QPainter(tray_pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLOR_PAPER))
        painter.drawEllipse(0, 0, 64, 64)
        if self.frames:
            face = self.frames[0].scaled(
                54, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap((64 - face.width()) // 2,
                               (64 - face.height()) // 2, face)
        painter.end()
        self.tray = QSystemTrayIcon(QIcon(tray_pixmap), self)
        tray_menu = QMenu()
        tray_menu.addAction("打开", self.show_from_tray)
        tray_menu.addAction("退出", self.quit_app)
        # 托盘菜单也是"右键菜单"，长相要跟桌宠本体一致（同一套 style_menu）
        self.tray.setContextMenu(style_menu(tray_menu))
        self.tray.setToolTip("鲸鲸桌宠 · 左键打开 / 右键菜单")
        # 左键也要能打开。托盘菜单是**右键**才弹的，而主人很自然会左键
        # 去点图标 —— 之前左键点了毫无反应，看起来像卡死了。
        self.tray.activated.connect(self._on_tray_click)
        self.tray.show()          # 启动即常驻（隐藏桌宠时的"回家路"）

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / self.fps))

        self.blink_timer = QTimer(self)
        self.blink_timer.setSingleShot(True)
        self.blink_timer.timeout.connect(self.blink)
        if any(self.idle_blinks) or any(self.expr_blinks):
            self.blink_timer.start(self.next_blink_gap())

    # ---------------------------------------------------------------- 大小
    def _fit_bowl(self, pix):
        """把饭碗从"画布空间原图"缩到**当前显示尺寸**。

        饭碗和底图一样要按 `k = 显示宽 / AHOGE_CANVAS[0]` 预缩放（它每帧都画，
        不预缩放就得每帧重采样）。构造与 `apply_pet_size` **必须共用这一个函数**：
        改大小那条路我一开始直接把画布原图塞进去了，饭碗于是按原图大小画出来 ——
        屏幕上是巨大一个碗（主人 2026-07-11 报的"改完大小碗的比例失调"）。
        调用时机也重要：必须在 `self.pixmap` 换成**新底图之后**调，否则 k 还是旧的。
        """
        if pix is None or pix.isNull():
            return None
        k = self.pixmap.width() / float(max(1, AHOGE_CANVAS[0]))
        width = max(6, int(round(BOWL_CANVAS_W * k)))
        return pix.scaledToWidth(width, Qt.SmoothTransformation)

    def apply_pet_size(self, height):
        """把桌宠整体缩放到新高度（**立即生效，不用重启**）。返回是否真改了。

        做法是按比例**缩放手里已有的 pixmap**，而不是重新读盘重采样：
          * 立绘本身的像素比显示尺寸大得多，一次几成的缩放看不出画质损失；
          * 这条路径不需要知道素材路径、也不碰 build_widget 的构造流程 ——
            状态机（frames / expressions / 眨眼配对 / 睡姿 / 呆毛 / 尾巴 / 饭碗）
            全部按同一比例走，不会出现"身子变大了呆毛还是小的"。
        呆毛与尾巴是**绘制时**按 `显示宽 / AHOGE_CANVAS` 换算的，所以跟着底图
        一起缩放之后，比例自动还是对的。
        """
        try:
            new_h = int(height)
        except (TypeError, ValueError):
            return False
        new_h = max(96, min(900, new_h))
        current = int(self._pet_height or 0) or int(self.base_h)
        factor = new_h / float(current)
        if abs(factor - 1.0) < 0.02:          # 变化太小就别折腾了
            self._pet_height = new_h
            return False

        was_sleeping = self.sleeping()

        # ① 首选：让外面用**原图重新加载**（main 里挂的 _asset_reloader）。
        #    这条路径是"从大图重采样"，每个尺寸都清晰；
        #    ② 兜底：缩放手里已有的 pixmap —— 能用，但连调两次会累积发糊，
        #    所以只在没挂 reloader（比如自检里手搓的桌宠）时才走。
        art = None
        reloader = getattr(self, "_asset_reloader", None)
        if reloader is not None:
            try:
                art = reloader(new_h)
            except Exception as err:
                print("[大小] 重新加载素材失败，退回缩放：%s: %s"
                      % (type(err).__name__, err), file=sys.stderr)
                art = None
        if art:
            self.frames = list(art["frames"])
            # 名字也一起换（顺序来自同一份 buckets，与原来一一对应）
            self.expr_names = [n for n, _p in art["expressions"]]
            self.expressions = [pix for _n, pix in art["expressions"]]
            self.idle_blinks = list(art["idle_blinks"])
            self.expr_blinks = list(art["expr_blinks"])
            self.sleep_pixmap = art["sleep_pixmap"]
            self.ahoge_pixmap = art["ahoge_pixmap"]
            self.tail_pixmap = art["tail_pixmap"]
            # 饭碗先**留着原图**：它的显示尺寸靠 `_fit_bowl()` 从底图宽度换算，
            # 而底图（self.pixmap）要到下面才更新 —— 这里就缩会用到旧的 k。
            art_bowls = (art["bowl_full_pixmap"], art["bowl_empty_pixmap"])
        else:
            art_bowls = None

            def one(pix):
                if pix is None or pix.isNull():
                    return pix
                return pix.scaled(max(2, int(round(pix.width() * factor))),
                                  max(2, int(round(pix.height() * factor))),
                                  Qt.KeepAspectRatio, Qt.SmoothTransformation)

            self.frames = [one(p) for p in self.frames]
            self.expressions = [one(p) for p in self.expressions]
            self.idle_blinks = [one(p) for p in self.idle_blinks]
            self.expr_blinks = [one(p) for p in self.expr_blinks]
            self.sleep_pixmap = one(self.sleep_pixmap)
            # ⚠️ 呆毛与尾巴**不能**在这里缩放：它们是"画布空间"的图层，存放的是
            # **原尺寸**素材，绘制时才按 k = 显示宽 / AHOGE_CANVAS[0] 换算。
            # 底图一改，k 自己就变了 —— 这里再缩一次就是**双重缩放**
            # （实测：调到 480 时尾巴大得离谱、调到 220 时尾巴缩没了）。
            # 饭碗相反：它在构造时就预缩放到显示尺寸（省每帧重采样），所以要跟着缩。
            self.bowl_full_pixmap = one(self.bowl_full_pixmap)
            self.bowl_empty_pixmap = one(self.bowl_empty_pixmap)

        every = list(self.frames) + list(self.expressions)
        every += [p for p in (self.idle_blinks + self.expr_blinks) if p is not None]
        if self.sleep_pixmap is not None:
            every.append(self.sleep_pixmap)
        self.base_w = max(p.width() for p in every)
        self.base_h = max(p.height() for p in every)
        self.setFixedSize(int(self.base_w * WINDOW_MARGIN),
                          int(self.base_h * WINDOW_MARGIN))

        # 画面跟着换：睡着的继续睡（换成缩放后的睡姿图），否则回到待机帧
        self.index = min(self.index, len(self.frames) - 1)
        if was_sleeping and self.sleep_pixmap is not None:
            self.pixmap = self.sleep_pixmap
        else:
            self.pixmap = self.frames[max(0, self.index)]
        self._return_pixmap = None

        # 饭碗的预缩放**必须放在这里**：`_fit_bowl()` 要用新底图的宽度算 k，
        # 所以得等 self.pixmap 换完再调（重新加载那条路留下的就是画布原图）。
        if art_bowls is not None:
            self.bowl_full_pixmap = self._fit_bowl(art_bowls[0])
            self.bowl_empty_pixmap = self._fit_bowl(art_bowls[1])

        self._pet_height = new_h
        # 新尺寸下重新贴边/回到底部基准，否则她会有一半在屏幕外面
        screen = self.screen().availableGeometry()
        self.home_y = max(screen.top(),
                          min(self.home_y, screen.bottom() - self.height()))
        if hasattr(self, "snap_to_edge"):
            self.snap_to_edge()
        # 能量条要**重建**，不能只 reposition：它的宽度是构造时按角色第一帧的
        # 内容宽度算死的、高度是固定像素，尺寸一变就配不上她的头了
        # （它的 follow() 只管位置不管大小）。
        if self.bars is not None:
            was_shown = self.bars.isVisible()
            self.bars.hide()
            self.bars.deleteLater()
            self.bars = EnergyBars(self)
            self.bars.set_values(self._energy, self._satiety,
                                 deep=self.deep_sleep())
            if was_shown and self.isVisible():
                self.bars.show()
        self.update()
        return True


class WhalePet(AnimMixin, LifeMixin, ChatMixin, LinkMixin, UiMixin, CoreMixin, QWidget):
    """鲸鲸本体。方法来自上面几个 mixin —— 它们合起来才是完整的一只。"""


def build_widget(frames, height, fps, opacity, on_top,
                 expressions=(), do_expr=True,
                 idle_blinks=None, expr_blinks=None, sleep_pixmap=None,
                 ahoge_pixmap=None, tail_pixmap=None,
                 bowl_full_pixmap=None, bowl_empty_pixmap=None,
                 do_talk=True, talk_gap=None,
                 chat=None, search=None, show_bars=SHOW_ENERGY_BARS):
    """返回**已经绑好素材**的 WhalePet 子类（保留老用法）。

    为什么是「返回一个类」而不是「返回实例」：调用方习惯写成
        WhalePet = build_widget(frames, ...); pet = WhalePet()
    这种「先拿类、再建实例」两步走（自检里也是这么用的，还依赖它拿到的是
    类本身）。素材在 build_widget 里就绑好，子类的 __init__ 只留一个 parent，
    于是既不用改调用方，也不用把 20 个参数塞进全局。
    """
    class BoundWhalePet(WhalePet):
        def __init__(self, parent=None):
            super().__init__(frames, height, fps, opacity, on_top,
                             expressions=expressions, do_expr=do_expr,
                             idle_blinks=idle_blinks, expr_blinks=expr_blinks,
                             sleep_pixmap=sleep_pixmap, ahoge_pixmap=ahoge_pixmap,
                             tail_pixmap=tail_pixmap,
                             bowl_full_pixmap=bowl_full_pixmap,
                             bowl_empty_pixmap=bowl_empty_pixmap,
                             do_talk=do_talk, talk_gap=talk_gap,
                             chat=chat, search=search, show_bars=show_bars,
                             parent=parent)

    return BoundWhalePet
