# -*- coding: utf-8 -*-
"""UI 小工具箱：**一处定义**字体、卡片、按钮、标题的样式。

为什么要单独抽：飞书/远程/钥匙/音效/记账这几个框原本各写各的
`setStyleSheet`，颜色和圆角很容易走形（同一个蓝色出现过三种写法）。
这里把它们收成几个函数，新窗口照着调，老窗口逐步换过来。

设计口径（对齐 dsh-whale-widget 的观感）：
  * 白卡片 + 1px 鲸鱼蓝描边 + 10~12px 圆角，卡片之间留 10~12px；
  * 主按钮：实心鲸鱼蓝 + 白色粗体；次按钮：淡蓝底 + 深蓝字；危险操作用砖红字；
  * 标题字号 13~15、正文 12、说明小字 10~11 且用灰蓝 —— 层级靠字号和灰度拉，不靠线框；
  * 金额/数字用稍大的字重，让它"一眼能读到"。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QPushButton

from pet_constants import (BUBBLE_FONT_FAMILIES, COLOR_BLUE, COLOR_DEEP,
                           COLOR_LIGHT, COLOR_PAPER)
CARD_BORDER = COLOR_BLUE
MUTED = "#7C8CA3"        # 说明小字：灰蓝，压在白卡片上不刺眼
DANGER = "#B4483C"       # 危险/警告：砖红
OK_GREEN = "#2F7D4F"     # 成功：草绿
WARN_AMBER = "#C8842A"   # 提醒：琥珀

RADIUS = 12


def ui_font(size, bold=False):
    """统一字体族（和气泡同一套，中文不会掉到难看的回退字体上）。"""
    font = QFont()
    font.setFamilies(list(BUBBLE_FONT_FAMILIES))
    font.setPixelSize(int(size))
    font.setBold(bool(bold))
    return font


def card_style(padding=12, radius=RADIUS, background="#FFFFFF", border=True,
               name="card"):
    """白卡片样式。**必须配合 setObjectName(name)**（见 pet_panels._card）。

    为什么用 `QFrame#card` 这种限定选择器：Qt 的样式表**会向子控件继承** ——
    直接给卡片写 `border:1px solid ...`，里面的每个 QLabel / QCheckBox / 滑块
    都会跟着长出边框来（第一版就这样，截图里一行行小方框全是这个原因）。
    用 id 选择器 + 给子控件显式清掉边框，`objectName` 只命中卡片本身。
    """
    line = ("border:1px solid %s;" % CARD_BORDER) if border else "border:none;"
    return (
        "QFrame#%s{background:%s;%s border-radius:%dpx;padding:%dpx;}"
        "QFrame#%s > QLabel,QFrame#%s > QCheckBox,QFrame#%s > QSlider,"
        "QFrame#%s > QLineEdit{background:transparent;border:none;}"
        % (name, background, line, radius, padding,
           name, name, name, name))


def label(text, size=12, bold=False, color=COLOR_DEEP, wrap=False):
    """一段统一风格的文字标签。"""
    lb = QLabel(text)
    lb.setFont(ui_font(size, bold))
    lb.setStyleSheet("color:%s;background:transparent;" % color)
    if wrap:
        lb.setWordWrap(True)
    return lb


def hint(text, wrap=True):
    """说明小字（灰蓝）。"""
    return label(text, size=10, color=MUTED, wrap=wrap)


def title(text):
    """窗口内的大标题。"""
    return label(text, size=15, bold=True)


def section(text):
    """小节标题：比正文粗一点、比大标题小一点。"""
    return label(text, size=12, bold=True, color=COLOR_BLUE)


def _button(text, bg, fg, border=""):
    btn = QPushButton(text)
    btn.setFont(ui_font(12, bold=(fg == "#FFFFFF")))
    btn.setCursor(Qt.PointingHandCursor)
    btn.setMinimumHeight(30)
    css = ("QPushButton{background:%s;color:%s;border:%s;"
           "border-radius:10px;padding:5px 16px;}"
           % (bg, fg, border or "none"))
    if bg == COLOR_BLUE:
        css += "QPushButton:hover{background:#3C7FC6;}"
    elif bg == COLOR_LIGHT:
        css += "QPushButton:hover{background:#D6E9FF;}"
    css += "QPushButton:disabled{background:#E7EDF5;color:#A9B6C7;}"
    btn.setStyleSheet(css)
    return btn


def primary_btn(text):
    """主操作：实心蓝。"""
    return _button(text, COLOR_BLUE, "#FFFFFF")


def ghost_btn(text):
    """次操作：淡蓝底深蓝字。"""
    return _button(text, COLOR_LIGHT, COLOR_DEEP)


def quiet_btn(text):
    """最轻的操作：白底描边（用于"试听"这类附属动作）。"""
    return _button(text, "#FFFFFF", COLOR_DEEP, border="1px solid %s" % COLOR_BLUE)


def dialog_style(dialog):
    """给整个对话框上底色与字体（子控件仍可用卡片样式覆盖）。"""
    dialog.setStyleSheet(
        "QDialog{background:%s;}"
        "QLabel{color:%s;}"
        "QCheckBox{color:%s;font-size:12px;}"
        "QScrollArea{border:none;background:transparent;}"
        # 滑块：不写这首的话，轨道右侧是系统默认的深色块，跟浅蓝卡片很违和
        "QSlider::groove:horizontal{height:6px;background:#E3ECF7;"
        "border-radius:3px;}"
        "QSlider::sub-page:horizontal{background:%s;border-radius:3px;}"
        "QSlider::add-page:horizontal{background:#E3ECF7;border-radius:3px;}"
        "QSlider::handle:horizontal{background:#FFFFFF;border:2px solid %s;"
        "width:13px;height:13px;margin:-5px 0;border-radius:8px;}"
        % (COLOR_PAPER, COLOR_DEEP, COLOR_DEEP, COLOR_BLUE, COLOR_BLUE))


def money_text(value, currency="CNY", digits=2):
    """金额显示：两位小数 + 币种符号（人民币用 ¥，其它用代码）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    mark = "¥" if str(currency).upper() == "CNY" else (str(currency).upper() + " ")
    return "%s%.*f" % (mark, digits, num)


