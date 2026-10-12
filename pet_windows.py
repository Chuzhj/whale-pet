# -*- coding: utf-8 -*-
"""独立小窗口：台词气泡、聊天窗口、时钟窗。

它们都由 factory 函数构造（parent 传桌宠），所以不需要碰 WhalePet 的内部。"""
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
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)
from pet_tags import (bubble_text_flags)


def make_bubble_font(size=None, bold=False):
    """气泡用的字体。带字体族优先级 + Emoji 兜底，避免中文或 👍 变豆腐块。

    `size` 不传就用默认字号（`BUBBLE_FONT_PX`）—— 设置里的「台词气泡字号」
    走这个参数；`bold` 留给以后要加粗的场合。
    """
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(BUBBLE_FONT_FAMILIES)
    font.setPixelSize(int(round(size)) if size else BUBBLE_FONT_PX)
    if bold:
        font.setBold(True)
    return font


def build_bubble(parent):
    """造一个专门显示台词的气泡窗口。

    为什么另开窗口，而不是把气泡画在宠物窗口里：
      1. 宠物窗口只比角色大 14%（257x290 装 170x255），上方仅剩 35px，
         放不下一行带内边距的文字；
      2. 想腾地方就得把窗口撑大，而 reset_position / snap_to_edge 都是
         按**窗口**尺寸算的 —— 一撑大，角色的落点就整体偏移，
         那是已经逐项验收过的行为，不该为一个气泡去改；
      3. 撑大还会留下一块"看起来透明、其实吃鼠标"的死区，
         点到桌面上的图标会被宠物窗口吞掉。

    这个窗口带 WindowTransparentForInput，鼠标事件整块穿透出去，
    所以它既不挡桌面操作，也不会抢走"点角色换表情"的判定。
    它把宠物窗口设成 parent —— Windows 会让"被拥有的窗口"始终压在
    拥有者之上，所以**气泡永远在桌宠上面**，不用自己去抢层。

    ⚠️ 层级（主人 2026-10-11 明确要求，别再改回去）：
        **别的程序窗口 > 气泡 > 桌宠**
    实现方式就是"去掉 WindowStaysOnTopHint"：
      * 气泡不再永远置顶 → 别的程序窗口正常盖住它（不会再糊在人家的界面上）；
      * 桌宠自己也不是永远置顶（见 pet_widget 的 on_top）→ 气泡与桌宠都在
        "普通窗口"这一层里，靠 Windows 的"附属窗口压在拥有者之上"保证顺序。
    以前那套"气泡主动躲开别的窗口"的做法已经删掉：主人要的是**被盖住**，
    而不是自己挪来挪去。
    """
    from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
    from PySide6.QtGui import (QColor, QFontMetrics, QPainter, QPainterPath,
                               QPen, QPolygonF)
    from PySide6.QtWidgets import QWidget

    class SpeechBubble(QWidget):
        def __init__(self):
            super().__init__(parent)

            # 注意这里**没有** WindowStaysOnTopHint（层级见 build_bubble 的说明）
            flags = (Qt.FramelessWindowHint | Qt.Tool
                     | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            # 显示时不抢焦点，否则每次冒台词都会把主人正在打字的窗口顶掉
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)

            self._font = make_bubble_font(BUBBLE_FONT_PX)
            self._font_px = BUBBLE_FONT_PX   # 当前字号（设置里能改）
            self._last_char_rect = None      # 上次说话时角色的位置（改字号要按它重排）
            self._text = ""
            self._text_box = QRect()
            self._tail_x = BUBBLE_TAIL_W + BUBBLE_RADIUS
            self._max_w = BUBBLE_MAX_W   # 当前这次显示用的最大宽度（聊天回复会更宽）
            self._placed_for = None       # 摆位缓存：位置没变就不重复 move()

            self._hold = QTimer(self)
            self._hold.setSingleShot(True)
            self._hold.timeout.connect(self.hide)

        # ------------------------------------------------------ 排版与定位
        def _measure(self, text, max_w=None):
            """按最大宽度折行，返回气泡的整体尺寸（含尾巴）。

            连中英文混排一起交给 Qt 的 TextWordWrap 处理 ——
            中文没有词边界，Qt 会按字符断行，正合需要。
            max_w 不传就沿用上一次的宽度：聊天回复用宽气泡，
            重绘时不能因为参数没带就把宽度又缩回默认值。
            """
            max_w = max_w or self._max_w
            # ⚠️ 宽度上限还要**夹进屏幕**：主人把气泡宽度调大、或者聊天回复用宽气泡时，
            # 一个比屏幕还宽的气泡怎么摆都会溢出（夹取函数只能二选一，选哪边都露出去）。
            try:
                avail = self.screen().availableGeometry().width() - 2 * BUBBLE_MARGIN
                if avail > 80:
                    max_w = min(max_w, avail)
            except Exception:
                pass
            metrics = QFontMetrics(self._font)
            inner_w = max_w - 2 * BUBBLE_PAD_X
            box = metrics.boundingRect(QRect(0, 0, inner_w, 10000),
                                       bubble_text_flags(), text)
            # 文字框宽度固定写成 inner_w（折行约束值），而不是实测宽度 ——
            # 实测算出的宽度更窄，拿它去 drawText 会让折行点往前挪，凭空多出一行。
            # 高度多留 2px：drawText 与 boundingRect 的取整偶尔差一行，
            # 差一行就是"最后一行被切掉"，给这一点余量就不必赌它们完全一致。
            self._text_box = QRect(BUBBLE_PAD_X, BUBBLE_PAD_Y, inner_w, box.height() + 2)
            out_h = box.height() + 2 * BUBBLE_PAD_Y + BUBBLE_TAIL_H
            # 高度也夹进屏幕：超长台词/长回复时，气泡顶多和屏幕一样高。
            # 真到那一步说明内容确实一屏放不下（Qt 会画到框外，不崩），
            # 但**不会再有"气泡整个跑出屏幕"**这种事（主人报过）。
            try:
                limit = self.screen().availableGeometry().height() - 2 * BUBBLE_MARGIN
                if limit > 80:
                    out_h = min(out_h, limit)
            except Exception:
                pass
            return (min(max_w, box.width() + 2 * BUBBLE_PAD_X), out_h)

        def _place(self, char_rect, screen_rect):
            """把气泡摆到角色头顶正上方，并保证整块不越出屏幕。

            **不做"主动避让"**（2026-10-11 主人明确要求）：气泡不再自己躲开
            别的窗口，而是靠**层级**解决 —— 见下面 __init__ 里那段说明。
            于是这里就只是"贴着头顶 + 夹进屏幕"这一件事，逻辑简单到不会出错。
            """
            w, h = self.width(), self.height()
            cx = char_rect.center().x()
            x = max(screen_rect.left() + BUBBLE_MARGIN,
                    min(cx - w // 2, screen_rect.right() - w - BUBBLE_MARGIN))
            # 先做底部夹取、再做顶部夹取：两者冲突时让"不跑出屏幕上方"优先
            y = char_rect.top() + BUBBLE_TAIL_TIP - h
            y = min(y, screen_rect.bottom() - h - BUBBLE_MARGIN)
            y = max(y, screen_rect.top() + BUBBLE_MARGIN)
            # 气泡被夹到屏幕边上时，中心就不对着角色了 ——
            # 记下尾巴该画在哪，让它仍然指着她的头顶
            self._tail_x = cx - x
            self.move(int(x), int(y))

        # ------------------------------------------------------ 对外接口
        def set_font_px(self, px):
            """改气泡字号（设置里的「台词气泡字号」）。返回是否真的变了。

            改完**立刻按新字号重新排版一次**：不然已经显示着的那个气泡要等到
            下一次说话才变大变小，主人会以为设置没生效。
            """
            try:
                px = max(BUBBLE_FONT_MIN, min(BUBBLE_FONT_MAX, int(px)))
            except (TypeError, ValueError):
                return False
            if px == int(getattr(self, "_font_px", 0)):
                return False
            self._font_px = px
            self._font = make_bubble_font(px)
            if self._text and self.isVisible() and self._last_char_rect is not None:
                self._placed_for = None      # 尺寸要重算，摆位缓存一并作废
                self.render(self._text, self._last_char_rect,
                            self.screen().availableGeometry())
            self.update()
            return True

        def render(self, text, char_rect, screen_rect, max_w=None):
            """把一段文字画到气泡上。

            **不动收起定时器、不重复置顶** —— 流式回话时这个方法每秒会被调几十次，
            每多做一个动作就是一次可感知的闪烁。两个缓存（尺寸、摆位）都是为它加的。
            """
            if max_w:
                self._max_w = max_w
            size = self._measure(text)
            if size != (self.width(), self.height()):
                self.setFixedSize(*size)      # 只有尺寸真变了才碰窗口
            self._text = text
            self._last_char_rect = char_rect   # 记下来：改字号时按它原地重排
            # ⚠️ 摆位缓存**必须把气泡自己的尺寸算进去**：角色没动、但气泡变高时，
            # 旧的位置会让它下边伸到屏幕外 —— 主人报的"台词气泡溢出屏幕"就是这个
            # （实测：超长句下溢 32px，而短句、贴边、聊天宽气泡都不溢出）。
            where = (char_rect.x(), char_rect.y(), char_rect.width(), char_rect.height(),
                     screen_rect.x(), screen_rect.y(),
                     screen_rect.width(), screen_rect.height(),
                     self.width(), self.height())
            if where != self._placed_for:
                self._place(char_rect, screen_rect)
                self._placed_for = where
            if not self.isVisible():
                self.show()
                self.raise_()
            self.update()

        def say(self, text, char_rect, screen_rect, hold=BUBBLE_HOLD, max_w=None):
            """说一句就收。

            hold > 0：停留这么多秒后自动收起；
            hold <= 0：**一直开着**（等模型回话、或让她的话停在桌面上时用），
                       并取消之前排好的收起。
            """
            if not text:
                return
            self.render(text, char_rect, screen_rect, max_w)
            if hold > 0:
                self._hold.start(int(hold * 1000))
            else:
                self._hold.stop()

        def text(self):
            """当前气泡里写的是哪句话。外面靠它判断"还停在刚说的那句上"。"""
            return self._text

        def reposition(self, char_rect, screen_rect):
            """角色被拖动时跟着走。"""
            if self.isVisible():
                self._place(char_rect, screen_rect)
                self._placed_for = None       # 摆位缓存作废，下次 render 重新算
                self.update()

        # ------------------------------------------------------ 绘制
        def paintEvent(self, _event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            w = self.width()
            body = QRectF(1.0, 1.0, w - 2.0, self.height() - BUBBLE_TAIL_H - 1.0)

            # 圆角矩形和尾巴三角各自成路径，再取并集 ——
            # 这样描边是一圈完整的外轮廓，不会在尾巴根部横着多出一段线。
            shape = QPainterPath()
            shape.addRoundedRect(body, BUBBLE_RADIUS, BUBBLE_RADIUS)
            tail = QPainterPath()
            half = BUBBLE_TAIL_W / 2.0
            tx = max(body.left() + BUBBLE_RADIUS + half,
                     min(self._tail_x, body.right() - BUBBLE_RADIUS - half))
            tail.addPolygon(QPolygonF([
                QPointF(tx - half, body.bottom() - 1.0),
                QPointF(tx + half, body.bottom() - 1.0),
                QPointF(tx, self.height() - 1.0),
            ]))
            shape = shape.united(tail)

            painter.setPen(QPen(QColor(*BUBBLE_LINE), 1.6))
            painter.setBrush(QColor(*BUBBLE_FILL))
            painter.drawPath(shape)

            painter.setPen(QColor(*BUBBLE_TEXT))
            painter.setFont(self._font)
            # flags 与 _measure() 共用同一个函数 —— 两边一旦分叉，
            # 就是"气泡宽度对、右边那截字凭空没了"（实测踩过）。
            painter.drawText(QRectF(self._text_box),
                             bubble_text_flags(align_top=True), self._text)
            painter.end()

    return SpeechBubble()


def chat_font(size, bold=False):
    """聊天窗口用的字体。和气泡同一套字体族，视觉上才是一家人。"""
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(BUBBLE_FONT_FAMILIES)
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def build_chat_window(parent, on_send, on_clear=None):
    """做一个和鲸鲸同风格的聊天窗口。

    无边框 + 圆角 + 半透明底，背景和标题栏由 paintEvent 自己画（这样圆角才干净），
    里面的控件只有颜色和字号，不带自己的背景 —— 所以每个子控件都要显式设透明，
    否则 Qt 会给它们刷上系统默认的灰底，一坨一坨盖在圆角上。

    parent 指向宠物窗口：Windows 会让被拥有的窗口始终压在拥有者之上，
    而且主人退出时它会跟着一起走。
    """
    from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
    from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                   QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

    class ChatWindow(QWidget):
        def __init__(self):
            super().__init__(parent)
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(CHAT_WIN_W, CHAT_WIN_H)

            self.on_send = on_send
            self.on_clear = on_clear
            self._busy = False
            self._drag_from = None
            self._reply_label = None
            self._greeted = False

            self._build_header()
            self._build_messages()
            self._build_input()

            QShortcut(QKeySequence(Qt.Key_Escape), self, self.hide)

        # ------------------------------------------------------ 搭界面
        def _build_header(self):
            self.avatar = QLabel("🐋", self)
            self.avatar.setGeometry(14, 9, 30, 30)
            self.avatar.setAlignment(Qt.AlignCenter)
            self.avatar.setFont(chat_font(17))
            # 底必须是浅色：鲸鱼 emoji 本身就是深蓝的，配深蓝底会糊成一坨看不出是鲸鱼
            self.avatar.setStyleSheet(
                "background:%s;border-radius:15px;" % COLOR_LIGHT)

            self.title = QLabel("鲸鲸", self)
            self.title.setGeometry(54, 8, 180, 20)
            self.title.setFont(chat_font(15, True))
            self.title.setStyleSheet("color:#FFFFFF;background:transparent;")

            self.subtitle = QLabel("蓝色大肥鱼 · 爱吃白饭", self)
            self.subtitle.setGeometry(54, 26, 200, 16)
            self.subtitle.setFont(chat_font(10))
            self.subtitle.setStyleSheet("color:#AFC6E8;background:transparent;")

            self.close_btn = QPushButton("✕", self)
            self.close_btn.setGeometry(CHAT_WIN_W - 42, 11, 28, 26)
            self.close_btn.setFont(chat_font(13))
            self.close_btn.setCursor(Qt.PointingHandCursor)
            self.close_btn.setStyleSheet(
                "QPushButton{background:transparent;color:#C9D8F2;border:none;"
                "border-radius:8px;}"
                "QPushButton:hover{background:rgba(255,255,255,0.18);color:#FFFFFF;}")
            # 这个按钮曾经只画了样子、没接槽函数 —— 点上去毫无反应（主人报的 bug）。
            # 用 hide 而不是 close：窗口是**复用**的（宠物那边缓存着 self.chat_window），
            # 真销毁了再开会重建，聊天记录和开场问候都会丢。Esc 走的也是 hide。
            self.close_btn.clicked.connect(self.hide)

        def _build_messages(self):
            self.scroll = QScrollArea(self)
            self.scroll.setGeometry(8, CHAT_TITLE_H + 6, CHAT_WIN_W - 16,
                                    CHAT_WIN_H - CHAT_TITLE_H - 6 - 58)
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QScrollArea.NoFrame)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.scroll.setStyleSheet(
                "QScrollArea{background:transparent;border:none;}"
                "QScrollBar:vertical{background:transparent;width:8px;margin:2px;}"
                "QScrollBar::handle:vertical{background:%s;border-radius:4px;"
                "min-height:24px;}"
                "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical"
                "{height:0;}" % COLOR_BLUE)
            self.scroll.viewport().setStyleSheet("background:transparent;")

            self._rows = QWidget()
            self._rows.setStyleSheet("background:transparent;")
            self._rows_layout = QVBoxLayout(self._rows)
            self._rows_layout.setContentsMargins(6, 4, 6, 4)
            self._rows_layout.setSpacing(9)
            self._rows_layout.addStretch(1)
            self.scroll.setWidget(self._rows)

        def _build_input(self):
            bottom = CHAT_WIN_H - 52
            self.input = QLineEdit(self)
            self.input.setGeometry(12, bottom, CHAT_WIN_W - 12 - 82, 36)
            self.input.setFont(chat_font(12))
            self.input.setPlaceholderText("和鲸鲸说点什么…")
            self.input.setStyleSheet(
                "QLineEdit{background:#FFFFFF;border:1.5px solid %s;"
                "border-radius:12px;padding:0 12px;color:%s;}"
                "QLineEdit:focus{border:1.5px solid %s;}" % (COLOR_BLUE, COLOR_DEEP,
                                                             COLOR_DEEP))
            self.input.returnPressed.connect(self._emit_send)

            self.send_btn = QPushButton("发送", self)
            self.send_btn.setGeometry(CHAT_WIN_W - 62, bottom, 50, 36)
            self.send_btn.setFont(chat_font(12, True))
            self.send_btn.setCursor(Qt.PointingHandCursor)
            self.send_btn.setStyleSheet(
                "QPushButton{background:%s;color:#FFFFFF;border:none;"
                "border-radius:12px;}"
                "QPushButton:hover{background:#3C7FC6;}"
                "QPushButton:disabled{background:#BFD3E8;color:#F0F5FB;}"
                % COLOR_BLUE)
            self.send_btn.clicked.connect(self._emit_send)

        # ------------------------------------------------------ 画背景
        def paintEvent(self, _event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            w, h = self.width(), self.height()
            outer = QRectF(0.5, 0.5, w - 1, h - 1)

            shape = QPainterPath()
            shape.addRoundedRect(outer, CHAT_WIN_RADIUS, CHAT_WIN_RADIUS)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLOR_PAPER))
            painter.drawPath(shape)

            # 标题栏：用"圆角矩形 ∩ 顶部横条"取交集，上沿才有圆角、下沿是直的
            bar = QPainterPath()
            bar.addRect(QRectF(0, 0, w, CHAT_TITLE_H))
            painter.setBrush(QColor(COLOR_DEEP))
            painter.drawPath(shape.intersected(bar))

            painter.setPen(QPen(QColor(COLOR_BLUE), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(outer, CHAT_WIN_RADIUS, CHAT_WIN_RADIUS)
            painter.end()

        # ------------------------------------------------------ 拖动（无边框窗口自己搬）
        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton and event.position().y() <= CHAT_TITLE_H:
                self._drag_from = (event.globalPosition().toPoint()
                                   - self.frameGeometry().topLeft())
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._drag_from is not None and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_from)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            self._drag_from = None
            super().mouseReleaseEvent(event)

        # ------------------------------------------------------ 对外接口
        def open_window(self, char_rect, screen_rect):
            """显示并抢焦点 —— 要让人能直接开始打字。"""
            if not self._greeted:
                self.add_her("主人，有什么事吗？")
                self._greeted = True
            self._place(char_rect, screen_rect)
            self.show()
            self.raise_()
            self.activateWindow()
            self.input.setFocus()

        def _place(self, char_rect, screen_rect):
            """默认贴在角色左边；左边放不下就挪到右边，最后统一夹进屏幕。"""
            x = char_rect.left() - self.width() - 14
            if x < screen_rect.left() + 8:
                x = char_rect.right() + 14
            x = max(screen_rect.left() + 8,
                    min(x, screen_rect.right() - self.width() - 8))
            y = max(screen_rect.top() + 8,
                    min(char_rect.top() - 8, screen_rect.bottom() - self.height() - 8))
            self.move(int(x), int(y))

        def _row(self, label, mine):
            row = QWidget()
            row.setStyleSheet("background:transparent;")
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(0)
            if mine:
                box.addStretch(1)
                box.addWidget(label)
            else:
                box.addWidget(label)
                box.addStretch(1)
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
            self._scroll_bottom()
            return row

        def _make_label(self, text, mine):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setFont(chat_font(12))
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setMaximumWidth(int(CHAT_WIN_W * CHAT_BUBBLE_RATIO))
            label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            if mine:
                label.setStyleSheet("background:%s;color:#FFFFFF;border-radius:10px;"
                                    "padding:8px 11px;" % COLOR_DEEP)
            else:
                label.setStyleSheet("background:%s;color:%s;border:1px solid %s;"
                                    "border-radius:10px;padding:8px 11px;"
                                    % (COLOR_LIGHT, COLOR_DEEP, COLOR_BLUE))
            return label

        def add_me(self, text):
            self._row(self._make_label(text, True), True)

        def add_her(self, text):
            self._row(self._make_label(text, False), False)

        def add_note(self, text):
            """灰蓝小字：系统提示 / 工具调用流水，不算"她说的话"。

            刻意不复用 add_her —— 那条是"鲸鲸的台词"，混进系统提示会让
            聊天记录读起来分不清哪句是人说的。字体也小一号，视觉上降级。
            """
            label = QLabel(text)
            label.setWordWrap(True)
            label.setFont(chat_font(11))
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setMaximumWidth(int(CHAT_WIN_W * CHAT_BUBBLE_RATIO))
            label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
            label.setStyleSheet("background:transparent;color:%s;" % COLOR_BLUE)
            self._row(label, False)

        def begin_reply(self):
            """开一条空的"她"气泡，等着被流式填字。"""
            self._reply_label = self._make_label("", False)
            self._row(self._reply_label, False)

        def stream(self, piece):
            if self._reply_label is None:
                self.begin_reply()
            self._reply_label.setText(self._reply_label.text() + piece)
            self._scroll_bottom()

        def stream_full(self, text):
            """用整段文本刷新"她正在说"的那条气泡（流式时用）。

            不用上面的追加式 —— 心情标记是从回复**末尾**逐字长出来的，
            追加式没法把已经显示出去的半截 [mood:an 收回来；
            每次整段 setText 才能保证标记从头到尾都不可见。
            """
            if self._reply_label is None:
                self.begin_reply()
            self._reply_label.setText(text)
            self._scroll_bottom()

        def end_reply(self):
            if self._reply_label is not None and not self._reply_label.text():
                self._reply_label.setText("……")
            self._reply_label = None

        def fail_reply(self, text):
            """把还没写完的那条气泡改成错误提示。

            不新开一条 —— 否则会留下一个空的"……"气泡加一条红字，很脏。
            """
            if self._reply_label is not None:
                self._reply_label.setText(text)
                self._reply_label.setStyleSheet(
                    "color:#B4483C;background:#FDECEA;border:1px solid #E9B3AC;"
                    "border-radius:10px;padding:8px 11px;")
                self._reply_label = None
            else:
                self.add_error(text)
            self._scroll_bottom()

        def add_error(self, text):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setFont(chat_font(11))
            label.setMaximumWidth(int(CHAT_WIN_W * CHAT_BUBBLE_RATIO))
            label.setStyleSheet("color:#B4483C;background:transparent;")
            self._row(label, False)

        def clear(self):
            while self._rows_layout.count() > 1:
                item = self._rows_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            self._reply_label = None
            self._greeted = False

        def set_busy(self, busy):
            self._busy = busy
            self.send_btn.setEnabled(not busy)
            self.input.setEnabled(not busy)
            self.input.setPlaceholderText("鲸鲸正在想…" if busy else "和鲸鲸说点什么…")
            if not busy:
                self.input.setFocus()

        def _emit_send(self):
            text = self.input.text().strip()
            if not text or self._busy:
                return
            self.input.clear()
            self.on_send(text)

        def _scroll_bottom(self):
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().maximum()))

    return ChatWindow()


