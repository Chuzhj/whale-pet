# -*- coding: utf-8 -*-
"""生成鲸鲸桌宠的音效（纯标准库，随时可重跑）。

为什么要自己合成、而不是拿现成的音频：
  * 桌宠要能**自由再分发**；别人做好的音效（哪怕是免费的）授权状态往往说不清；
  * 自己合成的这几个几十毫秒的小音，体积一共几 KB，随包带走毫无负担；
  * 参数（时长/频率/衰减）写在代码里，想调随时改一行重跑。

产出（写进 assets/sounds/）：
    press.wav        按压：短促上扬的"啵"
    release.wav      松开：略低的下落音
    feed.wav         吃饭：两声"咔叽"（先低后高）
    task_done.wav    任务结束：三音上行琶音（像经验球的"叮～"）
    remind.wav       提醒：两声清脆"叮叮"
    sleep.wav        入睡：柔和下滑音

用法：
    python make_sounds.py                # 默认写到 <本目录>/assets/sounds
    python make_sounds.py --out D:\\x     # 指定输出目录
"""
import argparse
import math
import struct
import wave
from pathlib import Path

RATE = 22050          # 采样率：够用且体积小
PEAK = 0.42           # 满幅的 42%：留足余量，避免爆音


def _tone(freq_from, freq_to, seconds, decay=6.0, attack=0.005,
          vibrato=0.0, vib_hz=6.0):
    """一段正弦音：频率从 freq_from 滑到 freq_to，指数衰减，带 5ms 起音。

    attack 那几毫秒是防"咔哒"的：直接从 0 跳到满幅会有脉冲噪声。
    """
    n = max(1, int(RATE * seconds))
    out = []
    phase = 0.0
    for i in range(n):
        t = i / RATE
        k = i / n
        freq = freq_from + (freq_to - freq_from) * k
        if vibrato:
            freq *= 1.0 + vibrato * math.sin(2 * math.pi * vib_hz * t)
        phase += 2 * math.pi * freq / RATE
        env = math.exp(-decay * k)
        if t < attack:
            env *= t / attack
        out.append(math.sin(phase) * env)
    return out


def _silence(seconds):
    return [0.0] * max(1, int(RATE * seconds))


def _mix(*parts):
    """把几段拼起来（不是叠加）：返回拼接后的样本表。"""
    out = []
    for p in parts:
        out.extend(p)
    return out


def _overlay(a, b, at_seconds, gain=1.0):
    """把 b 叠加到 a 的第 at_seconds 秒处（长度取两者较长）。"""
    start = int(RATE * at_seconds)
    out = list(a)
    if len(out) < start + len(b):
        out.extend([0.0] * (start + len(b) - len(out)))
    for i, v in enumerate(b):
        out[start + i] += v * gain
    return out


def _write(path: Path, samples):
    """写 16bit 单声道 WAV，并做软限幅（叠加后可能超过满幅）。"""
    peak = max(1e-9, max(abs(v) for v in samples))
    gain = min(1.0, PEAK / peak)
    frames = bytearray()
    for v in samples:
        s = int(max(-1.0, min(1.0, v * gain)) * 32767)
        frames += struct.pack("<h", s)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(bytes(frames))
    return path.stat().st_size


def build(out_dir: Path):
    made = []

    # 按压：短促上扬，"啵"
    made.append(("press.wav", _mix(
        _tone(660, 990, 0.085, decay=7.0))))

    # 松开：略低、略长
    made.append(("release.wav", _mix(
        _tone(520, 380, 0.110, decay=5.5))))

    # 吃饭：两声"咔叽"（低→高，带一点颤音，像咀嚼）
    chomp1 = _tone(480, 300, 0.060, decay=9.0, vibrato=0.05)
    chomp2 = _tone(560, 340, 0.060, decay=9.0, vibrato=0.05)
    made.append(("feed.wav", _mix(chomp1, _silence(0.035), chomp2)))

    # 任务结束：三音上行琶音（C6-E6-G6），带余韵
    arp = _tone(1046.5, 1046.5, 0.10, decay=5.0)
    arp = _overlay(arp, _tone(1318.5, 1318.5, 0.12, decay=5.0), 0.055)
    arp = _overlay(arp, _tone(1568.0, 1568.0, 0.22, decay=3.4), 0.110)
    made.append(("task_done.wav", arp))

    # 提醒：两声清脆"叮叮"
    ding = _tone(1174.7, 1174.7, 0.13, decay=4.2)
    made.append(("remind.wav", _mix(ding, _silence(0.075),
                                    _tone(1396.9, 1396.9, 0.20, decay=3.6))))

    # 入睡：柔和下滑
    made.append(("sleep.wav", _mix(_tone(500, 300, 0.26, decay=3.0))))

    info = []
    for name, samples in made:
        size = _write(out_dir / name, samples)
        info.append((name, len(samples) / RATE, size))
    return info


def main():
    ap = argparse.ArgumentParser(description="生成鲸鲸桌宠的音效")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent / "assets" / "sounds",
                    help="输出目录")
    args = ap.parse_args()
    info = build(args.out)
    total = 0
    print("[音效] 写入 %s" % args.out)
    for name, secs, size in info:
        total += size
        print("   %-15s %5.2f 秒  %5.1f KB" % (name, secs, size / 1024))
    print("[音效] 共 %d 个 / %.1f KB" % (len(info), total / 1024))


if __name__ == "__main__":
    main()
