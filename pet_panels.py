# -*- coding: utf-8 -*-
"""两个新面板：**小鲸鱼记账**（余额/今日已用/峰谷）与**音效设置**。

设计口径对齐 dsh-whale-widget 那套（观感与交互，不是代码）：

* 记账面板：顶部一眼看到"总余额"，下面三张小卡片分别是**今日已用 / 近 7 天 / 峰谷**，
  再往下是柱状图和逐日明细。**"部分数据"必须显式标出来** —— 记账只在
  "程序开着 + 余额取得回来"时才发生，不标就等于骗自己今天只花了这么点。
* 音效面板：一条总开关 + 一条音量，下面每个事件一行、行尾一个「试听」——
  想试就试，不用先保存；改动全程在内存里，「保存」才落盘。

两个面板都**只做展示**：数据由桌宠通过回调喂进来（`state_fn` / `get_cfg`），
所以面板本身不碰记账本、不碰配置，逻辑仍然在可单测的那一层。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QScrollArea,
                               QSizePolicy, QSlider, QVBoxLayout, QWidget)

from pet_constants import COLOR_BLUE, COLOR_DEEP, COLOR_PAPER
from pet_time import format_alarm_time
from pet_ui import (DANGER, MUTED, OK_GREEN, WARN_AMBER, card_style,
                    dialog_style, ghost_btn, hint, label, money_text,
                    page_bg_style, page_window, primary_btn, quiet_btn,
                    section, title, ui_font)


class _WindowForward(QDialog):
    """只当"控制器"用的窗口基类：**自己永远不显示**，一切转发给 `self.frame`。

    为什么要有它：面板的内容住在 `pet_ui.page_window` 造出来的那个窗口里，
    面板类本身只需要一个地方挂方法/属性。但如果面板类**自己也是一个会显示的
    QDialog**，屏幕上就会出现"一个空窗口 + 里面一层窗口"叠着 —— 主人
    2026-07-11 报的"记账打开后有窗口嵌套叠加"就是这个（更早还有一层
    `_PageHost`，那个还带系统原生边框，叠得更明显）。

    规矩：`self.frame` 才是窗口，本类只转发 show/hide/close/isVisible。
    """
    frame = None

    # ---- 转发给真正的窗口 ----
    def show(self):
        if self.frame is not None:
            self.frame.show()
            self.frame.raise_()

    def hide(self):
        if self.frame is not None:
            self.frame.hide()

    def close(self):
        if self.frame is not None:
            self.frame.close()

    def isVisible(self):
        return bool(self.frame is not None and self.frame.isVisible())

    def raise_(self):
        if self.frame is not None:
            self.frame.raise_()

    def resize(self, *args):
        if self.frame is not None:
            self.frame.resize(*args)

    def move(self, *args):
        if self.frame is not None:
            self.frame.move(*args)

    def window(self):
        return self.frame if self.frame is not None else self


def _page_host(parent, title_text, width, height, embedded, subtitle=""):
    """铺一层"页面底"，返回 (最外层窗口, 真正的页面窗口, 往里塞内容的布局)。

    为什么要有 embedded 这条路：记账与音效这两个面板有**两个入口** ——
    右键菜单里的独立窗口，和设置窗口里的一页。外观必须是同一套（主人
    的要求），但独立窗口得有无边框圆角、关闭按钮，而设置页只是别人
    QStackedWidget 里的一块，不能再套一个窗口。

    三个返回值分别派什么用：
      * 第一个（frame）：show/hide 该打在它身上 —— **独立窗口时它就是
        page_window 本身**（不再套壳，套壳会变成"窗口里还有窗口"）；
      * 第二个（page）：真正的「页面窗口」（带标题行和关闭按钮），嵌入时
        它是面板自己；
      * 第三个（lay）：内容往这里加。
    """
    page, _ = page_window(parent, title_text, width, height, subtitle)
    if embedded:
        # 嵌进设置页：面板自己就是那一页，外面**没有**额外窗口。
        # 整窗透明这层不用了（当子控件时省一次离屏合成）。
        page.setAttribute(Qt.WA_TranslucentBackground, False)
        page_bg_style(parent)
        # 滑块的样子来自 dialog_style（轨道淡蓝、手柄白底蓝边）。设置窗口的
        # 页面只是普通 QWidget，没人给它上这套 —— 不在这里补，滑块会掉回
        # 系统默认的深灰轨道，跟整页的浅蓝很违和（截图里一眼看得出）。
        dialog_style(parent)
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        parent.setLayout(lay)
        return parent, parent, lay

    # 独立窗口：**就用 page_window 本身**（它已经无边框、圆角、带标题和关闭）。
    #
    # ⚠️ 必须 setParent(None)：Qt 的规矩是"显示一个子窗口会**连带显示它的父窗口**"，
    # 而这里的 page 起初挂在面板对象下（面板也是个 QDialog）—— 不断开的话，
    # `panel.show()` 会同时把面板自己那个空窗口显示出来，屏幕上就是两个窗口叠着
    # （主人 2026-07-11 报的"记账打开后有窗口嵌套叠加"）。
    # 断了父子之后，面板对象只当"控制器"，永远不出现在屏幕上。
    page.setParent(None)
    page.setWindowFlag(Qt.Tool, True)
    page.setModal(False)
    page.setWindowTitle(title_text)          # 任务栏/自检能认出这是哪个面板
    page.setAttribute(Qt.WA_TranslucentBackground, True)   # 圆角外面要真透明
    return page, page, page.body


def _card(widget, padding=12, bg="#FFFFFF"):
    """把 widget 变成一张卡片（objectName 是样式表限定符，别去掉）。

    `bg` 给的是"面板底"的颜色：卡片圆角与页面底之间有一圈细缝，颜色贴合
    才不会露出白角 —— 独立窗口是白底（默认值），设置页里传淡蓝页面底。
    """
    widget.setObjectName("card")
    widget.setStyleSheet(card_style(padding=padding, background=bg))
    return widget


# ---------------------------------------------------------------------------
# 近 7 天柱状图：自绘（几行 QPainter，比塞一个图表库轻得多）
# ---------------------------------------------------------------------------
class _Bars(QWidget):
    """近 N 天消费柱状图。数据 = [(day, amount or None, partial)]。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, data):
        self._data = list(data or [])
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        base_y = h - 20                      # 留给日期标签
        amounts = [a for _d, a, _p in self._data if a]
        top = max(amounts) if amounts else 1.0
        if top <= 0:
            top = 1.0
        n = max(1, len(self._data))
        slot = w / n
        bar_w = min(34.0, slot * 0.52)
        painter.setFont(ui_font(9))
        for i, (day, amount, partial) in enumerate(self._data):
            cx = slot * (i + 0.5)
            ratio = 0.0 if not amount else max(0.0, min(1.0, amount / top))
            bar_h = max(2.0, (base_y - 16) * ratio)
            rect_x = cx - bar_w / 2
            rect_y = base_y - bar_h
            path = QPainterPath()
            path.addRoundedRect(rect_x, rect_y, bar_w, bar_h, 5, 5)
            today = (i == len(self._data) - 1)
            color = QColor(COLOR_BLUE if today else "#A9C8EC")
            if partial:
                color = QColor(WARN_AMBER if today else "#E8C79A")
            painter.fillPath(path, color)
            # 金额（柱子够高才写在柱子上，否则写在柱顶上方）
            painter.setPen(QColor(COLOR_DEEP))
            text = "—" if amount is None else ("%.2f" % amount)
            if bar_h > 26:
                painter.drawText(int(rect_x) - 6, int(rect_y) + 15,
                                 int(bar_w) + 12, 14,
                                 Qt.AlignCenter, text)
            else:
                painter.drawText(int(rect_x) - 10, int(rect_y) - 17,
                                 int(bar_w) + 20, 14,
                                 Qt.AlignCenter, text)
            painter.setPen(QColor(MUTED))
            painter.drawText(int(cx - slot / 2), base_y + 3, int(slot), 16,
                             Qt.AlignCenter, str(day)[5:])   # 只显示 MM-DD