def build_clock_window(parent):
    """做一个和鲸鲸同风格的时钟小窗（主人文档里的要求 1）。

    显示与本地系统一致的日期和时间，右上角叉号随时可关。
    画法与聊天窗口同一套（整窗圆角 + 标题栏取交集），视觉上是一家人。
    叉号**必须接槽** —— 聊天窗那个装饰按钮漏接槽的教训刚过几天。
    """
    from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
    from PySide6.QtWidgets import QLabel, QPushButton, QWidget

    CLOCK_W, CLOCK_H = 236, 118

    class ClockWindow(QWidget):
        def __init__(self):
            super().__init__(parent)
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(CLOCK_W, CLOCK_H)

            self.title = QLabel("现在的时间", self)
            self.title.setGeometry(14, 8, 140, 20)
            self.title.setFont(chat_font(12, True))
            self.title.setStyleSheet("color:#FFFFFF;background:transparent;")

            self.close_btn = QPushButton("✕", self)
            self.close_btn.setGeometry(CLOCK_W - 40, 9, 26, 24)
            self.close_btn.setFont(chat_font(12))
            self.close_btn.setCursor(Qt.PointingHandCursor)
            self.close_btn.setStyleSheet(
                "QPushButton{background:transparent;color:#C9D8F2;border:none;"
                "border-radius:8px;}"
                "QPushButton:hover{background:rgba(255,255,255,0.18);color:#FFFFFF;}")
            self.close_btn.clicked.connect(self.hide)     # ← 上次漏的就是这行

            self.date_label = QLabel(self)
            self.date_label.setGeometry(0, CHAT_TITLE_H + 6, CLOCK_W, 22)
            self.date_label.setAlignment(Qt.AlignCenter)
            self.date_label.setFont(chat_font(12))
            self.date_label.setStyleSheet(
                "color:%s;background:transparent;" % COLOR_DEEP)

            self.time_label = QLabel(self)
            self.time_label.setGeometry(0, CHAT_TITLE_H + 30, CLOCK_W, 44)
            self.time_label.setAlignment(Qt.AlignCenter)
            self.time_label.setFont(chat_font(30, True))
            self.time_label.setStyleSheet(
                "color:%s;background:transparent;" % COLOR_BLUE)

            self._tick()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(1000)

            QShortcut(QKeySequence(Qt.Key_Escape), self, self.hide)

            self._drag_from = None

        def _tick(self):
            now = datetime.now()
            week = "一二三四五六日"[now.weekday()]
            self.date_label.setText("%d年%d月%d日 星期%s"
                                    % (now.year, now.month, now.day, week))
            self.time_label.setText(now.strftime("%H:%M:%S"))

        def paintEvent(self, _event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            w, h = self.width(), self.height()
            outer = QRectF(0.5, 0.5, w - 1, h - 1)
            shape = QPainterPath()
            shape.addRoundedRect(outer, CHAT_WIN_RADIUS, CHAT_WIN_RADIUS)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLOR_PAPER))
            painter.drawPath(shape)
            bar = QPainterPath()
            bar.addRect(QRectF(0, 0, w, CHAT_TITLE_H))
            painter.setBrush(QColor(COLOR_DEEP))
            painter.drawPath(shape.intersected(bar))
            painter.setPen(QPen(QColor(COLOR_BLUE), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(outer, CHAT_WIN_RADIUS, CHAT_WIN_RADIUS)
            painter.end()

        # 标题栏拖动 —— 和聊天窗口同一套
        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton and event.position().y() <= CHAT_TITLE_H:
                self._drag_from = (event.globalPosition().toPoint()
                                   - self.frameGeometry().topLeft())
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._drag_from is not None and event.buttons() & Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_from)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            self._drag_from = None
            super().mouseReleaseEvent(event)

        def open_window(self, anchor, screen_rect):
            """贴着角色右上角弹出来，夹进屏幕。"""
            x = anchor.x() + anchor.width() + 14
            if x + self.width() > screen_rect.right() - 8:
                x = anchor.left() - self.width() - 14
            x = max(screen_rect.left() + 8,
                    min(x, screen_rect.right() - self.width() - 8))
            y = max(screen_rect.top() + 8,
                    min(anchor.top() - 8, screen_rect.bottom() - self.height() - 8))
            self.move(int(x), int(y))
            self._tick()
            self.show()
            self.raise_()

    return ClockWindow()
