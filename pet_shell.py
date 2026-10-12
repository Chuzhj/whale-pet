# -*- coding: utf-8 -*-
"""桌宠主壳：**一个窗口、两个模式**（工作 / 聊天），PCL 启动器那种左栏布局。

为什么要把两个独立窗口合成一个：
  1. 聊天窗口（pet_windows.build_chat_window）和 Harness 工作台
     （pet_bridges.build_harness_console）本来就是**同一件事的两张脸** ——
     都是"左边挑一段、右边说话"。分成两个窗口，主人想换个模式得先关一个再开一个，
     两个窗口还会互相压层（都是置顶的 Qt.Tool，谁在上全看谁后开）。
  2. 参考 PCL 的观感：左栏是**常驻的导航**，右栏跟着模式换内容。
     左栏始终是任务对话列表，于是"切模式"不丢失位置感；在聊天模式下点某个任务，
     直接进工作模式并选中它 —— 这一步在旧版是做不到的。
  3. 顶部那一排"启动/下载/设置"式的操作栏**故意不做**：能收进左栏底部或
     卡片标题栏的操作，就不占一整条横栏（主人明确不要）。

对外只暴露三样东西（调用方契约，名字不能改）：
  * `build_shell(...)` 造那唯一的窗口；
  * `shell.chat` —— 聊天门面，接口与旧 build_chat_window 的实例**逐名对齐**
    （add_me / add_her / begin_reply / stream / stream_full / end_reply /
     fail_reply / add_error / clear / set_busy / open_window / _rows_layout …），
    所以桌宠那边原来的调用点可以原样留着；
  * `shell.work` —— 工作门面，接口与旧 build_harness_console 的实例对齐
    （refresh_sessions / adopt_session / current_target / stream / tool / note /
     fail / finish / set_status / set_busy / listw / _rows …）。

两个门面都是**薄壳**：方法体只有一行转发，真正的状态与控件全在 Shell 上。
这样"同一个窗口"这件事在代码里是**结构性**的，不靠两个类互相同步（那种写法迟早分叉）。
"""
import re

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFontMetrics, QPainter, QPainterPath, QPen,
                           QTextCursor)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QStyledItemDelegate,
                               QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

# 工作模式正文的 markdown 渲染收在本文件（见下面「markdown → 富文本」那一节），
# 所以这里不再从 pet_bridges 取 render_transcript_html：那份还在给旧工作台用，
# 两处共用一份就得迁就旧的观感数字 —— 以前正是靠在 _render 里连做三次字符串
# replace 去压它的边距，那种"改别人的输出"很容易在对方一动就悄悄失效。
from pet_constants import (BUBBLE_FONT_FAMILIES, CHAT_BUBBLE_RATIO, COLOR_BLUE,
                           COLOR_DEEP, COLOR_LIGHT, COLOR_PAPER,
                           SESSION_LOG_AVAILABLE)
# 注意：私有名必须显式导入：`import *` 带不走下划线开头的名字，而 _session_log 是在
#    pet_constants 的 try 块里绑定的 —— 漏了不会当场报错，要等刷新列表那行才
#    NameError，再被 except 吞掉就变成"任务列表永远是空的"（这个坑项目里踩过）。
from pet_constants import _session_log
from pet_time import (_cn_num)
from pet_ui import *          # noqa: F401,F403  （MUTED / DANGER / 字体 / 卡片 / 按钮）
from pet_ui import card_style

# ---------------------------------------------------------------------------
# 比例常量：全部写在一处，改观感只动这里
# ---------------------------------------------------------------------------
SHELL_W, SHELL_H = 980, 660          # 默认尺寸（可 resize，照 PCL 的横宽比）
SHELL_MIN_W, SHELL_MIN_H = 760, 520  # 再小卡片就挤了
SHELL_RADIUS = 14                    # 整窗圆角
HEADER_H = 46                        # 头部条（只在最上面一行：标题 + 关闭）
RAIL_W = 184                         # 左栏宽
RAIL_PAD = 10                        # 栏内左右留白
ITEM_H = 46                          # 任务条目高
ITEM_RADIUS = 10                     # 条目圆角
ITEM_GAP = 6                         # 条目间距
BODY_MARGIN = 14                     # 右侧内容区外边距
CARD_RADIUS = 14                     # 卡片圆角
CARD_PAD = 14                        # 卡片内边距
CARD_GAP = 12                        # 卡片之间
INPUT_H = 34                         # 输入框高
BTN_H, BTN_W = 34, 76                # 主按钮高 / 宽
TITLE_SIZE, ITEM_TITLE_SIZE = 15, 13
ITEM_SUB_SIZE, BODY_SIZE, STATUS_SIZE = 11, 13, 11

RAIL_BG = "#F1F6FD"                  # 很浅的蓝：和右侧白卡片形成分栏
DIVIDER = "#DCE8F7"                  # 左栏与内容区之间那条 1px
CARD_BG = "#FFFFFF"
RAIL_TEXT = COLOR_DEEP
RAIL_SUB = MUTED

PAGE_CHAT, PAGE_WORK = 0, 1


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _elide(text, width, bold=False, size=BODY_SIZE):
    """按像素宽截断并加省略号。

    为什么不用 QFontMetrics.elidedText 直接写在外面就行：这里要的宽度常常由
    "卡片可用宽 - 按钮宽 - 间距"算出来，调用点很多，统一收一处省得各算各的。
    """
    metrics = QFontMetrics(ui_font(size, bold))
    return metrics.elidedText(str(text or ""), Qt.ElideRight, max(20, int(width)))


def _card():
    """一张白卡片。objectName 是样式表的限定符（见 pet_ui.card_style 的说明），
    去掉它整张卡片的圆角和白底就全没了。"""
    frame = QFrame()
    frame.setObjectName("card")
    frame.setStyleSheet(card_style(padding=CARD_PAD, radius=CARD_RADIUS))
    return frame


def _auto_bubble_width(shell):
    """气泡最大宽度：按**卡片内实际可用宽**的比例算，而不是写死像素。

    旧聊天窗口是 setFixedSize 的，写死 0.74*372 没问题；这个窗口能 resize，
    写死就变成"窗口拉宽了气泡还那么窄"（或者反过来溢出卡片）。
    """
    inner = max(200, shell.chat_scroll.viewport().width() - 12)
    return int(inner * CHAT_BUBBLE_RATIO)


def _scrollbar_qss():
    """统一的细滚动条样式。

    为什么是一个函数、要在好几处都调一遍：Qt 的样式表**遇到子控件自己的
    setStyleSheet 就停止向下传播** —— 给 Shell 设一次不够，task_list /
    chat_scroll / view 各自设过样式表，滚动条就退回系统默认长相（截图里那条
    没有把手的空槽就是这么来的）。所以同一段样式在"有滚动条的地方"各贴一次。
    """
    return (
        "QScrollBar:vertical{background:transparent;width:8px;margin:2px;}"
        "QScrollBar::handle:vertical{background:#C6D9F0;border-radius:4px;"
        "min-height:26px;}"
        "QScrollBar::handle:vertical:hover{background:%s;}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical"
        "{background:transparent;}" % COLOR_BLUE)


# ---------------------------------------------------------------------------
# markdown → 富文本：工作模式正文的排版
#
# Harness 的回答是 markdown，而旧的渲染路径（pet_bridges.render_transcript_html）
# 只做"转义 + 换行"，于是屏幕上原样出现 `**粗体**`、反引号、行首的 `#` ——
# 一段回答看着像没排版过的源码。这里补一个**子集渲染**：不引第三方库
# （项目只带 PySide6 + 标准库），只做"回答里真会出现的那些"语法；
# 链接、图片、脚注、HTML 内嵌**故意不做**，宁可原样显示文字，也不要猜错了排版。
# 表格 2026-10-11 补上了（主人要求）：只在"表头 + 对齐行 + 至少一行数据"三者
# 齐全时才认，缺一样就退回普通文字显示 —— 详见 _md_table 的说明。
#
# 两条设计口径，先说清：
#   1. **结构按原文认，进 HTML 的一律是转义后的文本**。先整段转义再认结构是错的
#      —— `>` 会变成 `&gt;`，引用块再也认不出来；只转义不切块也是错的 ——
#      回答里的 `<div>`、`a < b` 会被 QTextBrowser 当标签解析，结构当场冲坏。
#   2. **每次从全文重拼**，绝不往已有 HTML 后面追加：正文是一小块一小块流进来的，
#      半截的 ``` 围栏在"追加"模式下会把后面全部内容都染成代码。代价是重复解析，
#      但一段回答顶多几千字，比"渲染错一次就得整段重来"便宜得多。
# ---------------------------------------------------------------------------
MONO_FAMILIES = (["Consolas", "Courier New"]
                 + [f for f in BUBBLE_FONT_FAMILIES if f != "sans-serif"])
# 代码字体带一串回退（最后挂上中文字体）：Consolas 没有汉字，
# 不留回退的话代码块里的中文会掉到系统随便挑的字体上。
MONO_CSS = ",".join(("'%s'" % f) if " " in f else f
                    for f in MONO_FAMILIES) + ",monospace"
CODE_SIZE = BODY_SIZE - 1        # 等宽字体同样的 px 看着更大，压 1px 才和正文齐
CODE_BG = "#E6EBF2"              # 代码块浅灰底（要比正文块的 #F4F6F8 明显深一点，
                                 #   一样深就糊在一起了 —— 截图里试出来才定的）
CODE_BAR = COLOR_BLUE            # 代码块左首那道细色条
QUOTE_BG = "#EFF3F9"             # 引用块底（同理，压深一点才看得出是另一块）
QUOTE_BAR = "#C7D7EA"            # 引用块左首的竖线
QUOTE_FG = "#5F6B7A"             # 引用块文字：压灰，和正文拉开层级
# 标题字号（正文是 13）。**故意不用 h1~h3 标签**：Qt 的富文本导入器把 h1~h6 的
# 字号写死在代码里（实测 h1 恒为 26px、h3 恒为 16px，行内 font-size 和外面套
# span 都盖不住），连上下 margin 也一并不认 —— 三个标题会紧紧贴成一块。
# 所以标题用带 font-size/font-weight/margin 的 <p> 自己排，代价是丢掉 h 标签的
# 语义（这个正文区里没人读语义，排版说了算）。
HEAD_PX = {1: 17, 2: 15, 3: 14}
HEAD_TOP = {1: 8, 2: 6, 3: 5}
# 表格（2026-10-11 补上；以前"故意不做"，主人说原样显示竖线不行）。
TABLE_BORDER = DIVIDER         # 表格线：就是左栏那条分割线的浅蓝。**不另调色** ——
                               #   项目里所有"浅色细线"都用它，自己再挑一个迟早和
                               #   别处对不上（深一点就像表格网格线，重了会抢正文）。
TABLE_HEAD_BG = "#F1F6FD"      # 表头底：和左栏底色 RAIL_BG 同一个值，看着是一套
TABLE_HEAD_FG = COLOR_DEEP     # 表头字色：深蓝，跟左栏文字同源
TABLE_PAD = "3px 8px"          # 单元格内边距。**横向 8px 是特意留的**：0 的话相邻
                               #   两列的文字会挨到那条 1px 线上，看着像没有分栏；
                               #   纵向只给 3px，是因为 Qt 的单元格本来就会给行高留
                               #   一点余量，写 5px 表格会比正文段落胖一圈。