# ---------------------------------------------------------------------------
# 小鲸鱼记账面板
# ---------------------------------------------------------------------------
def build_balance_panel(parent, state_fn, on_refresh, on_reconcile,
                        on_save_warn=None, embedded=False):
    """记账面板。

    state_fn() -> {
        "balance": {"total","granted","recharge","currency","at"} | None,
        "today":   day_summary | None,
        "recent":  [(day, summary|None), ...],
        "peak":    {...},
        "note":    "顶部一句提示（可选）",
        "warn":    {"below": 金额, "above": 金额}（可选，预警线）
    }
    on_refresh()            —— 让桌宠去查一次余额（异步；查完自己调 refresh()）
    on_reconcile(credits, other_debits) -> summary | None
    on_save_warn(below, above) -> bool   —— 存两条预警线（0 = 关）
    embedded=True           —— 当成设置窗口里的一页（不建自己的窗口壳）
    """

    class BalancePanel(_WindowForward):
        def __init__(self):
            super().__init__(parent)
            # 标题行由外壳负责（page_window 里有标题 + 关闭按钮），
            # 所以窗口标题只留给任务栏/自检看。
            self.setWindowTitle("小鲸鱼记账")
            if not embedded:
                self.setWindowFlag(Qt.Tool, True)
                self.setModal(False)
                self.resize(620, 700)
                # ⚠️ 面板对象自己**永远不显示**：它起初挂在可见的桌宠窗口下，
                # 而 Qt 会把"子窗口"跟着父窗口一起显示出来 —— 屏幕上于是多出一个
                # 100x30 的空窗口叠在面板上（主人报的"窗口嵌套叠加"就是这个 +
                # 更早那层带系统边框的 _PageHost）。摘掉父子关系后它就只是个
                # "控制器"，真正的窗口是 self.frame。
                self.setParent(None)
            # 面板内部卡片缝隙的颜色跟着宿主走：独立窗口是白卡片底，
            # 设置页里是淡蓝页面底（否则圆角处会露一圈白角）
            self._card_bg = COLOR_PAPER if embedded else "#FFFFFF"

            # 前两个是"窗口栈"：frame 是最外层（show/hide 要打在它身上），
            # page 是真正带标题行的那层；第三个才是放内容的布局。
            self.frame, self.win, root = _page_host(
                self, "小鲸鱼记账", 620, 700, embedded,
                "余额、今日已用、峰谷与逐日明细")

            # 头部那行：左边一句说明（note），右边"刷新余额"。
            # 标题和关闭都在外壳上，面板里不再重复画一遍。
            # 说明文字可能折成两行，所以按钮**顶部对齐** —— 居中的话它会
            # 悬在两行中间，看着像浮在空中（截图里一眼看出来过）。
            head = QHBoxLayout()
            head.setSpacing(10)
            self.note = hint("")
            head.addWidget(self.note, 1)
            self.refresh_btn = ghost_btn("刷新余额")
            self.refresh_btn.clicked.connect(lambda: on_refresh())
            head.addWidget(self.refresh_btn, 0, Qt.AlignTop)
            root.addLayout(head)

            # ---- 三张小卡片 ----
            cards = QHBoxLayout()
            cards.setSpacing(10)
            self.big_card, self.big_value, self.big_sub = self._make_card(
                "总余额", "—", "")
            self.today_card, self.today_value, self.today_sub = self._make_card(
                "今日已用", "—", "")
            self.peak_card, self.peak_value, self.peak_sub = self._make_card(
                "峰谷", "—", "")
            for c in (self.big_card, self.today_card, self.peak_card):
                cards.addWidget(c, 1)
            root.addLayout(cards)

            # ---- 近 7 天 ----
            chart_card = _card(QFrame(), bg=self._card_bg)
            chart_lay = QVBoxLayout(chart_card)
            chart_lay.setContentsMargins(12, 10, 12, 10)
            chart_lay.setSpacing(6)
            chart_lay.addWidget(section("近 7 天消费"))
            self.bars = _Bars()
            chart_lay.addWidget(self.bars)
            root.addWidget(chart_card)

            # ---- 逐日明细 ----
            root.addWidget(section("逐日明细"))
            self.detail_area = QScrollArea()
            self.detail_area.setWidgetResizable(True)
            # 明细区的背景**必须透明**：QScrollArea 默认会铺一层浅灰，
            # 空列表时那条灰带很显眼（截图里一眼看到过），跟整页的白卡片不搭。
            self.detail_area.setStyleSheet(
                "QScrollArea{background:transparent;border:none;}"
                "QScrollArea > QWidget > QWidget{background:transparent;}")
            self.detail_area.viewport().setStyleSheet("background:transparent;")
            self.detail_host = QWidget()
            self.detail_lay = QVBoxLayout(self.detail_host)
            self.detail_lay.setContentsMargins(2, 2, 2, 2)
            self.detail_lay.setSpacing(6)
            self.detail_area.setWidget(self.detail_host)
            root.addWidget(self.detail_area, 1)

            # ---- 预警线（挂件的"余额预警 / 今日预算"，0 = 关）----
            warn_card = _card(QFrame(), bg=self._card_bg)
            warn_lay = QHBoxLayout(warn_card)
            warn_lay.setContentsMargins(12, 8, 12, 8)
            warn_lay.setSpacing(8)
            warn_lay.addWidget(label("提醒我", 11))
            warn_lay.addWidget(hint("余额低于"))
            self.warn_below = QLineEdit("0")
            self.warn_below.setFixedWidth(74)
            self.warn_below.setFont(ui_font(12))
            warn_lay.addWidget(self.warn_below)
            warn_lay.addWidget(hint("今日超过"))
            self.warn_above = QLineEdit("0")
            self.warn_above.setFixedWidth(74)
            self.warn_above.setFont(ui_font(12))
            warn_lay.addWidget(self.warn_above)
            warn_lay.addWidget(hint("元（0 = 不提醒）"))
            warn_lay.addStretch(1)
            self.warn_btn = quiet_btn("保存")
            self.warn_btn.clicked.connect(self._save_warn)
            warn_lay.addWidget(self.warn_btn)
            root.addWidget(warn_card)

            foot = QHBoxLayout()
            self.fix_btn = quiet_btn("余额校正…")
            self.fix_btn.clicked.connect(self._ask_reconcile)
            self.fix_btn.setToolTip(
                "中途充值 / 平台调过余额时，用这个把某天的消费重算准")
            foot.addWidget(self.fix_btn)
            foot.addStretch(1)
            # 页脚**不再放第二个「关闭」**：标题行右上角已经有了（page_window 自带），
            # 再放一个就是两个关闭按钮并排，一眼就很业余（主人截图里看到过）。
            # 属性名 `close_btn` 留着指向标题行那个 —— 自检/调用方都在用。
            self.close_btn = self.win.close_btn
            root.addLayout(foot)

        # ---- 显隐：调用方只认 show()/raise_()，这里转给真正的窗口 ----
        def show(self):
            if embedded:
                QDialog.show(self)
            else:
                self.frame.show()

        def raise_(self):
            if embedded:
                QDialog.raise_(self)
            else:
                self.frame.raise_()

        # ---- 小工具 ----
        def _make_card(self, cap, value, sub):
            box = _card(QFrame(), bg=self._card_bg)
            # 三张卡片**等高**：右边两张的字会换行（"进入谷价 4 小时 12 分"），
            # 不锁高度的话三张卡片会参差不齐，一眼就看得出没对齐
            box.setMinimumHeight(104)
            lay = QVBoxLayout(box)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(2)
            lay.addWidget(hint(cap))
            val = label(value, size=18, bold=True, color=COLOR_DEEP)
            lay.addWidget(val)
            sb = hint(sub)
            lay.addWidget(sb)
            lay.addStretch(1)
            return box, val, sb

        def _ask_reconcile(self):
            """余额校正：问两个数，交给桌宠算。"""
            dlg = QDialog(self)
            dlg.setWindowTitle("余额校正")
            dlg.setWindowFlag(Qt.Tool, True)
            dialog_style(dlg)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(8)
            lay.addWidget(label("这段时间里实际到账了多少？", 12, bold=True))
            lay.addWidget(hint("比如中途充了 50 元、平台送了 5 元，就填 55。"
                               "填 0 表示没充值。"))
            credit = QLineEdit("0")
            credit.setFont(ui_font(12))
            other = QLineEdit("0")
            other.setFont(ui_font(12))
            lay.addWidget(label("累计到账（充值 + 赠金）", 11))
            lay.addWidget(credit)
            lay.addWidget(label("其它非调用扣减（一般填 0）", 11))
            lay.addWidget(other)
            tip = hint("")
            lay.addWidget(tip)
            row = QHBoxLayout()
            row.addStretch(1)
            cancel = ghost_btn("取消")
            cancel.clicked.connect(dlg.reject)
            ok = primary_btn("重算")
            row.addWidget(cancel)
            row.addWidget(ok)
            lay.addLayout(row)

            def do_it():
                try:
                    c = float(credit.text().strip() or 0)
                    o = float(other.text().strip() or 0)
                except ValueError:
                    tip.setText("数字填得不对，只能填数字。")
                    return
                result = on_reconcile(c, o)
                if result is None:
                    tip.setText("算出来是负数（到账/扣减和余额对不上），"
                                "数字再核一下。")
                    return
                dlg.accept()
                self.refresh()

            ok.clicked.connect(do_it)
            dlg.show()

        def _save_warn(self):
            """存两条预警线。数字填不对就不写，别把配置写坏。"""
            if on_save_warn is None:
                return
            try:
                below = float(self.warn_below.text().strip() or 0)
                above = float(self.warn_above.text().strip() or 0)
            except ValueError:
                self.note.setText("预警线只能填数字。")
                return
            if on_save_warn(max(0.0, below), max(0.0, above)):
                self.note.setText("预警线已保存：余额低于 %s / 今日超过 %s 时她会喊你。"
                                  % (_fmt(below), _fmt(above)))
            else:
                self.note.setText("预警线写不进去（目录只读或被占用？）")

        # ---- 刷新 ----
        def refresh(self):
            st = state_fn() or {}
            bal = st.get("balance") or {}
            currency = bal.get("currency") or "CNY"
            if bal:
                self.big_value.setText(money_text(bal.get("total"), currency))
                granted = float(bal.get("granted") or 0)
                recharge = float(bal.get("recharge") or 0)
                parts = ["充值 %s" % money_text(recharge, currency)]
                if granted > 0:
                    parts.append("赠金 %s" % money_text(granted, currency))
                self.big_sub.setText(" · ".join(parts))
            else:
                self.big_value.setText("—")
                self.big_sub.setText("还没查到，点「刷新余额」")

            today = st.get("today")
            if today:
                self.today_value.setText(money_text(today["amount"], currency))
                # 时间区间不带空格、标记用短词：卡片窄，长一句就折行成
                # "（部分数/据）" 那种断法，很不好看
                span = "%s→%s" % (_hhmm(today.get("first_at")),
                                  _hhmm(today.get("last_at")))
                mark = "（部分）" if today.get("partial") else ""
                if today.get("corrected"):
                    mark += "（校正）"
                self.today_sub.setText(span + mark)
                self.today_value.setStyleSheet(
                    "color:%s;background:transparent;"
                    % (WARN_AMBER if today.get("partial") else COLOR_DEEP))
            else:
                self.today_value.setText("—")
                self.today_sub.setText("今天还没观测到余额")
                self.today_value.setStyleSheet("color:%s;background:transparent;"
                                               % COLOR_DEEP)

            peak = st.get("peak") or {}
            off = bool(peak.get("offpeak"))
            self.peak_value.setText("谷价" if off else "峰价")
            self.peak_value.setStyleSheet(
                "color:%s;background:transparent;"
                % (OK_GREEN if off else WARN_AMBER))
            # 带上**为什么**（周末 / 法定节假日 / 高峰时段）：
            # 主人看到"今天是谷价"时会想知道原因，不然会以为算错了
            why = peak.get("why") or ""
            tail = "%s %s" % (peak.get("next_label", ""),
                              peak.get("remain_text", ""))
            self.peak_sub.setText(("%s · %s" % (why, tail)) if why else tail)
            self.note.setText(st.get("note") or
                              "记账靠「观测余额」推算：余额降 = 花掉的，升 = 充值。")
            recent = st.get("recent") or []
            warn = st.get("warn") or {}
            if not self.warn_below.hasFocus():
                self.warn_below.setText(_fmt(warn.get("below") or 0))
            if not self.warn_above.hasFocus():
                self.warn_above.setText(_fmt(warn.get("above") or 0))
            self.bars.set_data([(d, (s or {}).get("amount"), (s or {}).get("partial"))
                                for d, s in recent])
            self._fill_details(recent, currency)

        def _fill_details(self, recent, currency):
            while self.detail_lay.count():
                item = self.detail_lay.takeAt(0)
                wdg = item.widget()
                if wdg is not None:
                    wdg.deleteLater()
            rows = [(d, s) for d, s in reversed(recent) if s]
            if not rows:
                self.detail_lay.addWidget(hint("还没有记录。点上面「刷新余额」"
                                               "就能开始记第一笔。"))
                self.detail_lay.addStretch(1)
                return
            for day, s in rows:
                card = _card(QFrame(), padding=10, bg=self._card_bg)
                lay = QHBoxLayout(card)
                lay.setContentsMargins(10, 6, 10, 6)
                lay.addWidget(label(str(day), 12, bold=True))
                amount = label(money_text(s.get("amount"), currency), 13, bold=True)
                lay.addWidget(amount)
                lay.addStretch(1)
                span = "%s→%s" % (_hhmm(s.get("first_at")), _hhmm(s.get("last_at")))
                extra = []
                if s.get("increase"):
                    extra.append("充值 %s" % money_text(s["increase"], currency))
                if s.get("corrected"):
                    extra.append("校正")
                if s.get("partial"):
                    extra.append("部分")
                text = span + (("　" + " · ".join(extra)) if extra else "")
                lb = hint(text)
                if s.get("partial"):
                    lb.setStyleSheet("color:%s;background:transparent;" % WARN_AMBER)
                lay.addWidget(lb)
                self.detail_lay.addWidget(card)
            self.detail_lay.addStretch(1)

    return BalancePanel()


