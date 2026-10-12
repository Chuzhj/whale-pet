# -*- coding: utf-8 -*-
"""存储层：配置与密钥（DPAPI 加密）、精力/饱食状态、对话日志、长期记忆、长期提醒。

全部集中在这里，是因为它们的共同规矩（原子写、BOM 容错、失败要出声）以前散在八处，各写各的。"""
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


def config_path():
    """配置文件放在 exe（或脚本）旁边。

    不能用 resource_dir()：打包后它指向 sys._MEIPASS 那个临时解压目录，
    每次启动都是新的一份，写进去的 key 下次就找不到了。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / CONFIG_NAME
    return Path(__file__).resolve().parent / CONFIG_NAME


def _warn(message):
    """把"以前被 except: pass 吞掉"的问题报到 stderr。

    打包成窗口程序时终端看不见，但命令行跑 / 重定向日志时会留痕 ——
    比"磁盘满了、文件被占用，而她只是不说话"这种无迹可寻强得多。
    """
    try:
        print("[警告] %s" % message, file=sys.stderr)
    except Exception:
        pass


def _read_text(path):
    """读文本，容忍带 BOM 的 UTF-8。

    记事本"另存为 UTF-8"默认就带 BOM，而 json.loads 遇到 BOM 会直接抛错 ——
    以前这个错被静默吞掉，表现为"配置全回默认、api_key 凭空消失"，
    主人只会觉得"她怎么又不理我了"。
    """
    return Path(path).read_text(encoding="utf-8-sig")


def _atomic_write_text(path, text):
    """原子写文本（临时文件 + os.replace），返回是否成功。

    桌宠随时会被强杀 / 断电：就地重写留下半截文件后，json 读不出来，
    闹钟、记忆、日志就无声地丢了。同分区 replace 是原子的，不存在半截。
    """
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError as err:
        _warn("写 %s 失败：%s" % (target, err))
        return False


def _write_json(path, obj):
    """把对象写成 JSON 文件（原子 + 带缩进）。返回是否成功。"""
    return _atomic_write_text(
        path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _write_jsonl(path, entries):
    """整体重写一个 jsonl（原子）。返回是否成功。"""
    return _atomic_write_text(
        path, "".join(json.dumps(e, ensure_ascii=False) + "\n"
                      for e in entries))


def _append_jsonl(path, entry):
    """往 jsonl 追加一行。日志绝不能影响聊天本身，所以失败只吭一声不抛。

    追加是**单次 write**，本身就不会写出半截行（就地整体重写才会），
    所以这里不需要原子写。
    """
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as err:
        _warn("追加 %s 失败：%s" % (path, err))


def _read_jsonl(path):
    """读 jsonl（容忍 BOM、容忍坏行）。坏行跳过，但不影响其余条目。"""
    try:
        raw = _read_text(path).splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _dpapi(data, unprotect=False):
    """DPAPI 加/解密。成功返回 bytes，失败返回 None（由调用方决定退路）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(bytes(data), len(data))
        blob_in = DATA_BLOB(len(data),
                            ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        func = (ctypes.windll.crypt32.CryptUnprotectData if unprotect
                else ctypes.windll.crypt32.CryptProtectData)
        # 两个函数的参数形状一致（第 2 个参数一个进一个出，传 None 即可）
        if not func(ctypes.byref(blob_in), None, None, None, None, 0,
                    ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _protect_secret(value):
    """把明文密钥变成 "enc:v1:<base64>"；做不到就原样返回（宁可明文也别丢 key）。"""
    value = str(value or "")
    if not value or value.startswith(_SECRET_PREFIX):
        return value
    blob = _dpapi(value.encode("utf-8"), unprotect=False)
    if blob is None:
        return value
    return _SECRET_PREFIX + base64.b64encode(blob).decode("ascii")


def _unprotect_secret(value):
    """把配置里的密钥读回明文（明文/空值原样返回，解不开就报一声并当空）。"""
    value = str(value or "")
    if not value.startswith(_SECRET_PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(_SECRET_PREFIX):])
    except Exception:
        return ""
    plain = _dpapi(blob, unprotect=True)
    if plain is None:
        _warn("配置里的密钥解不开（换过 Windows 账户或换过机器？）—— "
              "请在「设置 / 更换 API key」里重新填一次")
        return ""
    return plain.decode("utf-8", "replace")


def _seal_secrets(cfg):
    """落盘前把密钥加密（**内存里始终是明文**，只有写文件这一下变密文）。"""
    out = dict(cfg)
    for key in SECRET_KEYS:
        if out.get(key):
            out[key] = _protect_secret(out[key])
    return out


def _mask_secret(value):
    """令牌/密钥的打码显示（日志、终端、截图里都不该出现原文）。"""
    value = str(value or "")
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-2:]