TABLE_MIN_CELL = "200px"       # 每列的最小宽度。列数不多时靠它把列撑开（列与列
                               #   之间不挤）；列数太多时它顶不过下面 TABLE_MAX_COLS
                               #   那道保险，见 _md_table 的说明。
TABLE_MAX_COLS = 7             # 一张表最多几列。**超了就拆成左右两块竖着排**，
                               #   每块重复表头。为什么是这个数、为什么非得拆：
                               #   Qt 富文本的表格在算宽度时，除内容宽度外还给每个
                               #   单元格留一份自己的最小宽度，**外加** 20px 的内边距，
                               #   这个最小值任何 CSS 都压不下去 —— 实测 16 列时
                               #   最小宽度就有 200px 左右，比整个正文区（最窄 320px）
                               #   还宽，横向滚动条必然冒出来（min-width 从 200px 改到
                               #   0、改 12%、改 4em 全试过，一个字都不变）。
                               #   7 列是在最窄窗口（约 400px 可用宽）下实测不出滚动条、
                               #   且每列还放得下两三个汉字的档位。
COL_SPLIT_HINT = True          # 拆表这件事的开关（留着方便一键关掉对比）

_MD_FENCE_RE = re.compile(r"^ {0,3}(`{3,})(.*)$")
_MD_HR_RE = re.compile(r"^ {0,3}(?:-{3,}|\*{3,}|_{3,})$")
_MD_HEAD_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_QUOTE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
_MD_UL_RE = re.compile(r"^( *)([-*+])\s+(.*)$")
_MD_OL_RE = re.compile(r"^( *)(\d{1,9})[.)]\s+(.*)$")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
# 加粗/斜体都要求"两侧不是 ASCII 字母数字"：`2*3*4`、`a**b**c` 原样留着，
# 而中文紧贴着标记写（`中文**粗体**`，最常见的那种）照样认。
# 注意**不能用 \w** —— Python 的 \w 把汉字也算进去，中文加粗会全部漏掉。
_MD_BOLD_RE = re.compile(r"(?<![0-9A-Za-z*])\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*"
                         r"(?![0-9A-Za-z*])")
_MD_ITALIC_RE = re.compile(r"(?<![0-9A-Za-z*])\*(?=\S)([^*\n]+?)(?<=\S)\*"
                           r"(?![0-9A-Za-z*])")


