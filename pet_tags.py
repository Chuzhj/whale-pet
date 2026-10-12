# -*- coding: utf-8 -*-
"""回复标记：mood / memory / remind / alarm / cancel 的剥离、流式半截隐藏、气泡预览。

标记的「词汇表」（正则 + 名字表）也在这里，新增标记只改这一个文件。"""
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


def _extract_tag(text, regex, pick):
    """删掉文本里所有该形状的标记，返回 (干净文本, pick(最后一个匹配的 groups) 或 None)。

    抽成公用件：三个标记的剥取逻辑同构（全局删 + 取最后一个值）。
    """
    text = text or ""
    hits = list(regex.finditer(text))
    if not hits:
        return text.strip(), None
    clean = " ".join(regex.sub("", text).split())     # 抹掉标记后顺手归拢空白
    return clean, pick(hits[-1].groups())


def strip_mood_tag(text):
    """剥掉心情标记（不限位置），返回 (干净文本, 心情 或 None)。

    **没找到标记必须返回 None，不能返回 "idle"** —— 用默认值表示"缺失"会造出
    一个剥不掉的死角：标记恰好写成 [mood:idle] 时，"剥到了 idle"和"没有标记"
    完全无法区分，于是调用方不更新文本，`[mood:idle]` 直接漏进气泡显示
    （实测踩过）。缺失就是缺失，默认值由调用方决定。
    名字不认识的写法仍然落回 "idle"（那是"剥到了但值不认识"）。
    """

    def pick(groups):
        mood = groups[0].lower()
        return mood if mood in MOOD_NAMES else "idle"

    return _extract_tag(text, MOOD_TAG_RE, pick)


def hide_mood_fragment(text):
    """流式渲染时用：把整段/末尾半截的标记从**显示**里藏掉。

    剥标记（strip_mood_tag）只认完整闭合的标记，管不了打字机打到一半的
    "[mood:an" —— 所以画面渲染一律先过这道。

    半截的判据是"方括号里的词**是不是某个已知标记的前缀**"，而不是
    原来的"以 a/m/r 开头"。后者会把 "[apple]" 这类正常正文也一起藏掉，
    表现出来就是打出去的字闪一下又回来。
    """
    text = text or ""
    text = MOOD_FRAGMENT_RE.sub("", text)
    tail = _TAG_TAIL_RE.search(text)
    if tail:
        word = tail.group(1).lower()
        if word and any(k.startswith(word) for k in _TAG_KEYWORDS):
            text = text[:tail.start()]
    return text.rstrip()


def strip_memory_tag(text):
    """剥掉记忆标记（不限位置），返回 (干净文本, 记忆内容或 None)。"""
    return _extract_tag(text, MEMORY_TAG_RE, lambda g: g[0].strip())


def strip_remind_tag(text):
    """剥掉提醒标记（不限位置），返回 (干净文本, (分钟数, 事项) 或 None)。"""
    text, groups = _extract_tag(text, REMIND_TAG_RE,
                                lambda g: (float(g[0]), g[1].strip()))
    return text, groups


def strip_alarm_tag(text):
    """剥掉长期提醒标记，返回 (干净文本, (datetime, 事项) 或 None)。

    日期时间解析失败（2月30日这种）就当没这个标记 —— 剥掉但**不登记**，
    免得在界面上留一段谁也看不懂的方括号。
    """
    def pick(groups):
        try:
            when_dt = datetime(int(groups[0]), int(groups[1]), int(groups[2]),
                               int(groups[3]), int(groups[4]))
        except ValueError:
            return None
        return (when_dt, groups[5].strip())

    text, got = _extract_tag(text, ALARM_TAG_RE, pick)
    return text, got


def strip_cancel_tag(text):
    """剥掉取消标记（不限位置），返回 (干净文本, 关键词 或 None)。

    **关键词可能是空串，那和"没有标记"(None) 完全是两回事** ——
    空串 = "全都取消"，None = "主人没要求取消"。
    这正是本项目踩过的"用默认值表示缺失"的坑（标记漏进气泡那次），
    所以这里宁可返回 ""，也不拿 None 兼职。
    """
    text, got = _extract_tag(text, CANCEL_TAG_RE, lambda g: g[0].strip())
    return text, got


def strip_reply_tags(text):
    """一次剥掉所有标记（mood / memory / remind / alarm / cancel），
    返回 (正文, mood, memory, remind, alarm, cancel)。

    几个剥取函数都是**全局精确匹配**（不锚末尾），所以各调一次就够 ——
    不需要循环，也不怕模型把标记写乱位置。这是被实机逼出来的：
    模型经常把标记写在句子中间还接着补话，只认末尾时标记会直接漏进气泡。
    """
    text, mood = strip_mood_tag(text)
    text, memory = strip_memory_tag(text)
    text, remind = strip_remind_tag(text)
    text, alarm = strip_alarm_tag(text)
    text, cancel = strip_cancel_tag(text)
    return (text, (mood if mood is not None else "idle"), memory, remind,
            alarm, cancel)


def bubble_preview(text, limit=CHAT_BUBBLE_CHARS):
    """桌面气泡里显示的那一段。

    全文存在聊天窗口里，气泡只当"她在桌面上说了一句"。太长就截断，
    但要**明说**剩下的在窗口里 —— 不静默丢内容。
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……（剩下的在聊天窗口里）"


def bubble_text_flags(align_top=False):
    """气泡文字的排版 flags。

    **量尺寸和画文字必须共用这一个函数。** 踩过的坑：`_measure()` 里带了
    TextWordWrap、`paintEvent` 里漏了，于是"量"按换行算（气泡宽度是对的）、
    "画"按一整行算并被裁进矩形 —— 气泡右边那截字凭空消失，而且代码看上去毫无问题。
    合成一处之后，两边不可能再分叉。
    """
    from PySide6.QtCore import Qt

    flags = int(Qt.TextWordWrap) | int(Qt.AlignLeft)
    return flags | (int(Qt.AlignTop) if align_top else 0)