def _hhmm(ts):
    """时间戳 → 北京时间的 HH:MM。"""
    from datetime import datetime, timedelta, timezone
    if not ts:
        return "--:--"
    return datetime.fromtimestamp(float(ts),
                                  timezone(timedelta(hours=8))).strftime("%H:%M")


def _fmt(value):
    """预警线的显示：整数就不带小数（"50" 比 "50.00" 好读）。"""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    return "%g" % num


# ---------------------------------------------------------------------------
# 音效设置
# ---------------------------------------------------------------------------
def build_sound_dialog(parent, get_cfg, on_save, preview, on_config_changed,
                       embedded=False):
    """音效设置面板。

    get_cfg() -> {"enabled":bool, "volume":int, "events":{事件:bool}}
    on_save(cfg) -> bool            （写配置；返回是否成功）
    preview(name)                   （试听）
    on_config_changed(cfg)          （立刻生效，不用等保存 —— 试听要用新音量）
    embedded=True                   —— 当成设置窗口里的一页（不建自己的窗口壳）
    """
    from pet_sound import labels as sound_labels

    class SoundDialog(_WindowForward):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("音效设置")
            if not embedded:
                self.setWindowFlag(Qt.Tool, True)
                self.setModal(False)
                self.resize(520, 560)
                # 同 BalancePanel：面板对象自己不显示，摘掉父子关系免得
                # Qt 把"子窗口"跟着可见的桌宠窗口一起显示出来（窗口嵌套叠加）
                self.setParent(None)
            self._card_bg = COLOR_PAPER if embedded else "#FFFFFF"

            self.frame, self.win, root = _page_host(
                self, "音效", 520, 560, embedded,
                "总开关、音量与每个事件的试听")

            root.addWidget(hint("音效文件是程序现场合成的（几 KB），"
                                "不引用任何第三方素材。"))

            top = _card(QFrame(), bg=self._card_bg)
            top_lay = QGridLayout(top)
            top_lay.setContentsMargins(12, 10, 12, 10)
            top_lay.setHorizontalSpacing(10)
            self.enabled = QCheckBox("开启音效")
            self.enabled.setFont(ui_font(12, True))
            top_lay.addWidget(self.enabled, 0, 0, 1, 2)
            self.vol_label = label("音量 70%", 11)
            top_lay.addWidget(self.vol_label, 1, 0)
            self.volume = QSlider(Qt.Horizontal)
            self.volume.setRange(0, 100)
            self.volume.setSingleStep(5)
            self.volume.valueChanged.connect(self._on_volume)
            top_lay.addWidget(self.volume, 1, 1)
            root.addWidget(top)

            root.addWidget(section("哪些时候响"))
            self.events = {}
            rows = _card(QFrame(), bg=self._card_bg)
            rows_lay = QVBoxLayout(rows)
            rows_lay.setContentsMargins(12, 8, 12, 8)
            rows_lay.setSpacing(6)
            for key, text in sound_labels():
                line = QHBoxLayout()
                box = QCheckBox(text)
                box.setFont(ui_font(12))
                box.stateChanged.connect(lambda *_a: self._push())
                line.addWidget(box, 1)
                try_btn = quiet_btn("试听")
                try_btn.setMinimumWidth(56)
                try_btn.clicked.connect(lambda _c=False, k=key: self._preview(k))
                line.addWidget(try_btn)
                rows_lay.addLayout(line)
                self.events[key] = box
            root.addWidget(rows)
            root.addStretch(1)

            foot = QHBoxLayout()
            reset = quiet_btn("恢复默认")
            reset.clicked.connect(self._reset)
            foot.addWidget(reset)
            foot.addStretch(1)
            cancel = ghost_btn("取消")
            cancel.clicked.connect(self.reload)
            save = primary_btn("保存")
            save.clicked.connect(self._save)
            foot.addWidget(cancel)
            foot.addWidget(save)
            root.addLayout(foot)
            self.status = hint("")
            root.addWidget(self.status)
            self.reload()

        # ---- 读写 ----
        def reload(self):
            cfg = get_cfg() or {}
            self.enabled.setChecked(bool(cfg.get("enabled", True)))
            self.volume.setValue(int(cfg.get("volume", 70)))
            events = cfg.get("events") or {}
            for key, box in self.events.items():
                box.setChecked(bool(events.get(key, True)))
            self._push()
            self.status.setText("")

        def _values(self):
            return {
                "enabled": self.enabled.isChecked(),
                "volume": int(self.volume.value()),
                "events": {k: b.isChecked() for k, b in self.events.items()},
            }

        def _push(self):
            """改动立刻生效（内存里），这样「试听」听到的就是当前音量。"""
            cfg = self._values()
            on_config_changed(cfg)
            self.vol_label.setText("音量 %d%%" % cfg["volume"])

        def _on_volume(self, _v):
            self._push()

        def _preview(self, key):
            self._push()
            preview(key)

        def _reset(self):
            self.enabled.setChecked(True)
            self.volume.setValue(70)
            for box in self.events.values():
                box.setChecked(True)
            self._push()
            self.status.setText("已恢复默认（点「保存」才会写进配置）")

        def _save(self):
            cfg = self._values()
            if on_save(cfg):
                self.status.setText("已保存。")
                self._push()
            else:
                self.status.setText("配置写不进去（目录只读或被占用？）")
                self.status.setStyleSheet("color:%s;background:transparent;" % DANGER)

        # 显隐转给真正的窗口（理由同记账面板：独立窗口下外面还有一层转发壳）
        def show(self):
            if embedded:
                QDialog.show(self)
            else:
                self.frame.show()

        def raise_(self):
            if embedded:
                QDialog.raise_(self)
            else:
                self.frame.raise_()

    return SoundDialog()