def _mask_url(url):
    """把 URL 里的 ?t=<token> 打码，其余照旧。"""
    return re.sub(r"([?&]t=)([^&\s]+)",
                  lambda m: m.group(1) + _mask_secret(m.group(2)), str(url))


def security_warnings(cfg):
    """启动时把"开着、但等于把电脑交出去"的组合点出来，返回要打印的行。

    这些开关都是主人自己开的，也都有正当用途（手机要连就得 listen 0.0.0.0），
    所以这里**只警告、不改行为**。但必须说出来 —— 它们的组合等于
    "同一个局域网里的任何设备（或者飞书群里任何人）都能在这台电脑上执行代码"，
    而这种状态最危险的地方恰恰是"看不出来"。
    """
    out = []
    bind = str(cfg.get("remote_bind") or "")
    if cfg.get("remote_enabled") and bind not in ("127.0.0.1", "localhost", "::1"):
        out.append("远程遥控正监听 %s：同一局域网都能连（要用手机时临时开、"
                   "用完把绑定地址改回 127.0.0.1）。" % bind)
        if cfg.get("remote_allow_harness"):
            out.append("远程派活也开着 —— 拿到那个链接的设备可以在这台电脑上"
                       "执行代码。")
    if cfg.get("feishu_enabled"):
        if cfg.get("feishu_allow_harness"):
            out.append("飞书派活开着 —— 能给她发消息的人可以在这台电脑上执行代码。")
        if not str(cfg.get("feishu_allow_sender") or "").strip():
            out.append("飞书没填「只认这个人的消息」：群消息现在一律忽略，"
                       "要她在群里应答请先填上你自己的 open_id。")
    return out


def load_config(path=None):
    """读配置。文件不存在、读坏了，一律退回默认值 —— 不能因为配置有问题就起不来。

    读坏了**会吭一声**（stderr）：静默回落成默认值看起来像"她突然没配 key"，
    不吭声的话这个故障根本查不出来。
    """
    cfg = dict(DEFAULT_CONFIG)
    target = Path(path) if path else config_path()
    try:
        raw = json.loads(_read_text(target))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if v not in (None, "")})
    except (FileNotFoundError, IsADirectoryError):
        pass
    except (ValueError, OSError, UnicodeDecodeError) as err:
        _warn("读配置 %s 失败（已按默认值继续）：%s" % (target, err))
    # 密钥在文件里是密文（见 _protect_secret），读进来就还原成明文 ——
    # 上层代码拿到的永远是明文，不需要知道存储格式。
    for key in SECRET_KEYS:
        if cfg.get(key):
            cfg[key] = _unprotect_secret(cfg[key])
    env_key = os.environ.get(API_KEY_ENV)
    if env_key:
        cfg["api_key"] = env_key   # 环境变量优先，方便临时换 key
    return cfg


def save_api_key(key, path=None):
    """把 key 写回配置文件，返回**是否写成功**。

    三条保命规矩：
    1. **先读旧配置再合并** —— 只动 api_key，provider / model / search 等原样保留，
       不能因为界面里换个 key 就把主人手调的其他配置冲掉；
    2. **原子写**（临时文件 + os.replace）—— 写到一半崩了/断电，JSON 就废了，
       桌宠下次直接起不来。replace 在同一分区上是原子的，不存在半截文件；
    3. 旧文件读不出来按默认配置重写 —— 和 load_config 的容错方向一致。

    返回值以前是"写进去的那份配置"，但没有任何调用方用它，
    现在改成 bool：**写失败要能传回界面**（目录只读、文件被占用、盘满），
    不能抛异常把 Qt 槽掀了，也不能静默说"存好了"。
    """
    target = Path(path) if path else config_path()
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(_read_text(target))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if v not in (None, "")})
    except (FileNotFoundError, IsADirectoryError):
        pass
    except (ValueError, OSError, UnicodeDecodeError) as err:
        _warn("旧配置 %s 读不出来（按默认重写）：%s" % (target, err))
    cfg["api_key"] = (key or "").strip()
    # 落盘前加密：文件里不留明文（读回来时自动解开，见 load_config）
    return _write_json(target, _seal_secrets(cfg))


