# -*- coding: utf-8 -*-
"""飞书接入：让鲸鲸挂在飞书机器人上，随时被主人使唤。

和 pet_remote.py 的分工一模一样 —— **本文件只管协议**：连飞书、收事件、
发消息。它不 import 任何 Qt，也不知道桌宠有什么功能。"命令怎么解释、
状态怎么流转"全在 whale_pet.py 那边。唯一的外部依赖是 websockets。

## 为什么走"长连接"

飞书收消息有两条路：

1. **Webhook 回调** —— 飞书往你的公网地址 POST。家里/宿舍没有公网 IP，
   得内网穿透，还得自己处理签名验签。
2. **长连接（WebSocket）** —— 本机**主动**连 open.feishu.cn，飞书顺着这条
   通道把事件推下来。不需要公网 IP、不需要域名、不需要配白名单。

桌宠显然只能用第 2 条。代价：**长连接只支持"企业自建应用"**（应用商店里的
应用不行）—— 所以得先去开放平台建一个自建应用。好消息是注册飞书个人账号
就能建，不需要真的加入某家公司。

## 为什么不用官方 SDK（lark-oapi）

官方有 `lark-oapi`，但**用它是个坑，实测数据**：

    import lark_oapi                     →  56.3 秒，加载 10623 个模块
    绕过 __init__ 只 import ws.client     →   9.5 秒，仍然 9786 个模块

原因在它的 `__init__.py` 里写了 `from .api import *` —— 把 2500+ 个接口的
数据模型全拉进来了（`ws.client` 又会连带 `event.dispatcher_handler`，
后者同样把全部事件的模型拉一遍）。

对一个"开机就要挂在后台"的桌宠来说，为了收消息先卡 10~56 秒不可接受，
打包进 exe 还要多几十 MB。而真正需要的只有**一个 WebSocket + 一个极小的
帧结构**，所以这里自己实现。

**协议是怎么确认的（不是猜的）**：逐行读了官方 SDK 的
`lark_oapi/ws/client.py` / `const.py` / `enum.py` / `pb/pbbp2_pb2.py`，
拿到下面这些地面真值；并用它生成的 protobuf 类当"标准答案"反向校验了
本文件的帧编解码（见 `_test_feishu.py` 的第 8 节）。字段号如下：

    Frame:  1 SeqID(u64) 2 LogID(u64) 3 service(i32) 4 method(i32)
            5 headers(repeated Header) 6 payload_encoding(str)
            7 payload_type(str) 8 payload(bytes) 9 LogIDNew(str)
    Header: 1 key(str) 2 value(str)
    method: 0=CONTROL 1=DATA；header["type"]: ping/pong/event/card

## 三个必须守住的硬约束

* **事件要在 3 秒内回**，超时飞书会重推。所以收到消息只做"解包 + 去重 +
  交给上层"，绝不在回调里干活（真派活可能要跑好几分钟）。
* **重推是常态**，必须按 message_id 去重，否则一句"帮我跑测试"会派两次活。
* **长连接是集群推送、不广播**：同一应用起多个客户端，事件只会随机落到
  其中一个。所以桌宠只该起**一条**连接。
"""

import importlib.util
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

FEISHU_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"

# 长连接的"取连接地址"接口（和官方 SDK 的 GEN_ENDPOINT_URI 一致）。
# 拿它当"凭据对不对"的预检：这一步过了，说明 app_id/secret 是好的。
GEN_ENDPOINT_URI = "/callback/ws/endpoint"

# 取 tenant_access_token 的接口。**只用来"问为什么"**：长连接接口对凭证错误
# 只回一个空 msg 的数字码，而这个接口会明说 "app secret invalid"。
TOKEN_ENDPOINT_URI = "/open-apis/auth/v3/tenant_access_token/internal"

# 一条飞书文本消息的舒适长度。飞书本身能塞 150KB，但屏幕上超过这个数
# 就变成一坨了，拆开发更像"人说话"。
CHUNK_LIMIT = 1400

# 去重表最多记这么多条 message_id（再多就是很久以前的消息了）。
SEEN_MAX = 400

# 帧里的 method（对应官方 enum.FrameType）
FRAME_CONTROL = 0
FRAME_DATA = 1

# 帧头的 key（官方 const.py 里那几个 HEADER_*）
H_TYPE = "type"
H_MESSAGE_ID = "message_id"
H_TRACE_ID = "trace_id"
H_SUM = "sum"
H_SEQ = "seq"
H_BIZ_RT = "biz_rt"

# 帧头的 value（官方 enum.MessageType）
MSG_EVENT = "event"
MSG_PING = "ping"
MSG_PONG = "pong"

# 断线重连的默认值；建连后服务端会用 ClientConfig 覆盖它们。
DEFAULT_PING_INTERVAL = 120
DEFAULT_RECONNECT_INTERVAL = 120
DEFAULT_RECONNECT_NONCE = 30

# ---- 媒体（2026-10-11：手机拍照发过来 / 电脑截图发过去）----
# 存盘目录名 + 临时目录名。放在**配置目录**下（和日志/记忆同一个地方），
# 这样整个程序只有一处"数据在哪儿"的口径，备份/搬家不会漏。
MEDIA_DIRNAME = "feishu_media"
MEDIA_TMP_DIRNAME = "_tmp"

# 发给飞书的图片上限。按开放平台文档："图片大小不能超过 10M"。
# **本地先截住**而不是等接口回错：10MB 的东西传一遍要几十秒，失败还费流量。
IMAGE_MAX_BYTES = 10 * 1024 * 1024

# Content-Type → 扩展名。**只认这些**，认不出来就按 .png 存（图片消息几乎都是它）。
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp", "image/x-ms-bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tif",
    "image/heic": ".heic", "image/heif": ".heic",
}

# 扩展名 → 上传时要写的 Content-Type。**这张表必须和上面那张分开写**：
# 反过来查（遍历 _MIME_EXT 找 values()==ext）看起来省事，实则有两个坑 ——
# ① 一个扩展名对应多个键（.jpg 有 image/jpeg 和 image/jpg），结果取决于
#    字典顺序，哪天加了键就可能悄悄从 jpeg 变成 pjpeg；
# ② 一旦过滤条件不合适（比如排掉带 "x-" 的键），整个 .bmp 会因为
#    image/x-ms-bmp 被连带排掉，全落到兜底的 image/png 上。
# 拆成两张表之后，`to_mime(ext)` 就是一次确定的查找。
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heic",
}
DEFAULT_IMAGE_EXT = ".png"
DEFAULT_IMAGE_MIME = "image/png"


