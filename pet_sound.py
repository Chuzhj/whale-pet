# -*- coding: utf-8 -*-
"""音效：播放 `assets/sounds/` 里那几个自己合成的小 WAV。

为什么用 `winsound` 而不是 QtMultimedia：
  * PySide6 **Essentials 里没有 QtMultimedia**（它在 Addons 包里，装进来要多几十 MB），
    而桌宠只需要"放一段几十毫秒的 WAV"，`winsound` 是 Windows 标准库、零依赖；
  * `winsound.PlaySound(..., SND_ASYNC)` 是**非阻塞**的 —— 不会卡住动画。

音量的处理：`winsound` 没有音量参数，所以按需要把主文件**缩放成一份缓存**
（`sounds_cache/<名字>.<音量>.wav`，几 KB，只在音量变化后重新渲染一次）。
这样"全局音量滑块"是真的立刻生效，而不是摆设。

三条保命规矩（音效永远不能影响桌宠本体）：
  1. 任何异常都吞在里面，只记一行日志；
  2. 文件缺失 / 不是 Windows / winsound 不可用 → 整体静默失效；
  3. 播放一律异步，绝不 `SND_SYNC`（那会把界面冻住一个音的时长）。
"""
import struct
import sys
import wave
from pathlib import Path

from pet_constants import SOUND_FILES, SOUND_LABELS
from pet_assets import resource_dir

try:                                    # Windows 才有；别的平台整体降级
    import winsound
except Exception:                       # pragma: no cover - 非 Windows
    winsound = None

# 事件默认全开（与 DEFAULT_CONFIG 里那份一致；这里留一份是为了"配置没读到时也能用"）
SOUND_EVENTS_DEFAULT = {key: True for key in SOUND_FILES}

_configured = None      # {"enabled":bool,"volume":int,"events":{...},"cache_dir":Path}
_warned = set()         # 只对每种问题各吭一声，别刷屏


def _log(msg):
    try:
        sys.stderr.write("[音效] %s\n" % msg)
    except Exception:
        pass


def available():
    """能不能放音：有 winsound + 主文件在。"""
    if winsound is None:
        return False
    return sound_dir().exists()


def sound_dir():
    return resource_dir() / "assets" / "sounds"


def configure(enabled=True, volume=70, events=None, cache_dir=None):
    """更新播放设置（桌宠启动时调一次，设置面板保存后再调一次）。"""
    global _configured
    try:
        vol = int(volume)
    except (TypeError, ValueError):
        vol = 70
    vol = max(0, min(100, vol))
    ev = dict(SOUND_EVENTS_DEFAULT)
    if isinstance(events, dict):
        for key in ev:
            if key in events:
                ev[key] = bool(events[key])
    _configured = {"enabled": bool(enabled), "volume": vol, "events": ev,
                   "cache_dir": Path(cache_dir) if cache_dir else None}


def current():
    """给设置面板读回当前值。"""
    if _configured is None:
        configure()
    return dict(_configured)


def _scaled_path(name, volume):
    """按音量拿一份可播的文件路径；音量 100 直接用主文件。"""
    master = sound_dir() / SOUND_FILES[name]
    if not master.exists():
        return None
    if volume >= 100:
        return master
    cache_dir = (_configured or {}).get("cache_dir")
    if cache_dir is None:                    # 没给缓存目录就按原音量放
        return master
    target = cache_dir / ("%s.%d.wav" % (name, volume))
    if target.exists():
        return target
    try:
        with wave.open(str(master), "rb") as fh:
            params = fh.getparams()
            frames = fh.readframes(params.nframes)
        gain = volume / 100.0
        out = bytearray()
        for i in range(0, len(frames) - 1, 2):
            (sample,) = struct.unpack_from("<h", frames, i)
            out += struct.pack("<h", int(max(-32768, min(32767, sample * gain))))
        cache_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as fh:
            fh.setnchannels(params.nchannels)
            fh.setsampwidth(params.sampwidth)
            fh.setframerate(params.framerate)
            fh.writeframes(bytes(out))
        return target
    except Exception as err:
        if "cache" not in _warned:
            _warned.add("cache")
            _log("音量缓存渲染失败（按原音量播放）：%s" % err)
        return master


def play(name, force=False):
    """放一个音效。**任何失败都只是没声**，绝不上抛。

    force=True 给设置面板的「试听」用：忽略总开关与事件开关。
    返回是否真的交给系统播了（自检用得上）。
    """
    if winsound is None or name not in SOUND_FILES:
        return False
    cfg = _configured or {}
    if not force:
        if not cfg.get("enabled", True):
            return False
        if not (cfg.get("events") or {}).get(name, True):
            return False
    volume = int(cfg.get("volume", 70) if not force else 90)
    if not force and volume <= 0:
        return False
    path = _scaled_path(name, volume)
    if path is None:
        if "missing" not in _warned:
            _warned.add("missing")
            _log("找不到音效文件：%s（跑一次 make_sounds.py 就能补齐）"
                 % (sound_dir() / SOUND_FILES[name]))
        return False
    try:
        winsound.PlaySound(str(path),
                           winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
        return True
    except Exception as err:
        if "play" not in _warned:
            _warned.add("play")
            _log("播放失败（以后不再重复报）：%s" % err)
        return False


def preview(name):
    """设置面板的「试听」：绕过开关，用固定音量放一次。"""
    return play(name, force=True)


def labels():
    """事件 -> 中文名（设置面板按这个顺序列）。"""
    return [(key, SOUND_LABELS.get(key, key)) for key in SOUND_FILES]