def _md_escape(text):
    """HTML 转义。**进 HTML 之前必须先过这一道**。

    转义 `&` 要在最前面：先换 `<` 再换 `&` 的话，`&lt;` 会被二次转义成
    `&amp;lt;`，屏幕上就出现"&lt;"这几个字了（经典顺序坑）。
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _md_emphasis(seg):
    """加粗、斜体。先粗后斜：粗体先把 `**` 吃掉，斜体那遍就不会去啃它的边角。"""
    seg = _MD_BOLD_RE.sub(r"<b>\1</b>", seg)
    return _MD_ITALIC_RE.sub(r"<i>\1</i>", seg)


def _md_inline(escaped):
    """行内语法。入参**必须已经转义**（见 _md_escape），返回 HTML 片段。

    行内代码先切出来，切完的部分才做粗斜体 —— 反过来说，
    `` `a**b**c` `` 里的星号必须是字面量，不能被加粗吃掉。
    """
    out, pos = [], 0
    for m in _MD_CODE_RE.finditer(escaped):
        out.append(_md_emphasis(escaped[pos:m.start()]))
        # 行内代码要"等宽 + 浅底 + 小圆角"。**圆角这条 Qt 做不到**：
        # 富文本的行内文字格式（QTextCharFormat）只有背景刷，没有边框/圆角，
        # 写了 border-radius 也只是被引擎忽略（不用第三方库就没别的路）。
        out.append('<code style="font-family:%s;background-color:%s;">%s</code>'
                   % (MONO_CSS, CODE_BG, m.group(1)))
        pos = m.end()
    out.append(_md_emphasis(escaped[pos:]))
    return "".join(out).replace("\n", "<br>")


# 表格的单元格样式。
#   * `word-wrap:break-word` 必须有：单元格里出现一个超长的英文串/URL/路径时，
#     没有它 Qt 会**直接加宽表格**（超出可用宽 → 横向滚动条），有它才在格内断行。
#   * 边框只画"上 + 左"两条：Qt 的单元格边框画在格子自己的矩形里，相邻格子共用
#     同一条边，所以外框和内部网格线靠这两条就能全出来（右边和下边由邻格/最后一格
#     的邻边补上，最后再给整张表补一条右边线）。
#   * `min-width` 是宽度控制的关键，理由写在 _md_table 里。
_TABLE_CELL_CSS = ("border-top:1px solid %s;border-left:1px solid %s;"
                   "padding:%s;word-wrap:break-word;min-width:%s;"
                   "font-size:%dpx;" % (TABLE_BORDER, TABLE_BORDER, TABLE_PAD,
                                        TABLE_MIN_CELL, BODY_SIZE))
# 注意结尾那个分号：这段 CSS 后面还要**拼**上 `text-align:` / `background-color:`，
# 少一个分号就会拼出 `font-size:13pxtext-align:center` 这种废声明，
# 顺带把它后面那条声明也吃掉（探针第一版就是被这个咬的：对齐全丢、
# 表头底也没了 —— 屏幕上看着"表格画出来了但没样式"）。


def _box_cell(inner, bg, bar="", fg="", mono=False, pad="5px 9px"):
    """浅底方块：**单格表格**，可选左首一道细色条。

    谁在用：代码块（灰底 + 蓝条 + 等宽）、引用块（浅底 + 灰蓝条）、
    还有每一条轮次卡片（角色底 + 无边条）。

    为什么非得套一层表格：Qt 富文本的块格式（QTextBlockFormat）没有边框 API ——
    在 `<pre>` / `<div>` 上写 `border-left` 会被引擎直接忽略（像素级实测过：
    什么都没有）。能画竖线的只有表格单元格：`QTextTableCellFormat` 带
    leftBorder*，HTML 里 `<td style="border-left:...">` 也确实被解析并真画出来
    （实测 3px 蓝条横跨整块高度）。
    另一个更重要的原因：**块级 div 的背景不会盖住块之间的 margin**，
    一段一段就变成"灰条 + 白缝"；单元格的背景刷满整个格子，卡片才是完整一片。
    `width=100%` 占满可用宽度，`white-space:pre-wrap` 让代码长行折行 ——
    代码块一旦顶出横向滚动条，嵌在卡片里就没法看了。
    """
    styles = ["background-color:" + bg]
    if bar:
        styles.append("border-left:3px solid " + bar)
    styles.append("padding:" + pad)
    if mono:
        styles += ["font-family:" + MONO_CSS, "font-size:%dpx" % CODE_SIZE,
                   "white-space:pre-wrap"]
    if fg:
        styles.append("color:" + fg)
    return ('<table cellspacing="0" cellpadding="0" width="100%"><tr>'
            '<td style="' + ";".join(styles) + '">' + inner + "</td></tr></table>")


def _md_code_block(code):
    """围栏代码块：等宽 + 浅灰底 + 左首一道细色条，换行照原样保留、长行折行。"""
    return _box_cell(_md_escape(code), CODE_BG, CODE_BAR, mono=True)


def _md_split_cells(line):
    """把一行表格拆成单元格：先按 `|` 切，再把 `\\|` 还原成真正的竖线。

    为什么要"先换占位再切"而不能直接切：`| a \\| b | c |` 里第一个竖线是**内容**
    不是分隔符，直接 split 会多切出一格、把整张表的列数搞错。换成 \\x00 这个正文里
    绝不可能出现的字符当占位，切完再换回**真的 `|`** —— 不是换回 `\\|`：
    到这一步"转义"的使命已经完成（它挡掉了"被当分隔符"这件事），屏幕上该看到的
    就是一根竖线；留成 `\\|` 的话屏幕上会出现反斜杠（探针一开始就是这么发现的）。

    首尾竖线可有可无（`a | b` 和 `| a | b |` 都认）：去掉一行两端那个空串就行，
    但**只去掉一个**：`|| a |`（空的首格）去掉左边那个之后还留着空的第一格，
    这是对的 —— 真写了空单元格就得当空单元格。
    """
    line = line.strip()
    parts = line.replace("\\|", "\x00").split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [p.strip().replace("\x00", "|") for p in parts]


def _md_delim_cells(line):
    """对齐行 `|---|:--:|` → 每一格的对齐方式；**不是对齐行就返回 None**。

    返回 None 是这张表"合不合法"的唯一判据，所以这里判得比实际需要更严：
      * 每格至少一个 `-`（`|:|:|` 这种不给过）；
      * 只允许 `-` 和 `:`，多一个别的字符就整行作废。
    为什么严：认错的代价是"把一段普通文字吃成表格"，那是排版事故；
    不认的代价只是原样显示 —— 两害相权，宁可漏认。
    """
    cells = _md_split_cells(line)
    if not cells:
        return None
    aligns = []
    for cell in cells:
        dash = cell.strip()
        if "-" not in dash or set(dash) - set("-:"):
            return None
        # `:--` 左对齐、`--:` 右对齐、`:--:` 居中、`---` 默认（跟左对齐一样，
        # 所以直接返回 None 表示"不写 text-align"）。
        left = dash.startswith(":")
        right = dash.endswith(":")
        aligns.append("center" if (left and right)
                      else ("right" if right else ("left" if left else None)))
    return aligns


def _md_table_rows(lines, i, cols, aligns):
    """从 i 起收表格的数据行，返回 (每格已渲染的二维列表, 下一行下标)。

    返回 [[单元格 HTML, ...], ...] 而不是拼好的 `<tr>`：列数太多时上层要把一张表
    **按列拆成左右两块**，得先拿到结构才能重排（见 _md_table）。

    停止条件两选一：空行，或者这行里没有可用的 `|`。
    为什么"只要带竖线就继续看"是安全的：`_md_split_cells` 至少得到 1 格，所以
    后面那个 `len(cells) != cols` 的检查必然把它踢出表格 —— 表格在第一个列数
    对不上的地方结束，跟 CommonMark 的行为一致（也顺手挡住了"表里夹一段散文、
    散文里恰好有竖线"的误吃）。
    """
    rows, n = [], len(lines)
    while i < n and lines[i].strip() and "|" in lines[i]:
        cells = _md_split_cells(lines[i])
        if len(cells) != cols:
            break
        # 每格都走一遍生产路径的行内渲染：**先转义再认语法**（口径见文件开头），
        # 于是单元格里的 `<div>` 变成 `&lt;div&gt;`、`**粗体**` 变成 <b>。
        rows.append([(_md_inline(_md_escape(c)) if c
                      else "&nbsp;")     # 空单元格给个不换行空格，不然行高会塌
                     for c in cells])
        i += 1
    return rows, i


def _md_table_html(header, aligns, rows, cols):
    """把"表头 + 数据行"拼成一段 <table>；cols 是这一段要用的列下标序列。

    单独抽出来是为了"列太多就拆成左右两块"那条路能复用同一套拼装：
    两块除了列下标不同，画法一模一样。
    """
    ths = []
    for k in cols:
        cell = header[k]
        # 单元格内容一律走生产路径的行内渲染（**先转义**，见文件开头那条口径）：
        # `<div>` 于是变成 &lt;div&gt;，`**粗体**` 和行内代码照常生效。
        inner = _md_inline(_md_escape(cell)) if cell else "&nbsp;"
        align = "text-align:%s;" % aligns[k] if aligns[k] else ""
        ths.append('<th style="%s%sbackground-color:%s;color:%s;'
                   'font-weight:bold;">%s</th>'
                   % (_TABLE_CELL_CSS, align, TABLE_HEAD_BG, TABLE_HEAD_FG,
                      inner))
    trs = []
    for row in rows:
        tds = []
        for k in cols:
            inner = row[k] if k < len(row) else "&nbsp;"
            align = "text-align:%s;" % aligns[k] if aligns[k] else ""
            tds.append('<td style="%s%s">%s</td>'
                       % (_TABLE_CELL_CSS, align, inner))
        trs.append("<tr>" + "".join(tds) + "</tr>")
    # 右边线/下边线单独补：每个单元格只画了"上 + 左"两条（相邻格子共用同一条边，
    # 画两条会重叠成 2px），所以表格最右一列的右缘和最下一行的下缘得在这里补上。
    # 注意 `100%%`：这一段是 % 格式化的模板，字面的百分号必须写两个。
    return ('<table cellspacing="0" cellpadding="0" width="100%%" '
            'style="border-right:1px solid %s;border-bottom:1px solid %s;">'
            '<tr>%s</tr>%s</table>'
            % (TABLE_BORDER, TABLE_BORDER, "".join(ths), "".join(trs)))


def _md_table(lines, i):
    """从下标 i（表头行）起识别并渲染一张 markdown 表格。

    返回 (HTML, 下一行下标) 或者 None —— **返回 None 就是不合法，交回上层按普通
    段落显示**。这是硬要求：宁可看竖线，也不能让一段普通文字被吃成表格。

    合法的最低门槛：表头 + 对齐行 + **至少一行数据**，三者缺一不认。所以
      * `| x |`（下一行不是对齐行）→ 普通文字，`|` 照样看得见；
      * `| a | b |` + `|---|---|`（没有数据行）→ 也是普通文字（这条覆盖了
        "正在流式打字、数据行还没到"的那一瞬间：这时候按文字显示，数据行一到
        下一帧就整块变表格，比先猜成表格再跳一次稳）。
      * 行内列数对不上（表头 3 列、数据行 2 列）同样不认 —— 那多半是主人手打的
        文字里正好有竖线，当表格切开对齐会乱成一团。

    列数超过 TABLE_MAX_COLS 就**拆成左右两块竖着排**（每块重复一遍表头）。
    为什么不干脆让它自己缩：Qt 富文本表格的每个单元格除内容宽度外，还固定带一份
    自己的最小宽度（含内边距），列数一多这个下限就顶过正文区宽度，横向滚动条必然
    出现，而且它是**任何 CSS 都压不下去的**（实测 min-width 写 200px / 60px / 20px /
    0 / 12% / 4em 六种，横向滚动条一模一样 —— 见 TABLE_MAX_COLS 的注释）。
    既然压不住就少放几列：拆成的两块语义上还是同一张表（表头逐块重复，行与行
    仍然一一对应），比让人左右拖着滚动条看强。列数正好等于上限时不拆。
    """
    header = _md_split_cells(lines[i])
    aligns = _md_delim_cells(lines[i + 1])
    if not header or aligns is None or len(header) != len(aligns):
        return None
    body, j = _md_table_rows(lines, i + 2, len(header), aligns)
    if not body:
        return None
    ncols = len(header)
    if COL_SPLIT_HINT and ncols > TABLE_MAX_COLS:
        # 按上限切块；除不尽时最后一块少几列（不硬凑空列）
        parts = []
        for a in range(0, ncols, TABLE_MAX_COLS):
            parts.append(_md_table_html(header, aligns, body,
                                        list(range(a, min(ncols, a
                                                         + TABLE_MAX_COLS)))))
        return "".join(parts), j
    return _md_table_html(header, aligns, body, list(range(ncols))), j


def _md_is_table_start(lines, i):
    """这一行是不是一张**合法**表格的表头行。

    两件事都要看：这行的列数，和下一行的对齐行列数**必须一致**。
    为什么要比列数：`| a | b |` 后面挂一行 `| --- |`（只写了一格的对齐行）在
    CommonMark 里不算表格，这里也照旧不认 —— 两边列数不等的"对齐行"几乎可以
    肯定不是主人想打的表格。
    """
    if i + 1 >= len(lines) or "|" not in lines[i]:
        return False
    aligns = _md_delim_cells(lines[i + 1])
    if aligns is None:
        return False
    return len(_md_split_cells(lines[i])) == len(aligns)


def _md_list(items):
    """列表。支持**两级**缩进 —— 回答里嵌套要点太常见，拍平了很难看。

    items 是 [(层级, 标签, 已渲染内容), ...]。用一个小栈开合标签：
    开嵌套列表时上一条的 `<li>` 是**故意不闭合**的，这样子列表落在它里面。
    Qt 自己的 ul/ol 带默认缩进，这里不再手写 padding（会和它的缩进叠成一大片空白）。
    """
    out, stack = [], []           # stack: [标签, 层级]
    for level, kind, text in items:
        while stack and stack[-1][1] > level:
            out.append("</li></%s>" % stack.pop()[0])
        if not stack or stack[-1][1] < level:
            out.append('<%s style="margin-top:2px;margin-bottom:2px;">' % kind)
            stack.append([kind, level])
        elif stack[-1][0] != kind:
            # 同一层里换了列表种类（无序→有序）：收掉旧的、开新的
            out.append("</li></%s>" % stack.pop()[0])
            out.append('<%s style="margin-top:2px;margin-bottom:2px;">' % kind)
            stack.append([kind, level])
        else:
            out.append("</li>")
        out.append("<li>%s" % text)
    while stack:
        out.append("</li></%s>" % stack.pop()[0])
    return "".join(out)


def md_to_html(text):
    """把一段 markdown 渲染成 HTML 片段（工作模式正文用）。

    做到了：粗体、斜体、行内代码、围栏代码块、1~3 级标题、无序/有序列表
    （两级缩进）、引用、分隔线、表格（表头 + 对齐行 + 数据行，见 _md_table）。
    没做：链接、图片、HTML 内嵌、嵌套引用、代码块的语言标签
    （```python 的 python 只是被忽略，不当标签显示）。
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            # 连续空行**直接跳过**（空行本身不出块）：段间距由块自己的
            # margin 给，留着空行的话模型连打三个空行就是一屏空白。
            i += 1
            continue

        # 表格要排在所有其它块之前认：表头行本身不含别的块标记，但"对齐行"
        # `|---|---|` 长得像分隔线（`---`），被下面那条 _MD_HR_RE 先抢走的话，
        # 表格永远拼不出来、而且屏幕上会只剩一条横线、竖线没了。
        if _md_is_table_start(lines, i):
            table = _md_table(lines, i)
            if table is None:
                # "像表格但不合法" = 有表头和对齐行、还没有数据行。**这两行必须
                # 自己当普通文字吐出来**：不能 `continue` 掉（那会把两行都丢掉），
                # 也不能只吐表头把对齐行留给下面（那行会被 <hr> 吃掉、竖线凭空消失）。
                # 两行合成一个段落，屏幕上就是主人写的那两行原文。
                out.append('<p style="margin-top:3px;margin-bottom:3px;">%s</p>'
                           % _md_inline(_md_escape(line)) + "<br>"
                           + _md_inline(_md_escape(lines[i + 1])))
                i += 2
                continue
            out.append(table[0])
            i = table[1]
            continue

        if _MD_FENCE_RE.match(line):
            buf, i = [], i + 1
            while i < n:
                closer = _MD_FENCE_RE.match(lines[i])
                if closer and not closer.group(2).strip():   # 收口那行只有反引号
                    i += 1
                    break
                buf.append(lines[i])
                i += 1
            # 没等到收口（正在打字的那一瞬间）→ **照常当代码块渲染到末尾**。
            # 为什么选"容忍到闭合"而不是"退回普通文本"：围栏里是一整块代码，
            # 退回普通文本会让屏幕上先出现一排反引号和没有缩进的代码、等收口
            # 落下的瞬间整块跳一次；先当代码块只是多渲染一个还没写完的块，不闪。
            # 反引号数量写歪的**历史**消息（回答里引用代码段时很容易歪）也走这条：
            # 宁可多染一块，也不要在正文中间露出一排 ```。
            out.append(_md_code_block("\n".join(buf)))
            continue

        if _MD_HR_RE.match(line):
            # 分隔线也走"表格单元格"这条唯一的画线路子：块级 border 是空的，
            # 而 Qt 自带的 <hr> 会画一条 2px 的近黑线（实测），跟这套浅蓝配色
            # 不是一路。这里用一格很扁的单元格 + border-top，得到 1px 的分隔线。
            out.append('<table cellspacing="0" cellpadding="0" width="100%"><tr>'
                       '<td style="border-top:1px solid ' + DIVIDER
                       + ';font-size:1px;line-height:1px;">&nbsp;</td>'
                       '</tr></table>')
            i += 1
            continue

        head = _MD_HEAD_RE.match(line)
        if head:
            level = min(3, len(head.group(1)))     # #### 以上一律按三级排
            out.append('<p style="font-size:%dpx;font-weight:bold;'
                       'margin-top:%dpx;margin-bottom:2px;">%s</p>'
                       % (HEAD_PX[level], HEAD_TOP[level],
                          _md_inline(_md_escape(head.group(2)))))
            i += 1
            continue

        if _MD_QUOTE_RE.match(line):
            buf = []
            while i < n:
                q = _MD_QUOTE_RE.match(lines[i])
                if not q:
                    break
                buf.append(q.group(1))
                i += 1
            # 引用**不递归**再解析块级语法（嵌套引用在回答里少见，递归会让
            # 这段代码翻一倍），行内标记仍然认。
            out.append(_box_cell(_md_inline(_md_escape("\n".join(buf))),
                                 QUOTE_BG, QUOTE_BAR, fg=QUOTE_FG,
                                 pad="4px 9px"))
            continue

        if _MD_UL_RE.match(line) or _MD_OL_RE.match(line):
            items = []
            while i < n:
                mu = _MD_UL_RE.match(lines[i])
                mo = None if mu else _MD_OL_RE.match(lines[i])
                m = mu or mo
                if not m:
                    # 条目里的续行（缩进了但没写新的 `-`）：并进上一条。
                    # 不并的话"一条要点写两行"会掉成一个没有圆点的独立段落。
                    if (items and lines[i].strip()
                            and lines[i][:1] in (" ", "\t")):
                        items[-1] = (items[-1][0], items[-1][1],
                                     items[-1][2] + "<br>"
                                     + _md_inline(_md_escape(lines[i].strip())))
                        i += 1
                        continue
                    break
                # 缩进换算层级：2 空格和 4 空格都算第一级，最多两级
                items.append((min(1, len(m.group(1)) // 2),
                              "ul" if mu else "ol",
                              _md_inline(_md_escape(m.group(3)))))
                i += 1
            out.append(_md_list(items))
            continue

        # 普通段落：吃掉连续的"非空、且不是别的块开头"的行
        buf = []
        while (i < n and lines[i].strip()
               and not _md_is_block_start(lines[i])
               # 表头也得让位。为什么在这里单独问一句、不给 _md_is_block_start
               # 加形参：那个函数还有别的调用者（列表续行那段），改签名会牵动它们，
               # 而"是不是表头"本来就要看下一行，跟那个函数的语义不是一回事。
               and not _md_is_table_start(lines, i)):
            buf.append(lines[i].strip())     # 段落内的缩进没意义，去掉
            i += 1
        out.append('<p style="margin-top:3px;margin-bottom:3px;">%s</p>'
                   % _md_inline(_md_escape("\n".join(buf))))
    return "".join(out)


def _md_is_block_start(line):
    """这一行是不是"另一个块的开头"（段落遇到它就得让位）。

    表格不在这里判：判表头要看**下一行**（对齐行），只给一行的签名做不到。
    段落的循环另有一句 `_md_is_block_start(lines[i])` 之外的表头检查 ——
    见 md_to_html 里那段说明。
    """
    return bool(_MD_FENCE_RE.match(line) or _MD_HR_RE.match(line)
                or _MD_HEAD_RE.match(line) or _MD_QUOTE_RE.match(line)
                or _MD_UL_RE.match(line) or _MD_OL_RE.match(line))


# 角色色表：与 pet_bridges.render_transcript_html 里那份一致（那份还在给旧工作台用）
_WORK_PALETTE = {
    "主人": ("#1F3A93", "#E8F0FE"),
    "助手": ("#1F2933", "#F4F6F8"),
    "工具": ("#5F6B7A", "#EDF1F4"),
    "错误": ("#B4483C", "#FDECEA"),
}
# 哪些角色的正文走 markdown：她写的回答是 markdown，主人手打的提示词也常带
# 粗体/代码（顺手就渲染了）；**工具流水和错误一律纯文本** ——
# 那两种是"名字 + 参数截断"的一行摘要，里面的 `**`、`_` 都是普通字符。
_MD_ROLES = ("助手", "主人")


def render_work_transcript_html(turns, live=""):
    """工作模式正文：每条一轮 = 角色标签 + 色块，正文走 markdown 渲染。

    角色标签**留在块的上面**（和旧工作台一样），不塞进块里当第一行 ——
    这样"谁说的"一眼能扫到，块里只有正文，markdown 的标题也不会跟标签抢层级。
    live 是还在流式的那一段，挂在末尾；它每一小片进来都会重拼一次全文。

    正文那层为什么是**单格表格**而不是 `<div>`：块级 `<div>` 的 background-color
    在 Qt 里会落到每一段的字符/块格式上，而**块与块之间的 margin 是不上色的** ——
    一段一段就变成了"灰条 + 白缝 + 灰条"，整块卡片看着像被划了几刀（截图里量到
    3~4px 宽的白缝）。表格单元格的背景是刷满整个格子的，格子里的 margin 一律
    露出格子底色，卡片于是连成一片。代价：圆角。Qt 富文本本来就不支持块级/单元格
    的 border-radius，那个 `border-radius:8px` 一直是白写的，所以不算丢东西。
    """
    parts = []
    items = list(turns) + ([("助手", live)] if live else [])
    for role, text in items:
        fg, bg = _WORK_PALETTE.get(role, _WORK_PALETTE["助手"])
        if role in _MD_ROLES:
            body = md_to_html(text)
        else:
            body = _md_escape(text).replace("\n", "<br>")
        if not body:
            body = "&nbsp;"           # 空块在 Qt 里高度为 0，看着像"丢了这条"
        parts.append(
            '<div style="margin-top:3px;margin-bottom:3px;'
            'margin-left:2px;margin-right:2px;">'
            '<span style="color:' + fg + ';font-weight:bold;">'
            + _md_escape(role) + "</span>"
            + _box_cell(body, bg, fg=fg, pad="4px 9px") + "</div>")
    return "".join(parts) or "<p>（这段对话还没有内容 —— 在下面说句话就开始）</p>"


# ---------------------------------------------------------------------------
# 左栏条目的画法：自定义 delegate
# ---------------------------------------------------------------------------
class _RailDelegate(QStyledItemDelegate):
    """把任务条目画成"圆角块 + 两行字"。

    为什么不用 QListWidget 的默认画法 + setItemWidget：
      * item.text() 必须留着（自检和调用方都在读它），setItemWidget 会把文字
        交给子控件、item.text() 变空串 —— 那是**悄悄**丢掉契约，比报错更难查；
      * 默认画法是单行、系统高亮色，做不出「选中态明显 + 副标题灰字」的观感。
    自绘一遍反而最短：选中态自己画圆角块，文字自己按行摆。
    """

    def sizeHint(self, option, index):
        return QSize(RAIL_W - 2 * RAIL_PAD, ITEM_H)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = option.rect
        selected = bool(option.state & QStyle_State_Selected)
        hovered = bool(option.state & QStyle_State_MouseOver)

        # 选中/悬停的底色：淡蓝块，不是系统高亮蓝（和菜单、气泡是同一套语言）
        if selected or hovered:
            box = rect.adjusted(1, 1, -1, -1)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLOR_BLUE if selected else COLOR_LIGHT))
            painter.drawRoundedRect(box, ITEM_RADIUS, ITEM_RADIUS)

        # 行首一个小方点：选中时反白，兼当"这是对话"的标记（不用 emoji）
        dot = 8
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#FFFFFF") if selected else QColor(COLOR_BLUE))
        painter.drawEllipse(rect.left() + 11, rect.center().y() - dot // 2,
                            dot, dot)

        text_x = rect.left() + 11 + dot + 9
        text_w = rect.right() - text_x - 9
        title = str(index.data(Qt.DisplayRole) or "")
        sub = str(index.data(Qt.UserRole) or "")
        fg = "#FFFFFF" if selected else RAIL_TEXT
        fg_sub = "#DCE9FB" if selected else RAIL_SUB

        # 两行整体垂直居中：先量出两行的高度和，再定第一行的 y。
        # 写死 y 的版本在条目高改一个像素时就会"上紧下松"，量一遍最省心。
        m_title = QFontMetrics(ui_font(ITEM_TITLE_SIZE))
        m_sub = QFontMetrics(ui_font(ITEM_SUB_SIZE))
        gap = 2
        total = m_title.height() + gap + m_sub.height()
        top = rect.center().y() - total // 2

        painter.setFont(ui_font(ITEM_TITLE_SIZE))
        painter.setPen(QColor(fg))
        painter.drawText(QRect(text_x, top, text_w, m_title.height()),
                         int(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine),
                         _elide(title, text_w, size=ITEM_TITLE_SIZE))
        painter.setFont(ui_font(ITEM_SUB_SIZE))
        painter.setPen(QColor(fg_sub))
        painter.drawText(QRect(text_x, top + m_title.height() + gap,
                               text_w, m_sub.height()),
                         int(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine),
                         _elide(sub, text_w, size=ITEM_SUB_SIZE))
        painter.restore()