def menu_style():
    """右键菜单的样式：和桌宠本体同一套视觉语言。

    对齐的是气泡/聊天框那套 —— 白底、鲸鱼蓝 1.8px 描边、圆角 12、
    文字用 COLOR_DEEP（#1E3A6E），选中项是淡蓝底而不是系统高亮蓝。
    不写 emoji：菜单是**功能入口**，靠分组和留白看就行（主人 2026-10-11 定的）。
    """
    return (
        "QMenu{background:#FFFFFF;border:1.8px solid %s;border-radius:12px;"
        "padding:6px;}"
        "QMenu::item{background:transparent;color:%s;padding:7px 18px;"
        "margin:1px 5px;border-radius:8px;}"
        "QMenu::item:selected{background:%s;color:%s;}"
        "QMenu::item:disabled{color:%s;padding:8px 18px;}"
        "QMenu::separator{height:1px;background:#E6EEF8;margin:6px 14px;}"
        "QMenu::right-arrow{width:9px;height:9px;}"
        "QMenu::indicator{width:0px;height:0px;}"     # 没有勾选项，别留空槽
        % (CARD_BORDER, COLOR_DEEP, COLOR_LIGHT, COLOR_DEEP, MUTED))


def style_menu(menu):
    """给菜单（含子菜单）上样式、字体，并**去掉系统原生边框**。

    为什么要显式去框：Windows 给弹出菜单套一圈原生边框 + 阴影，
    它会把我们画的圆角切在外面 —— 屏幕上看到的是"圆角卡片外套一个方框"。
    FramelessWindowHint 去掉那圈框，WA_TranslucentBackground 让圆角外面
    真的透明（不然四个角会露出窗口底色）。样式表里已经写了整块背景，
    所以不会出现"透明到发黑"的经典问题。

    子菜单是**独立的弹出窗口**，样式与窗口标志都不会自动继承，
    所以这里递归一遍 —— 否则"做个表情"点开还是系统默认长相，一眼就露馅。
    """
    from PySide6.QtWidgets import QMenu
    menu.setFont(ui_font(12))
    # 一次设全（不用 setWindowFlag 逐个设：实测 NoDropShadowWindowHint 那样设
    # 会被 Qt 丢掉，两个窗口标志要一起给）
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint
                        | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
    menu.setStyleSheet(menu_style())
    for action in menu.actions():
        sub = action.menu()
        if isinstance(sub, QMenu):
            style_menu(sub)
    return menu


