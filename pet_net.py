# -*- coding: utf-8 -*-
"""网络与解析：SSE 分帧、非流式 JSON 回复、厂商端点解析、必应结果解析、错误话术。

只做「字节 → 结构化」这一层，不发请求（发请求在 pet_bridges 里）。"""
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


def resolve_endpoint(cfg):
    """把 provider 预设和手填值合成最终的 (base_url, model, 厂商自带联网)。"""
    preset = PROVIDER_PRESETS.get(str(cfg.get("provider", "")).lower())
    base, model, native = preset if preset else ("", "", False)
    base = cfg.get("base_url") or base
    model = cfg.get("model") or model
    return str(base).rstrip("/"), str(model), bool(native)


def sse_delta(line):
    """解析一行 SSE，返回本次的增量文本。

    返回 None = 这行不是数据行；返回 "" = 收到结束标记 [DONE]。
    抽成纯函数是为了自检能直接喂字符串 —— 不用真联网也能验流式解析。
    """
    line = (line or "").strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return ""
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    choices = obj.get("choices") or []
    if not choices:
        return None
    piece = (choices[0].get("delta") or {}).get("content")
    if piece:
        return piece
    # 有的服务端把首包塞在 message 里而不是 delta
    return (choices[0].get("message") or {}).get("content") or None


def sse_finish_reason(line):
    """从一行 SSE 里取 finish_reason（没有就是 None）。

    单独抽出来是为了让"这份回复是被 max_tokens 截断的"可判：
    服务端会发 finish_reason="length"，那不是网络错误（回复照样能用），
    但主人该知道她话没说完 —— 不然看起来就像她突然哑了。
    """
    line = (line or "").strip()
    if not line.startswith("data:") or "[DONE]" in line:
        return None
    try:
        obj = json.loads(line[5:].strip())
    except ValueError:
        return None
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return str(reason) if reason else None


def reply_from_json(raw):
    """整块 JSON（非流式）里取回复正文。"""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def wants_search(text):
    """这句话要不要联网查一下。

    分两类：明确要求搜索的说法直接搜；时效性说法必须**同时**带疑问语气 ——
    否则"我今天吃了白饭"也会触发搜索。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(k in t for k in SEARCH_EXPLICIT):
        return True
    return (any(k in t for k in SEARCH_TIMELY)
            and any(k in t for k in SEARCH_QUESTION))


def search_query(text):
    """把口令和客套话从搜索词里摘掉。

    拿"帮我搜一下鲸鱼娘的梗"整句去搜，搜索引擎会被"帮我搜一下"这几个字干扰；
    摘成"鲸鱼娘的梗"结果才准。全摘没了（比如整句就是"搜一下"）就退回原句 ——
    **宁可搜得笨一点，也不能搜成空字符串**。
    """
    query = text or ""
    for word in SEARCH_EXPLICIT + SEARCH_FILLER:
        query = query.replace(word, " ")
    return " ".join(query.split()) or (text or "").strip()


def strip_tags(fragment):
    """去掉标签、还原 HTML 实体、把空白压平。

    最后那步不能省：网页里的 &nbsp; 会变成 U+00A0（不换行空格），
    肉眼看着是空格，跟真空格却不相等 —— 拿到摘要里就是一堆莫名其妙的对不齐。
    """
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment or ""))
    return " ".join(text.replace("\xa0", " ").split())


def parse_bing_results(page):
    """从 Bing 搜索结果页里抠出 (标题, 摘要) 列表。

    结构（2026-10 实测）：每个结果是一个 <li class="b_algo">，
    标题在它里面的 <h2><a>，摘要在 <p class="b_lineclampN">。
    抓网页天生脆 —— 所以**抠不到就返回空列表**，上层照常继续，
    既不抛异常，也不静默编造结果。
    """
    results = []
    page = page or ""
    # 按"结果块的起点"切段，而不是写死 `<li class="b_algo"` + `(?=</ol>)`：
    # 原来那种抠法要求 class 的写法、属性顺序、后面的 </ol> 全都一字不差，
    # 页面改一个引号就整段失配 —— 而失配的表现是"没搜到"，不是报错，
    # 主人只会以为模型不肯联网。现在只用它当**分隔符**，宽松得多。
    starts = [m.start() for m in re.finditer(
        r"""<li[^>]*class=(?:"[^"]*\bb_algo\b[^"]*"|'[^']*\bb_algo\b[^']*')""",
        page)]
    blocks = [page[start:(starts[i + 1] if i + 1 < len(starts) else len(page))]
              for i, start in enumerate(starts)]
    for block in blocks:
        title = re.search(r"<h2[^>]*>\s*<a[^>]*>(.*?)</a>", block, re.S)
        snippet = (re.search(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
                             block, re.S)
                   or re.search(r"<p[^>]*>(.*?)</p>", block, re.S))
        name = strip_tags(title.group(1) if title else "")
        body = strip_tags(snippet.group(1) if snippet else "")
        if name or body:
            results.append((name, body[:SEARCH_SNIPPET_CHARS]))
    return results


def format_search_digest(results):
    """把搜索结果压成一段能塞进 prompt 的参考材料。"""
    if not results:
        return ""
    lines = [SEARCH_HEADER]
    for i, (title, body) in enumerate(results[:SEARCH_MAX_RESULTS], 1):
        lines.append("%d. %s%s" % (i, title, ("：" + body) if body else ""))
    return "\n".join(lines)


def describe_net_error(err_name, code):
    """把网络错误翻译成她能说出口的一句话。绝不静默失败。"""
    err = err_name or ""
    if code == 401:
        return "门禁不认我这张脸……主人，那个 key 是不是填错了？"
    if code == 403:
        return "钥匙是对的，但这扇门不给我开……主人，是不是没开这个模型的权限？"
    if code in (402, 429):
        return "白饭不够了……额度好像用完啦。"
    if code == 404:
        return "它说没这个接口……base_url 是不是写错了？"
    if code is not None and 500 <= int(code) < 600:
        return "对面服务器打了个哈欠，我先歇会儿。"
    # 注意 Qt 把 setTransferTimeout 触发的超时报成 OperationCanceledError，
    # 名字里根本没有 "timeout" 字样 —— 按字面匹配会漏掉最常见的那一种。
    if ("Timeout" in err or "timeout" in err or "OperationCanceled" in err):
        return "我想太久了……先吃口白饭缓缓。"
    if ("HostNotFound" in err or "ConnectionRefused" in err
            or "UnknownNetwork" in err or "RemoteHostClosed" in err):
        return "网好像断了，我现在是条离线肥鱼。"
    return "刚才卡了一下，主人再说一遍？"