def save_config_values(updates, path=None):
    """把若干配置项合并写回（远程遥控 / 飞书接入的开关、端口、token 用）。

    和 save_api_key 同一套保命规矩：先读旧配置再合并（只动给定的键，
    不冲掉主人手调的其他配置）、原子写、坏文件当默认重写。返回是否写成功
    —— 以前它无保护地裸写，调用点又在 Qt 槽里，目录只读时点"保存"
    直接抛异常出去；现在返回 False，界面照常给一句人话。
    """
    target = Path(path) if path else config_path()
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(_read_text(target))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if v not in (None, "")})
    except (FileNotFoundError, IsADirectoryError):
        pass
    except (ValueError, OSError, UnicodeDecodeError) as err:
        _warn("旧配置 %s 读不出来（按默认重写）：%s" % (target, err))
    cfg.update(updates)
    return _write_json(target, _seal_secrets(cfg))


def _clamp_float(value, lo, hi, default):
    """把来路不明的数值收进 [lo, hi]。读状态文件时用 —— 文件可能被手改过。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:                      # NaN
        return default
    return max(lo, min(hi, v))


def data_dir(config_file=None):
    """日志和记忆文件放**配置文件同目录**（= exe 旁边），跟配置一起搬家。"""
    return (Path(config_file) if config_file else config_path()).parent


def _state_path(config_file=None):
    return data_dir(config_file) / STATE_NAME


def load_pet_state(config_file=None):
    """读回上次退出时的 (精力, 饱食度, 当时是不是睡着的)。

    文件不存在、读坏了、或者数值被手改成了越界值 → 一律夹回 [0, 上限]；
    整个文件都读不出来就当"满血满饭、醒着"开局，绝不能因为状态文件有问题就起不来。
    没有 `sleeping` 字段的旧文件按 False 处理（向前兼容）。
    """
    try:
        raw = json.loads(_read_text(_state_path(config_file)))
    except (OSError, ValueError, UnicodeDecodeError):
        return ENERGY_MAX, SATIETY_MAX, False
    if not isinstance(raw, dict):
        return ENERGY_MAX, SATIETY_MAX, False
    return (_clamp_float(raw.get("energy"), 0.0, ENERGY_MAX, ENERGY_MAX),
            _clamp_float(raw.get("satiety"), 0.0, SATIETY_MAX, SATIETY_MAX),
            bool(raw.get("sleeping", False)))


def save_pet_state(config_file, energy, satiety, sleeping=False):
    """把精力/饱食度/**睡没睡**落盘。**原子写**（临时文件 + os.replace）——

    和 api_key 那份配置同一个道理：桌宠随时可能被强杀 / 断电，
    JSON 写到一半就废了，下次启动只能读回默认值（= 白攒一天的精力）。

    为什么"睡没睡"必须存下来、不能像运行时那样从画面推导：
    运行时 `sleeping()` 看的是"现在画的是不是睡姿"，那是**同一个进程内**的事实；
    而重启之后画面是全新的一张，任何运行时状态都推不出来 ——
    精力 <= 60 也可能只是"她醒着但快没电了"（主人测过 5/21 那种开局）。
    所以这是一个**跨进程的事实**，只能持久化。
    （注意这和"影子标志"不冲突：影子标志指的是"同一个进程里能从画面算出来、
    却又存一份"的东西，那种才会失同步。）
    """
    target = _state_path(config_file)
    try:
        payload = json.dumps(
            {"energy": round(float(energy), 2),
             "satiety": round(float(satiety), 2),
             "sleeping": bool(sleeping),
             "saved": datetime.now().strftime(TIME_FMT)},
            ensure_ascii=False, indent=2) + "\n"
    except (TypeError, ValueError):
        return False
    return _atomic_write_text(target, payload)


def append_chat_log(config_file, who, text):
    _append_jsonl(data_dir(config_file) / CHAT_LOG_NAME,
                  {"t": datetime.now().strftime(TIME_FMT),
                   "who": who, "text": (text or "")[:200]})


def prune_chat_log(config_file, keep_days=CHAT_LOG_KEEP_DAYS):
    """删掉超过 keep_days 的对话日志（要求 4：每次启动时调用）。返回删掉几条。

    按**原始行**处理，不再先过 _read_jsonl：那一步会把解析不了的行直接丢掉，
    于是"顺手清理过期日志"变成了"顺手销毁读不出来的数据"。
    现在坏行原样留着，只有确认过期（时间戳合法且早于 cutoff）的才删。
    """
    path = data_dir(config_file) / CHAT_LOG_NAME
    try:
        raw_lines = _read_text(path).splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    if not raw_lines:
        return 0
    cutoff = datetime.now() - timedelta(days=keep_days)
    kept, removed = [], 0
    for line in raw_lines:
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except ValueError:
            kept.append(line)          # 坏行留着：宁可留着，也别替主人做销毁决定
            continue
        if not isinstance(entry, dict):
            kept.append(line)
            continue
        try:
            fresh = datetime.strptime(str(entry.get("t", "")), TIME_FMT) >= cutoff
        except ValueError:
            # 时间戳读不出来的条目**也留着**（以前当过期直接删）：
            # 真正读不出来的原因可能是时钟前跳，那样会一次性清空全部日志。
            fresh = True
        if fresh:
            kept.append(line)
        else:
            removed += 1
    if removed:
        _atomic_write_text(path, "".join(l + "\n" for l in kept))
    return removed


def append_memory(config_file, note):
    """往长期记忆追加一条（要求 4：只增不删，满了丢最旧）。返回是否写成功。"""
    note = (note or "").strip()
    if not note:
        return False
    path = data_dir(config_file) / MEMORY_LOG_NAME
    entries = _read_jsonl(path)
    entries.append({"t": datetime.now().strftime(TIME_FMT), "note": note[:120]})
    entries = entries[-MEMORY_MAX_ITEMS:]
    return _write_jsonl(path, entries)


def build_memory_context(config_file,
                         memory_limit=MEMORY_MAX_ITEMS,
                         chat_turns=CHAT_LOG_CONTEXT_TURNS):
    """把长期记忆 + 最近对话拼成一段可注入 system 的文字。

    每次启动都调用（要求 4）；什么都没有就返回 None，不注空串占位。
    记忆条数有上限、对话条数有上限 —— 这是给 prompt 系的尺子，不是给磁盘的：
    磁盘上长期记忆全保留，只是**每次注入时**最多带这么多，防止 prompt 撑爆。
    """
    lines = []
    memories = _read_jsonl(data_dir(config_file) / MEMORY_LOG_NAME)
    if memories:
        lines.append("【长期记忆（主人告诉过你的事，说话时要体现）】")
        for e in memories[-memory_limit:]:
            lines.append("- %s（%s）" % (e.get("note", ""), e.get("t", "")))
    chats = _read_jsonl(data_dir(config_file) / CHAT_LOG_NAME)
    if chats:
        lines.append("【最近的对话（供参考，别逐条复述）】")
        for e in chats[-chat_turns:]:
            lines.append("%s：%s" % (e.get("who", "?"), e.get("text", "")))
    return "\n".join(lines) if lines else None


def _alarm_path(config_file):
    return data_dir(config_file) / ALARM_LOG_NAME


def load_alarms(config_file):
    """读所有待触发的提醒（按时间升序）。文件坏了就当没有，绝不抛。"""
    out = []
    for e in _read_jsonl(_alarm_path(config_file)):
        when = str(e.get("when", ""))
        note = str(e.get("note", "")).strip()
        try:
            when_dt = datetime.strptime(when, ALARM_FMT)
        except ValueError:
            continue                    # 时间格式坏的条目直接跳过
        if not note:
            continue
        out.append({"id": str(e.get("id", "")), "when": when_dt, "note": note,
                    "created": str(e.get("created", ""))})
    out.sort(key=lambda a: a["when"])
    return out


def save_alarms(config_file, alarms):
    """整体重写提醒文件（条目少、又要改状态，jsonl 全量重写最省心）。

    **返回是否写成功**：这个返回值是"提醒有没有真的被消费掉"的唯一凭据，
    见 due_alarms —— 写失败却当成功，就会变成每分钟重复播报同一条提醒。
    改成原子写也是同一个道理（写完就崩 → 半截文件 → 提醒凭空消失）。
    """
    return _write_jsonl(
        _alarm_path(config_file),
        [{"id": a["id"], "when": a["when"].strftime(ALARM_FMT),
          "note": a["note"], "created": a.get("created", "")}
         for a in alarms])


def append_alarm(config_file, when_dt, note, now=None):
    """登记一条长期提醒，成功返回**这条的 id**（失败返回 None）。

    为什么必须返回 id、不能只返回 True：`_arm_alarm` 要拿它把"精确定时器"
    和"这条提醒"对应起来，主人取消提醒时才找得到该停哪一个。
    以前这里返回布尔值，`{"id": True, "timer": …}` 就永远匹配不上真实 id ——
    表现为**文件里的条目删掉了、到点照样弹出来**（自检抓到的）。

    时间先按 ALARM_FMT 的精度**归一**（文件里本来也只存到分钟）：不归一的话，
    "同一时刻同一件事不重复登记"这条判据几乎永远不成立 —— 带秒的时间
    一进文件就被截断，再读出来跟内存里的原值对不上（这是离线自检抓到的）。
    """
    note = (note or "").strip()
    if not note or not isinstance(when_dt, datetime):
        return None
    # 归一到文件的精度：内存里的值从此和文件里的值永远一致
    when_dt = datetime.strptime(when_dt.strftime(ALARM_FMT), ALARM_FMT)
    now = now or datetime.now()
    if when_dt <= now:
        return None                     # 时间已经过去了，不登记（让上层说清楚）
    alarms = load_alarms(config_file)
    if len(alarms) >= ALARM_MAX_ITEMS:
        return None
    # 同一时刻同一件事不重复登记（模型偶尔会连着说两遍）→ 返回已有那条的 id
    for a in alarms:
        if a["when"] == when_dt and a["note"] == note:
            if a["id"]:
                return a["id"]
            # 老文件里可能有 id 为空的条目：**补一个**再返回，
            # 否则上层拿到空串会报"登记失败"，而文件里其实躺着这条提醒。
            a["id"] = uuid.uuid4().hex[:12]
            save_alarms(config_file, alarms)
            return a["id"]
    # id 用 uuid 而不是"秒级时间戳 + 两位数随机"：后者同一秒登记两条
    # 有 1/90 的概率撞号，撞上之后 remove_alarm 按 id 过滤会一次删掉两条
    # （主人在同一秒里设了两个提醒时，取消一个会连另一个一起消失）。
    entry_id = uuid.uuid4().hex[:12]
    alarms.append({"id": entry_id, "when": when_dt, "note": note,
                   "created": now.strftime(TIME_FMT)})
    if not save_alarms(config_file, alarms):
        return None                     # 没落盘就不算登记成功
    return entry_id


def remove_alarm(config_file, alarm_id):
    """按 id 删一条（触发后调用），并整体重写文件。返回是否真的删掉了。"""
    if not alarm_id:
        return False
    alarms = load_alarms(config_file)
    kept = [a for a in alarms if a["id"] != alarm_id]
    if len(kept) != len(alarms):
        return save_alarms(config_file, kept)
    return False


def due_alarms(config_file, now=None):
    """取出**已到期**的提醒，**并且确认文件里已经删掉**，返回它们。

    程序关闭期间错过的提醒也走这条路：重启后第一次检查就会把它们捞出来
    补提醒（"十月二十号去跑步"晚看到一眼，也远好过永远看不到）。

    关键在"确认"两个字：以前无论写盘成没成功都把它们当"已消费"返回，
    而写失败时盘上条目还在 → 每分钟的巡检再把同一句提醒播一遍，
    变成无限重复。现在写不进去就**不返回**（下一分钟重试），
    宁可晚一分钟，也不能变成复读机。
    """
    now = now or datetime.now()
    alarms = load_alarms(config_file)
    due = [a for a in alarms if a["when"] <= now]
    if not due:
        return []
    if not save_alarms(config_file, [a for a in alarms if a["when"] > now]):
        _warn("提醒文件写不进去，这一批到期提醒推迟到下次巡检：%s"
              % "、".join(a["note"] for a in due))
        return []
    return due