# --------------------------------------------------------------------------
# 一、protobuf 手写编解码
#
# 只为上面那个 Frame/Header 服务 —— 没必要为一个固定 schema 拖进整个
# protobuf 运行时（而且官方 SDK 还把 protobuf 自己 vendor 了一份）。
# proto3 的规矩：值等于默认值（0/空）就不写进字节流；接收方读不到就是默认值，
# 语义完全一样 —— 所以编码时遇到 0 和空串直接跳过。
# --------------------------------------------------------------------------
def _varint_bytes(n):
    n = int(n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf, i):
    """读一个 varint，返回 (值, 下一个位置)。"""
    shift = 0
    val = 0
    n = len(buf)
    while True:
        if i >= n:
            raise ValueError("varint 被截断")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 超过 10 字节")


def _put_num(buf, field, val):
    """写一个 varint 字段（wire type 0）。"""
    buf.extend(_varint_bytes(field << 3))
    buf.extend(_varint_bytes(int(val)))


def _put_blob(buf, field, data):
    """写一个 length-delimited 字段（wire type 2）。

    **tag 必须 |2** —— 这个 2 就是 wire type，漏了它接收方会按 varint 去读，
    报一句莫名其妙的 "Unexpected end-group tag"（第一版就是这么错的，
    靠官方 protobuf 对拍才照出来）。
    """
    if data:
        buf.extend(_varint_bytes((field << 3) | 2))
        buf.extend(_varint_bytes(len(data)))
        buf.extend(data)


def _put_str(buf, field, text):
    if text:
        _put_blob(buf, field, str(text).encode("utf-8"))


def encode_frame(*, seq=0, log_id=0, service=0, method=0, headers=(),
                 payload_encoding="", payload=b""):
    """编一个 Frame。headers 是 [(key, value)]。

    **1~4 号字段无条件写，哪怕值是 0。** pbbp2 的 schema 是 proto2，这四个是
    `required`（拿官方生成类读出来的）—— 省掉它们，严格的解析器会直接判定
    "消息缺必需字段"。官方 SDK 的 ping 帧也是老老实实写 `08 00 10 00`。

    5~8 号是 optional：空就跳过，语义一样还省字节。
    """
    out = bytearray()
    _put_num(out, 1, seq)
    _put_num(out, 2, log_id)
    _put_num(out, 3, service)
    _put_num(out, 4, method)
    for key, value in headers:
        sub = bytearray()
        _put_str(sub, 1, key)
        _put_str(sub, 2, value)
        _put_blob(out, 5, bytes(sub))
    _put_str(out, 6, payload_encoding)
    _put_blob(out, 8, payload or b"")
    return bytes(out)


def _decode_header(buf):
    key, value = "", ""
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, i = _read_varint(buf, i)
            data = buf[i:i + ln]
            i += ln
            if field == 1:
                key = data.decode("utf-8", "replace")
            elif field == 2:
                value = data.decode("utf-8", "replace")
        elif wire == 0:
            _, i = _read_varint(buf, i)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError("Header 里未知的 wire type %d" % wire)
    return key, value


def decode_frame(data):
    """解一个 Frame，返回一个纯 dict。

    认出别的字段就直接跳过 —— 飞书以后往协议里加东西也炸不了。
    """
    out = {"seq": 0, "log_id": 0, "service": 0, "method": 0, "headers": [],
           "payload_encoding": "", "payload": b""}
    i = 0
    n = len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, i = _read_varint(data, i)
            if field in (1, 2, 3, 4):
                out["seq" if field == 1 else "log_id" if field == 2
                    else "service" if field == 3 else "method"] = val
        elif wire == 2:
            ln, i = _read_varint(data, i)
            if i + ln > n:
                raise ValueError("length-delimited 字段越界")
            chunk = data[i:i + ln]
            i += ln
            if field == 5:
                out["headers"].append(_decode_header(chunk))
            elif field == 6:
                out["payload_encoding"] = chunk.decode("utf-8", "replace")
            elif field == 8:
                out["payload"] = chunk
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError("Frame 里未知的 wire type %d" % wire)
    return out


def header_of(frame, key, default=""):
    for k, v in frame.get("headers") or ():
        if k == key:
            return v
    return default


# --------------------------------------------------------------------------
# 二、依赖探测
# --------------------------------------------------------------------------
def available():
    """飞书功能能不能用。只探"包装没装"，不 import（import 留到工作线程）。"""
    try:
        return importlib.util.find_spec("websockets") is not None
    except Exception:
        return False


def missing_reason():
    if available():
        return ""
    return "缺依赖：websockets"


# --------------------------------------------------------------------------
# 三、纯函数区：不碰网络、不碰线程，可以直接单测
# --------------------------------------------------------------------------
def media_root(config_file=None, tmp=False):
    """媒体根目录：<配置目录>/feishu_media[/_tmp]。

    配置目录用 pet_store.data_dir 的**同一套口径**（= 配置文件所在目录）——
    这里刻意不自己拼路径，否则"数据放哪"就会有两份定义，迟早分叉
    （打包成 exe 后配置在 exe 旁边，自己拼很容易指到别处去）。

    函数内 import pet_store：本文件在 pet_constants 里是被**先 import** 的那个，
    顶层 import pet_store 会转回来撞上半初始化的模块（拆分时踩过这种环）。
    """
    from pet_store import data_dir
    root = data_dir(config_file) / MEDIA_DIRNAME
    return (root / MEDIA_TMP_DIRNAME) if tmp else root


def ext_for_mime(content_type):
    """Content-Type → 扩展名。认不出来（飞书回了怪串 / 没有这个头）就 .png。

    为什么要它：接口回的是**二进制**，文件名只能自己起；不给对的扩展名，
    主人双击打不开（Windows 是认扩展名而不是认文件头的）。
    """
    raw = str(content_type or "").split(";")[0].strip().lower()
    return _MIME_EXT.get(raw, DEFAULT_IMAGE_EXT)


def media_day(now=None):
    """存盘用的日期目录名 YYYYMMDD。独立成一个函数是为了自检能喂死时间。"""
    return (now or datetime.now()).strftime("%Y%m%d")