# ---------------------------------------------------------------------------
# 「页面窗口」外壳：给设置 / 记账 / 提醒这些窗口用同一套长相
#
# 比例参考主人给的 PCL 截图（左竖栏 + 右侧白卡片），但配色仍用桌宠自己那套。
# 把外壳收在这里的原因和 card_style 一样：三个窗口各画一遍迟早走形。
# ---------------------------------------------------------------------------
RAIL_W = 176          # 左侧竖栏宽
RAIL_ITEM_H = 46      # 竖栏条目高
RAIL_MARGIN = 10      # 竖栏内左右留白
PAGE_RADIUS = 14      # 内容卡片圆角


def rail_style(background="#F1F6FD"):
    """左侧竖栏的样式（浅蓝底 + 右侧 1px 分隔线）。"""
    return ("QWidget#rail{background:%s;border-right:1px solid #DCE8F6;}"
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}" % background)


def rail_item_style(active=False):
    """竖栏里一个条目（任务/分类）的样式。

    选中态照 PCL 的做法：白底 + 左侧 3px 蓝条 + 深蓝粗体；未选中是透明底。
    用 `border-left` 做那条蓝条，比再塞一个 QFrame 省事，也不会把文字挤歪。
    """
    if active:
        return ("QPushButton{background:#FFFFFF;color:%s;border:none;"
                "border-left:3px solid %s;border-radius:10px;"
                "padding:6px 10px;text-align:left;font-weight:bold;}"
                "QPushButton:hover{background:#FFFFFF;}" % (COLOR_BLUE, COLOR_BLUE))
    return ("QPushButton{background:transparent;color:%s;border:none;"
            "border-left:3px solid transparent;border-radius:10px;"
            "padding:6px 10px;text-align:left;}"
            "QPushButton:hover{background:#E4EFFC;}" % COLOR_DEEP)


def page_bg_style(widget):
    """把一个窗口设成"页面底"：淡蓝底 + 透明子控件（卡片自己带白底）。"""
    widget.setStyleSheet(
        "QWidget{background:%s;color:%s;}"
        "QScrollArea{border:none;background:transparent;}"
        "QLabel{background:transparent;}" % (COLOR_PAPER, COLOR_DEEP))


def page_window(parent, title, width=560, height=600, subtitle=""):
    """造一个「页面窗口」：无边框圆角白卡片 + 标题行 + 关闭按钮。

    返回 `(dialog, body)`：把内容塞进 `body`（QVBoxLayout）就行。
    拖动：整块空白处都能拖（无边框窗口没有标题栏，不能只靠标题那行）。
    `dialog.close_btn` 是右上角那个关闭按钮（自检会点它）。
    """
    from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QVBoxLayout

    class PageWindow(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool
                                | Qt.NoDropShadowWindowHint)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setModal(False)
            self.resize(int(width), int(height))
            self._drag_from = None

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            card = QFrame()
            card.setObjectName("pagecard")
            card.setStyleSheet(
                "QFrame#pagecard{background:#FFFFFF;border:1.8px solid %s;"
                "border-radius:%dpx;}" % (CARD_BORDER, PAGE_RADIUS))
            outer.addWidget(card)
            inner = QVBoxLayout(card)
            inner.setContentsMargins(16, 14, 16, 14)
            inner.setSpacing(10)

            head = QHBoxLayout()
            head.setSpacing(8)
            head.addWidget(title_label(title))
            self.subtitle = QLabel(subtitle)
            self.subtitle.setFont(ui_font(11))
            self.subtitle.setStyleSheet("color:%s;background:transparent;" % MUTED)
            head.addWidget(self.subtitle)
            head.addStretch(1)
            self.close_btn = ghost_btn("关闭")
            self.close_btn.clicked.connect(self.hide)
            head.addWidget(self.close_btn)
            inner.addLayout(head)

            self.body = QVBoxLayout()
            self.body.setSpacing(10)
            inner.addLayout(self.body, 1)

        # ---- 无边框拖动（抓空白处就能拖）----
        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self._drag_from = (event.globalPosition().toPoint(),
                                   self.frameGeometry().topLeft())

        def mouseMoveEvent(self, event):
            if self._drag_from is not None:
                start, origin = self._drag_from
                self.move(origin + event.globalPosition().toPoint() - start)

        def mouseReleaseEvent(self, _event):
            self._drag_from = None

    return PageWindow(), None        # body 通过 dialog.body 取


def title_label(text):
    """页面标题（15 粗，深蓝）。"""
    return label(text, size=15, bold=True, color=COLOR_DEEP)
