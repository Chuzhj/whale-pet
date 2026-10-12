# -*- coding: utf-8 -*-
"""统一风格的设置窗口：**左竖栏 + 右内容区**（PCL 启动器那种分栏）。

为什么要单独抽一个窗口壳：
  原来 API key / 飞书 / 远程控制各是一个独立小对话框，字号、按钮、留白
  三套写法（同一个蓝色出现过三种深浅）。主人 2026-10-11 的要求是
  "只有一个设置入口"，所以这里做成**外壳 + 页面工厂**的形态：
  外壳（本文件）负责竖栏、切页、选中态、标题；每个功能只交一个
  `-> QWidget` 的工厂函数，放进 `pages` 里即可，各页面之间互不知情。

三条设计口径（对齐 pet_ui.page_window 与 pet_shell 的比例）：
  * **页面懒加载**：第一次点开才建。飞书页要起长连接、音效页要读配置，
    一开设置窗口就全建一遍是白花时间，也会让"没配好"的报错提前冒出来。
  * **可选中态靠重上样式**，不用 setCheckable：竖栏条目是 QPushButton，
    Qt 的 `:checked` 伪状态会和 rail_item_style 里写的背景打架；
    直接 `setStyleSheet(rail_item_style(True/False))` 只有一处真相。
  * **条目多了能滚**：竖栏主体是 QScrollArea，以后加页面不用改布局。

对外只暴露 build_settings()，返回的窗口有：
  * `open_page(key=None)` —— 显示窗口并切到某页（key 为空保持当前页）；
  * `reload()` —— 让当前页重新读一遍数据（面板有 reload/refresh 就调它）；
  * `hide()` —— 关闭按钮走的就是它；
  * `rail_buttons` —— {key: 按钮}，自检要点它。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QVBoxLayout,
                               QWidget)

from pet_ui import (RAIL_ITEM_H, RAIL_W, hint, page_bg_style,
                    page_window, rail_item_style, rail_style)

SETTINGS_W, SETTINGS_H = 900, 620     # 默认尺寸：宽度够放两列卡片
BODY_MARGIN = 14                      # 右内容区外边距（和 pet_shell 一致）
RAIL_PAD = 10                         # 竖栏内左右留白


def embedded_page(child):
    """把"独立窗口那种面板"塞进设置页：套一层页面底 + 零外边距的容器。

    记账面板 / 音效面板原本各自是一个 QDialog，它们自带 `page_bg_style`
    背景与留白；直接 addWidget 到 QStackedWidget 上是不行的（QStackedWidget
    的页面必须是一个"底"），套一层只负责给底，内容照样由面板自己画。
    """
    page = QWidget()
    page_bg_style(page)
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(child)
    return page


class SettingsWindow:
    """设置窗口的**薄壳**（真正的控件树在 self._win 里，由 page_window 造）。

    为什么不用继承：`page_window` 返回的类是在 pet_ui 内部定义的（拿不到
    类名去继承），而组合一份薄壳还能顺手把 `isVisible/show/raise_` 这些
    调用方会用的方法透传出去（见 __getattr__）。
    """

    def __init__(self, parent, pages, on_close=None):
        self.pages = list(pages or [])
        # page_window 返回的 dialog 已经自带：无边框圆角白卡片、标题行、
        # 关闭按钮（连接的是 hide）、以及"抓空白处就能拖"。
        self._win, _ = page_window(parent, "设置", SETTINGS_W, SETTINGS_H)
        self._title_label = self._find_title_label("设置")
        self._on_close = on_close
        if on_close is not None:
            # 关闭按钮**本来就连着 hide**（见 pet_ui.page_window），
            # 这里只是再挂一个回调，不覆盖原行为（自检会点它验"点了就藏"）。
            self._win.close_btn.clicked.connect(self._notify_close)

        self._built = {}              # key -> 页面控件（懒加载的账本）
        self._title_of = {}           # key -> 竖栏上写的标题
        self._current = None
        self.rail_buttons = {}

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())
        body.addWidget(self._build_stack(), 1)
        self._win.body.addLayout(body, 1)

    # ---------------------------------------------------------------- 造壳
    def _find_title_label(self, text):
        """把标题行那个 QLabel 找出来（切页时要改它的字）。

        pet_ui.page_window 只把 subtitle/close_btn 挂到 dialog 上，标题标签
        没留引用。用 findChildren 按文字找是唯一不动 pet_ui 的办法 ——
        外面还有别人在改那个文件，这一版不许碰它。
        """
        for lb in self._win.findChildren(QLabel):
            if lb.text() == text:
                return lb
        return None

    def _build_rail(self):
        """左竖栏：每个 page 一个条目，下面是弹簧；条目多了能滚。"""
        rail = QWidget()
        rail.setObjectName("rail")           # rail_style 是按这个 id 命中
        rail.setFixedWidth(RAIL_W)
        rail.setStyleSheet(rail_style())

        outer = QVBoxLayout(rail)
        outer.setContentsMargins(RAIL_PAD, 10, RAIL_PAD, 10)
        outer.setSpacing(0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        box = QVBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        for key, title, _factory in self.pages:
            btn = QPushButton(title)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(RAIL_ITEM_H)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setStyleSheet(rail_item_style(False))
            btn.clicked.connect(lambda _c=False, k=key: self.open_page(k))
            box.addWidget(btn)
            self.rail_buttons[key] = btn
            self._title_of[key] = title
        box.addStretch(1)                    # 条目顶到上面去
        area.setWidget(host)
        outer.addWidget(area, 1)
        return rail

    def _build_stack(self):
        """右内容区：一个 QStackedWidget，每个 page 一个占位页（懒加载）。"""
        wrap = QWidget()
        page_bg_style(wrap)
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(BODY_MARGIN, BODY_MARGIN, BODY_MARGIN,
                                 BODY_MARGIN)
        outer.setSpacing(0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        return wrap

    # ---------------------------------------------------------------- 对外
    def open_page(self, key=None):
        """显示窗口；key 给了就切到那一页，没给就保持当前页。

        返回当前页的 key（自检拿它断言"真的切过去了"）。
        """
        keys = [k for k, _t, _f in self.pages]
        if key is None:
            key = self._current if self._current is not None else (
                keys[0] if keys else None)
        if key not in keys:
            key = self._current if self._current is not None else (
                keys[0] if keys else None)
        if key is None:
            return None
        self._ensure_page(key)
        self.stack.setCurrentWidget(self._built[key])
        self._current = key
        self._sync_rail()
        self._sync_title()
        self._win.show()
        self._win.raise_()
        # 切过去之后再让页面自己读一遍数据（音效面板的 reload、记账的 refresh）
        self._reload_key(key)
        return key

    def reload(self):
        """重新加载当前页的数据（页面没建过就先建出来）。"""
        if self._current is None:
            return
        if self._current not in self._built:
            self._ensure_page(self._current)
        self._reload_key(self._current)

    def hide(self):
        self._win.hide()

    def page(self, key):
        """取某个页面控件（没建过返回 None）—— 给自检和调试用。"""
        return self._built.get(key)

    # ---------------------------------------------------------------- 内部
    def __getattr__(self, name):
        # 薄壳：open_page/reload/hide 之外的名字（isVisible/show/raise_/close…）
        # 一律转给真正的窗口。用 __dict__ 取值，避免"属性还没赋值"时自己递归。
        try:
            win = self.__dict__["_win"]
        except KeyError:
            raise AttributeError(name)
        return getattr(win, name)

    def _factory(self, key):
        for k, _t, factory in self.pages:
            if k == key:
                return factory
        return None

    def _ensure_page(self, key):
        if key in self._built:
            return self._built[key]
        factory = self._factory(key)
        widget = factory() if factory is not None else None
        if widget is None:
            # 工厂没给出控件也得有个页面，否则 stack 里索引会错位
            widget = embedded_page(hint("这一页暂时没有内容。"))
        self._built[key] = widget
        self.stack.addWidget(widget)
        return widget

    def _reload_key(self, key):
        page = self._built.get(key)
        if page is None:
            return
        # 页面自己带 reload（音效）/ refresh（记账）就调一下；
        # 两者都没有的页（远程、飞书是按钮驱动的）就什么都不做。
        for name in ("reload", "refresh"):
            fn = getattr(page, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    # 页面刷新失败不该把切页弄崩：显示旧数据，比白屏强
                    pass
                return

    def _sync_rail(self):
        """选中态：当前页白底 + 左蓝条，其余透明。"""
        for key, btn in self.rail_buttons.items():
            btn.setStyleSheet(rail_item_style(key == self._current))

    def _sync_title(self):
        if self._title_label is not None and self._current is not None:
            self._title_label.setText(
                "设置 · %s" % self._title_of.get(self._current, ""))

    def _notify_close(self):
        try:
            self._on_close()
        except Exception:
            # 回调是调用方给的，它炸了不该连累"关窗口"这个动作
            pass


def build_settings(parent, pages, on_close=None):
    """pages = [(key, 标题, 页面工厂 callable -> QWidget), ...]，返回设置窗口实例。

    页面工厂是**懒调用**的：第一次 open_page 到那一页才执行。
    """
    return SettingsWindow(parent, pages, on_close=on_close)