def media_filename(image_key, content_type="", now=None):
    """图片存盘的文件名：<时间戳>_<image_key 前 12 位><按 Content-Type 猜的扩展名>。

    ⚠️ image_key 要**先洗净再用**：它是**外面**（飞书消息内容）传进来的字符串，
    直接拼进路径理论上能塞 "../" —— 本机程序谈不上被攻击，但这个函数会被自检
    和将来的别的调用点复用，留一个"不信任外部串"的习惯比省那几个字符划算。
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(image_key or ""))[:12] or "img"
    return "%s_%s%s" % (stamp, safe, ext_for_mime(content_type))


def save_media_bytes(data, image_key, content_type, root, now=None):
    """把下载到的二进制落到 <root>/<YYYYMMDD>/<文件名>，返回落盘路径。

    **先建目录再写**：feishu_media/ 和日期子目录第一次跑都不存在。
    返回的是最终路径（不是临时名）—— 理由见下面注释。
    """
    day_dir = Path(root) / media_day(now)
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / media_filename(image_key, content_type, now)
    # 直接写到位，不走"临时文件 + os.replace"：这里的文件是**只增不改**的
    # 存档，写到一半崩掉最坏是留一个残缺图片；而临时名换名反而会让主人
    # 在目录里看到一堆中间态。写失败（盘满/只读）由调用方 catch OSError
    # 翻译成"写盘失败"，不会静默。
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _human_size(nbytes):
    """给人看的文件大小。1 位小数就够 —— 精确到 KB 的"123 KB"是主人要的颗粒度。"""
    try:
        n = float(nbytes)
    except (TypeError, ValueError):
        return "?"
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    return "%.0f KB" % (n / 1024.0)


# --------------------------------------------------------------------------
# 飞书事件日志（落盘）
#
# 为什么要有它：飞书那边的进展原来只进内存里那个 40 行的环（`_feishu_log`），
# **只有打开设置窗口才看得见**；而 /shot 传不出去、发消息失败这类事正好都发生
# 在主人不看窗口的时候。2026-10-11 查"图传不到手机"时，磁盘上一点线索都没有
# —— 只能靠猜。所以凡是"话说出去/图发出去"失败，都往这个文件里写一行。
# --------------------------------------------------------------------------
FEISHU_LOG_NAME = "feishu_log.txt"
FEISHU_LOG_MAX_BYTES = 256 * 1024     # 超了就砍掉前半，只留最近的
_feishu_log_lock = threading.Lock()


def feishu_log_path(config_file=None):
    """日志文件：<配置目录>/feishu_log.txt（和数据目录同一套口径）。"""
    from pet_store import data_dir
    return Path(data_dir(config_file)) / FEISHU_LOG_NAME


def append_feishu_log(config_file, line, now=None):
    """往日志文件追加一行（带时间戳）。**任何失败都吞掉、绝不上抛**。

    为什么吞：它只是"留个线索"，写不进去（目录只读、盘满）也不该反过来把
    回复/发图这条路带崩 —— 日志是给排查用的，不是关键路径。
    多线程会同时写（长连接线程、发送线程、主线程），所以有一把模块级锁。
    """
    text = str(line or "").strip()
    if not text:
        return
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = feishu_log_path(config_file)
        with _feishu_log_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.stat().st_size > FEISHU_LOG_MAX_BYTES:
                    old = path.read_text(encoding="utf-8", errors="replace")
                    path.write_text(old[len(old) // 2:], encoding="utf-8")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n" % (stamp, text))
    except Exception:
        pass


def image_failure_reason(err, path="", size=0):
    """把"发图失败"的异常翻成**主人能照做**的一句话（飞书里就回这句）。

    为什么要单独一个函数：真正失败的是发送线程，它手里只有异常对象；
    而主人要的不是异常名，是"下一步干什么"。所以按原因分流，并且**把飞书
    返回体原文带上**（`_http_multipart` 已经把原文塞进异常了）。
    """
    text = str(err or "")
    name = Path(path).name if path else "(截图)"
    where = "（%s，%s）" % (name, _human_size(size)) if size else "（%s）" % name
    low = text.lower()
    if "10mb" in low or "超过 10mb" in text:
        return ("图太大%s：飞书只收 10MB 以内的图片，这张没收。\n"
                "（本机就把上传拦下来了，没白传一遍）" % where)
    if "找不到文件" in text:
        return "截图文件找不到了%s（可能被清理或杀毒软件动了）。再发一次 /shot。" % where
    if "图片是空的" in text or "0 字节" in text:
        return "截图是空文件%s，没东西可发。再发一次 /shot。" % where
    if "tenant_access_token" in text:
        return ("取 access token 失败（凭证过期或被改过）%s：%s\n"
                "去「设置 → 飞书接入」点一下「测试凭据」。" % (where, text[:200]))
    if "连不上飞书" in text or "timed out" in low or "timeout" in low:
        return ("连飞书超时/没网%s：%s\n"
                "等网络顺了再发一次 /shot。" % (where, text[:300]))
    if "HTTP" in text or "上传被拒" in text or "code=" in text:
        return ("飞书那边拒了%s：%s\n"
                "把上面这句原话留着 —— 这是飞书给的原因。" % (where, text[:400]))
    return "发图失败%s：%s: %s" % (where, type(err).__name__, text[:300])


# 非文字消息的"种类" → content JSON 里的取值字段。
# 为什么单独列一张表：`normalize_event` 里那句 `content = json.loads(raw)`
# 把原始 JSON **丢掉了**，只留了 text —— 于是图片消息的 image_key 根本没被读出来，
# 表现就是"发照片过去她一声不吭"。现在按类型去 content 里取真正有用的那个 key。
_MEDIA_KEYS = {
    "image": "image_key",
    "file": "file_key",
    "audio": "file_key",
    "media": "file_key",       # 视频：content 里是 file_key + image_key 两个
    "sticker": "file_key",
    "post": None,              # 富文本：没有单个"文件"字段，按不支持处理
}


def _read_media_spec(ev):
    """从归一化后的事件里读出 (消息种类, 关键值)。文字消息返回 ("text", "")。

    返回值 `spec` 的含义随种类变：图片是 image_key、文件/语音/视频是 file_key、
    post（富文本）和认不出的类型是空串。

    **为什么要单独一个函数、而不是塞进 normalize_event**：
    normalize_event 的输出会被上层到处用（会话判断、去重、路由），往里加一个
    "媒体字段"会让那个字典越来越肥；而这条信息只在"收到非文字消息"那一条路上
    有用。放成一个纯函数还能被自检直接喂样例。
    """
    if ev is None:
        return "unknown", ""
    mtype = str(ev.get("message_type") or "").strip().lower()
    if not mtype or mtype == "text":
        # 老事件/缺字段的按文字处理：这时 text 有值就有话，没值上层会直接返回
        return "text", ""
    content = ev.get("content")
    if not isinstance(content, dict):
        # normalize_event 已经帮我们 json.loads 过了；这里再兜一层是为了
        # 让这个函数单独喂 {"message_type": "image"} 时不炸
        try:
            content = json.loads(ev.get("content") or "{}")
        except Exception:
            content = {}
    if not isinstance(content, dict):
        content = {}
    field = _MEDIA_KEYS.get(mtype)
    if field is None:
        # 认识这个类型、但 content 里没有我们需要的字段（post 就是这种）
        return mtype, ""
    value = content.get(field)
    return mtype, (str(value).strip() if value else "")


_CMD_ALIASES = (
    ("help", ("/help", "/?", "?", "help", "帮助", "菜单")),
    ("status", ("/status", "/状态", "状态")),
    ("harness", ("/harness", "/h", "harness", "/工作", "工作模式", "派活")),
    ("chat", ("/chat", "/闲聊", "闲聊", "/exit", "退出")),
    ("list", ("/list", "/列表", "列表", "看对话")),
    ("new", ("/new", "/新对话", "新对话")),
    # 截图（主人 2026-10-11 要的"手机看电脑画面"）。别名给的是**一眼能记住**的
    # 那几个：/shot 是主名，/screen 和 /截图 是顺手能想到的写法。
    # 加在这里就够了 —— parse_command 是"整句全等"匹配，不会把
    # "/screenshot 帮我看看"这种带参数的话误吃进来（那就当普通文本）。
    ("shot", ("/shot", "/screen", "/screenshot", "/截图", "截图")),
)


def parse_command(text):
    """把一句话拆成 (kind, name, arg)。**永远返回三个元素。**

    * ("cmd", "help"|"harness"|"chat"|"list"|"status"|"new", None)
    * ("cmd", "pick", 序号)   —— 纯数字（可以带 / 前缀）
    * ("text", 原文, None)    —— 其余都当自然语言

    固定三元组是有意的：早先"普通文本返回两元组、命令返回三元组"，
    调用方一解包就炸（`not enough values to unpack`）—— 变长返回是坑。

    纯函数，不懂状态。**"数字只在挑会话时才算挑"这层判断放在桌宠那边** ——
    那需要知道当前是不是在选，放这儿就和状态耦合了，也没法单测。
    """
    s = (text or "").strip()
    if not s:
        return ("text", "", None)
    low = s.lower()
    for name, aliases in _CMD_ALIASES:
        if low in aliases:
            return ("cmd", name, None)
    m = re.fullmatch(r"/?(\d{1,2})", s)
    if m:
        return ("cmd", "pick", int(m.group(1)))
    return ("text", s, None)


def split_text(text, limit=CHUNK_LIMIT):
    """把长回复切成几段，尽量在换行处下刀（别把一句话劈两半）。
    空字符串返回 []，调用方据此判断"这条不用发"。"""
    s = (text or "").strip()
    if not s:
        return []
    if len(s) <= limit:
        return [s]
    out, buf = [], ""
    for line in s.splitlines():
        # 单行本身就超长（比如粘了一整段 JSON）：硬切
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if not buf:
            buf = line
        elif len(buf) + 1 + len(line) <= limit:
            buf += "\n" + line
        else:
            out.append(buf)
            buf = line
    if buf:
        out.append(buf)
    return out


def normalize_event(event):
    """把 v2 信封里的 `event` 部分拍成一个纯 dict。

    长连接推上来的是 `{"schema":"2.0","header":{...},"event":{...}}`，
    所以这里收的已经是 `event` 那一层（`normalize_payload` 负责剥壳）。

    **拍平放在协议层**，好处是桌宠只认一种形状：哪天飞书改了字段名，
    只动这一个函数，桌宠那边一行都不用改。
    """
    if not isinstance(event, dict):
        return None
    msg = event.get("message") or {}
    if not isinstance(msg, dict):
        return None
    raw = msg.get("content") or ""
    try:
        content = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        content = {}
    if not isinstance(content, dict):
        content = {}
    text = str(content.get("text") or "")
    mentions = msg.get("mentions") or []
    # 群里 @机器人 时正文里会留一个 @_user_1 之类的占位符，得抹掉，
    # 不然"@_user_1 帮我看看"会把占位符一起喂给模型。
    for m in mentions:
        key = str((m or {}).get("key") or "")
        if key:
            text = text.replace(key, " ")
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    return {
        "chat_id": str(msg.get("chat_id") or ""),
        "chat_type": str(msg.get("chat_type") or ""),      # p2p / group
        "message_id": str(msg.get("message_id") or ""),
        "message_type": str(msg.get("message_type") or ""),
        "sender_open_id": str(sender_id.get("open_id") or ""),
        "sender_type": str(sender.get("sender_type") or ""),
        "text": text.strip(),
        # content 那份 JSON **原样留着**（2026-10-11 加图片消息时补）：
        # 图片消息的正文是空串，真正有用的东西（image_key）只在 content 里；
        # 以前这里只留 text，等于把"照片"这件事整个丢了 —— 上层收不到
        # 任何信号，只能静默忽略。留着原 dict 让 _read_media_spec 去取，
        # 上层不用再解析一遍字符串。
        "content": content,
        "mention_names": [str((m or {}).get("name") or "") for m in mentions],
    }


def normalize_payload(raw_payload):
    """剥掉 v2 信封，返回 (event_type, 归一化后的事件 dict)。

    飞书长连接推的是明文 JSON，**不用解密、不用验签**（建连时已经鉴过权）——
    这和 Webhook 模式很不一样，是长连接白送的好处。
    """
    if isinstance(raw_payload, (bytes, bytearray)):
        raw_payload = raw_payload.decode("utf-8", "replace")
    try:
        payload = json.loads(raw_payload)
    except Exception:
        return "", None
    if not isinstance(payload, dict):
        return "", None
    header = payload.get("header") or {}
    etype = str(header.get("event_type") or "")
    if not etype and payload.get("uuid"):
        # v1 信封：{"uuid":..., "event":{"type":...}}
        ev = payload.get("event") or {}
        etype = str(ev.get("type") or "")
        return etype, normalize_event(ev)
    return etype, normalize_event(payload.get("event") or {})


# --------------------------------------------------------------------------
# 四、HTTP（只用了标准库 urllib —— 少一个依赖少一个打包麻烦）
# --------------------------------------------------------------------------
def _request(url, data=None, headers=None, timeout=15, method="POST", blob=False):
    """一个薄薄的 urllib 包装，**所有** HTTP 都从这儿走。

    为什么要把原来写死在 `_http_json` 里的那几句抽出来：上传图片走的是
    **multipart/form-data + 二进制响应**，不能复用 JSON 那套（body 不是 JSON、
    返回也不是 JSON），但"超时怎么算、4xx 时也要把 body 读出来"这两条坑
    是一模一样的 —— 各写一遍必然有一处忘掉。

    blob=False 返回解析好的 JSON（飞书报错也带 JSON body，所以 4xx/5xx 也读）；
    blob=True 原样返回 (bytes, Content-Type)，给图片这种二进制接口用。
    """
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "") if blob else ""
    except urllib.error.HTTPError as err:
        # 飞书连错误也是 JSON，把 body 带出去 —— 只给"HTTP 400"等于没说。
        # 留 400 字：飞书的错误体里除了 code/msg 还有 error.log_id 和
        # troubleshooter（排查建议），截太短就刚好把那半截砍掉了。
        body = err.read()
        raise RuntimeError("HTTP %s：%s" % (
            err.code, body.decode("utf-8", "replace")[:400]))
    except urllib.error.URLError as err:
        # 连不上 / 读超时：说清"等了多少秒"，不然主人只看到一句 WinError。
        # 所有调用点都显式传了 timeout（本文件里没有"不设超时"的 urlopen），
        # 所以这里几乎总是"socket 超时"或"没网/DNS 挂了"。
        raise RuntimeError("连不上飞书（最多等 %s 秒）：%s" % (timeout, err))
    if blob:
        return body, ctype
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise RuntimeError("返回的不是 JSON：%s" % body[:200])


def _http_json(url, payload, timeout=15, bearer="", method="POST"):
    """POST/GET 一个 JSON 接口。飞书报错时也会带 JSON body，所以 4xx/5xx
    也把 body 读出来 —— 不然主人只会看到一句"HTTP 400"，不知道错在哪。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _request(url, data=data, headers=headers, timeout=timeout,
                    method=method)