# QStyle.State_* 在不同 PySide6 版本下既可能在 Qt 命名空间也可能在 QStyle 里，
# 这里取一次常量，免得在 paint 里反复做 try/except。
try:
    from PySide6.QtWidgets import QStyle
    QStyle_State_Selected = QStyle.State_Selected
    QStyle_State_MouseOver = QStyle.State_MouseOver
except Exception:                                     # pragma: no cover
    QStyle_State_Selected = Qt.ItemIsSelectable
    QStyle_State_MouseOver = Qt.ItemIsUserCheckable


# ---------------------------------------------------------------------------
# 两个门面：只有转发，没有状态
# ---------------------------------------------------------------------------
class GrowingInput(QTextEdit):
    """会**自动长高**的输入框（主人 2026-10-11 的要求）。

    规矩（就是主人原话）：一行写满自动换行、框跟着长高；长到 `max_lines` 行
    **就不再长**，剩下的内容用**鼠标滚轮**翻（Qt 自带的滚动条负责）；
    回车发送、**Shift+回车**手动换行（多行输入总得留个换行的口子）。

    为什么不用 `QLineEdit`：它是**单行**控件，写满了只会**横向滚动**、
    永远不会换行 —— 想让它换行只能自己拿 `QFontMetrics` 切行再画，不值当。
    `QTextEdit` 天生会折行，我们只需要把"该多高"算对。
    """

    def __init__(self, parent, on_send, max_lines=4, line_px=None):
        super().__init__(parent)
        self._on_send = on_send
        self._max_lines = max(1, int(max_lines))
        self._line_px = int(line_px or INPUT_H)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setTabChangesFocus(True)      # Tab 去下一个控件，别在框里插制表符
        self.textChanged.connect(self._auto_grow)
        self._auto_grow()

    # 上下各留这么多（对称）—— 一行文字高 ≈17px，加它就约等于原来的 INPUT_H=34
    _PAD_V = 8

    def refresh_height(self):
        """字体/样式改完之后从外面调一次。

        为什么必需：**构造时那次量高度发生在 `setFont` 之前** ✗ ——
        用默认字体量出来的高会一直挂着，直到第一次输入才纠正。
        """
        self._auto_grow()

    def _auto_grow(self):
        """按内容定高（1 行起、max_lines 行封顶），并让文字**垂直居中**。

        两个坑，都是实测出来的：
          ① 高度别用文档像素高（`document().size()` / `documentLayout().documentSize()`）——
             那两个数**已经含 Qt 自己的边距**，再手算加一次就重复计 ✗（空框量出 51px）。
             改用 `cursorRect()`：把光标移到文末问它"你在第几像素"，那是**排版之后**的
             真实位置 ✓，没有需要猜的边距；
          ② 光有高度还不够：`QLineEdit` 的文字**天生垂直居中**，换成 `QTextEdit` 之后
             默认**贴顶** —— 主人截图里"字在上边、下面空一截"就是这个 ✗。
             所以把富余高度 `slack` 平分给上下（`setViewportMargins`），
             于是**一行时视觉上和原来那个单行框一模一样** ✓。
        """
        doc = self.document()
        doc.setDocumentMargin(0)
        cur = self.textCursor()
        cur.movePosition(QTextCursor.End)
        r = self.cursorRect(cur)
        text_h = r.bottom() + self.fontMetrics().descent() + 2   # +2：光标矩形比文档矮 1~2px
        line = self.fontMetrics().lineSpacing()
        cap = self._max_lines * line + 2 * self._PAD_V
        h = max(line + 2 * self._PAD_V, min(text_h + 2 * self._PAD_V, cap))
        self.setFixedHeight(int(h))
        slack = max(0, int(self.height() - text_h))
        self.setViewportMargins(0, slack // 2, 0, slack - slack // 2)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and not (event.modifiers() & Qt.ShiftModifier):
            self._on_send()          # 回车 = 发送（Shift+回车留给手动换行）
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._auto_grow()            # 窗口变宽变窄都会改变折行，得重算

    def plain(self):
        """取正文。用它而不是 Qt 的 `text()`（那个返回富文本，会带 HTML 标签）。"""
        return self.toPlainText()

    def input_style(self):
        """输入框的 QSS。选择器必须是 QTextEdit —— 还写 QLineEdit 就完全不生效。"""
        return ("QTextEdit{background:#FFFFFF;border:1.5px solid %s;"
                "border-radius:10px;padding:0 12px;color:%s;}"
                "QTextEdit:focus{border:1.5px solid %s;}"
                "QTextEdit:disabled{background:#F4F7FB;color:#A9B6C7;}"
                % (COLOR_BLUE, COLOR_DEEP, COLOR_DEEP))


class ShellChat:
    """聊天门面。方法名与旧 build_chat_window 实例**逐一对齐**（契约）。"""

    def __init__(self, shell):
        self._shell = shell

    # ---- 窗口 ----
    def open_window(self, char_rect, screen_rect):
        self._shell.show_window("chat", char_rect, screen_rect)

    def isVisible(self):
        return self._shell.isVisible()

    def hide(self):
        # ⚠️ 这个必须转发：`hide_into_tray()` 会挨个 hide 桌宠的附属窗口，
        # 门面上少一个 hide 就会在那里抛 AttributeError —— 后果是**后面那句
        # `self.hide()` 根本执行不到，桌宠就再也藏不起来了**
        # （主人 2026-07-11 报的"有时候点隐藏没反应"：开过这个窗口之后必现）。
        self._shell.hide()

    def raise_(self):
        self._shell.raise_()

    def close(self):
        self._shell.hide()

    @property
    def _rows_layout(self):
        return self._shell._rows_layout

    # ---- 加消息 ----
    def add_me(self, text):
        self._shell._chat_add(text, True)

    def add_her(self, text):
        self._shell._chat_add(text, False)

    def add_note(self, text):
        self._shell._chat_add_note(text)

    # ---- 流式 ----
    def begin_reply(self):
        self._shell._chat_begin_reply()

    def stream(self, piece):
        self._shell._chat_stream(piece)

    def stream_full(self, text):
        self._shell._chat_stream_full(text)

    def end_reply(self):
        self._shell._chat_end_reply()

    def fail_reply(self, text):
        self._shell._chat_fail_reply(text)

    def add_error(self, text):
        self._shell._chat_add_error(text)

    def clear(self):
        self._shell._chat_clear()

    def set_busy(self, busy):
        self._shell._chat_set_busy(busy)


class ShellWork:
    """工作门面。方法名与旧 build_harness_console 实例**逐一对齐**（契约）。"""

    def __init__(self, shell):
        self._shell = shell

    # ---- 窗口 ----
    def open_window(self):
        self._shell.refresh_sessions()
        self._shell.show_window("work")

    def isVisible(self):
        return self._shell.isVisible()

    def hide(self):
        # 同 ShellChat：门面上少了 hide，hide_into_tray 会在中途抛异常，
        # 桌宠就再也藏不起来（主人报过的"点隐藏没反应"）
        self._shell.hide()

    def raise_(self):
        self._shell.raise_()

    def close(self):
        self._shell.hide()

    # ---- 列表 ----
    @property
    def listw(self):
        return self._shell.task_list

    @property
    def _rows(self):
        return self._shell._rows

    def refresh_sessions(self, keep_row=None):
        self._shell.refresh_sessions(keep_row)

    def adopt_session(self, session_id, cwd=""):
        self._shell.adopt_session(session_id, cwd)

    def current_target(self):
        return self._shell._get_target()

    # ---- 正文 ----
    def stream(self, piece):
        self._shell._work_stream(piece)

    def tool(self, line):
        self._shell._work_tool(line)

    def note(self, text, role="助手"):
        self._shell._work_note(text, role)

    def fail(self, text):
        self._shell._work_fail(text)

    def finish(self, text):
        self._shell._work_finish(text)

    def set_status(self, text):
        self._shell.set_status(text)

    def set_busy(self, busy):
        self._shell._work_set_busy(busy)


# ---------------------------------------------------------------------------
# 主壳
# ---------------------------------------------------------------------------
class Shell(QWidget):
    """双模式窗口本体。**整个程序只造一个**（旧代码在三个地方各建一份窗口，
    是真正需要合并的部分）。"""

    def __init__(self, parent, on_chat_send, on_chat_clear, on_task_send,
                 on_task_select, get_task_target, on_mode_change=None,
                 on_task_delete=None):
        super().__init__(parent)
        # 无边框 + Tool：和聊天窗口、时钟窗同一套（不进任务栏、不参与
        # "关掉最后一个窗口就退出"）。
        # **不加 WindowStaysOnTopHint** —— 主人 2026-10-11 定的层级是
        # "别的程序窗口 > 气泡 > 桌宠"，全项目都不置顶了。窗口仍然压得住桌宠，
        # 靠的是 parent 关系（Windows 保证附属窗口在拥有者之上），不是 topmost。
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(SHELL_W, SHELL_H)
        self.setMinimumSize(SHELL_MIN_W, SHELL_MIN_H)
        # 头部空白处（按钮之间）也能拖，不然一条 46px 的条只有文字那几个像素能抓
        self.setMouseTracking(True)

        self.on_chat_send = on_chat_send
        self.on_chat_clear = on_chat_clear
        self.on_task_send = on_task_send
        self.on_task_select = on_task_select
        self._get_target = get_task_target
        self._on_mode_change = on_mode_change
        # 删任务：左栏右键那条菜单会调它（桌宠那边负责弹确认框再真删）
        self._on_task_delete = on_task_delete
        # **子会话默认折叠**（主人 2026-10-11）：只记"哪几段主任务被展开了"，
        # 展开过的记在这——刷新时不会又缩回去（刷新很频繁：干完一轮活就刷一次）
        self._expanded = set()
        self._kids = {}          # 主会话 id → 它的子会话行（右键菜单要用这个数）

        self.mode = "work"                  # 默认工作模式：主人点进来多半是派活
        self._busy = False                  # 聊天忙（门面 set_busy）
        self._work_busy = False             # 派活忙（门面 set_busy）
        self._drag_from = None
        self._greeted = False
        self._reply_label = None
        self._in_refresh = False            # 刷新重建列表时抑制 _on_row 的模式切换

        # 任务列表状态（语义与旧工作台一致）
        self._rows = []                     # 会话元信息，第 0 项 = "开一段新对话"
        self._turns = []                    # 当前显示的正文
        self._live = ""                     # 正在流式的那一段
        self._log_path = ""
        self._want_id = ""                  # 下次刷新要选中的会话 id

        self.chat = ShellChat(self)
        self.work = ShellWork(self)
        self._build()

    # ==================================================================
    # 搭界面
    # ==================================================================
    def _build(self):
        self.setStyleSheet(self._shell_qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())
        body.addWidget(self._build_divider())
        body.addWidget(self._build_content(), 1)
        root.addLayout(body, 1)

    def _shell_qss(self):
        """整窗样式：只写"大面积底色 + 滚动条"这些**跨页面**的部分。

        按钮/卡片/输入框各有各的 helper（pet_ui），别在这里重复一遍 ——
        同一个蓝色在多处写第二遍，就是走形的开始（pet_ui 的模块注释里写过）。
        """
        return _scrollbar_qss()

    def _build_header(self):
        self.header = QFrame(self)
        self.header.setObjectName("shellHeader")
        self.header.setFixedHeight(HEADER_H)
        # 注意：QSS 里凡是"百分号 + 数字 + px"（`%dpx`）都**不要**走 `%` 格式化：
        #    `%` 会把它当成又一个占位符 → 参数不够，运行期抛
        #    "not enough arguments for format string"，而报错行号指向整段、
        #    一个字都不提 `%dpx`（探针第一版就栽在这）。半径要么直接内插成字面量，
        #    要么写成 `%%dpx`（这里选前者：数字就一个，`%s` 传进去最不容易看错）。
        header_qss = ("QFrame#shellHeader{background:%s;"
                      "border-top-left-radius:%spx;"
                      "border-top-right-radius:%spx;}"
                      % (COLOR_DEEP, SHELL_RADIUS, SHELL_RADIUS))
        self.header.setStyleSheet(header_qss)

        box = QHBoxLayout(self.header)
        box.setContentsMargins(14, 0, 12, 0)
        box.setSpacing(10)

        # 名字用 "鲸鲸"（自检认这个名字，改名后别再退回旧名）
        self.title = QLabel("鲸鲸", self.header)
        self.title.setFont(ui_font(14, True))
        self.title.setStyleSheet("color:#FFFFFF;background:transparent;")

        self.subtitle = QLabel("蓝色大肥鱼 · 爱吃白饭", self.header)
        self.subtitle.setFont(ui_font(10))
        self.subtitle.setStyleSheet("color:#AFC6E8;background:transparent;")

        names = QVBoxLayout()
        names.setContentsMargins(0, 0, 0, 0)
        names.setSpacing(0)
        names.addWidget(self.title)
        names.addWidget(self.subtitle)

        # 关闭按钮用**文字**，不用叉号之类的符号（主人嫌那些符号丑）
        self.close_btn = QPushButton("关闭", self.header)
        self.close_btn.setFont(ui_font(12))
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFixedSize(52, 26)
        self.close_btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.14);color:#EAF2FF;"
            "border:none;border-radius:8px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.26);color:#FFFFFF;}")
        # hide() 而不是 close()：窗口是**复用**的（记录/列表/滚动位置都要留着），
        # 真销毁了下次打开得重建，聊天记录跟着丢。调用方靠 isVisible() 判断要不要写日志。
        self.close_btn.clicked.connect(self.hide)

        box.addLayout(names)
        box.addStretch(1)
        box.addWidget(self.close_btn)
        return self.header

    def _build_rail(self):
        """左栏：任务列表 + 栏底一整行的模式切换按钮。**没有顶部操作栏**。"""
        rail = QFrame(self)
        rail.setObjectName("rail")
        rail.setFixedWidth(RAIL_W)
        rail_qss = ("QFrame#rail{background:%s;border-top-left-radius:%spx;"
                    "border-bottom-left-radius:%spx;}"
                    % (RAIL_BG, SHELL_RADIUS, SHELL_RADIUS))
        rail.setStyleSheet(rail_qss)

        box = QVBoxLayout(rail)
        box.setContentsMargins(RAIL_PAD, 8, RAIL_PAD, RAIL_PAD)
        box.setSpacing(ITEM_GAP)

        self.task_list = QListWidget(rail)
        self.task_list.setObjectName("taskList")
        self.task_list.setFont(ui_font(ITEM_TITLE_SIZE))
        self.task_list.setFrameShape(QFrame.NoFrame)
        self.task_list.setItemDelegate(_RailDelegate(self.task_list))
        # 右键任务条目 → 删掉这段任务（主人 2026-10-11）。策略只设一次：
        # 放进 refresh_sessions 里会**每次刷新都重复连接**，右键就弹两次 ✗
        self.task_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self._on_rail_menu)
        self.task_list.setSpacing(ITEM_GAP)          # 条目之间的 6px 空隙
        self.task_list.setUniformItemSizes(True)     # 高度一致 → 滚动更稳
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.task_list.setSelectionMode(QListWidget.SingleSelection)
        self.task_list.setStyleSheet(
            "QListWidget{background:transparent;border:none;padding:0;}"
            "QListWidget::item{border:none;background:transparent;}"
            + _scrollbar_qss())     # 见 _scrollbar_qss：子控件自己的样式表会挡住继承
        # 条目多到装不下时靠它滚（要求：竖栏可滚）
        self.task_list.currentRowChanged.connect(self._on_row)
        box.addWidget(self.task_list, 1)

        # 整行切换按钮：工作模式显示「切换聊天模式」，聊天模式显示「切换工作模式」
        self.mode_btn = ghost_btn("切换聊天模式")
        self.mode_btn.setFont(ui_font(12, True))
        self.mode_btn.setMinimumHeight(0)
        self.mode_btn.setFixedHeight(BTN_H)
        self.mode_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.mode_btn.setCursor(Qt.PointingHandCursor)
        self.mode_btn.clicked.connect(self.toggle_mode)
        box.addWidget(self.mode_btn)
        return rail

    def _build_divider(self):
        line = QFrame(self)
        line.setFixedWidth(1)
        line.setStyleSheet("background:%s;border:none;" % DIVIDER)
        self.divider = line
        return line

    def _build_content(self):
        """右侧内容区：一个 QStackedWidget，两页各一张白卡片。

        用 QStackedWidget 而不是每次重建控件：切模式时**两边的状态都要活着**
        （聊天记录、滚动位置、任务列表选中项）—— 重建就等于每次切模式都清空一次。
        """
        wrap = QWidget(self)
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(BODY_MARGIN, BODY_MARGIN, BODY_MARGIN, BODY_MARGIN)
        outer.setSpacing(0)

        self.stack = QStackedWidget(wrap)
        self.chat_page = self._build_chat_page()
        self.work_page = self._build_work_page()
        self.stack.addWidget(self.chat_page)         # index = PAGE_CHAT
        self.stack.addWidget(self.work_page)         # index = PAGE_WORK
        outer.addWidget(self.stack)
        self._sync_mode_ui()
        return wrap

    # ---------------------------------------------------------------- 聊天页
    def _build_chat_page(self):
        card = _card()
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(CARD_GAP)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.chat_title = label("鲸鲸", TITLE_SIZE, True)
        # 「清空」放卡片标题栏右侧的小按钮 —— 要求是别放顶部整条操作栏
        self.chat_clear_btn = quiet_btn("清空")
        self.chat_clear_btn.setFont(ui_font(11))
        self.chat_clear_btn.setMinimumHeight(0)
        self.chat_clear_btn.setFixedHeight(24)
        self.chat_clear_btn.setCursor(Qt.PointingHandCursor)
        self.chat_clear_btn.clicked.connect(self._emit_clear)
        head.addWidget(self.chat_title)
        head.addStretch(1)
        head.addWidget(self.chat_clear_btn)
        box.addLayout(head)

        # 空态提示**不放进 _rows_layout** —— 那个布局是"消息行"专用，
        # 自检按 `_rows_layout.count() == 1` 断言"清干净了"。所以清空提示单独
        # 占一个槽：有消息时藏起来、清空后显示；契约和观感两头都顾上。
        self.chat_empty = QLabel("聊天记录清空了，重新说吧。")
        self.chat_empty.setFont(ui_font(BODY_SIZE))
        self.chat_empty.setAlignment(Qt.AlignCenter)
        self.chat_empty.setStyleSheet("color:%s;background:transparent;" % MUTED)
        self.chat_empty.setVisible(False)
        box.addWidget(self.chat_empty, 1)

        box.addWidget(self._build_chat_scroll(), 1)

        # 输入行：输入框 + 主按钮
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(8)
        self.chat_input = GrowingInput(card, self._emit_chat_send, max_lines=4)
        self.chat_input.setFont(ui_font(BODY_SIZE))
        self.chat_input.setPlaceholderText("和鲸鲸说点什么…")
        self.chat_input.setStyleSheet(self.chat_input.input_style())
        self.chat_input.refresh_height()   # 字体/样式都设完了再量一次（构造时量的是默认字体）
        self.chat_send_btn = primary_btn("发送")
        self.chat_send_btn.setFixedSize(BTN_W, BTN_H)
        self.chat_send_btn.clicked.connect(self._emit_chat_send)
        foot.addWidget(self.chat_input, 1)
        foot.addWidget(self.chat_send_btn)
        box.addLayout(foot)
        return card

    def _build_chat_scroll(self):
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QScrollArea.NoFrame)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}" + _scrollbar_qss())
        self.chat_scroll.viewport().setStyleSheet("background:transparent;")

        self._rows_widget = QWidget()
        self._rows_widget.setStyleSheet("background:transparent;")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 6, 0)     # 右侧留一点给滚动条
        self._rows_layout.setSpacing(8)
        # 末尾常驻一个弹簧：新消息 insert 到它前面。
        # **`clear()` 之后必须只剩这 1 项** —— 自检按这个数断言。
        self._rows_layout.addStretch(1)
        self.chat_scroll.setWidget(self._rows_widget)
        return self.chat_scroll

    # ---------------------------------------------------------------- 工作页
    def _build_work_page(self):
        card = _card()
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        # 卡片顶部一行标题：任务名（自检/调用方认的 title 之外的第二个纯展示标签）
        self.work_title = label("Harness 工作台", TITLE_SIZE, True)
        box.addWidget(self.work_title)

        # 标题下面那行小字：左边状态，右边「刷新」小按钮（不是顶部整条操作栏）
        stat = QHBoxLayout()
        stat.setContentsMargins(0, 0, 0, 0)
        stat.setSpacing(8)
        self.work_status = label("", STATUS_SIZE, color=COLOR_BLUE)
        self.work_status.setWordWrap(False)
        self.work_refresh_btn = quiet_btn("刷新")
        self.work_refresh_btn.setFont(ui_font(11))
        self.work_refresh_btn.setMinimumHeight(0)
        self.work_refresh_btn.setFixedHeight(24)
        self.work_refresh_btn.setCursor(Qt.PointingHandCursor)
        self.work_refresh_btn.clicked.connect(lambda: self.refresh_sessions())
        stat.addWidget(self.work_status, 1)
        stat.addWidget(self.work_refresh_btn)
        box.addLayout(stat)

        # 正文区（历史记录）：HTML 渲染逻辑照搬旧工作台
        self.view = QTextBrowser()
        self.view.setFont(ui_font(BODY_SIZE))
        self.view.setOpenExternalLinks(False)
        # 视口和文档都要透明：QSS 只管控件，QTextBrowser 的**文档**另有一层白底，
        # 不关掉它就会在卡片圆角里面画出一个白色直角（截图里能看到）。
        self.view.viewport().setAutoFillBackground(False)
        palette = self.view.palette()
        palette.setColor(palette.ColorRole.Base, Qt.transparent)
        self.view.setPalette(palette)
        self.view.setStyleSheet(
            "QTextBrowser{background:transparent;border:none;padding:0;}"
            + _scrollbar_qss())
        box.addWidget(self.view, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(8)
        self.work_input = GrowingInput(card, self._emit_task_send, max_lines=4)
        self.work_input.setFont(ui_font(BODY_SIZE))
        self.work_input.setPlaceholderText("在这段对话里说点什么…（回车派活）")
        self.work_input.setStyleSheet(self.work_input.input_style())
        self.work_input.refresh_height()
        self.work_send_btn = primary_btn("派活")
        self.work_send_btn.setFixedSize(BTN_W, BTN_H)
        self.work_send_btn.clicked.connect(self._emit_task_send)
        foot.addWidget(self.work_input, 1)
        foot.addWidget(self.work_send_btn)
        box.addLayout(foot)
        return card

    # ==================================================================
    # 画整窗底 + 拖动
    # ==================================================================
    def paintEvent(self, _event):
        """自己画整窗圆角底与边框。

        为什么不让样式表 `border-radius` 去裁：Qt 的样式表**不裁子控件**，
        左栏那块浅蓝矩形是直角填满的，会从圆角里戳出来。
        所以底和描边交给 QPainter，子控件只负责自己那块（头部两角、左栏左侧两角）。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        outer = QRect(0, 0, self.width() - 1, self.height() - 1)
        shape = QPainterPath()
        shape.addRoundedRect(float(outer.x()), float(outer.y()),
                             float(outer.width()), float(outer.height()),
                             SHELL_RADIUS, SHELL_RADIUS)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLOR_PAPER))
        painter.drawPath(shape)
        painter.setPen(QPen(QColor(COLOR_BLUE), 1.5))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(outer, SHELL_RADIUS, SHELL_RADIUS)
        painter.end()

    def mousePressEvent(self, event):
        """头部空白可拖（照旧聊天窗口那套写法）。"""
        if event.button() == Qt.LeftButton and event.position().y() <= HEADER_H:
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

    # ==================================================================
    # 模式与摆位
    # ==================================================================
    def set_mode(self, mode):
        """切模式。**什么都不重建** —— 只换 stack 那一页和栏底按钮的文字。

        切完要**广播**出去（`on_mode_change`）：窗口在"工作"模式时输入框那句是
        派活、在"聊天"模式时是闲聊，桌宠那边的 `_harness_mode` 必须跟着变 ——
        两边不同步就会出现"看着像聊天，实际在家里改文件"。
        """
        self.mode = "chat" if str(mode) == "chat" else "work"
        self._sync_mode_ui()
        if self._on_mode_change is not None:
            try:
                self._on_mode_change(self.mode)
            except Exception:
                pass          # 广播失败不能把切模式本身带崩

    def toggle_mode(self):
        self.set_mode("work" if self.mode == "chat" else "chat")

    def _sync_mode_ui(self):
        self.stack.setCurrentIndex(PAGE_CHAT if self.mode == "chat" else PAGE_WORK)
        # 文字里始终含"切换"两个字（探针按这个断言）
        self.mode_btn.setText("切换工作模式" if self.mode == "chat" else "切换聊天模式")
        if self.mode == "work":
            self._refresh_work_header()
            self.set_status(self._idle_status())

    def show_window(self, mode, char_rect=None, screen_rect=None):
        """统一入口：切模式 → 摆到角色旁边 → show + raise。

        摆位算法照旧的 ChatWindow._place，只把左右偏好反过来：
        新窗口宽 980，贴在角色左侧时常常会顶到屏幕左边被夹回来，
        于是"贴左边"这个首选形同虚设；优先右侧更稳。
        """
        if mode:
            self.set_mode(mode)
        if char_rect is not None and screen_rect is not None:
            self._place(char_rect, screen_rect)
        # 左栏是**两个模式共用**的任务列表（主人要求"左边小栏是任务"），
        # 所以聊天模式打开时也要把它填上 —— 否则聊天时左栏一片空白。
        # 只在空的时候扫一次：切模式/反复开窗口不该每次去读一遍会话日志。
        if not self._rows:
            self.refresh_sessions()
        self.show()
        self.raise_()
        self.activateWindow()
        if self.mode == "chat":
            if not self._greeted:
                self._chat_add("主人，有什么事吗？", False)
                self._greeted = True
            self.chat_input.setFocus()
        else:
            self.work_input.setFocus()

    def _place(self, char_rect, screen_rect):
        x = char_rect.right() + 14
        if x > screen_rect.right() - self.width() - 8:
            x = char_rect.left() - self.width() - 14      # 右侧放不下 → 挪到左边
        x = max(screen_rect.left() + 8,
                min(x, screen_rect.right() - self.width() - 8))
        y = max(screen_rect.top() + 8,
                min(char_rect.top() - 8,
                    screen_rect.bottom() - self.height() - 8))
        self.move(int(x), int(y))

    # ==================================================================
    # 聊天侧（由 ShellChat 转发进来）
    # ==================================================================
    def _chat_insert(self, widget):
        """把一行**插在末尾弹簧之前**，并滚到底。

        为什么每次重算 count()-1：这个索引是"弹簧在哪"的唯一真相。
        自己存一个计数器的话，删一条消息不同步就会把内容插到弹簧后面（永远看不见）。
        """
        self._rows_layout.insertWidget(max(0, self._rows_layout.count() - 1),
                                       widget)
        self.chat_empty.setVisible(False)      # 有内容了，空态提示让位
        self._scroll_bottom()
        return widget

    def _chat_label(self, text, mine, danger=False):
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setFont(ui_font(BODY_SIZE))
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lb.setMaximumWidth(_auto_bubble_width(self))
        lb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        if danger:
            lb.setStyleSheet("color:%s;background:#FDECEA;border:1px solid #E9B3AC;"
                             "border-radius:10px;padding:8px 11px;" % DANGER)
        elif mine:
            lb.setStyleSheet("background:%s;color:#FFFFFF;border-radius:10px;"
                             "padding:8px 11px;" % COLOR_DEEP)
        else:
            lb.setStyleSheet("background:%s;color:%s;border:1px solid %s;"
                             "border-radius:10px;padding:8px 11px;"
                             % (COLOR_LIGHT, COLOR_DEEP, COLOR_BLUE))
        return lb

    def _chat_row(self, widget, mine):
        """左右分栏的一行：我说的靠右、她说的靠左（和旧聊天窗一致）。

        注意：行高必须锁成 Fixed。QScrollArea 的 widgetResizable=True 会把内容控件
        拉到视口大小，QVBoxLayout 于是把多出来的高度**分给每一行**——气泡被纵向
        拉成长条，行内 QLabel 的 QSS 背景就顺着拉长的那块画下去，截图里看着像
        "半透明色块压在其他气泡上"（探针第一版就是这样）。锁掉垂直策略，
        行只占自己的尺寸提示高度，气泡才贴得住内容。
        """
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        if mine:
            box.addStretch(1)
            box.addWidget(widget)
        else:
            box.addWidget(widget)
            box.addStretch(1)
        return self._chat_insert(row)

    def _chat_add(self, text, mine):
        self._chat_row(self._chat_label(text, mine), mine)

    def _chat_add_note(self, text):
        """灰蓝小字：系统提示 / 工具流水，不算"她说的话"（与旧实现同口径）。"""
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setFont(ui_font(11))
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lb.setMaximumWidth(_auto_bubble_width(self))
        lb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        lb.setStyleSheet("background:transparent;color:%s;" % COLOR_BLUE)
        self._chat_row(lb, False)

    def _chat_begin_reply(self):
        self._reply_label = self._chat_label("", False)
        self._chat_row(self._reply_label, False)

    def _chat_stream(self, piece):
        if self._reply_label is None:
            self._chat_begin_reply()
        self._reply_label.setText(self._reply_label.text() + str(piece))

    def _chat_stream_full(self, text):
        """整段刷新：心情标记是从回复**末尾**逐字长出来的，
        追加式没法把已经显示出去的半截标记收回来（旧实现的注释里写过这个坑）。"""
        if self._reply_label is None:
            self._chat_begin_reply()
        self._reply_label.setText(str(text))

    def _chat_end_reply(self):
        if self._reply_label is not None and not self._reply_label.text():
            self._reply_label.setText("……")
        self._reply_label = None

    def _chat_fail_reply(self, text):
        """把还没写完的那条气泡改成错误提示；没有在写的气泡就单开一条红字。
        不新开一条 —— 否则会留下一个空的"……"加一条红字，很脏（旧版踩过）。"""
        if self._reply_label is not None:
            self._reply_label.setText(str(text))
            self._reply_label.setStyleSheet(
                "color:%s;background:#FDECEA;border:1px solid #E9B3AC;"
                "border-radius:10px;padding:8px 11px;" % DANGER)
            self._reply_label = None
        else:
            self._chat_add_error(text)

    def _chat_add_error(self, text):
        lb = QLabel(str(text))
        lb.setWordWrap(True)
        lb.setFont(ui_font(11))
        lb.setMaximumWidth(_auto_bubble_width(self))
        lb.setStyleSheet("color:%s;background:transparent;" % DANGER)
        self._chat_row(lb, False)

    def _chat_clear(self):
        """只留一行提示。**自检按 `_rows_layout.count() == 1` 断言**，
        所以清完必须只剩末尾那个弹簧。"""
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._reply_label = None
        self._greeted = False
        self.chat_empty.setVisible(True)

    def _chat_set_busy(self, busy):
        self._busy = bool(busy)
        self.chat_send_btn.setEnabled(not busy)
        self.chat_input.setEnabled(not busy)
        self.chat_input.setPlaceholderText("鲸鲸正在想…" if busy
                                          else "和鲸鲸说点什么…")
        if not busy:
            self.chat_input.setFocus()

    def _emit_chat_send(self):
        text = self.chat_input.plain().strip()
        if not text or self._busy:
            return
        self.chat_input.clear()
        self.on_chat_send(text)

    def _emit_clear(self):
        if self.on_chat_clear is not None:
            self.on_chat_clear()

    def _scroll_bottom(self):
        """滚到底。post 到下一次事件循环再滚。

        同步滚的话，QLabel 刚 setText、布局还没重算完，scrollbar 的 maximum()
        还是旧值 —— 长回复会长到一半就停住（旧实现用 QTimer.singleShot(0, ...)
        就是这个原因，这里保持一致）。
        """
        QTimer.singleShot(0, self._scroll_bottom_now)

    def _scroll_bottom_now(self):
        try:
            bar = self.chat_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:
            pass          # 窗口已经销毁（关闭时排队的 singleShot 可能还没跑）

    # ==================================================================
    # 工作侧（由 ShellWork 转发进来）
    # ==================================================================
    def _add_session_row(self, r, num=None, depth=0, parent_title="",
                         sub_count=0, collapsed=False):
        """往左栏加一行会话。

        * `num` 有值 → **主会话**（主人自己开的那段），标题前带「对话一/二…」；
        * `depth > 0` → **子会话**（子智能体跑的），缩进 + `└`，
          副标题写「子任务 · 第 N 层」，鼠标悬停能看到是哪段主会话派出去的。
        主子身份不是猜的：`dsh_session_log` 从会话日志头部读 DSH 自己写的
        `origin` / `parentSession` / `delegationDepth`（见那里的说明）。
        """
        indent = " " * (6 * min(max(depth, 0), 3))       # 最多缩 3 层，再深就顶格了
        if depth > 0:
            prefix = "%s└ " % indent
            sub = "子任务 · 第 %d 层 · %s · %d 轮" % (
                depth, r.get("time_label", ""), r.get("turns", 0))
            tip = ("子智能体跑的（第 %d 层）" % depth)
            if parent_title:
                tip += "，由「%s」派出" % parent_title
        else:
            prefix = "对话%s  " % _cn_num(num)          # 主会话才有编号
            sub = "主会话 · %s · %d 轮" % (r.get("time_label", ""),
                                           r.get("turns", 0))
            tip = "主智能体跑的（你自己开的对话）"
            if sub_count:
                # 折叠时得让主人**知道有子会话**，否则会以为活没派出去 ——
                # 那正是当初把子会话列出来的理由（见 18.2 那段说明）
                folded = "" if collapsed else "（已展开）"
                sub += " · %d 个子任务%s" % (sub_count, folded)
                tip += "；它派出去 %d 个子任务，右键可以%s" % (
                    sub_count, "展开" if collapsed else "折叠")
        # 省略宽度要**扣掉前缀**再算：不扣的话缩进会吃掉标题的配额，
        # 子会话标题被截成"你要给…"这种看不出所以然的样子（截图里一眼看到）。
        room = RAIL_W - 2 * RAIL_PAD - 48
        room -= QFontMetrics(ui_font(ITEM_TITLE_SIZE)).horizontalAdvance(prefix)
        short = prefix + _elide(r["title"], max(48, room), size=ITEM_TITLE_SIZE)
        item = QListWidgetItem(short)
        # 副标题走 UserRole：item.text() 必须留着（自检/调用方都在读），
        # 而 delegate 要从 item 上多取一行字，只有角色这条路不给 text 加料。
        item.setData(Qt.UserRole, sub)
        item.setToolTip("%s\n%s" % (r.get("title") or r.get("id"), tip))
        self.task_list.addItem(item)
        self._rows.append(r)

    def _toggle_kids(self, main_id):
        """展开/折叠某段主任务的子会话，然后重建左栏。

        重建而不是"就地插入/删除那几行"：列表里行的位置 = `_rows` 的下标，
        就地改容易让两边错位（右键拿到的行会指到另一段任务上，删除就删错了 ✗）。
        重建一次的开销可以忽略。
        """
        if main_id in self._expanded:
            self._expanded.discard(main_id)
        else:
            self._expanded.add(main_id)
        self.refresh_sessions()

    def _on_rail_menu(self, pos):
        """左栏任务列表的右键菜单（主人 2026-10-11）：删掉选中的这段任务。

        第 0 行是「开一段新对话」占位项，没有 id 也不该能删 —— 直接放行。
        真正的删除（含确认框）在桌宠那边（`pet_mixin_link.delete_task_session`），
        这里只负责把"选中了哪一段"问出来。
        """
        item = self.task_list.itemAt(pos)
        row = self.task_list.row(item) if item is not None else -1
        if row <= 0 or (row - 1) >= len(self._rows):
            return
        r = self._rows[row - 1]
        if self._on_task_delete is None:
            return
        menu = QMenu(self)
        mark = "〔子任务〕" if r.get("kind") == "sub" else ""
        # 主会话那行多一项：展开 / 折叠它的子任务（子会话**默认折叠**）
        if r.get("kind") != "sub":
            mid = str(r.get("id") or "")
            n = len(self._kids.get(mid) or [])
            if n:
                if mid in self._expanded:
                    menu.addAction("折叠 %d 个子任务" % n,
                                   lambda: self._toggle_kids(mid))
                else:
                    menu.addAction("展开 %d 个子任务" % n,
                                   lambda: self._toggle_kids(mid))
                menu.addSeparator()
        menu.addAction("删除「%s%s」…" % (mark, (r.get("title") or "")[:14]),
                       lambda: self._on_task_delete(r.get("id"), r.get("title")))
        menu.exec(self.task_list.mapToGlobal(pos))

    def refresh_sessions(self, keep_row=None):
        """重扫会话列表并重建左栏。

        每次刷新都重扫是有意的：干完一轮活之后那段对话的轮数/时间都变了，
        不重扫主人看到的就是旧数据。选中项尽量停在原来那段（keep_row）。

        列表按「主会话 + 它派出去的子会话」分组显示（`group_sessions`），
        子会话缩进挂在父亲下面 —— 这样一眼能看出哪段是主人自己开的、
        哪段是子智能体在跑。
        """
        want_row = keep_row if keep_row is not None else self._tid()
        self._in_refresh = True
        self.task_list.blockSignals(True)      # 重建时别触发一串 _on_row
        self.task_list.clear()
        self._rows = []
        self.task_list.addItem(QListWidgetItem("开一段新对话"))
        self.task_list.item(0).setData(Qt.UserRole, "说第一句话就开一段")
        if SESSION_LOG_AVAILABLE:
            try:
                rows = [r for r in _session_log.list_sessions() if r.get("cwd")]
            except Exception:
                rows = []                      # 日志坏了不能拖垮整个窗口
            try:
                groups = _session_log.group_sessions(rows)
            except Exception:
                groups = [(r, []) for r in rows]   # 分组出错就退回平铺，别丢东西
            num = 0
            self._kids = {}
            for main, kids in groups:
                mid = str((main or {}).get("id") or "")
                if main is not None:
                    num += 1
                    self._kids[mid] = kids
                    # 想选中的正是某个子会话 → **自动展开**它的父任务：
                    # 否则那一行根本不在列表里，"_want_id 粘性"就永远找不到它
                    # （"第二句话接着说同一段"全靠它）
                    if self._want_id and any(
                            str(k.get("id") or "") == self._want_id for k in kids):
                        self._expanded.add(mid)
                show_kids = main is None or mid in self._expanded
                if main is not None:
                    self._add_session_row(main, num=num, sub_count=len(kids),
                                          collapsed=not show_kids)
                if show_kids:
                    for sub in kids:
                        self._add_session_row(
                            sub, depth=max(1, int(sub.get("depth") or 1)),
                            parent_title=(main or {}).get("title", ""))
        self.task_list.blockSignals(False)
        row = want_row
        found = False
        if self._want_id:
            # 新开的那段一旦进了列表就选上 —— 不选它，主人第二句话又会被
            # 当成"再开一段"（旧的 _want_id 机制，语义照搬）
            for i, r in enumerate(self._rows, 1):
                if r["id"] == self._want_id:
                    row, found = i, True
                    break
        if self.task_list.count():
            row = min(max(int(row), 0), self.task_list.count() - 1)
            if found or not self._want_id:
                # **只有真选中了才消耗**那个待选 id。
                # 目标还没进列表时（新会话一条消息都没有，list_sessions 会滤掉它）
                # 必须留着：等 finish() 那次刷新、那段对话终于有内容进列表了，
                # 才能把它选上 —— "第二句话接着说是同一段"全靠这一步。
                # 早先写成"刷新过就清掉"，粘性就没了（子智能体交付时提过这个疑点）。
                self._want_id = ""
            self.task_list.setCurrentRow(row)
            self._on_row(row)                  # blockSignals 把信号挡掉了，手动补一次
        self._in_refresh = False

    def _tid(self):
        """左栏当前选中的行号。列表还没建起来时给 0（= "开一段新对话"）。"""
        try:
            row = self.task_list.currentRow()
        except RuntimeError:
            return 0
        return row if row is not None and row >= 0 else 0

    def adopt_session(self, session_id, cwd=""):
        """桥报回"这段活跑在哪个会话上"：记下 id，刷一次并尽量选中它。

        对**新开**的会话，这一次可能选不中 —— 刚建出来时还没有用户消息，
        list_sessions 会把空会话滤掉；id 留在 _want_id 里，下一轮刷新（finish）就会选上。
        """
        self._want_id = session_id
        self.refresh_sessions()

    def _on_row(self, index):
        """左栏选中项变了：换正文 + 通知外面（选中的是哪段）。"""
        if index is None or index < 0:
            return
        if index == 0:
            self._log_path = ""
            target = None
        elif index - 1 < len(self._rows):
            r = self._rows[index - 1]
            target = {"id": r["id"], "cwd": r["cwd"], "title": r["title"]}
            self._log_path = r["log_path"]
        else:
            return
        if self.on_task_select is not None:
            self.on_task_select(target)
        # 聊天模式下点任务 = "我要接着这段干活" → 直接切到工作模式并把它选上。
        # 刷新过程里的行变化不算（_in_refresh），否则一刷新就把人从聊天里踢出去。
        if not self._in_refresh and self.mode == "chat":
            self.set_mode("work")
        self._refresh_work_header()
        if self._log_path:
            self._load_history()
        else:
            self._turns = []
            self._render()
            self.set_status("开一段新对话：说第一句话，她就替你在这里开一段。")

    def _load_history(self):
        try:
            self._turns = _session_log.read_transcript(self._log_path)
        except Exception as err:
            self._turns = [("错误", "读不出来：%s: %s"
                            % (type(err).__name__, err))]
        self._render(scroll=True)

    def _render(self, scroll=False):
        """整篇重拼 HTML 再塞给正文区（**不追加**，见文件上面那条口径）。

        渲染走本文件的 render_work_transcript_html：先按角色分块，每块正文
        做 markdown → HTML（转义在里面）。这里只管两件事：
          * 正文文档自己的背景刷透明 —— QTextBrowser 默认给文档铺一层白底，
            它不受 QSS 的 `background:transparent` 管，会盖住卡片自己的底色；
          * scroll=True 时滚到底（流式回话要一直看着最新那几行）。
        段间距、卡片留白这些观感数字全在渲染器里，这里不再对 HTML 做字符串修补
        （旧版是靠三次 replace 去压 pet_bridges 那份的边距，那种"改别人的输出"
        对方一动就悄悄失效）。
        """
        body = render_work_transcript_html(self._turns, self._live)
        self.view.document().setDefaultStyleSheet(
            "body{background:transparent;}")
        self.view.setHtml(body)
        if scroll:
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _work_stream(self, piece):
        self._live += str(piece)
        self._render(scroll=True)

    def _work_tool(self, line):
        self._turns.append(("工具", line))       # 灰字流水（色表在 _WORK_PALETTE）
        self._render(scroll=True)

    def _work_note(self, text, role="助手"):
        self._turns.append((role, text))
        self._render(scroll=True)

    def _work_fail(self, text):
        self._live = ""
        self._turns.append(("错误", text))
        self._render(scroll=True)

    def _work_finish(self, text):
        """一轮干完：清流式缓存 → 从日志重读当权威版本 → 刷列表。

        为什么不把 text 直接追加进 _turns：日志里那段带着完整工具流水和真实轮次，
        重读一次显示的就和"任务对话"里完全一致，不用维护第二套拼装逻辑。
        刷列表不能省：新开的会话到这时才**有内容**，才会出现在列表里被选上。
        """
        self._live = ""
        if self._log_path:
            self._load_history()
        elif text:
            self._turns.append(("助手", text))
            self._render(scroll=True)
        self.refresh_sessions()

    def _work_set_busy(self, busy):
        self._work_busy = bool(busy)
        self.work_input.setEnabled(not busy)
        self.work_send_btn.setEnabled(not busy)
        self.work_send_btn.setText("干活中…" if busy else "派活")
        if not busy:
            self.work_input.setFocus()

    def _emit_task_send(self):
        text = self.work_input.plain().strip()
        if not text or self._work_busy:
            return
        self.work_input.clear()
        cur = self._get_target()
        self._turns.append(("主人", text))
        if cur:
            self.set_status("接着「%s」干…" % (cur.get("title") or ""))
        else:
            self.set_status("给她开一段新对话…")
        self._render(scroll=True)
        self.on_task_send(text)

    def set_status(self, text):
        """卡片标题下那行小字（状态行）。太长就截断 —— 它是**一行**，
        换行会把正文区顶下去，卡片高度跟着跳。"""
        self._status_text = str(text or "")
        self._fit_status()

    def _fit_status(self):
        # 样式/字体变化在 __init__ 里就会先来一次（setStyleSheet 触发 changeEvent），
        # 那时状态行还没建出来 —— 不守卫的话窗口根本建不起来（探针第一次跑就撞上了）
        if not hasattr(self, "work_status"):
            return
        room = max(80, self.work_status.width() - 4)
        self.work_status.setText(_elide(getattr(self, "_status_text", ""), room,
                                        size=STATUS_SIZE))

    def _idle_status(self):
        """空闲时状态行显示什么：没选会话就提示怎么开始，选了就显示它的工作目录。"""
        target = self._get_target()
        if not target:
            return "开一段新对话：说第一句话，她就替你在这里开一段。"
        cwd = str(target.get("cwd") or "")
        return ("工作目录 " + cwd) if cwd else "接的是：%s" % (target.get("title") or "")

    def _refresh_work_header(self):
        target = self._get_target()
        name = (target.get("title") if target else "") or "开一段新对话"
        self.work_title.setText(_elide(name, max(120, self.width() - RAIL_W - 90),
                                       bold=True, size=TITLE_SIZE))
        self.work_title.setToolTip(name)

    # ==================================================================
    # 事件
    # ==================================================================
    def changeEvent(self, event):
        if event.type() == QEvent.FontChange or event.type() == QEvent.StyleChange:
            self._fit_status()
        super().changeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_status()
        # 窗口变宽变窄后，已画好的气泡宽度也该跟着变（不然拉宽了气泡还是窄的）。
        # 走 singleShot 而不是当场算：resizeEvent 里视口还是旧宽度，
        # 当场算会把气泡按上一次的宽度夹一遍（看着像"拉宽了没反应"）。
        QTimer.singleShot(0, self._reflect_bubble_widths)

    def _reflect_bubble_widths(self):
        try:
            widest = _auto_bubble_width(self)
            for i in range(self._rows_layout.count() - 1):
                widget = self._rows_layout.itemAt(i).widget()
                if widget is None:
                    continue
                for lb in widget.findChildren(QLabel):
                    lb.setMaximumWidth(widest)
        except RuntimeError:
            pass          # 窗口已销毁（排队的 singleShot 还没跑）

    def hideEvent(self, event):
        # 收起时清掉拖动状态：拖动中途窗口被隐藏（比如 Alt+Tab 或程序主动 hide），
        # 残留的 _drag_from 会让"下次按下鼠标没动、窗口自己跳一下"。
        self._drag_from = None
        super().hideEvent(event)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_shell(parent, on_chat_send, on_chat_clear=None, on_task_send=None,
                on_task_select=None, get_task_target=None,
                on_mode_change=None, on_task_delete=None):
    """造那唯一的主壳窗口，返回 Shell 实例。

    参数（顺序与调用方约定一致）：
      * on_chat_send(text)     —— 聊天输入框回车/点发送；
      * on_chat_clear()        —— 卡片标题栏那个「清空」；
      * on_task_send(text)     —— 工作输入框回车/点派活；
      * on_task_select(row)    —— 左栏选了哪段（"开一段新对话"那行传 None）；
      * get_task_target()      —— 问外面"现在接的是哪段"（返回 dict 或 None）；
      * on_mode_change(mode)   —— 模式变了（栏底那个按钮点的）：桌宠靠它同步
                                  `_harness_mode`，否则会出现"看着像聊天、
                                  实际在派活"的错位。

    后四个留了默认值：只写 `build_shell(parent, on_chat_send)` 也能起来
    （接线是分步做的，缺一个回调不该让整个窗口建不出来）。
    """
    if get_task_target is None:
        get_task_target = lambda: None
    return Shell(parent, on_chat_send, on_chat_clear, on_task_send,
                 on_task_select, get_task_target, on_mode_change,
                 on_task_delete)