# ---------------------------------------------------------------------------
# 查看提醒：把挂着的提醒列成卡片，每条自带「取消」
#
# 以前「查看提醒」只是让桌宠念一遍（见 pet_mixin_ui.show_alarms）：念完就没了，
# 主人想删掉一条得原话告诉她自己取消 —— 界面里能看见、能直接删才算"查得到"。
# ---------------------------------------------------------------------------
class AlarmWindow:
    """提醒列表窗口（薄壳：真正的控件在 self.win 里）。"""

    def __init__(self, parent, alarms, on_cancel, on_close=None):
        self._on_cancel = on_cancel
        self.win, _ = page_window(parent, "提醒", 560, 520, "")

        self.host = QWidget()
        page_bg_style(self.host)
        box = QVBoxLayout(self.host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        inner = QWidget()
        self.list_lay = QVBoxLayout(inner)
        self.list_lay.setContentsMargins(0, 0, 6, 0)
        self.list_lay.setSpacing(8)
        self.area.setWidget(inner)
        box.addWidget(self.area, 1)

        foot = QHBoxLayout()
        self.count = hint("")
        foot.addWidget(self.count)
        foot.addStretch(1)
        # 这里**不再放第二个「关闭」**：外壳（page_window）标题行右上角已经有一个，
        # 页脚再来一个就是两个关闭按钮并排（截图里一眼看到，很业余）。
        # 但属性名 `close_btn` 要留着 —— 自检和调用方都在用它。
        self.close_btn = self.win.close_btn
        box.addLayout(foot)

        self.win.body.addWidget(self.host, 1)
        if on_close is not None:
            # page_window 的关闭按钮本来就接着 hide，这里只多挂一个回调
            self.win.close_btn.clicked.connect(on_close)
        self.reload(alarms)

    # ---- 对外 ----
    def reload(self, alarms=None):
        """重画列表。alarms 给 None 时沿用上一次那份（取消一条后重画用）。"""
        if alarms is not None:
            self._alarms = list(alarms)
        rows = list(getattr(self, "_alarms", []))
        while self.list_lay.count():
            item = self.list_lay.takeAt(0)
            old = item.widget()
            if old is not None:
                old.setParent(None)      # 先摘出父控件，再排队销毁
                old.deleteLater()
        if not rows:
            self.list_lay.addWidget(
                hint("现在没有挂着的提醒。跟她说一句「十分钟后叫我喝水」"
                     "就会出现在这里。"))
        else:
            for item in rows:
                self._add_row(item)
        self.list_lay.addStretch(1)
        self.count.setText("共 %d 条" % len(rows))

    def hide(self):
        self.win.hide()

    def __getattr__(self, name):
        try:
            win = self.__dict__["win"]
        except KeyError:
            raise AttributeError(name)
        return getattr(win, name)

    # ---- 内部 ----
    def _add_row(self, item):
        card = _card(QFrame(), padding=10, bg=COLOR_PAPER)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        text = QVBoxLayout()
        text.setSpacing(2)
        note = label(str(item.get("note") or ""), 13, bold=True)
        note.setWordWrap(True)
        text.addWidget(note)
        when = item.get("when")
        # 时间那一行尽量说人话（"今天 08:00"/"明天 09:30"）；真解析不出来
        # （比如文件被手改成怪值）就退回原始字符串，总比整条卡片打不开强
        try:
            stamp = format_alarm_time(when) if when is not None else "时间不明"
        except Exception:
            stamp = str(when or "时间不明")
        text.addWidget(hint("%s · %s" % (stamp, item.get("kind") or "长期")))
        lay.addLayout(text, 1)

        cancel = quiet_btn("取消")
        cancel.clicked.connect(
            lambda _c=False, it=dict(item): self._cancel(it))
        lay.addWidget(cancel, 0, Qt.AlignTop)
        self.list_lay.addWidget(card)

    def _cancel(self, item):
        """取消一条：交给桌宠去删（它才知道短期/长期两种记在哪），然后重画。"""
        gone = 0
        if self._on_cancel is not None:
            try:
                gone = int(self._on_cancel(item) or 0)
            except Exception:
                gone = 0
        # 不管桌宠报不报成功，都把这条从列表里拿掉：留着一条"点了没反应"的
        # 卡片比空列表更让人困惑（配置写不进去时桌宠那边会自己说一句）
        left = [a for a in getattr(self, "_alarms", [])
                if not (a.get("id") and a.get("id") == item.get("id"))
                and not (a.get("note") == item.get("note")
                         and a.get("kind") == item.get("kind"))]
        self.reload(left)
        if gone <= 0:
            self.count.setText("这条没能删掉（配置写不进去？）")


def build_alarm_window(parent, alarms, on_cancel, on_close=None):
    """提醒窗口：每条一张卡片（事项 + 时间 + 「取消」），空列表给一句提示。

    alarms 每项：{"note": 事项, "when": datetime, "id": 可选, "kind": 可选}
    on_cancel(item) -> 删掉了几条
    on_close()      —— 关闭时额外做的收尾（调用方用来摘引用防 GC）
    """
    return AlarmWindow(parent, alarms, on_cancel, on_close=on_close)