def _multipart(fields, files):
    """手搓一个 multipart/form-data 请求体，返回 (body, content_type)。

    **不能用 urllib 的 encode_multipart**：标准库里根本没有这个函数
    （那是 requests/urllib3 的），而本项目的硬规矩是"不新增第三方依赖"，
    所以这里按 RFC 7578 自己拼：

        --<边界>\\r\\n
        Content-Disposition: form-data; name="image_type"\\r\\n\\r\\n
        message\\r\\n
        --<边界>\\r\\n
        Content-Disposition: form-data; name="image"; filename="x.png"\\r\\n
        Content-Type: image/png\\r\\n\\r\\n
        <原始字节>\\r\\n
        --<边界>--\\r\\n

    两个容易踩的点：
    1. **边界串不能出现在内容里**，出现的话接收方会在那儿提前截断。
       所以选了 uuid4 的十六进制（32 个十六进制字符），并再 check 一遍；
       撞上了就换一个（循环有限次，纯防御，实际几乎不可能触发）。
    2. 结尾那个 `--<边界>--` 后面**必须跟 CRLF**，只写 `--` 有些实现会
       判定"body 不完整"（飞书这边实测要带）。
    """
    for _ in range(8):
        boundary = "----WhalePetFormBoundary" + uuid.uuid4().hex
        marker = boundary.encode("ascii")
        # 只拿"待发送的原始字节"去撞，别每圈 join 一次整个 body（那是白花钱）
        hits = any(marker in bytes(v) for _n, _fn, _ct, v in (files or ()))
        if not hits:
            break
    else:
        # 极端兜底：换成时间戳 + 随机数，仍然撞上就只能认了（不会是正常图片内容）
        boundary = "----WhalePetFormBoundary%d%s" % (int(time.time()),
                                                    random.random())
    lines = []
    for name, value in (fields or ()):
        lines.append(b"--" + boundary.encode("ascii"))
        lines.append(('Content-Disposition: form-data; name="%s"' % name)
                     .encode("utf-8"))
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))
    for name, filename, ctype, data in (files or ()):
        lines.append(b"--" + boundary.encode("ascii"))
        lines.append(('Content-Disposition: form-data; name="%s"; filename="%s"'
                      % (name, filename)).encode("utf-8"))
        lines.append(("Content-Type: %s" % ctype).encode("utf-8"))
        lines.append(b"")
        lines.append(bytes(data))
    lines.append(b"--" + boundary.encode("ascii") + b"--")
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, "multipart/form-data; boundary=%s" % boundary


