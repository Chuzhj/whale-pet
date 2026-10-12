# -*- coding: utf-8 -*-
"""时间与时长：中文数字、「半小时 / 一个半小时 / 一刻钟」解析、本地提醒意图兜底、时间人话化。

零 Qt 依赖 —— 主人的原话是最可信的时长来源，模型给不准时靠这里兜底。"""
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


def cn_number(text):
    """把简单中文数字转成数值（十 / 二十 / 二十五 / 半）；解析不了返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        pass
    if text == "半":
        return 0.5
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    m = re.fullmatch(r"([一二三四五六七八九])?十([一二三四五六七八九])?", text)
    if m:                                   # 十 / 十五 / 二十 / 二十五
        tens = _CN_DIGITS.get(m.group(1) or "一", 1)
        ones = _CN_DIGITS.get(m.group(2) or "", 0)
        return tens * 10 + ones
    return None


def parse_duration(text):
    """从一句话里解析时长（秒，float）；没有时长表达返回 None。

    支持：十秒 / 30秒 / 半小时 / 一个半小时 / 一个小时 / 两小时 /
    三十分钟 / 一刻钟 / 0.5分钟 这类常规说法。
    "下午三点"这类绝对时间解析不出来（那是模型结合当前时间的活）；
    而"三点十分"里的"十分"是**时刻**不是时长，这里显式排除，
    免得主人说"三点十分叫我"被登记成"十分钟后"。
    """
    text = text or ""
    m = _DURATION_COMPOUND_RE.search(text)           # 一个半小时 / 1个半钟头
    if m:
        n = cn_number(m.group(1))
        if n is not None and n >= 0:
            return (float(n) + 0.5) * 3600.0
    if _QUARTER_RE.search(text):                     # 一刻钟 = 15 分钟
        return 900.0
    for m in _DURATION_RE.finditer(text):
        if _CLOCK_BEFORE_RE.search(text[:m.start()]):
            continue                                 # "三点十分" → 不是时长
        n = cn_number(m.group(1))
        if n is None or n < 0:
            continue
        unit = m.group(2)
        if unit in ("秒", "秒钟"):
            return float(n)
        if unit in ("分钟", "分"):
            return float(n) * 60.0
        if unit in ("小时", "钟头", "钟"):            # "一个钟头" = 1 小时
            return float(n) * 3600.0
    return None


def reminder_from_user(text):
    """本地兜底：从用户原话解析"X 后提醒我 Y"，返回 (秒, 事项) 或 None。

    只在**祈使句**（含"提醒我/叫我/喊我"）且有明确时长时才生效 ——
    "我三十分钟后要开会"这种陈述句不会被误判成设提醒。
    """
    text = text or ""
    if not _REMIND_ASK_RE.search(text):
        return None
    seconds = parse_duration(text)
    if not seconds or seconds <= 0:
        return None
    m = re.search(r"(?:提醒我|叫我|喊我|提醒一下|提醒下)\s*"
                  r"([^。！？，,；;、\s]{1,12})", text)
    note = (m.group(1) if m else "提醒").strip()
    note = re.sub(r"^(一下|去|来|记得)+", "", note) or "提醒"
    return seconds, note


def cancel_from_user(text):
    """从主人原话判断"要取消提醒"并抽出关键词；不是取消意图就返回 None。

    返回 "" = **全部取消**；返回关键词 = 只取消对得上的那些。
    "" 和 None 是两回事（同 strip_cancel_tag 的约定），调用方必须分清。
    """
    text = (text or "").strip()
    if not text or not _CANCEL_INTENT_RE.search(text):
        return None
    if _CANCEL_NEG_RE.search(text):
        return None                     # "先不取消/还没取消" 一概不动
    key = _CANCEL_STRIP_RE.sub("", text).strip()
    key = re.sub(r"[，。！？,.!?\s]+", "", key)
    return key if len(key) >= 2 else ""


def note_matches(note, key):
    """提醒内容与关键词是否对得上（**双向包含**）。

    单向（key in note）会漏掉主人说得更长的情况："十月二十号跑步" vs 记事
    "跑步"；反向也兜住"跑步"对"跑步"这种核心词。空关键词 = 全都要。
    """
    note = (note or "").strip()
    key = (key or "").strip()
    if not key:
        return True
    if not note:
        return False
    return key in note or note in key


def format_seconds(seconds):
    """把秒数说成人话：10 秒 / 30 分钟 / 1 小时 30 分钟。"""
    seconds = int(round(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        minutes = seconds / 60.0
        return ("%d 分钟" % round(minutes)) if abs(minutes - round(minutes)) < 0.05 \
            else ("%.1f 分钟" % minutes)
    hours, rest = divmod(seconds, 3600)
    minutes = int(round(rest / 60.0))
    return ("%d 小时" % hours) if minutes == 0 else \
        ("%d 小时 %d 分钟" % (hours, minutes))


def now_line():
    """给模型的"当前本地时间"注入行 —— **每次请求现取**。

    不能启动时算死：聊半小时之后，注入的时间还停在启动那一刻，
    模型给"半小时后"算出来的时间点就全错了（提醒功能的地基）。
    """
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return ("【当前本地时间】%d年%d月%d日 星期%s %s。主人问时间、算时长、"
            "设提醒都要以此为准。" % (now.year, now.month, now.day, week,
                                    now.strftime("%H:%M")))


def format_alarm_time(when_dt, now=None):
    """把提醒时间说成人话：今天 08:00 / 明天 08:00 / 10月20日 08:00。"""
    now = now or datetime.now()
    delta_days = (when_dt.date() - now.date()).days
    hhmm = when_dt.strftime("%H:%M")
    if delta_days == 0:
        return "今天 %s" % hhmm
    if delta_days == 1:
        return "明天 %s" % hhmm
    if delta_days == 2:
        return "后天 %s" % hhmm
    return "%d月%d日 %s" % (when_dt.month, when_dt.day, hhmm)


def _cn_num(n):
    """1~99 的中文数字，给「对话一 / 对话二 / 对话三」编号用。"""
    digits = "零一二三四五六七八九"
    if n <= 10:
        return "十" if n == 10 else digits[n]
    if n < 20:
        return "十" + digits[n - 10]
    tens, ones = divmod(n, 10)
    return digits[tens] + "十" + (digits[ones] if ones else "")
