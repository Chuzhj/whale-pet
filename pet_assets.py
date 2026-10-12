# -*- coding: utf-8 -*-
"""素材：定位资源目录、按角色标识配对、发现分桶、按目标高度缩放。"""
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


def resource_dir():
    """返回资源根目录。

    打包成 exe 后，PyInstaller 会把 --add-data 带上的文件解压到 sys._MEIPASS；
    直接跑 .py 时则用脚本自身所在目录。两条路径都支持，代码不用分叉。
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def asset_key(stem):
    """把文件名归一成"角色标识"，用于把睁眼图和闭眼图配成对。

    01_front     -> front      expr_happy  -> happy
    02_front_var_a -> var_a    blink_happy -> happy
    blink_front  -> front      04_back     -> back
    """
    name = stem.lower()
    if name.startswith("blink_"):
        name = name[len("blink_"):]
    elif name.startswith("expr_"):
        name = name[len("expr_"):]
    else:
        head, sep, tail = name.partition("_")
        if sep and head.isdigit():
            name = tail
    if "var" in name:
        return name[name.index("var"):]
    if "front" in name:
        return "front"
    return name


def discover_assets(assets_dir: Path):
    """扫描素材目录，按文件名关键字归类。

    加入新素材时不用改代码：文件名里带 expr 的当"点击表情"，带 back 的当
    背面，带 var 的当变体，带 front 的当正面主图。
    判断顺序是 expr → back → var → front，避免 var 文件因为同时含有 front
    而被误判。
    """
    if not assets_dir.is_dir():
        raise SystemExit("[素材] 找不到目录：%s" % assets_dir)

    buckets = {"front": [], "variant": [], "back": [],
               "expression": [], "blink": [], "sleep": [], "ahoge": [], "tail": [],
               "bowl": []}
    for path in sorted(assets_dir.iterdir()):
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        name = path.stem.lower()
        if "expr" in name:
            buckets["expression"].append(path)
        elif "bowl" in name:               # 白饭：bowl_full / bowl_empty
            buckets["bowl"].append(path)
        elif "sleep" in name:
            buckets["sleep"].append(path)
        elif "ahoge" in name:
            buckets["ahoge"].append(path)
        elif "tail" in name:
            buckets["tail"].append(path)
        elif "blink" in name:
            buckets["blink"].append(path)
        elif "back" in name:
            buckets["back"].append(path)
        elif "var" in name:
            buckets["variant"].append(path)
        elif "front" in name:
            buckets["front"].append(path)

    total = sum(len(v) for v in buckets.values())
    if total == 0:
        raise SystemExit("[素材] %s 里没有可用图片" % assets_dir)
    return buckets


def load_scaled(path: Path, target_h: int):
    """读图并一次性缩放到目标高度，之后每帧只做绘制、不再重采样。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        raise SystemExit("[素材] 无法读取：%s" % path)
    return pixmap.scaledToHeight(target_h, Qt.SmoothTransformation)