def _http_multipart(url, fields, files, timeout=60, bearer=""):
    """上传 multipart 表单，**返回 (解析后的 JSON, 响应原文前 400 字)**。

    响应原文**两种情况都带出来**（不只是"回的不是 JSON"时）：上传被拒时飞书
    回的 JSON 里除了 code/msg 还可能有更细的原因，而失败提示要能把它原样摆给
    主人看 —— 2026-10-11 的 /shot"图传不到手机"就是死在"错误只留在内存里、
    没人看得见"，事后完全查不出。
    """
    body, ctype = _multipart(fields, files)
    headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    raw, _ = _request(url, data=body, headers=headers, timeout=timeout,
                      method="POST", blob=True)
    text = raw.decode("utf-8", "replace")[:400]
    try:
        return json.loads(text), text
    except ValueError:
        return None, text


def _credential_reason(domain, app_id, app_secret, timeout=12):
    """飞书拒了长连接时，再去 token 接口问一句"为什么"。

    为什么要多问一次：长连接接口（/callback/ws/endpoint）对凭证错误只回一个
    **空 msg** 的数字码（实测 code=1000040345、msg 为空），而
    /auth/v3/tenant_access_token/internal 会明说 `app secret invalid`（code 10014）。
    诊断信息很值钱 —— 主人看到的应该是后者。
    """
    try:
        d = _http_json(domain + TOKEN_ENDPOINT_URI,
                       {"app_id": app_id, "app_secret": app_secret},
                       timeout=timeout)
    except Exception as err:
        return "（再问 token 接口也失败：%s）" % err
    if d.get("code") == 0:
        return "（token 接口说凭据有效，那就不是凭证问题）"
    return "（token 接口原话：code=%s %s）" % (d.get("code"),
                                              d.get("msg") or "无 msg")


def check_credentials(app_id, app_secret, domain=FEISHU_DOMAIN, timeout=12):
    """预检 app_id / app_secret 对不对。

    走的就是建连的第一步（POST /callback/ws/endpoint）—— 所以它说行就真行，
    比"能不能取到 tenant_access_token"更贴近长连接的实际要求。
    返回 (ok, 一句人话)。
    """
    if not app_id or not app_secret:
        return False, "App ID / App Secret 还没填"
    try:
        d = _http_json(domain + GEN_ENDPOINT_URI,
                       {"AppID": app_id, "AppSecret": app_secret},
                       timeout=timeout)
    except Exception as err:
        return False, "连不上飞书：%s" % err
    if d.get("code") == 0:
        url = ((d.get("data") or {}).get("URL") or "")
        return True, ("凭据有效，长连接地址已拿到" if url else "凭据有效")
    why = _credential_reason(domain, app_id, app_secret, timeout=timeout)
    return False, "飞书拒绝了：code=%s %s %s" % (d.get("code"),
                                                d.get("msg") or "(空)", why)


# --------------------------------------------------------------------------
# 五、长连接客户端
# --------------------------------------------------------------------------
_STATE_LABELS = {
    "idle": "未启动",
    "connecting": "连接中…",
    "online": "在线",
    "offline": "断线，正在重连…",
    "error": "出错了",
    "stopped": "已停止",
}


class FeishuClient(object):
    """一条飞书长连接 + 一套发消息的 REST 调用。

    生命周期：`start()` 起一个 daemon 线程（立刻返回），`stop()` 拆掉。
    线程里是一个"连上 → 收帧 → 断了就重连"的循环。
    """

    def __init__(self, app_id, app_secret, on_event,
                 on_log=None, domain="feishu"):
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.on_event = on_event or (lambda d: None)
        self.on_log = on_log or (lambda s: None)
        self.domain = LARK_DOMAIN if str(domain).lower() == "lark" else FEISHU_DOMAIN

        self._conn_url = ""
        self._service_id = 0
        self._ping_interval = DEFAULT_PING_INTERVAL
        self._reconnect_interval = DEFAULT_RECONNECT_INTERVAL
        self._reconnect_nonce = DEFAULT_RECONNECT_NONCE

        self._thread = None
        self._stop = threading.Event()
        self._state = "idle"
        self._last_error = ""
        self._lock = threading.Lock()
        self._seen = {}                       # message_id -> 时间
        self._parts = {}                      # 分片合包：msg_id -> [buf, 到期时间]
        self._token = ""
        self._token_exp = 0.0
        # 发消息现在跑在桌宠的工作线程里（回复不再卡住界面），而长连接线程
        # 也可能同时发东西 —— token 缓存是两边共用的，得有一把锁。
        self._token_lock = threading.Lock()
        self._online = False
        self._started_at = 0.0
        self._last_event_at = 0.0

    # ---------------------------------------------------------------- 状态
    def status(self):
        """给界面看的一眼状态。只读，可以随便调。"""
        return {
            "state": self._state,
            "label": _STATE_LABELS.get(self._state, self._state),
            "online": self._online,
            "error": self._last_error,
            "uptime": (time.time() - self._started_at) if self._started_at else 0.0,
            "last_event_ago": (time.time() - self._last_event_at)
                              if self._last_event_at else -1.0,
        }

    def is_online(self):
        return bool(self._online)

    # ------------------------------------------------------------ 生命周期
    def start(self):
        """起长连接。**不阻塞**：立刻返回，真实连接在后台线程里做。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not available():
                self._state = "error"
                self._last_error = missing_reason()
                return False
            if not self.app_id or not self.app_secret:
                self._state = "error"
                self._last_error = "App ID / App Secret 还没填"
                return False
            self._stop.clear()
            self._state = "connecting"
            self._last_error = ""
            self._online = False
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="feishu-ws")
            self._thread.start()
        return True

    def stop(self, timeout=8.0):
        """拆连接。可以反复调（没起着就是空操作）。

        `self._stop` 是个 Event 而不是 bool —— 重连的等待用的是
        `Event.wait(interval)`，所以一点"停"就立刻醒，不用等满 120 秒。
        """
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        # 把正在 recv 的连接踢一下：recv 的超时是 1 秒，所以最多等 1 秒
        # 循环就会看到 stop 标志退出。
        if thread is not None:
            thread.join(timeout=timeout)
        self._online = False
        self._state = "stopped"
        return True

    # ------------------------------------------------------------ 主循环
    def _run(self):
        import websockets.sync.client as wsc
        self._started_at = time.time()
        attempt = 0
        while not self._stop.is_set():
            try:
                self._discover()
                self._state = "connecting"
                # proxy=None 是**必须显式写**的：websockets 15 的默认值是
                # True = 自动读 HTTP_PROXY/HTTPS_PROXY 环境变量。机器上一旦
                # 有这些变量，连接就会绕去代理然后失败。官方 SDK 也把它
                # 钉成 None（见其 _ws_connect_kwargs）。
                with wsc.connect(self._conn_url, open_timeout=20,
                                 ping_interval=20, ping_timeout=20,
                                 proxy=None,
                                 ssl=self._ssl_context()) as ws:
                    self._online = True
                    self._state = "online"
                    self._last_error = ""
                    attempt = 0                  # 连上过就把退避重置
                    self.on_log("飞书长连接已建立")
                    self._loop(ws)
            except Exception as err:
                self._online = False
                self._last_error = "%s: %s" % (type(err).__name__, err)
                self._state = "offline"
                self.on_log("飞书长连接中断：%s" % self._last_error)
            if self._stop.is_set():
                break
            attempt += 1
            # 第一次重连带一点随机抖动（照官方 SDK 的做法，避免同一应用下
            # 所有客户端同时回来把服务端顶住）；之后按服务端给的间隔。
            delay = (random.random() * self._reconnect_nonce if attempt == 1
                     else self._reconnect_interval)
            self._stop.wait(max(1.0, delay))
        self._online = False
        if self._state != "stopped":
            self._state = "idle"

    def _ssl_context(self):
        """建 HTTPS 的校验上下文（只给 wss:// 用）。

        显式建一个而不是走默认，是为了**失败时能给出人话** ——
        证书问题在打包后的 exe 里会表现成一句莫名其妙的 SSL 报错，
        留着这个口子将来好排查。ws:// 用不上（测试里拿本地假飞书跑），
        硬塞进去反而会让连接建不起来。
        """
        if not self._conn_url.startswith("wss://"):
            return None
        return ssl.create_default_context()

    def _discover(self):
        """拿长连接地址 + 服务端给的参数（心跳间隔、重连策略）。"""
        d = _http_json(self.domain + GEN_ENDPOINT_URI,
                       {"AppID": self.app_id, "AppSecret": self.app_secret},
                       timeout=20)
        if d.get("code") != 0:
            raise RuntimeError("取连接地址失败：code=%s %s"
                               % (d.get("code"), d.get("msg") or ""))
        data = d.get("data") or {}
        url = str(data.get("URL") or "")
        if not url:
            raise RuntimeError("飞书没给长连接地址")
        self._conn_url = url
        # service_id 在查询串里，心跳帧要把它填回去
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        try:
            self._service_id = int((q.get("service_id") or ["0"])[0])
        except Exception:
            self._service_id = 0
        conf = data.get("ClientConfig") or {}
        try:
            if conf.get("PingInterval"):
                self._ping_interval = int(conf["PingInterval"])
            if conf.get("ReconnectInterval"):
                self._reconnect_interval = int(conf["ReconnectInterval"])
            if conf.get("ReconnectNonce"):
                self._reconnect_nonce = int(conf["ReconnectNonce"])
        except Exception:
            pass

    def _loop(self, ws):
        """连上之后的收帧循环。每 1 秒醒一次，顺便看该不该发心跳。"""
        last_ping = 0.0
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                if time.time() - last_ping >= self._ping_interval:
                    ws.send(encode_frame(service=self._service_id,
                                         method=FRAME_CONTROL,
                                         headers=[(H_TYPE, MSG_PING)]))
                    last_ping = time.time()
                continue
            try:
                self._handle(ws, raw)
            except Exception as err:
                # 单帧处理失败不能把连接带下去 —— 记一笔继续收
                self.on_log("处理帧失败：%s: %s" % (type(err).__name__, err))

    def _handle(self, ws, raw):
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        frame = decode_frame(raw)
        if frame["method"] == FRAME_CONTROL:
            self._handle_control(frame)
            return
        if frame["method"] != FRAME_DATA:
            return
        payload = frame["payload"]
        # 大消息会被拆成多帧，按 message_id 合起来（照官方 SDK 的规矩）
        try:
            total = int(header_of(frame, H_SUM, "1") or 1)
        except Exception:
            total = 1
        if total > 1:
            payload = self._combine(header_of(frame, H_MESSAGE_ID), total,
                                    header_of(frame, H_SEQ), payload)
            if payload is None:
                return
        etype = header_of(frame, H_TYPE)
        if etype == MSG_EVENT:
            self._dispatch(payload)
        # 无论认不认识，都回一个 ack —— 不回的话飞书会当成超时重推
        self._ack(ws, frame)

    def _handle_control(self, frame):
        etype = header_of(frame, H_TYPE)
        if etype == MSG_PONG and frame["payload"]:
            # 服务端可以中途用 pong 携带新的参数，照官方 SDK 那样吃下来
            try:
                conf = json.loads(frame["payload"].decode("utf-8"))
                if conf.get("PingInterval"):
                    self._ping_interval = int(conf["PingInterval"])
                if conf.get("ReconnectInterval"):
                    self._reconnect_interval = int(conf["ReconnectInterval"])
            except Exception:
                pass

    def _combine(self, msg_id, total, seq, chunk):
        """把分片的负载拼起来。还没拼齐就返回 None。"""
        try:
            idx = int(seq)
        except Exception:
            return chunk
        if not (0 <= idx < total):
            return chunk
        now = time.time()
        slot = self._parts.get(msg_id)
        if slot is None or now > slot[1]:
            slot = [[None] * total, now + 10.0]
            self._parts[msg_id] = slot
        slot[0][idx] = chunk
        if any(p is None for p in slot[0]):
            return None
        self._parts.pop(msg_id, None)
        return b"".join(slot[0])

    def _dispatch(self, payload):
        etype, ev = normalize_payload(payload)
        if not etype:
            return
        if etype != "im.message.receive_v1":
            self.on_log("忽略未订阅的事件：%s" % etype)
            return
        if not ev or not ev.get("message_id"):
            return
        if not self._mark_seen(ev["message_id"]):
            self.on_log("忽略重推的消息 %s" % ev["message_id"][-12:])
            return
        self._last_event_at = time.time()
        self.on_event(ev)          # 只做转发：真正的活在桌宠主线程里干

    def _ack(self, ws, frame):
        """把收到的帧原样回一个 ack。

        形状照官方 SDK：保留原来所有字段和帧头，**只换 payload**，
        再补一个 biz_rt（处理耗时，毫秒）。不回 ack 或者回错，飞书都会重推。
        """
        headers = list(frame["headers"]) + [(H_BIZ_RT, "0")]
        body = json.dumps({"code": 200}).encode("utf-8")
        ws.send(encode_frame(seq=frame["seq"], log_id=frame["log_id"],
                             service=frame["service"], method=frame["method"],
                             headers=headers, payload=body))

    def _mark_seen(self, message_id):
        """没见过就记下并返回 True；见过（飞书重推）返回 False。"""
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = time.time()
            if len(self._seen) > SEEN_MAX:
                for k in sorted(self._seen, key=self._seen.get)[:SEEN_MAX // 4]:
                    self._seen.pop(k, None)
            return True

    # -------------------------------------------------------------- 发消息
    def _tenant_token(self):
        """tenant_access_token，带缓存（提前 5 分钟换新）。

        加锁：这个缓存现在会被长连接线程和"发消息工作线程"同时碰。
        锁里再查一次缓存（double-check），避免两边各取一个 token。
        """
        now = time.time()
        if self._token and now < self._token_exp:
            return self._token
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_exp:
                return self._token
            d = _http_json(
                self.domain + "/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": self.app_id, "app_secret": self.app_secret}, timeout=15)
            if d.get("code") != 0:
                raise RuntimeError("取 tenant_access_token 失败：code=%s %s"
                                   % (d.get("code"), d.get("msg")))
            self._token = str(d.get("tenant_access_token") or "")
            expire = int(d.get("expire") or 7200)
            self._token_exp = now + max(60, expire - 300)
            return self._token

    def _post(self, path, body):
        d = _http_json(self.domain + path, body,
                       timeout=20, bearer=self._tenant_token())
        if d.get("code") != 0:
            raise RuntimeError("飞书接口报错：code=%s %s"
                               % (d.get("code"), d.get("msg")))
        return d

    def _send_message(self, chat_id, msg_type, content, receive_id_type="chat_id"):
        """发任意类型的消息（text / image / post …）。

        抽出来的理由：原来"发消息"这件事只有 `send_text` 一条路，里面写死了
        `msg_type="text"` + `content={"text": …}`。发图片时 `content` 要的是
        `{"image_key": …}`，硬塞进 text 那条路发出去是一条**空文本消息**
        （飞书不报错，只是主人什么也看不到）—— 所以这里把
        "msg_type + content 由调用方给"这件事做成一个口子。

        `receive_id_type` 默认 chat_id（正常业务都是"发到某个会话"）；`--doctor`
        的图片自测只拿得到主人的 open_id（配置里的白名单）时就传 "open_id"。
        """
        return self._post(
            "/open-apis/im/v1/messages?receive_id_type=%s" % receive_id_type,
            {"receive_id": chat_id, "msg_type": msg_type,
             "content": content if isinstance(content, str)
             else json.dumps(content, ensure_ascii=False)})

    def _reply_message(self, message_id, msg_type, content):
        """回复某条消息，同样支持任意 msg_type（图片消息要用它）。"""
        return self._post(
            "/open-apis/im/v1/messages/%s/reply" % message_id,
            {"msg_type": msg_type,
             "content": content if isinstance(content, str)
             else json.dumps(content, ensure_ascii=False)})

    def download_resource(self, message_id, file_key, rtype="image", timeout=60):
        """下载一条消息里的资源（图片），返回 (bytes, Content-Type)。

        接口：`GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image`

        ⚠️ **两个 id 千万别搞混**（这是本条最容易错的地方）：
          * 路径里的 `{message_id}` 用**事件的 message_id**（om_xxx）——
            它指的是"哪一条消息"，资源挂在消息上；
          * `{file_key}` 用消息内容 JSON 里的 **image_key**（img_v2_xxx）——
            它才是那份二进制本身。
        第一版我差点把 image_key 当消息 id 传进路径，那样飞书会回
        404/权限错，而且报错完全指不到"你传错 id 了"。

        它的身体是**二进制**（不是 JSON），所以必须走 `_request(blob=True)`：
        拿 `_http_json` 去接，第一步 json 解析就炸，而且炸出来的信息毫无用
        （只会说"返回的不是 JSON"）。
        """
        path = "/open-apis/im/v1/messages/%s/resources/%s" % (
            urllib.parse.quote(str(message_id), safe=""),
            urllib.parse.quote(str(file_key), safe=""))
        url = "%s%s?type=%s" % (self.domain, path,
                                urllib.parse.quote(str(rtype or "image")))
        headers = {"Authorization": "Bearer " + self._tenant_token()}
        return _request(url, data=None, headers=headers, timeout=timeout,
                        method="GET", blob=True)

    def upload_image(self, path, image_type="message", timeout=60):
        """上传一张图，返回 image_key。之后才能用 `send_image` 发出去。

        接口：`POST /open-apis/im/v1/images`
        表单：`image_type=message`（**发给聊天用的图片**；换别的值飞书不认）
              `image=<文件内容>`

        失败原因的颗粒度是刻意做的：
          * 文件不在      → "找不到文件"
          * 读不出来      → 写清 OSError
          * 超 10MB       → 本地就拦住（见 IMAGE_MAX_BYTES 的注释）
          * 回的不是 JSON → 把回的内容前 200 字带出去（网关/反代常见）
          * code != 0     → 飞书原话
        """
        p = Path(path)
        if not p.is_file():
            raise RuntimeError("找不到文件：%s" % p)
        try:
            raw = p.read_bytes()
        except OSError as err:
            raise RuntimeError("读图片失败：%s" % err)
        if not raw:
            raise RuntimeError("图片是空的（0 字节）")
        if len(raw) > IMAGE_MAX_BYTES:
            raise RuntimeError("图片 %s 超过 10MB 上限，飞书不收"
                               % _human_size(len(raw)))
        ctype = _EXT_MIME.get(p.suffix.lower(), DEFAULT_IMAGE_MIME)
        d, raw_text = _http_multipart(
            self.domain + "/open-apis/im/v1/images",
            [("image_type", str(image_type or "message"))],
            [("image", p.name, ctype, raw)],
            timeout=timeout, bearer=self._tenant_token())
        if d is None:
            raise RuntimeError("上传接口没回 JSON：%s" % (raw_text or "(空)"))
        if d.get("code") != 0:
            # **把返回体原文也带上**：飞书除了 msg 还会给 error.log_id /
            # troubleshooter，只报一句"上传被拒"就查不下去了
            raise RuntimeError("上传被拒：code=%s %s（返回体：%s）"
                               % (d.get("code"), d.get("msg") or "(空 msg)",
                                  raw_text or "(空)"))
        key = str((d.get("data") or {}).get("image_key") or "")
        if not key:
            raise RuntimeError("飞书没回 image_key（code=0 但 data 是空的）")
        return key

    def send_image(self, chat_id, path, reply_id="", image_type="message",
                   receive_id_type="chat_id"):
        """把一张本地图片发到会话里（拿不准就回挂在主人那条消息下面）。

        两步是**不可拆**的：先上传换 image_key，再用 image_key 发图片消息。
        （飞书没有"一步传图到会话"的接口。）

        返回发出去用的 image_key；失败原样抛，由调用方翻成人话 ——
        这里不吞异常，因为"上传失败"和"发送失败"要说给主人的话不一样。
        """
        key = self.upload_image(path, image_type=image_type)
        content = {"image_key": key}
        if reply_id:
            # 也走 reply：图片挂在主人那句 "/shot" 下面，手机上一眼看到因果关系
            self._reply_message(reply_id, "image", content)
        else:
            self._send_message(chat_id, "image", content,
                               receive_id_type=receive_id_type)
        return key

    def send_text(self, chat_id, text):
        """往某个会话发消息（不必先收到消息，主动推也用它）。

        长文本自动分片。返回值是"**每一片都发出去了**"才为 True ——
        以前是"有一片成功就算成功"，后面几片悄悄丢了调用方也不知道，
        长回复被截断时看起来就像模型没说完。现在失败片会写进日志，
        并且返回值如实反映。**不做自动重试**：POST 发消息不是幂等的，
        重试会把同一句话发两遍，比丢一片更烦人。
        """
        parts = split_text(text)
        if not parts:
            return True
        failed = 0
        for i, part in enumerate(parts):
            try:
                self._send_message(chat_id, "text", {"text": part})
            except Exception as err:
                failed += 1
                self.on_log("发消息失败（第 %d/%d 片）：%s"
                            % (i + 1, len(parts), err))
        if failed:
            # 只写日志，不动 _last_error —— 那个字段是"连接状态"，把它当成
            # "发送失败"用会让状态卡片显示成"连接出错"，反而误导。
            self.on_log("本次发送有 %d/%d 片失败" % (failed, len(parts)))
        return failed == 0

    def reply_text(self, message_id, text, chat_id=""):
        """回复某条消息。

        **第一片走 reply**（挂在主人那句话下面，读起来像"接话"）；
        **后面的分片走普通发送** —— reply 接口一次只能塞一片，
        连着 reply 同一条消息会变成"一句话被劈成好几条挂在同一个位置"。
        所以 chat_id 尽量带上；实在没有就退回"每片都 reply"，
        至少不丢内容。
        """
        parts = split_text(text)
        if not parts:
            return True
        ok = self._reply_one(message_id, parts[0])
        for part in parts[1:]:
            ok = (self.send_text(chat_id, part) if chat_id
                  else self._reply_one(message_id, part)) and ok
        return ok

    def _reply_one(self, message_id, text):
        try:
            self._reply_message(message_id, "text", {"text": text})
            return True
        except Exception as err:
            self.on_log("回复失败：%s" % err)
            return False
