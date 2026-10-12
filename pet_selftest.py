# -*- coding: utf-8 -*-
"""离屏自检：不开窗口、不联网，验证素材、渲染、状态机、标记载荷。

它是**唯一能在打包好的 exe 里跑**的检查手段（GUI 模式没有控制台），
所以宁可长，也不要拆成"记得手动跑"的几个脚本。
"""
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
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import QObject
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_constants import _pet_feishu, _session_log      # 私有名：import * 带不走
from pet_assets import (asset_key, load_scaled)
from pet_bridges import (build_chat, build_feishu, build_harness_console,
                         build_remote, parse_balance_reply)
from pet_panels import (build_balance_panel, build_sound_dialog)
from pet_net import (describe_net_error, format_search_digest, parse_bing_results, reply_from_json, resolve_endpoint, search_query, sse_delta, wants_search)
from pet_store import (_dpapi, _read_jsonl, _state_path, append_alarm, append_chat_log, append_memory, build_memory_context, data_dir, due_alarms, load_alarms, load_config, load_pet_state, prune_chat_log, save_alarms, save_api_key)
from pet_tags import (bubble_preview, hide_mood_fragment, strip_alarm_tag, strip_cancel_tag, strip_memory_tag, strip_mood_tag, strip_remind_tag, strip_reply_tags)
from pet_time import (cancel_from_user, format_alarm_time, format_seconds, now_line, parse_duration, reminder_from_user)
from pet_windows import (build_chat_window, build_clock_window)
from pet_widget import EnergyBars, WhalePet, build_widget


def run_selftest_logged(buckets, assets_dir, awake_h, sleep_h, log_path):
    """自检 + 把输出同时写进文件，返回自检的退出码。

    为什么需要它：打包成 exe 之后 `console=False`（GUI 模式没有控制台），
    `print` 是空操作 —— 想在打包后确认"素材/依赖/Qt 插件都进去了"，
    只能让程序自己把结果写到一个文件里。stdout 为 None 时也要能工作
    （窗口模式下 sys.stdout 就是 None）。
    """
    import contextlib

    class _Tee:
        def __init__(self, streams):
            self.streams = streams

        def write(self, text):
            for st in self.streams:
                try:
                    st.write(text)
                except Exception:
                    pass
            return len(text)

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

    with open(log_path, "w", encoding="utf-8") as fh:
        streams = [st for st in (sys.stdout, fh) if st is not None]
        with contextlib.redirect_stdout(_Tee(streams)):
            code = run_selftest(buckets, assets_dir, awake_h, sleep_h)
        fh.write("\n[自检] 退出码=%d\n" % code)
    return code


def run_selftest(buckets, assets_dir, awake_h, sleep_h):
    """离屏自检：不开窗口，验证 alpha 通道与动画帧能否正常渲染。

    ⚠️ 开头这一排 `xxx_ok = False` **不能删**：各段检查分散在几十个
    try 里，任何一段提前抛异常，"赋值"那一步就没执行；等到最后的
    `if not (config_ok and ...)` 一读就是 `UnboundLocalError` ——
    那时整轮自检会崩在收尾处，**真正出问题的那一段反而看不到**。
    （2026-10-10 拆分时就踩过一次：精力那块先炸，沉睡守卫的 print 跟着崩。）
    所以这里一次性把最终判定要用到的**全部**标志预置成 False
    （2026-10-11 加飞书媒体那节时又多一个 —— 数字别再写死了，
    少预置一个就会在收尾处炸出 UnboundLocalError）。
    """
    wrap_ok = tail_ok = asleep_ok = config_ok = preset_ok = key_ok = sse_ok = json_ok = preview_ok = trigger_ok = query_ok = protect_ok = wide_ok = ask_ok = mood_ok = mood_act_ok = click_no_expr_ok = sleep_click_ok = clock_ok = log_ok = mem_ctx_ok = memory_tag_ok = key_rebuild_ok = dpapi_ok = hot_ok = window_ok = close_btn_ok = keydlg_ok = flag_ok = clear_ok = dblclick_ok = bing_ok = err_ok = balance_ok = tray_ok = hide_ok = now_ok = mid_ok = tag_ok = remind_ok = dur_ok = cancel_ok = parse_ok = timers_ok = alarm_ok = expr_ok = lock_ok = bowl_ok = fed_ok = energy_ok = bar_ok = awake_menu_ok = guard_ok = sleep_restore_ok = bridge_ok = False
    ledger_ok = sound_ok = panel_ok = bubble_ok = menu_ok = False
    print("[自检] 素材目录：%s" % assets_dir)
    print("[自检] 尺寸基准：醒着 %d px（×%.2f）/ 睡姿 %d px（×%.2f）"
          % (awake_h, AWAKE_SCALE, sleep_h, SLEEP_SCALE))
    print("[自检] 分桶：正面 %d / 变体 %d / 背面 %d / 表情 %d / 眨眼 %d / 睡姿 %d / 呆毛 %d / 尾巴 %d"
          % (len(buckets["front"]), len(buckets["variant"]),
             len(buckets["back"]), len(buckets["expression"]),
             len(buckets["blink"]), len(buckets["sleep"]),
             len(buckets["ahoge"]), len(buckets["tail"])))

    pool_paths = buckets["front"] + buckets["variant"]
    if not pool_paths:
        pool_paths = buckets["back"]

    print("[自检] 透明通道检查 ---------------------------------")
    opaque_corners = 0
    for path in pool_paths:
        pixmap = load_scaled(path, awake_h)
        image = pixmap.toImage()
        corners = [image.pixelColor(0, 0),
                   image.pixelColor(image.width() - 1, 0),
                   image.pixelColor(0, image.height() - 1),
                   image.pixelColor(image.width() - 1, image.height() - 1)]
        max_corner_alpha = max(c.alpha() for c in corners)
        center_alpha = image.pixelColor(image.width() // 2,
                                        image.height() // 2).alpha()
        if max_corner_alpha > 16:
            opaque_corners += 1
        print("  %-14s %4dx%-4d  alpha通道=%s  四角最大alpha=%-3d  中心alpha=%d"
              % (path.name, pixmap.width(), pixmap.height(),
                 pixmap.hasAlphaChannel(), max_corner_alpha, center_alpha))

    if opaque_corners:
        print("[自检] 警告：有 %d 张图的四角不透明，说明背景没抠干净 —— "
              "桌宠会显示成一块白方块。" % opaque_corners)
    else:
        print("[自检] 透明底检查通过：所有素材四角均为透明。")

    expr_pixmaps = [load_scaled(p, awake_h) for p in buckets["expression"]]
    blink_paths = {asset_key(p.stem): p for p in buckets["blink"]}

    def selftest_blink(source_path):
        path = blink_paths.get(asset_key(source_path.stem))
        return load_scaled(path, awake_h) if path else None

    sleep_test = (load_scaled(buckets["sleep"][0], sleep_h)
                  if buckets["sleep"] else None)
    from PySide6.QtGui import QPixmap
    ahoge_test = (QPixmap(str(buckets["ahoge"][0]))
                  if buckets["ahoge"] else None)
    tail_test = (QPixmap(str(buckets["tail"][0]))
                 if buckets["tail"] else None)
    bowl_full_test = next((QPixmap(str(p)) for p in buckets["bowl"]
                           if "empty" not in p.stem.lower()), None)
    bowl_empty_test = next((QPixmap(str(p)) for p in buckets["bowl"]
                            if "empty" in p.stem.lower()), None)
    WhalePet = build_widget(
        [load_scaled(p, awake_h) for p in pool_paths],
        awake_h, 30, 1.0, True,
        expressions=[(asset_key(src.stem), pix)
                     for src, pix in zip(buckets["expression"], expr_pixmaps)],
        do_expr=True,
        idle_blinks=[selftest_blink(p) for p in pool_paths],
        expr_blinks=[selftest_blink(p) for p in buckets["expression"]],
        sleep_pixmap=sleep_test,
        ahoge_pixmap=ahoge_test,
        tail_pixmap=tail_test,
        bowl_full_pixmap=bowl_full_test,
        bowl_empty_pixmap=bowl_empty_test,
        # 能量条默认是关的（主人 2026-10-09 验收后隐藏），但自检要**强制打开**
        # 它来测 —— 否则这套逻辑就没人看着了，哪天写坏了也发现不了。
        show_bars=True,
        do_talk=True)
    pet = WhalePet()
    pet.show()
    for _ in range(45):
        pet._tick()
    shot = pet.grab()
    print("[自检] 渲染检查 ------------------------------------")
    print("  窗口尺寸 %dx%d，离屏渲染结果 %dx%d，非空=%s"
          % (pet.width(), pet.height(), shot.width(), shot.height(),
             not shot.isNull()))
    print("  缩放只由点击弹跳驱动：1.000 ~ %.3f" % (1.0 + BOUNCE_GAIN))

    if pet.do_expr:
        def which(pixmap):
            for i, p in enumerate(pet.expressions):
                if p is pixmap:
                    return i
            return -1

        idle = pet.pixmap
        # 连点一整圈，检查是否严格按 0,1,2,3 的顺序轮换
        walked = []
        for _ in range(len(pet.expressions)):
            pet.react()
            walked.append(which(pet.pixmap))
        sequential = walked == list(range(len(pet.expressions)))

        # 切完之后必须一直停在表情上 —— 不再自动收回待机
        held = which(pet.pixmap) >= 0
        pet.back_to_idle()
        restored = pet.pixmap is idle

        print("[自检] 表情状态机 ----------------------------------")
        print("  表情数 %d / 轮换顺序=%s / 顺序正确=%s / 点击后保持=%s / 可回待机=%s"
              % (len(pet.expressions), walked, sequential, held, restored))
        if not (sequential and held and restored):
            print("[自检] 失败：表情状态机行为不符合预期。")
            return 1

    paired_idle = [p for p in pet.idle_blinks if p is not None]
    paired_expr = [p for p in pet.expr_blinks if p is not None]
    if paired_idle or paired_expr:
        # 待机状态下眨眼
        pet.back_to_idle()
        idle_target = pet.current_blink_pixmap()
        pet.blink()
        blinked = pet.pixmap is idle_target
        pet.end_blink(pet._blink_token)
        unblinked = pet.pixmap is not idle_target

        # 表情状态下也要能眨 —— 这是本轮的核心诉求
        pet.react()
        expr_pixmap = pet.pixmap
        expr_target = pet.current_blink_pixmap()
        pet.blink()
        expr_blinked = expr_target is not None and pet.pixmap is expr_target
        pet.end_blink(pet._blink_token)
        expr_restored = pet.pixmap is expr_pixmap

        print("[自检] 眨眼 ----------------------------------------")
        print("  配对情况：待机 %d/%d 张、表情 %d/%d 个都配到了闭眼帧"
              % (len(paired_idle), len(pet.frames),
                 len(paired_expr), len(pet.expressions)))
        print("  待机 睁→闭=%s 闭→睁=%s" % (blinked, unblinked))
        print("  表情 睁→闭=%s 闭→睁=%s" % (expr_blinked, expr_restored))
        if not (blinked and unblinked and expr_blinked and expr_restored):
            print("[自检] 失败：眨眼切换不符合预期。")
            return 1

    if pet.sleep_pixmap is not None:
        pet.sleep()
        slept = pet.sleeping() and pet.pixmap is pet.sleep_pixmap

        # 一次完整呼吸中，画面高度会变化多少
        peak_px = pet.sleep_pixmap.height() * SLEEP_BREATH_AMP

        pet.blink()                      # 睡着时不该眨眼
        no_blink = pet.pixmap is pet.sleep_pixmap

        pet.wake_up()
        woken = (not pet.sleeping()) and pet.pixmap is pet.frames[pet.index]

        print("[自检] 睡觉 ----------------------------------------")
        print("  睡姿图 %dx%d（基准高 %d，缩放系数 %.2f）"
              % (pet.sleep_pixmap.width(), pet.sleep_pixmap.height(),
                 sleep_h, SLEEP_SCALE))
        print("  进入睡眠=%s / 呼吸起伏=%.1f%%（约 %.0f 像素，周期 %.1fs）"
              % (slept, SLEEP_BREATH_AMP * 100, peak_px, SLEEP_BREATH_PERIOD))
        print("  睡着时不眨眼=%s / 可唤醒=%s" % (no_blink, woken))
        if not (slept and no_blink and woken and peak_px > 3):
            print("[自检] 失败：睡觉状态不符合预期。")
            return 1

    if pet.ahoge_pixmap is not None:
        pet.back_to_idle()
        visible_idle = pet.ahoge_visible()

        # 让相位走完一整圈，量一下摆动的角度范围
        angles = []
        for step in range(9):
            phase = math.pi * step / 4.0
            angles.append(AHOGE_SWING * math.sin(phase))
        swing = max(angles) - min(angles)

        # 表情状态下应当继续叠着，动作不许断。注意要用**立绘表情**验 ——
        # 后面新增的 12 张特写表情自带呆毛、本来就不叠层（v39 加进来时，
        # 这条断言因为 react() 轮到了特写而变红过）。
        plain_exprs = [pm for name, pm in zip(pet.expr_names, pet.expressions)
                       if name not in SELF_CONTAINED_EXPRESSIONS]
        if plain_exprs:
            pet.show_expression(plain_exprs[0])
        visible_on_expr = pet.ahoge_visible()
        pet.back_to_idle()

        pet.blink()                       # 眨眼期间同样不该隐身
        visible_on_blink = pet.ahoge_visible()
        pet.end_blink(pet._blink_token)

        # 连推几个 tick，确认相位一直在走、每次变化都很小（没有归零式跳变）
        pet._ahoge_phase = 0.0
        pet._ahoge_angle = 0.0
        samples = []
        for _ in range(6):
            pet._tick()
            samples.append(pet._ahoge_angle)
        steps = [abs(b - a) for a, b in zip(samples, samples[1:])]
        advancing = all(s > 0 for s in steps)
        continuous = max(steps) < AHOGE_SWING * 0.5

        pet.sleep()
        hidden_on_sleep = not pet.ahoge_visible()
        pet.wake_up()
        visible_after_wake = pet.ahoge_visible()

        # 复现过的坑：先"去睡觉"，再"做个表情"。
        # 若 sleep 标志没被清掉，会停在"画面是站姿、标志还是睡着"的矛盾状态里，
        # 呆毛层被当成睡觉藏起来，整根消失。这里把那条路径钉住。
        woke_on_expr = True
        if plain_exprs:
            pet.sleep()
            pet.show_expression(plain_exprs[0])
            woke_on_expr = (not pet.sleeping()) and pet.ahoge_visible()
            pet.back_to_idle()

        print("[自检] 呆毛 ----------------------------------------")
        print("  呆毛层 %dx%d，贴在原图 (%d, %d) 处"
              % (pet.ahoge_pixmap.width(), pet.ahoge_pixmap.height(),
                 AHOGE_ORIGIN[0], AHOGE_ORIGIN[1]))
        print("  待机叠加=%s / 眨眼时仍叠加=%s / 表情时仍叠加=%s / 睡觉时隐藏=%s / 醒来恢复=%s"
              % (visible_idle, visible_on_blink, visible_on_expr,
                 hidden_on_sleep, visible_after_wake))
        print("  睡着时切表情会自动醒来并恢复呆毛=%s" % woke_on_expr)
        print("  摆动 %.0f°（±%.0f°，周期 %.1fs）/ 相位持续推进=%s / 单步最大变化=%.2f°"
              % (swing, AHOGE_SWING, AHOGE_PERIOD, advancing, max(steps)))
        print("  轴心（根部）= (%.0f, %.0f)，位于呆毛层内的 (%.0f, %.0f)"
              % (AHOGE_PIVOT[0], AHOGE_PIVOT[1],
                 AHOGE_PIVOT[0] - AHOGE_ORIGIN[0],
                 AHOGE_PIVOT[1] - AHOGE_ORIGIN[1]))
        if not (visible_idle and visible_on_blink and visible_on_expr
                and hidden_on_sleep and visible_after_wake and woke_on_expr
                and advancing and continuous and swing > 5):
            print("[自检] 失败：呆毛状态不符合预期。")
            return 1

    if pet.tail_pixmap is not None:
        pet.back_to_idle()
        t_idle = pet.tail_visible()

        t_angles = [TAIL_SWING * math.sin(math.pi * s / 4.0) for s in range(9)]
        t_swing = max(t_angles) - min(t_angles)

        pet.blink()
        t_on_blink = pet.tail_visible()
        pet.end_blink(pet._blink_token)

        t_on_expr = True
        if pet.expressions:
            pet.show_expression(pet.expressions[0])
            t_on_expr = pet.tail_visible()
            pet.back_to_idle()

        pet.sleep()
        t_hidden = not pet.tail_visible()
        pet.wake_up()
        t_after_wake = pet.tail_visible()

        pet._tail_phase = 0.0
        pet._tail_angle = 0.0
        t_samples = []
        for _ in range(6):
            pet._tick()
            t_samples.append(pet._tail_angle)
        t_steps = [abs(b - a) for a, b in zip(t_samples, t_samples[1:])]
        t_advancing = all(s > 0 for s in t_steps)

        print("[自检] 尾巴 ----------------------------------------")
        print("  尾巴层 %dx%d，贴在原图 (%d, %d) 处"
              % (pet.tail_pixmap.width(), pet.tail_pixmap.height(),
                 TAIL_ORIGIN[0], TAIL_ORIGIN[1]))
        print("  待机叠加=%s / 眨眼时仍叠加=%s / 表情时仍叠加=%s / 睡觉时隐藏=%s / 醒来恢复=%s"
              % (t_idle, t_on_blink, t_on_expr, t_hidden, t_after_wake))
        print("  摆动 %.0f°（±%.0f°，周期 %.1fs）/ 相位持续推进=%s / 单步最大变化=%.2f°"
              % (t_swing, TAIL_SWING, TAIL_PERIOD, t_advancing, max(t_steps)))
        print("  轴心（脖颈）= (%.0f, %.0f)，位于尾巴层内的 (%.0f, %.0f)"
              % (TAIL_PIVOT[0], TAIL_PIVOT[1],
                 TAIL_PIVOT[0] - TAIL_ORIGIN[0],
                 TAIL_PIVOT[1] - TAIL_ORIGIN[1]))
        if not (t_idle and t_on_blink and t_on_expr
                and t_hidden and t_after_wake and t_advancing and t_swing > 5):
            print("[自检] 失败：尾巴状态不符合预期。")
            return 1

    # 素材一致性：眨眼帧 / 表情帧里不能残留"烘焙"的部件。
    # 残留的话，独立图层会和它叠成重影；一旦图层被藏起来（换状态时），
    # 整个部件又会凭空消失 —— 两种都很难看，而且很难查。
    from PySide6.QtGui import QImage
    # 跳过"自带部件"的特写表情：它们不叠系统部件层，所以素材里那把呆毛
    # 不是"残留"，是本身就画在那儿的（见 SELF_CONTAINED_EXPRESSIONS）。
    frames_to_check = sorted(
        [p for p in buckets["blink"] + buckets["expression"]
         if asset_key(p.stem) not in SELF_CONTAINED_EXPRESSIONS])
    special_count = len(buckets["blink"] + buckets["expression"]) - len(frames_to_check)
    results = []
    for label, zone, limit, blue_only in (
            ("呆毛", AHOGE_CLEAR_ZONE, AHOGE_CLEAR_MAX, True),
            ("尾巴", TAIL_CLEAR_ZONE, TAIL_CLEAR_MAX, False)):
        zx0, zy0, zx1, zy1 = zone
        left = []
        for path in frames_to_check:
            img = QImage(str(path)).convertToFormat(QImage.Format_ARGB32)
            if img.isNull():
                continue
            hits = 0
            for y in range(zy0, min(zy1, img.height()), 2):
                for x in range(zx0, min(zx1, img.width()), 2):
                    c = img.pixelColor(x, y)
                    if c.alpha() <= 40:
                        continue
                    if not blue_only or (c.blue() - c.red()) >= 25:
                        hits += 1
            if hits > limit:
                left.append("%s(%d)" % (path.name, hits))
        results.append((label, left))

    print("[自检] 素材一致性 ----------------------------------")
    print("  已查 %d 帧（眨眼 + 表情）的呆毛区与尾巴区；另有 %d 帧是"
          "自带部件的特写表情（不叠部件层），已跳过"
          % (len(frames_to_check), special_count))
    bad = False
    for label, left in results:
        if left:
            bad = True
            print("  [!] 这些帧里还留着烘焙的%s：%s" % (label, ", ".join(left)))
        else:
            print("  %s：未发现残留 ✓" % label)
    if bad:
        print("      先跑 strip_baked_ahoge.py 清干净再打包。")
        return 1

    if pet.do_talk and pet.bubble is not None:
        from PySide6.QtCore import QRect
        print("[自检] 台词 ----------------------------------------")
        blank = [k for k, v in LINES.items() if not v or any(not s.strip() for s in v)]
        uncovered = [n for n in pet.expr_names if n not in EXPR_LINES]

        char = QRect(1400, 900, 170, 255)
        screen = QRect(0, 0, 2560, 1600)
        over, widest, tallest = [], 0, 0
        for pool in list(LINES.values()) + list(EXPR_LINES.values()):
            for text in pool:
                pet.bubble.say(text, char, screen, hold=0.0)
                geo = pet.bubble.geometry()
                widest = max(widest, geo.width())
                tallest = max(tallest, geo.height())
                if not screen.contains(geo):
                    over.append(text)

        # 贴着屏幕四角时最容易翻车：气泡会被"夹"到角色旁边甚至屏幕外
        for corner in (QRect(6, 4, 170, 255), QRect(0, 0, 170, 255),
                       QRect(2560 - 176, 4, 170, 255),
                       QRect(6, 1600 - 260, 170, 255)):
            pet.bubble.say("贴边测试：看看这句会不会跑出屏幕外面去", corner, screen,
                           hold=0.0)
            if not screen.contains(pet.bubble.geometry()):
                over.append("贴边 @ %s" % (corner,))

        pet.bubble.say("主人，我不是大肥鱼，只是有点圆。", char, screen)
        # 注意：自检跑在 offscreen 平台上，**那里没有字体引擎** ——
        # 文字会渲染成一排豆腐块，但尺寸、描边、底色全都正常。
        # 所以这里只能验"画出了东西、尺寸对得上"，字形是否正常必须另做目视验收。
        shot = pet.bubble.grab()
        shown = (pet.bubble.isVisible() and not shot.isNull()
                 and shot.width() == pet.bubble.width()
                 and shot.height() == pet.bubble.height())

        # 长句必须**真的**换行。少一个 TextWordWrap，drawText 会按一整行排版再裁掉右边，
        # 而气泡尺寸仍然是对的（量尺寸那半边带着这个标志）——
        # 只看尺寸永远发现不了，只能数"墨迹铺开了几行"。
        pet.bubble.say("白饭白饭白饭白饭白饭白饭白饭白饭白饭白饭白饭白饭", char, screen)
        image = pet.bubble.grab().toImage()
        ink_rows = []
        for row in range(image.height()):
            for col in range(0, image.width(), 2):
                pixel = image.pixelColor(col, row)
                # 只认文字色（深蓝）—— 描边是亮蓝，不会被误算成墨迹
                if (pixel.alpha() > 60 and pixel.red() < 110
                        and pixel.green() < 110):
                    ink_rows.append(row)
                    break
        ink_spread = (max(ink_rows) - min(ink_rows)) if ink_rows else 0
        wrap_ok = ink_spread > BUBBLE_FONT_PX * 1.5   # 只画一行的话高约一个字
        # 气泡被屏幕边夹住、中心不再对着角色时，尾巴也得指着她的头顶
        tail_ok = abs((pet.bubble.x() + pet.bubble._tail_x)
                      - char.center().x()) <= 1

        real_screen = pet.screen().availableGeometry()
        pet.back_to_idle()
        top_awake = pet.head_top()
        pet.sleep()                          # 睡姿尺寸不同，气泡也得照样摆对
        top_sleep = pet.head_top()
        asleep_ok = real_screen.contains(pet.bubble.geometry())
        # 睡姿是横躺的，图片上方一大半是透明留白。若气泡按"图片上沿"来贴，
        # 这两个值会相等 —— 那样气泡会飘到她头顶几十像素开外（实测差 70px）。
        anchored = top_awake < top_sleep and (top_sleep - top_awake) > 20
        spoken = pet.bubble._text
        pet.idle_talk()                       # 睡着时不该插话
        quiet = pet.bubble._text == spoken
        timer_alive = pet.talk_timer.isActive()
        pet.wake_up()

        pet.bubble.hide()
        hidden = not pet.bubble.isVisible()
        pet.close()                           # 气泡是独立窗口，得跟着一起收
        closed_with_pet = not pet.bubble.isVisible()
        # 关掉之后必须 show 回来 —— 她是后续所有断言的载体，而且"隐藏时不说台词"
        # 的守卫会挡住后面所有的 say（实测踩过：这个测试把 pet 留在隐藏态，
        # 双击断言直接红了）
        pet.show()

        print("  台词池 %d 组 / %d 句；表情专属 %d 组 / %d 句"
              % (len(LINES), sum(len(v) for v in LINES.values()),
                 len(EXPR_LINES), sum(len(v) for v in EXPR_LINES.values())))
        print("  气泡最宽 %d px（上限 %d）/ 最高 %d px / 越界 %d 句"
              % (widest, BUBBLE_MAX_W, tallest, len(over)))
        print("  显示=%s / 收起=%s / 随宠物窗口一起关掉=%s / 尾巴指向角色=%s"
              % (shown, hidden, closed_with_pet, tail_ok))
        print("  睡着时不插话=%s / 闲话定时器续期=%s / 睡姿时气泡不越界=%s"
              % (quiet, timer_alive, asleep_ok))
        print("  视觉顶端：醒姿 %d px / 睡姿 %d px（气泡贴着这个高度，不是图片上沿）=%s"
              % (top_awake, top_sleep, anchored))
        print("  空白台词=%d 组 / 缺专属台词的表情=%s" % (len(blank), uncovered or "无"))
        print("  长句自动换行=%s（墨迹铺开 %d px，单行约 %d px）"
              % (wrap_ok, ink_spread, BUBBLE_FONT_PX))
        if (blank or over or not shown or not hidden or not closed_with_pet
                or not tail_ok or not quiet or not timer_alive or not asleep_ok
                or not anchored or not wrap_ok or widest > BUBBLE_MAX_W + 1):
            print("[自检] 失败：台词气泡不符合预期。")
            return 1

    # 对话与联网这一段**全部用纯函数 + 固定样本**验，不真联网 ——
    # 自检必须能在断网环境下跑，否则它就成了"网络状态检测器"而不是"代码检测器"。
    print("[自检] 对话与联网 ----------------------------------")

    base_cfg = load_config(Path("__这个配置文件不存在__/none.json"))
    config_ok = all(k in base_cfg for k in DEFAULT_CONFIG)

    b_qwen, m_qwen, n_qwen = resolve_endpoint({"provider": "qwen"})
    b_over, m_over, _ = resolve_endpoint({"provider": "deepseek",
                                          "base_url": "http://127.0.0.1:1/v1"})
    preset_ok = (b_qwen == PROVIDER_PRESETS["qwen"][0]
                 and m_qwen == PROVIDER_PRESETS["qwen"][1] and n_qwen is True
                 and b_over == "http://127.0.0.1:1/v1"
                 and m_over == PROVIDER_PRESETS["deepseek"][1])

    key_ok = (not build_chat(None, dict(base_cfg)).ready()
              and build_chat(None, dict(base_cfg, api_key="test-key")).ready())

    # 流式 SSE：拼出来的正文必须跟原文一字不差，且注释行/空行/[DONE] 都不许混进来
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"主人"}}]}',
        ': 心跳注释行，应被忽略',
        "",
        'data: {"choices":[{"delta":{"content":"，白饭"}}]}',
        'data: {"choices":[{"delta":{}}]}',
        "data: [DONE]",
    ]
    pieces = [sse_delta(line) for line in sse_lines]
    sse_ok = (pieces[0] == "主人" and pieces[1] is None and pieces[2] is None
              and pieces[3] == "，白饭" and pieces[5] == "")

    # 有的服务端/代理不吃 stream，直接回一整块 JSON —— 这条兜底必须也在
    json_ok = (reply_from_json('{"choices":[{"message":{"content":"离线也能读"}}]}')
               == "离线也能读"
               and reply_from_json("这不是 json") == ""
               and reply_from_json("{}") == "")

    # 桌面气泡只显示预览：太长要截断，而且必须**说清楚**剩下的在聊天窗口里
    long_text = "这是一句比较长的话，用来验证截断。" * 20
    cut = bubble_preview(long_text)
    preview_ok = (bubble_preview("主人晚上好。") == "主人晚上好。"
                  and bubble_preview("") == ""
                  and len(cut) <= CHAT_BUBBLE_CHARS + 16
                  and cut.startswith(long_text[:20])
                  and "聊天窗口" in cut)

    trigger_ok = (wants_search("帮我搜一下鲸鱼娘")
                  and wants_search("今天天气怎么样？")
                  and wants_search("现在 DeepSeek 多少钱")
                  and not wants_search("我今天吃了白饭")   # 有"今天"但没疑问语气 → 不搜
                  and not wants_search(""))
    query_ok = (search_query("帮我搜一下鲸鱼娘的梗") == "鲸鱼娘的梗"
                and search_query("搜一下") == "搜一下")   # 摘空了要退回原句，不能变空

    # 待机闲话不许顶掉刚收到的回复 —— 把"气泡还开着、装的正是那句"这条路钉住
    pet._chat_last = "刚收到的回复"
    pet.say("刚收到的回复", hold=0)
    pet.idle_talk()
    protect_ok = pet.chatting() and pet.bubble.text() == "刚收到的回复"
    pet._chat_last = ""
    pet.bubble.hide()

    # 宽气泡参数必须能从宠物透传到气泡 —— 漏了这层，聊天回复一走就 TypeError
    pet.say("宽气泡参数透传测试", max_w=BUBBLE_MAX_W_CHAT)
    wide_ok = (pet.bubble._max_w == BUBBLE_MAX_W_CHAT and pet.bubble.width() > 0)
    pet.say("普通台词应该回落常规宽度")
    wide_ok = wide_ok and pet.bubble._max_w == BUBBLE_MAX_W

    # --ask 的启动排程也必须真的排得上 —— 它曾因 main() 里漏 import QTimer 而静默失效
    try:
        pet.schedule_ask("自检用的问题", delay=0.05)
        ask_ok = True
    except Exception:
        ask_ok = False

    # 心情标记：剥得干净、认得出、落得对 —— 三层缺一不可
    t1, m1 = strip_mood_tag("我不是大肥鱼……只是有点圆。[mood:shy]")
    t2, m2 = strip_mood_tag("普通回复没有标记")
    t3, m3 = strip_mood_tag("名字写错了 [mood:whatever]")
    t4, m4 = strip_mood_tag("全角冒号 [mood：sleep]")
    t5, m5 = strip_mood_tag("平静的回复 [mood:idle]")      # idle 标记也要剥掉
    mood_ok = (t1 == "我不是大肥鱼……只是有点圆。" and m1 == "shy"
               and t2 == "普通回复没有标记" and m2 is None
               and t5 == "平静的回复" and m5 == "idle"
               and t3 == "名字写错了" and m3 == "idle"     # 不认识的值宁可不动作
               and t4 == "全角冒号" and m4 == "sleep"
               and hide_mood_fragment("好[mo") == "好"      # 打字机打到一半的
               and hide_mood_fragment("好的[mood:angry]") == "好的")

    # 心情落到画面：生气 → 表情保持；idle → 不动画面；sleep → 真的趴下
    mood_act_ok = False
    try:
        pet.back_to_idle()
        pet.apply_mood("angry")
        angry_shown = pet.showing_expression()
        pet.apply_mood("idle")
        idle_kept = pet.showing_expression()     # 平静回复不打断当前表情
        pet.apply_mood("sleep")
        asleep_now = pet.sleeping()
        pet.wake_up(quiet=True)
        mood_act_ok = angry_shown and idle_kept and asleep_now
    except Exception:
        mood_act_ok = False

    # ---- 主人文档里的四条要求 ----
    import tempfile
    from PySide6.QtCore import QEvent, QPointF, Qt as QtFlags
    from PySide6.QtGui import QMouseEvent

    press_ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(20.0, 20.0),
                           QPointF(20.0, 20.0), QtFlags.LeftButton,
                           QtFlags.LeftButton, QtFlags.NoModifier)
    release_ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(20.0, 20.0),
                             QPointF(20.0, 20.0), QtFlags.LeftButton,
                             QtFlags.LeftButton, QtFlags.NoModifier)

    # 要求 2：单击只说话，**不换表情**
    pet.back_to_idle()
    pm_before = pet.pixmap
    pet.mousePressEvent(press_ev)
    pet.mouseReleaseEvent(release_ev)
    click_no_expr_ok = (pet.pixmap is pm_before and bool(pet.bubble.text()))
    pet.bubble.hide()

    # 要求 3：睡觉时左键点不醒，只嘟囔一句继续睡
    pet.apply_mood("sleep")
    pet.mousePressEvent(press_ev)
    pet.mouseReleaseEvent(release_ev)
    sleep_click_ok = (pet.sleeping()
                      and pet.bubble.text() in LINES["sleep_grumble"])
    pet.wake_up(quiet=True)
    pet.bubble.hide()

    # 要求 1：时钟窗口建得出来、**叉号真的能关**（漏接槽的教训刚过几天）
    clock_ok = False
    try:
        clock = build_clock_window(pet)
        clock.show()
        was_visible = clock.isVisible()
        clock.close_btn.click()
        clock_ok = was_visible and not clock.isVisible()
        clock.close()
    except Exception:
        clock_ok = False

    # 要求 4：对话日志三天清理、长期记忆只增不删、启动上下文注入
    log_ok = mem_ctx_ok = False
    try:
        with tempfile.TemporaryDirectory() as td2:
            cfgf = os.path.join(td2, CONFIG_NAME)
            Path(cfgf).write_text("{}", encoding="utf-8")
            stale = (datetime.now() - timedelta(days=4)).strftime(TIME_FMT)
            with open(data_dir(cfgf) / CHAT_LOG_NAME, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": stale, "who": "主人", "text": "四天前的对话"},
                                    ensure_ascii=False) + "\n")
            append_chat_log(cfgf, "鲸鲸", "刚刚说的话")
            removed = prune_chat_log(cfgf)
            entries = _read_jsonl(data_dir(cfgf) / CHAT_LOG_NAME)
            log_ok = (removed == 1 and len(entries) == 1
                      and entries[0]["text"] == "刚刚说的话")
            append_memory(cfgf, "主人是CS专业")
            append_memory(cfgf, "喜欢短回答")
            ctx = build_memory_context(cfgf)
            mem_ctx_ok = (ctx is not None and "主人是CS专业" in ctx
                          and "刚刚说的话" in ctx
                          and "四天前的对话" not in ctx)   # 过期的不再注入
    except Exception:
        log_ok = mem_ctx_ok = False

    # memory 标记：剥得掉（连在 mood 后面也行）、半截藏得住
    tm, mn = strip_memory_tag("好的我记住了[mood:idle][memory:主人喜欢短回答]")
    memory_tag_ok = (mn == "主人喜欢短回答"
                     and tm == "好的我记住了[mood:idle]"
                     and strip_memory_tag("没有标记")[1] is None
                     and hide_mood_fragment("嗯[mem") == "嗯"
                     and hide_mood_fragment("好[memory:长]") == "好")

    # 界面改 key：原子写、保留其他配置、热生效、能现补客户端
    import tempfile
    key_ok2 = key_rebuild_ok = False
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg_file = os.path.join(td, CONFIG_NAME)
            # 先埋一份"主人手调过"的配置，换 key 后其他字段必须原样
            Path(cfg_file).write_text(json.dumps(
                {"provider": "deepseek", "model": "deepseek-chat",
                 "api_key": "sk-old", "search": "off"}, ensure_ascii=False),
                encoding="utf-8")
            # v60 起 save_api_key 返回的是 bool（写成功与否），不再是那份配置；
            # 密钥落盘也变成**密文**（DPAPI）。所以这里同时验三件事：
            # ① 写成功；② 运行时读回来是明文（load_config 负责解密）；
            # ③ 磁盘上没有明文 —— 这条是 v60 新增的安全断言。
            wrote = save_api_key("sk-new-999", cfg_file)
            raw_text = Path(cfg_file).read_text(encoding="utf-8")
            back = json.loads(raw_text)
            runtime = load_config(cfg_file)
            dpapi_ok = _dpapi(b"probe") is not None      # 本机 DPAPI 可用吗
            key_ok2 = (wrote is True
                       and runtime["api_key"] == "sk-new-999"
                       and back["provider"] == "deepseek"       # 别把别的配置冲掉
                       and back["model"] == "deepseek-chat"
                       and not list(Path(td).glob("*.tmp")))    # 原子写不留残渣
            if dpapi_ok:
                # DPAPI 可用时：文件里必须是密文，明文一个字都不能留
                key_ok2 = (key_ok2 and "enc:v1:" in raw_text
                           and "sk-new-999" not in raw_text)
            else:
                # 不可用时是**已知降级路径**（_protect_secret 原样返回），
                # 只要求功能照样能用，不装成"加密过了"
                key_ok2 = key_ok2 and back.get("api_key") == "sk-new-999"
            # 热生效：cfg 改了下一次请求就带新 key（Authorization 每次 ask 现取）
            pet.set_api_key("sk-hot-123", path=cfg_file)
            hot_ok = pet.chat.cfg["api_key"] == "sk-hot-123" and pet.chat.ready()
            # 补建路径：客户端被拆掉后，set_api_key 要能现建一套并接好信号
            pet.chat, pet.search = None, None
            pet.set_api_key("sk-rebuilt", path=cfg_file)
            rebuilt = (pet.chat is not None and pet.chat.ready()
                       and pet.chat.cfg["api_key"] == "sk-rebuilt")
            probe = []
            pet.chat.chunk.connect(probe.append)     # 信号必须接回（双保险）
            pet.chat.chunk.emit("信号通")
            key_rebuild_ok = rebuilt and probe == ["信号通"]
    except Exception:
        key_ok2 = key_rebuild_ok = False

    # 聊天窗口：建得出来、消息进得去、清得干净、确实是无边框的
    from PySide6.QtCore import QEvent, QPointF, Qt as QtFlags
    from PySide6.QtGui import QMouseEvent

    window_ok = False
    close_btn_ok = False
    try:
        # 走**新壳**（聊天/派活共用的那个窗口）—— 旧 build_chat_window 已经不用了，
        # 但门面接口逐名对齐，所以下面这些调用一个字都不用改。
        shell = pet._shell_window()
        window = shell.chat
        window.add_me("主人说的话")
        window.add_her("她说的话")
        window.begin_reply()
        window.stream("流")
        window.stream("式")
        window.end_reply()
        window.set_busy(True)
        window.set_busy(False)
        window.fail_reply("出错了")
        filled = window._rows_layout.count()      # 3 条消息 + 1 个弹簧
        window.clear()
        window_ok = (filled >= 4 and window._rows_layout.count() == 1
                     and bool(shell.windowFlags() & QtFlags.FramelessWindowHint)
                     and getattr(shell, "title", None) is not None
                     and shell.title.text() == "鲸鲸"       # 改名后别再退回旧名
                     and "爱莉" not in shell.subtitle.text())
        # 右上角叉号必须真的能关掉窗口 —— 曾经它只画了样子、没接槽函数，
        # 点上去毫无反应（主人报的 bug）。这里 show() 之后直接 click() 一次看可见性，
        # 比"读一眼代码里有没有 connect"可靠得多：按钮漏接槽在代码上完全看不出异常。
        shell.show()
        was_visible = shell.isVisible()
        shell.close_btn.click()
        close_btn_ok = was_visible and not shell.isVisible()
        shell.close()
    except Exception:
        window_ok = False
        close_btn_ok = False

    # 设置窗口点「关闭」不能带崩全局 —— 它曾是程序里**唯一的普通窗口**（其他全是
    # Qt.Tool），关掉它就触发"最后一个窗口关闭 → 退出程序"，把整个桌宠带走
    # （主人实测踩到）。断言两层：全局开关关掉 + 设置窗口归入 Tool 家族。
    # 2026-10-11 起 API key 搬进了设置窗口（右键菜单里那项已撤），所以这里
    # 验的是设置窗口 —— 不变量（关掉它不能退出程序）一个字没变。
    keydlg_ok = False
    try:
        from PySide6.QtWidgets import QApplication as _QApp
        pet.open_settings("chat")
        dlg = pet._settings_win
        flag_ok = bool(dlg.windowFlags() & QtFlags.Tool)
        dlg.close()
        keydlg_ok = flag_ok and _QApp.instance().quitOnLastWindowClosed() is False
    except Exception:
        keydlg_ok = False

    # 「清空聊天框」只擦界面 —— 记忆文件、日志、模型上下文都必须原样
    clear_ok = False
    try:
        with tempfile.TemporaryDirectory() as td3:
            cfgf = os.path.join(td3, CONFIG_NAME)
            Path(cfgf).write_text("{}", encoding="utf-8")
            append_memory(cfgf, "测试记忆条目")
            saved_mem = _read_jsonl(data_dir(cfgf) / MEMORY_LOG_NAME)
            saved_ctx = pet.chat.history[:] if pet.chat else []
            if pet.chat_window is None:
                pet._shell_window()
            pet.chat_window.add_me("要被擦掉的话")
            pet.clear_chat_display()
            clear_ok = (_read_jsonl(data_dir(cfgf) / MEMORY_LOG_NAME) == saved_mem
                        and pet.chat_window._rows_layout.count() == 1
                        and pet.chat.history == saved_ctx)
            pet.chat_window.close()
            pet.chat_window = None
    except Exception:
        clear_ok = False

    # 双击必须真的走到"打开聊天窗口"这条路上；没配 key 时该给提示而不是崩
    dblclick_ok = False
    try:
        press = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(20.0, 20.0),
                            QPointF(20.0, 20.0), QtFlags.LeftButton,
                            QtFlags.LeftButton, QtFlags.NoModifier)
        # 这个测试必须显式回到"还没配 key"的状态 —— 它排在本段靠后，
        # 而上面的 key 测试会把 pet.chat 重建并 ready()（实测踩过顺序坑）
        saved_chat, saved_search = pet.chat, pet.search
        pet.chat, pet.search = None, None
        pet.mouseDoubleClickEvent(press)
        dblclick_ok = pet.bubble.text().startswith("我还没拿到钥匙")
        if getattr(pet, "_key_dialog", None) is not None:
            pet._key_dialog.close()
            pet._key_dialog = None
        pet.chat, pet.search = saved_chat, saved_search

        # 双击**饭碗**不该弹聊天窗口（主人实测报的：想在碗上连点几下喂她，
        # 第二次点被 Qt 当成双击、窗口蹦出来了）。同时补的那一口不能少。
        pet.back_to_idle()
        pet._rice_left = BOWL_RICE_CLICKS
        saved_win = pet.chat_window
        pet.chat_window = None
        bowl_pt = QPointF(pet.bowl_rect().center())
        dbl_bowl = QMouseEvent(QEvent.MouseButtonDblClick, bowl_pt, bowl_pt,
                               QtFlags.LeftButton, QtFlags.LeftButton,
                               QtFlags.NoModifier)
        rice_before = pet._rice_left
        pet.mouseDoubleClickEvent(dbl_bowl)
        dblclick_ok = (dblclick_ok
                       and pet._rice_left == rice_before - 1   # 当成"又吃一口"
                       and pet.chat_window is None)             # 窗口没被打开
        pet.chat_window = saved_win
    except Exception:
        dblclick_ok = False

    bing_fixture = (
        '<ol id="b_results">'
        '<li class="b_algo"><h2><a href="https://a.example">'
        '<strong>标题一</strong></a></h2>'
        '<p class="b_lineclamp2">摘要&nbsp;甲</p></li>'
        '<li class="b_algo"><h2><a href="https://b.example">标题二</a></h2>'
        '<p class="b_lineclamp3">摘要乙</p></li>'
        "</ol>")
    hits = parse_bing_results(bing_fixture)
    digest = format_search_digest(hits)
    bing_ok = (len(hits) == 2 and hits[0][0] == "标题一" and hits[0][1] == "摘要 甲"
               and hits[1][0] == "标题二"
               and digest.startswith(SEARCH_HEADER) and "标题一" in digest
               and parse_bing_results("<html>什么都没有</html>") == []
               and format_search_digest([]) == "")

    # 四类网络错误必须是四句**不同**的台词 —— 都落回同一句就等于没做映射
    error_lines = {
        describe_net_error("AuthenticationRequiredError", 401),
        describe_net_error("ContentAccessDenied", 403),
        describe_net_error("OperationCanceledError", None),
        describe_net_error("HostNotFoundError", None),
    }
    err_ok = len(error_lines) == 4 and all(error_lines)

    print("  配置：缺失时退回默认值=%s / provider 预设解析=%s / 无 key 自动关闭=%s"
          % (config_ok, preset_ok, key_ok))
    print("  SSE 流式解析=%s / 非流式 JSON 兜底=%s / 回复预览截断=%s"
          % (sse_ok, json_ok, preview_ok))
    print("  联网触发词=%s / 搜索词清洗=%s / Bing 结果解析=%s / 错误台词四类互不相同=%s"
          % (trigger_ok, query_ok, bing_ok, err_ok))
    print("  待机闲话不顶掉刚收到的回复=%s / --ask 排程可用=%s / 宽气泡参数透传=%s"
          % (protect_ok, ask_ok, wide_ok))
    print("  心情标记剥取=%s / 心情落到画面=%s" % (mood_ok, mood_act_ok))
    print("  文档要求：单击不换表情=%s / 睡觉点不醒=%s / 时钟窗叉号=%s"
          % (click_no_expr_ok, sleep_click_ok, clock_ok))
    print("  文档要求：对话日志三天清理=%s / 长期记忆注入=%s / 记忆标记剥取=%s"
          % (log_ok, mem_ctx_ok, memory_tag_ok))
    print("  界面改 key：落盘且不冲掉其他配置=%s / 热生效+现补客户端=%s"
          % (key_ok2, key_rebuild_ok))
    print("  聊天窗口=%s / 双击入口=%s / 叉号能关窗=%s / 钥匙框不带走全局=%s"
          % (window_ok, dblclick_ok, close_btn_ok, keydlg_ok))
    print("  清空聊天框不碰记忆=%s" % clear_ok)
    # 查余额：解析层六种情况 + 真 DeepSeek 端到端
    ok1, t1 = parse_balance_reply(
        b'{"is_available":true,"balance_infos":[{"currency":"CNY",'
        b'"total_balance":"58.19","granted_balance":"0.00"}]}', 200, "NoError")
    ok2, t2 = parse_balance_reply(
        b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"10.00",'
        b'"granted_balance":"3.00"},{"currency":"USD","total_balance":"2.50",'
        b'"granted_balance":"0.00"}]}', 200, "NoError")
    ok3, t3 = parse_balance_reply(b"", 401, "NoError")
    ok4, t4 = parse_balance_reply(b"", 402, "NoError")
    ok5, t5 = parse_balance_reply(b"not json", 200, "NoError")
    ok6, t6 = parse_balance_reply(b"{}", 200, "NoError")
    balance_ok = (ok1 and "58.19 CNY" in t1
                  and ok2 and "10.00 CNY" in t2 and "含赠送 3.00" in t2
                  and "2.50 USD" in t2
                  and not ok3 and "key" in t3
                  and not ok4 and "余额不足" in t4
                  and not ok5 and not ok6 and "没说余额" in t6)
    print("  余额解析六情况=%s" % balance_ok)
    # 托盘：图标常驻 + 隐藏/打开/退出闭环
    tray_ok = hide_ok = False
    try:
        tray_actions = [a.text() for a in pet.tray.contextMenu().actions()
                        if a.text() in ("打开", "退出")]
        tray_ok = (pet.tray is not None and pet.tray.isVisible()
                   and tray_actions == ["打开", "退出"]
                   and not pet.tray.icon().isNull())
        pet.hide_into_tray()
        hidden = (not pet.isVisible()
                  and pet.tray.isVisible()               # 桌宠藏了，托盘还在
                  and not pet.bubble.isVisible())        # 台词也不许留在屏幕上
        # 隐藏状态下**任何**台词都不能露面（闲话路径也要挡）
        pet.say("这句不该出现在屏幕上")
        pet.idle_talk()
        no_ghost_talk = not pet.bubble.isVisible()
        pet.show_from_tray()
        back = pet.isVisible()
        # 左键单击托盘图标也要能打开（不只是右键菜单里的「打开」）
        from PySide6.QtWidgets import QSystemTrayIcon as _Tray
        pet.hide_into_tray()
        pet._on_tray_click(_Tray.Trigger)
        click_opens = pet.isVisible()
        said = pet.bubble.text()
        # 已经在显示时再点一次：不许重复说"我回来啦"，也不能反过来藏起来
        pet._on_tray_click(_Tray.Trigger)
        click_idle_safe = pet.isVisible() and pet.bubble.text() == said
        # 右键的 Context 必须放行（菜单是 Qt 接管的，别抢）
        pet._on_tray_click(_Tray.Context)
        context_safe = pet.isVisible()
        hide_ok = (hidden and back and no_ghost_talk
                   and click_opens and click_idle_safe and context_safe)
        pet.show()
    except Exception:
        tray_ok = hide_ok = False
    print("  托盘图标常驻=%s / 托盘打开（右键菜单与左键单击）=%s" % (tray_ok, hide_ok))
    # 本地时间注入 + 定时提醒
    now_txt = now_line()
    now_ok = (str(datetime.now().year) in now_txt
              and str(datetime.now().day) in now_txt
              and "星期" in now_txt and "当前本地时间" in now_txt)

    # 各类标记**乱序**也要全剥掉（顺序不能全指望模型）
    (t_ord, m_ord, n_ord, r_ord, a_ord,
     c_ord) = strip_reply_tags(
        "好，25 分钟后叫你。[remind:25|起来走走][memory:主人容易久坐][mood:shy]")
    (t_none, m_none, n_none, r_none, a_none,
     c_none) = strip_reply_tags("没有标记的普通回复")
    # 实机场景：标记夹在句中、后面还接着感慨 —— 不锚末尾才剥得掉
    (t_mid, m_mid, n_mid, r_mid, a_mid,
     c_mid) = strip_reply_tags(
        "好嘞，半分钟后叫你喝水～[mood:happy][remind:1|喝水]（悄悄：其实是四舍五入）")
    mid_ok = (t_mid == "好嘞，半分钟后叫你喝水～（悄悄：其实是四舍五入）"
              and m_mid == "happy" and r_mid == (1.0, "喝水") and n_mid is None)
    r3 = strip_remind_tag("午休一下 [remind:30|休息]")
    r4 = strip_remind_tag("全角也行 [remind：7.5｜喝水]")
    # 取消标记：关键词可以为空（= 全都取消），**不能和"没这个标记"混为一谈**
    c_kw = strip_cancel_tag("好，划掉了 [cancel:跑步]")
    c_all = strip_cancel_tag("行，都清了 [cancel:]")
    tag_ok = (t_ord == "好，25 分钟后叫你。" and m_ord == "shy"
              and n_ord == "主人容易久坐" and r_ord == (25.0, "起来走走")
              and t_none == "没有标记的普通回复" and m_none == "idle"
              and n_none is None and r_none is None
              and r3 == ("午休一下", (30.0, "休息"))
              and r4 == ("全角也行", (7.5, "喝水"))
              and mid_ok and a_ord is None and a_none is None and a_mid is None
              and c_ord is None and c_none is None and c_mid is None
              and c_kw == ("好，划掉了", "跑步") and c_all == ("行，都清了", "")
              and hide_mood_fragment("嗯[rem") == "嗯")

    # 提醒：登记得进、到点会把藏起来的桌宠叫出来
    remind_ok = False
    try:
        before = len(pet.reminders)
        scheduled = pet.add_reminder(30, "休息")
        in_list = (len(pet.reminders) == before + 1
                   and pet.reminders[-1]["timer"].isActive())
        pet.hide_into_tray()
        was_hidden = not pet.isVisible()
        pet.fire_reminder("休息")           # 直接触发，不等定时器（自检同步跑）
        remind_ok = (scheduled and in_list and was_hidden
                     and pet.isVisible()                 # 到点自动弹出来
                     and "休息" in pet.bubble.text()
                     and len(pet.reminders) == before)   # 触发后从清单里清掉
        pet.bubble.hide()
    except Exception:
        remind_ok = False
    print("  本地时间注入=%s / 三类标记乱序剥取=%s / 定时提醒到点弹出=%s"
          % (now_ok, tag_ok, remind_ok))
    # 时长解析与"用户原话优先"的纠偏（十秒不能被进位成 1 分钟）
    dur_ok = (parse_duration("十秒后提醒我睡觉") == 10
              and parse_duration("30秒后叫我") == 30
              and parse_duration("半小时后提醒我") == 1800
              and parse_duration("一个小时后叫我") == 3600
              and parse_duration("两小时") == 7200
              and parse_duration("三十分钟后提醒我休息") == 1800
              and parse_duration("下午三点提醒我开会") is None   # 绝对时间交给模型
              and parse_duration("我去睡一会儿") is None)       # 陈述句不误判
    user_req = reminder_from_user("十秒后提醒我睡觉")
    dur_ok = dur_ok and user_req == (10.0, "睡觉")
    dur_ok = dur_ok and reminder_from_user("我三十分钟后要开会") is None  # 没"提醒我"
    dur_ok = dur_ok and reminder_from_user("提醒我喝水") is None          # 没时长
    dur_ok = dur_ok and format_seconds(10) == "10 秒"
    dur_ok = dur_ok and format_seconds(1800) == "30 分钟"
    dur_ok = dur_ok and format_seconds(5400) == "1 小时 30 分钟"
    # 纠偏：模型给 60 秒、主人说十秒 → 落成 10 秒
    pet._last_question = "十秒后提醒我睡觉"
    model_said, user_said = 60.0, parse_duration(pet._last_question)
    corrected = user_said if (model_said > user_said * 2
                              or model_said < user_said * 0.5) else model_said
    dur_ok = dur_ok and corrected == 10
    print("  时长解析与纠偏=%s" % dur_ok)
    # 取消提醒（主人的要求：取消掉的必须**真从记录里删掉**，随时可取消）
    cancel_ok = False
    try:
        with tempfile.TemporaryDirectory() as td6:
            cf6 = os.path.join(td6, CONFIG_NAME)
            Path(cf6).write_text("{}", encoding="utf-8")
            saved_cfg = pet._config_path
            pet._config_path = cf6

            # ① 本地兜底：从原话里抽关键词（模型没给标记时的保险）
            parse_ok = (cancel_from_user("取消提醒我跑步") == "跑步"
                        and cancel_from_user("把跑步的提醒删掉") == "跑步"
                        and cancel_from_user("把提醒都取消了吧") == ""
                        and cancel_from_user("提醒我跑步") is None       # 设提醒≠取消
                        and cancel_from_user("先不取消提醒") is None)    # 否定说法不动

            # ② 真删：短期（内存定时器）+ 长期（文件条目）一起清，且定时器要停
            pet.reminders = []
            pet.add_reminder_seconds(60, "跑步")
            pet.add_reminder_seconds(60, "喝水")
            pet.add_alarm(datetime.now() + timedelta(days=2), "跑步")
            before_short = len(pet.reminders)
            before_long = len(load_alarms(cf6))
            before_timers = [t["id"] for t in pet._alarm_timers]
            fired = pet.cancel_reminders("跑步")
            short_gone = len(pet.reminders) == before_short - 1
            long_gone = len(load_alarms(cf6)) == before_long - 1
            # 关键：被取消那条的精确定时器必须真的**摘掉**（_disarm_alarm 把它
            # 移出列表并 stop）—— 只删文件的话到点照样弹提醒，比不删还糟
            after_timers = [t["id"] for t in pet._alarm_timers]
            timers_ok = len(after_timers) == len(before_timers) - 1
            cancel_ok = (parse_ok and fired == 2 and short_gone and long_gone
                         and timers_ok)

            # ③ 空关键词 = 全都取消，不留尾巴
            all_gone = pet.cancel_reminders("")
            cancel_ok = (cancel_ok and all_gone == 1 and not pet.reminders
                         and not load_alarms(cf6))
            pet._config_path = saved_cfg
    except Exception:
        cancel_ok = False
    print("  取消提醒：原话解析/短期+长期真删/定时器停掉/空关键词=全清=%s"
          % cancel_ok)
    # 长期提醒：解析、持久化、跨重启、以及"对话日志三天清理不动它"
    alarm_ok = False
    try:
        # ① 标记解析（含中文"年月日"写法和全角）
        a_text, a_got = strip_alarm_tag("记下了 [alarm:2026-10-20 08:00|去跑步]")
        a2, a2_got = strip_alarm_tag("好 [alarm：2026年10月20号 08点00分｜去跑步]")
        bad = strip_alarm_tag("坏的 [alarm:2026-02-30 08:00|不存在]")[1]
        alarm_ok = (a_text == "记下了" and a_got == (datetime(2026, 10, 20, 8, 0), "去跑步")
                    and a2_got == (datetime(2026, 10, 20, 8, 0), "去跑步")
                    and bad is None)          # 2月30日解析失败 → 不登记
        with tempfile.TemporaryDirectory() as td4:
            cfgf = os.path.join(td4, CONFIG_NAME)
            Path(cfgf).write_text("{}", encoding="utf-8")
            fut = datetime.now() + timedelta(days=11)
            # ② 登记进独立文件
            aid = append_alarm(cfgf, fut, "去跑步")
            stored = load_alarms(cfgf)
            # 返回值必须是**真 id**（取消提醒靠它找定时器），不是 True
            alarm_ok = (alarm_ok and bool(aid) and len(stored) == 1
                        and aid == stored[0]["id"])
            # ③ 对话日志的"三天清理"**绝不能**碰提醒文件
            append_chat_log(cfgf, "主人", "四天前的话")
            entries = _read_jsonl(data_dir(cfgf) / CHAT_LOG_NAME)
            entries[0]["t"] = (datetime.now() - timedelta(days=4)).strftime(TIME_FMT)
            with open(data_dir(cfgf) / CHAT_LOG_NAME, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            removed = prune_chat_log(cfgf)
            alarm_ok = (alarm_ok and removed == 1
                        and len(load_alarms(cfgf)) == 1)     # 提醒还在！
            # ④ 到期才移除，且"关机期间错过"的也能捞回来
            past = datetime.now() - timedelta(hours=2)
            append_alarm(cfgf, datetime.now() + timedelta(minutes=5), "五分钟后的")
            save_alarms(cfgf, load_alarms(cfgf) + [
                {"id": "old1", "when": past, "note": "错过的那条", "created": ""}])
            due = due_alarms(cfgf)
            # 此时挂着"去跑步"（11 天后）+"五分钟后的"（5 分钟后）+"错过的那条"
            # （2 小时前）→ 到期的只有最后一条，摘掉后应剩 2 条
            alarm_ok = (alarm_ok and [d["note"] for d in due] == ["错过的那条"]
                        and len(load_alarms(cfgf)) == 2)
            # ⑤ 时间说人话
            alarm_ok = alarm_ok and format_alarm_time(datetime.now()) .startswith("今天")
            alarm_ok = alarm_ok and format_alarm_time(
                datetime.now() + timedelta(days=1)).startswith("明天")
    except Exception:
        alarm_ok = False
    print("  长期提醒：解析/持久化/三天清理不动它=%s" % alarm_ok)
    # 新表情（12 张）：素材在、台词齐、心情名注册、照常叠系统部件层，
    # 并且**身形锁死** —— 保护罩之外必须与原立绘逐字节相同
    expr_ok = False
    try:
        names_on_disk = {asset_key(p.stem) for p in buckets["expression"]}
        new_names = set(NEW_EXPRESSIONS)
        missing_asset = new_names - names_on_disk
        no_lines = [n for n in new_names if not EXPR_LINES.get(n)]
        unknown_mood = [n for n in new_names if n not in MOOD_NAMES]
        # 切到新表情 → 部件层照常叠（它们是全身构图，不是"自带部件的特写"）
        pet.back_to_idle()
        normal_parts = pet.ahoge_visible() and pet.tail_visible()
        idx = pet.expr_names.index(NEW_EXPRESSIONS[2])       # cry
        pet.show_expression(pet.expressions[idx])
        expr_parts = pet.ahoge_visible() and pet.tail_visible()
        expr_ok = (not missing_asset and not no_lines and not unknown_mood
                   and normal_parts and expr_parts
                   and not pet.showing_self_contained())
        pet.back_to_idle()
        expr_ok = expr_ok and pet.ahoge_visible() and pet.tail_visible()

        # ---- 身形锁死：与 rebuild_expr_body.py 的保护罩对齐 ----
        # 新表情是"原立绘身体 + 生成图的脸和手"合成的，保护罩之外理应
        # 一字未改。这条断言防的是退化：哪天有人又把 AI 直出的整图盖上去，
        # 身形会整体轻微重绘（鞋区曾有 45% 像素不同），这里立刻变红。
        # 用"整行切片比较"而不是逐像素循环 —— 4096 字节一次比，快两个数量级。
        LOCK_HEAD = (185, 145, 845, 800)
        LOCK_ARMS = (175, 780, 860, 1195)
        lock_x0 = min(LOCK_HEAD[0], LOCK_ARMS[0])
        lock_x1 = max(LOCK_HEAD[2], LOCK_ARMS[2])
        lock_y0 = min(LOCK_HEAD[1], LOCK_ARMS[1])
        lock_y1 = max(LOCK_HEAD[3], LOCK_ARMS[3])
        lock_ok = True
        front_path = next((p for p in buckets["front"]
                           if asset_key(p.stem) == "front"), None)
        if front_path is None:
            lock_ok = False
        else:
            ref_im = QImage(str(front_path)).convertToFormat(QImage.Format_ARGB32)
            rw, rh = ref_im.width(), ref_im.height()
            rbuf = memoryview(ref_im.bits()).cast("B")
            for path in buckets["expression"]:
                if not lock_ok or asset_key(path.stem) not in new_names:
                    continue
                im = QImage(str(path)).convertToFormat(QImage.Format_ARGB32)
                if im.size() != ref_im.size():
                    lock_ok = False
                    break
                gbuf = memoryview(im.bits()).cast("B")
                for y in range(rh):
                    row = y * rw * 4
                    spans = ([(0, rw)] if not (lock_y0 <= y <= lock_y1)
                             else [(0, lock_x0), (lock_x1 + 1, rw)])
                    for a, b in spans:
                        if a >= b:
                            continue
                        if rbuf[row + a*4:row + b*4] != gbuf[row + a*4:row + b*4]:
                            lock_ok = False
                            break
                    if not lock_ok:
                        break
        expr_ok = expr_ok and lock_ok
    except Exception:
        expr_ok = False
    print("  新表情：素材/台词/部件层/身形锁死=%s" % expr_ok)
    # 白饭（主人的要求）：脚边一碗白饭，点它跳一下 + 她变开心，
    # 连点会吃完、过一会儿自动添满。
    bowl_ok = False
    try:
        def bowl_bottom_mid(path):
            """一碗素材的 (底边中点 x, 底边 y, 宽, 高)。"""
            im = QImage(str(path)).convertToFormat(QImage.Format_ARGB32)
            w, h = im.width(), im.height()
            b = memoryview(im.bits()).cast("B")
            x0, x1, y1 = w, -1, -1
            for y in range(h):
                row = y * w * 4
                for x in range(w):
                    if b[row + x * 4 + 3] > 40:
                        if x < x0:
                            x0 = x
                        if x > x1:
                            x1 = x
                        y1 = y
            return ((x0 + x1) // 2, y1, w, h)

        full_path = next((p for p in buckets["bowl"]
                          if "empty" not in p.stem.lower()), None)
        empty_path = next((p for p in buckets["bowl"]
                           if "empty" in p.stem.lower()), None)
        # ① 两只碗必须**同画布、同锚点** —— 否则吃完换碗时碗会跳一下
        bf = bowl_bottom_mid(full_path) if full_path else None
        be = bowl_bottom_mid(empty_path) if empty_path else None
        bowl_ok = bool(bf) and bf == be

        # ② 只在醒着时画（睡姿是另一套构图）
        pet.back_to_idle()
        bowl_ok = bowl_ok and pet.bowl_visible()
        pet.sleep(quiet=True)
        bowl_ok = bowl_ok and not pet.bowl_visible()
        pet.wake_up(quiet=True)

        # ③ 点她身体**不该**喂饭；点碗**应该**喂饭（走真实 mouseReleaseEvent）
        from PySide6.QtCore import QEvent, QPointF, Qt as QtFlags
        from PySide6.QtGui import QMouseEvent

        def release_at(pos):
            ev = QMouseEvent(QEvent.MouseButtonRelease, pos, pos,
                             QtFlags.LeftButton, QtFlags.LeftButton,
                             QtFlags.NoModifier)
            pet._moved = False
            pet._dragging = False
            pet.mouseReleaseEvent(ev)

        pet.back_to_idle()
        pet._rice_left = BOWL_RICE_CLICKS
        release_at(QPointF(pet.width() / 2.0, pet.height() * 0.35))
        body_not_fed = pet._rice_left == BOWL_RICE_CLICKS

        rect = pet.bowl_rect()
        release_at(QPointF(rect.center()))
        ate_one = pet._rice_left == BOWL_RICE_CLICKS - 1
        fed_ok = (ate_one and pet._bowl_hop > 0.0            # 碗真的跳了
                  and pet.pixmap is pet.expressions[
                      pet.expr_names.index("happy")]          # 变开心
                  and bool(pet.bubble.text()))
        # ④ 画面上真的有那只碗（不是算了坐标却没画）
        img = pet.grab().toImage()
        painted = any(img.pixelColor(x, y).alpha() > 40
                      for x in range(rect.left(), rect.right() + 1, 2)
                      for y in range(rect.top(), rect.bottom() + 1, 2))

        # ⑤ 吃完换空碗 + 自动添饭
        pet._rice_left = 1
        pet.feed_bowl()
        eaten = (pet._rice_left == 0
                 and pet.bowl_pixmap() is pet.bowl_empty_pixmap
                 and pet._refill_timer.isActive())
        pet.refill_bowl()
        refilled = (pet._rice_left == BOWL_RICE_CLICKS
                    and pet.bowl_pixmap() is pet.bowl_full_pixmap)
        # ⑥ 爱心特效已按主人的要求删掉：不该再留下任何爱心相关的状态或接口。
        # 反向断言 —— 光看"画面里没有粉色"是不够的，那样会漏掉"接口还在、
        # 只是恰好没触发"的情况，代码迟早会被误改回去。
        no_hearts = (not hasattr(pet, "_hearts")
                     and not hasattr(pet, "draw_hearts")
                     and not hasattr(pet, "spawn_hearts"))

        bowl_ok = (bowl_ok and body_not_fed and fed_ok and painted
                   and eaten and refilled and no_hearts)
    except Exception:
        bowl_ok = False
    print("  白饭：素材同锚点/点碗会喂/点她不会/吃完自动添=%s" % bowl_ok)

    # 精力 / 饱食度（主人的要求）：衰减、自动睡、睡饱自动醒、沉睡叫不醒、
    # 饿极偷吃、状态跨重启。这里全部用**直接推进时间**来测 ——
    # 不依赖真实秒表，所以定得住（不会因为机器慢就偶发红）。
    energy_ok = False
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td5:
            cfg5 = os.path.join(td5, CONFIG_NAME)
            pet._config_path = cfg5
            pet._state_ready = True      # 模拟 main 调过 load_state()，允许写盘
            pet._energy, pet._satiety = ENERGY_MAX, SATIETY_MAX
            pet.back_to_idle()

            # ① 清醒：每分钟精力 -1、饱食 -2
            pet.advance_tiredness(60.0)
            awake_drop = (abs(pet._energy - (ENERGY_MAX - 1.0)) < 1e-6
                          and abs(pet._satiety - (SATIETY_MAX - 2.0)) < 1e-6)

            # ② 精力耗尽 → 当场睡着
            pet._energy = 0.4
            pet.advance_tiredness(60.0)
            auto_sleep = pet.sleeping()

            # ③ 睡着：每分钟 +20；睡满 200 **自己**醒（不用谁来叫）
            pet._energy = 100.0
            pet.advance_tiredness(60.0)
            recover = abs(pet._energy - 120.0) < 1e-6
            pet._energy = ENERGY_MAX - 1.0
            pet.advance_tiredness(60.0)
            auto_wake = (not pet.sleeping()) and pet._energy == ENERGY_MAX

            # ④ 沉睡（<= 60）：叫不醒、也换不了表情（换来换去等于把她弄醒）
            pet._energy = 30.0
            pet.sleep(quiet=True)
            refused = (pet.wake_up() is False and pet.sleeping())
            pet.set_expression("happy")
            no_expr = pet.pixmap is pet.sleep_pixmap

            # ⑤ 沉睡时问问题：含糊应付、不叫醒、不扣精力、压根不发给模型
            class _FakeChat:
                """只用来数"有没有真的请求模型" —— 沉睡时必须是 0 次。"""
                def __init__(self):
                    self.asked = 0

                def ready(self):
                    return True

                def busy(self):
                    return False

                def ask(self, *a, **k):
                    self.asked += 1
                    return True

            fake = _FakeChat()
            had_chat, pet.chat = pet.chat, fake
            energy_before = pet._energy
            pet.start_chat("主人你在吗")
            vague = (pet.sleeping() and pet._energy == energy_before
                     and fake.asked == 0 and bool(pet.bubble.text()))
            pet.chat = had_chat

            # ⑥ 精力回到 60 以上，就能像原来那样叫醒
            pet._energy = DEEP_SLEEP_ENERGY + 1.0
            woke = pet.wake_up(quiet=True) and not pet.sleeping()

            # ⑦ 吃空一碗：精力 -5 / 饱食 +20
            pet._energy, pet._satiety = 150.0, 100.0
            pet.eat_one_bowl()
            meal = (pet._energy == 145.0 and pet._satiety == 120.0)

            # ⑧ 空碗被点**不再结算**（以前会把 rice_left 减成负数，
            #    然后被"吃空"分支反复当成又吃完一碗 —— 能刷饱食度）
            pet._rice_left = 0
            pet._energy, pet._satiety = 150.0, 100.0
            pet.feed_bowl()
            empty_safe = (pet._energy == 150.0 and pet._satiety == 100.0)

            # ⑨ 饱食 <= 20 → 连偷吃 3 碗（+60 饱食 / -15 精力）
            pet.back_to_idle()
            pet._energy, pet._satiety = 200.0, 15.0
            pet.start_stealing()
            for _ in range(STEAL_MEALS):
                pet._steal_bite()
            steal = (abs(pet._satiety - 75.0) < 1e-6
                     and abs(pet._energy - 185.0) < 1e-6
                     and pet._steal_left == 0)

            # ⑩ 状态文件往返 + 被手改坏的越界值要夹紧
            pet._energy, pet._satiety = 123.0, 45.0
            saved = pet.save_state()
            pet._energy, pet._satiety = 0.0, 0.0
            pet.load_state()
            roundtrip = (saved and pet._energy == 123.0
                         and pet._satiety == 45.0)
            _state_path(cfg5).write_text('{"energy": 9999, "satiety": -5}',
                                         encoding="utf-8")
            pet.load_state()
            clamped = (pet._energy == ENERGY_MAX and pet._satiety == 0.0)

            # ⑪ 能量条：存在、且浮在宠物窗口**上方**（自检里是强制打开的）
            pet.place_at(400, 300)
            pet.bars.follow()
            bar_ok = (pet.bars is not None and pet.bars.width() > 100
                      and pet.bars.height() >= BAR_H * 2
                      and pet.bars.y() < pet.y())
            # 护栏：**默认必须是关的**（主人 2026-10-09 验收后要求隐藏）。
            # 哪天手滑把 SHOW_ENERGY_BARS 改回 True，这条会立刻红。
            bar_hidden_by_default = SHOW_ENERGY_BARS is False

            # ⑫ 精力 < 20 → 自动换上"眼困"（主人的要求）
            pet.back_to_idle()
            pet._energy = DROWSY_ENERGY - 1.0
            pet.apply_tired_face()
            drowsy_on = pet.showing_named(DROWSY_EXPR)

            # ⑬ 边界：正好 20 不算（规则是"小于 20"）
            pet._energy = DROWSY_ENERGY
            pet.apply_tired_face()
            drowsy_edge = not pet.showing_named(DROWSY_EXPR)

            # ⑭ 睡着时**不**眼困：保持睡姿（主人明确要求）
            pet._energy = DROWSY_ENERGY - 5.0
            pet.sleep(quiet=True)
            pet.apply_tired_face()
            sleep_kept = (pet.sleeping()
                          and not pet.showing_named(DROWSY_EXPR))

            # ⑮ 精力缓过来 → 放下眼困，回待机
            pet._energy = 120.0
            pet.wake_up(quiet=True)
            pet._energy = DROWSY_ENERGY - 1.0
            pet.apply_tired_face()
            pet._energy = 120.0
            pet.apply_tired_face()
            recovered = (not pet.showing_named(DROWSY_EXPR)
                         and not pet.sleeping())

            # ⑯ 偷吃台词分池：睡得着就用"梦话"那套，醒着用"心虚"那套
            pet.back_to_idle()
            pet._steal_left = 1
            pet._steal_bite()
            awake_line = pet.bubble.text() in LINES["steal"]
            pet._energy, pet._satiety = 20.0, 19.0
            pet.sleep(quiet=True)
            pet._steal_left = 1
            pet._steal_bite()
            sleep_line = pet.bubble.text() in LINES["steal_sleep"]
            pet._energy = 120.0
            pet.wake_up(quiet=True)

            # ⑰ 沉睡时**任何**换画面的入口都不许生效。
            #    主人实测报过：右键菜单「做个表情」原先直接拿 pixmap 调
            #    show_expression，绕过了 set_expression 的守卫，点一下就醒了。
            #    这里**拿真实菜单对象逐项 trigger**，而不是只测单个函数 ——
            #    以后往菜单里加东西，这条断言会自动把它一起带上。
            def menu_leaves(menu):
                """把菜单（含子菜单）摊平成"能点的叶子项"。"""
                out = []
                for act in menu.actions():
                    if act.menu() is not None:
                        out.extend(menu_leaves(act.menu()))
                    elif act.isEnabled() and act.text():
                        out.append(act)
                return out

            # 只点**睡眠相关**的那几项：另外几项会弹对话框 / 开窗口，自检里会卡住
            # （「做个表情」子菜单那 16 项也走这条判定被点到，所以点到的项数够多）
            SAFE = ("回到待机", "叫醒", "去睡觉")
            pet._energy = 30.0
            pet.sleep(quiet=True)
            clicked = []
            for act in menu_leaves(pet.build_menu()):
                # 用**子串**匹配，不用相等：菜单项现在带 emoji 和值摘要
                # （"🍚 回到待机"、"🔊 音效…（开 · 70%）"），相等匹配会全部漏掉，
                # 于是"点遍菜单她还睡着"这条断言会假通过（点 0 项也算过）。
                if any(k in act.text() for k in SAFE) or act.text() in pet.expr_names:
                    act.trigger()
                    clicked.append(act.text())
            # 要求点到的项要够多：菜单万一空了，"全都点过且没醒"是假通过
            blocked_menu = pet.sleeping() and len(clicked) >= 5

            # 对照：精力回到 60 以上，菜单换了表情要立刻生效
            pet._energy = 120.0
            pet.wake_up(quiet=True)
            for act in menu_leaves(pet.build_menu()):
                if act.text() in pet.expr_names:
                    act.trigger()
                    break
            awake_menu_ok = pet.showing_expression()

            guard_ok = (blocked_menu and awake_menu_ok)

            # ⑱ 睡/醒要能跨重启恢复（主人报的 bug：深睡到一半关掉程序、再打开
            #    就变成醒着，低精力"叫不醒"那套规则全部失效）。
            #    "当时是不是睡着的"重启后用画面推不出来 —— 精力 <= 60 既可能是
            #    睡着的深睡，也可能是"醒着但快没电了"（主人测过 5/21 那种开局），
            #    所以这是**跨进程的事实**，必须持久化。
            pet._energy, pet._satiety = 30.0, 50.0
            pet.sleep(quiet=True)            # 顺带验证：sleep() 会立刻落盘
            sleep_saved = load_pet_state(cfg5)[2] is True
            # ① 睡着时退出 → 再次启动的读取结果必须还是"睡着"
            restore_sleep = load_pet_state(cfg5)[2] is True
            # ② 对照组：同样精力低，但她是**醒着**退出的 → 不能被误判成深睡
            pet._energy, pet._satiety = 5.0, 21.0
            pet.pixmap = pet.frames[pet.index]   # 直接摆成站姿（绕开守卫）
            pet.save_state()
            awake_low = (not pet.sleeping()
                         and load_pet_state(cfg5)[2] is False)
            # ③ 旧版状态文件（没有 sleeping 字段）→ 当"醒着"处理，不能崩
            _state_path(cfg5).write_text('{"energy": 42, "satiety": 7}',
                                         encoding="utf-8")
            old_compat = load_pet_state(cfg5) == (42.0, 7.0, False)
            sleep_restore_ok = (sleep_saved and restore_sleep and awake_low
                                and old_compat)

            energy_ok = (awake_drop and auto_sleep and recover and auto_wake
                         and refused and no_expr and vague and woke and meal
                         and empty_safe and steal and roundtrip and clamped
                         and bar_ok and bar_hidden_by_default
                         and drowsy_on and drowsy_edge and sleep_kept
                         and recovered and awake_line and sleep_line and guard_ok
                         and sleep_restore_ok)
    except Exception:
        energy_ok = False
    print("  精力/饱食：清醒衰减/耗尽自动睡/睡满自醒/沉睡叫不醒（含糊应声、"
          "不扣精力、不发请求）/吃碗结算/空碗不刷/饿极偷吃三碗/跨重启+越界夹紧"
          "/能量条在头顶=%s" % energy_ok)
    print("  低精力表现：<20 自动眼困/正好20 不触发/睡着时不眼困/缓过来回待机"
          "/偷吃台词分睡醒两套=%s"
          % (drowsy_on and drowsy_edge and sleep_kept and recovered
             and awake_line and sleep_line))
    print("  沉睡守卫：真实菜单逐项点过（16 个表情/回到待机/叫醒）仍保持睡姿=%s"
          % guard_ok)
    print("  睡姿跨重启：睡时退出→读回仍是睡/醒着低精力不会误判/旧文件兼容=%s"
          % sleep_restore_ok)
    # 可选模块的可用性**必须在真包里验一次**：打包漏了依赖时功能会静默消失，
    # 而 GUI 模式下没有控制台、print 是空操作 —— 只有自检这条路能把它照出来。
    print("  可选模块：飞书=%s 远程=%s 派活=%s 任务对话=%s"
          % ("就绪" if FEISHU_AVAILABLE else
             ("不可用(%s)" % (_pet_feishu.missing_reason()
                              if _pet_feishu else "没加载")),
             "就绪" if REMOTE_AVAILABLE else "没加载",
             "就绪" if HARNESS_AVAILABLE else "没加载",
             "就绪" if SESSION_LOG_AVAILABLE else "没加载"))

    # 对外三条通道的**桥**能不能用（2026-10-10 深夜补）
    #
    # 为什么单列这一段：自检原来只验到"模块 import 得进来"（上面那行"可选模块=就绪"），
    # 但**模块 import 得来 ≠ 用到它的那段代码写得对**。拆分时就是这样漏的：
    # `_session_log / _pet_remote / _pet_feishu` 是在 pet_constants 的 try 块里
    # 绑定的私有名，`import *` 带不走，用到它们的模块漏了显式导入 —— 运行到那行
    # 才 NameError，还被 `except Exception` 吞掉，表现成"工作台看不到对话 /
    # 飞书和远程打不开"。这一段的每一条断言都直接碰那段代码，专防这类静默失灵。
    bridge_ok = False
    alias_ok = workbench_ok = remote_ok = feishu_ok = False
    try:
        import importlib
        import pet_bridges as _pb
        import pet_mixin_link as _pl

        # ① 模块完整性：可选模块的私有别名必须真的在（不是"import 得到"就算）
        alias_missing = []
        for mod in (_pb, _pl):
            for name in ("_session_log", "_pet_remote", "_pet_feishu"):
                if not hasattr(mod, name):
                    alias_missing.append("%s.%s" % (mod.__name__, name))
        alias_ok = not alias_missing

        # ② 工作台列表：磁盘上有几段对话，列表里就该有几段（+1 是"新对话"）
        #    这里用**新壳**（聊天/派活合一的那个窗口）来验 —— 旧 build_harness_console
        #    已经不再被程序使用，门面接口一致，所以断言口径不变。
        shell = pet._shell_window()
        console = shell.work
        console.refresh_sessions()
        want = _session_log.list_sessions() if SESSION_LOG_AVAILABLE else []
        workbench_ok = (console.listw.count() == 1 + len(console._rows)
                        and (not want or len(console._rows) >= 1))
        # 两个模式的一键切换：栏底按钮文字要跟着模式变，内容页要跟着换
        shell.set_mode("chat")
        chat_side_ok = "工作模式" in shell.mode_btn.text()
        shell.set_mode("work")
        work_side_ok = "聊天模式" in shell.mode_btn.text()
        if not (chat_side_ok and work_side_ok):
            workbench_ok = False
        shell.set_mode("chat")            # 别把窗口留在工作模式（后面还要用）

        # ③ 远程桥：真起一次再停（端口用空闲的，别占主人配置里那个）
        probe = __import__("socket").socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        remote = build_remote(pet, lambda: {"energy": 1.0}, None, None)
        remote_ok = bool(remote.start("t" * 16, free_port, "127.0.0.1", False))
        remote.stop()

        # ④ 飞书桥：能构造出来（**不连**，只验证这段代码不 NameError）
        feishu = build_feishu(pet)
        feishu_ok = feishu.status()["state"] == "idle"

        bridge_ok = alias_ok and workbench_ok and remote_ok and feishu_ok
        if not bridge_ok:
            print("      ↳ 明细：别名齐全=%s 工作台列表=%s 远程起停=%s 飞书桥=%s%s"
                  % (alias_ok, workbench_ok, remote_ok, feishu_ok,
                     ("  缺：" + ", ".join(alias_missing)) if alias_missing else ""))
    except Exception as err:
        # 这里**要出声**：这一段的失败方式就是"某个名字没导入"，
        # 静默成 False 的话，下次还得靠人肉二分
        print("      ↳ 抛异常：%s: %s" % (type(err).__name__, err))
        bridge_ok = False
    print("  对外通道的桥：私有别名齐全 / 工作台列表有内容 / 远程能起停 / "
          "飞书桥能构造=%s" % bridge_ok)

    # 记账 / 音效 / 新面板（2026-10-11 加"小鲸鱼记账 + 音效"时补）
    #
    # 这三块都是新加的、主人天天会碰的东西，而且**不需要联网**就能验：
    # 记账是纯函数（喂样本序列）、音效是文件 + 音量缓存、面板能离屏建出来。
    # 顺带说一句：这里所有 play() 都断言"不该响"，所以自检本身不会出声。
    pet._selftest_buckets = buckets
    pet._selftest_assets = assets_dir    # 清晰度检查要用它重新加载原图
    ledger_ok = sound_ok = panel_ok = bubble_ok = menu_ok = False
    shell_ok = settings_ok = feishu_media_ok = feishu_image_ok = False
    bubble_font_ok = False
    for _name, _fn in (("记账", _ledger_problems),
                       ("音效", _sound_problems),
                       ("面板", _panel_problems),
                       ("气泡避让", _bubble_problems),
                       ("右键菜单", _menu_problems),
                       ("两模式窗口", _shell_problems),
                       ("飞书媒体", _feishu_media_problems),
                       ("飞书发图", _feishu_image_chain_problems),
                       ("设置窗口", _settings_problems),
                       ("气泡字号", _bubble_font_problems)):
        try:
            _bad = _fn(pet)
        except Exception as _err:
            print("      ↳ %s检查抛异常：%s: %s"
                  % (_name, type(_err).__name__, _err))
            _bad = ["抛异常"]
        if _bad:
            print("      ↳ %s：%s" % (_name, "；".join(str(x) for x in _bad)))
        if _name == "记账":
            ledger_ok = not _bad
        elif _name == "音效":
            sound_ok = not _bad
        elif _name == "面板":
            panel_ok = not _bad
        elif _name == "气泡避让":
            bubble_ok = not _bad
        elif _name == "右键菜单":
            menu_ok = not _bad
        elif _name == "两模式窗口":
            shell_ok = not _bad
        elif _name == "飞书媒体":
            # 飞书媒体是**打桩**验的（下载/上传/发送/抓屏全是假的），
            # 所以它能在离屏、断网的情况下跑 —— 也必须在自检里跑：
            # 这条链路的坑（image_key 当消息 id、跨线程抓屏）只有代码级
            # 断言拦得住，真机上试出来的代价是"发一张图等半天没反应"。
            feishu_media_ok = not _bad
        elif _name == "飞书发图":
            # 这一节专门盯"工作线程碰 Qt 会把界面卡死"（真机 AppHangB1 的根因）
            # 以及上传/发消息的请求形状。它也是唯一在**工作线程**里跑真链路的
            # 自检 —— 旧那节全在主线程调桩，测不出这类 bug。
            feishu_image_ok = not _bad
        elif _name == "设置窗口":
            # ⚠️ 一节一个分支：以前这里写的是裸 else，任何新加的一节都会被算进
            # "设置窗口"这个名字下（汇总行里连名字都没有）
            settings_ok = not _bad
        elif _name == "气泡字号":
            bubble_font_ok = not _bad
    print("  记账算法 / 音效文件与音量 / 新面板 / 气泡避让 / 菜单风格 / "
          "两模式窗口 / 设置窗口 / 气泡字号=%s"
          % (ledger_ok and sound_ok and panel_ok and bubble_ok and menu_ok
             and shell_ok and settings_ok and bubble_font_ok))
    print("  飞书媒体（打桩，不碰真接口）：图片消息走下载 / 存盘路径与文件名 / "
          "非图片类型回人话 / 白名单对图片同样生效 / /shot 命令与别名 / "
          "跨线程抓屏不卡死且排到主线程 / 失败原因分得清=%s" % feishu_media_ok)
    print("  飞书发图（真桥 + 真工作线程，网络全打桩）：气泡只从主线程冒 / "
          "等下载时主线程心跳不停 / 工作线程异常被兜住且回话 / multipart 过 RFC 解析 / "
          "上传与发图请求形状对 / 失败提示说人话=%s" % feishu_image_ok)

    if not (config_ok and preset_ok and key_ok and sse_ok and json_ok
            and preview_ok and trigger_ok and query_ok and bing_ok and err_ok
            and protect_ok and ask_ok and wide_ok and window_ok and dblclick_ok
            and close_btn_ok and mood_ok and mood_act_ok
            and key_ok2 and key_rebuild_ok and keydlg_ok and clear_ok
            and balance_ok and tray_ok and hide_ok
            and now_ok and tag_ok and remind_ok and dur_ok and alarm_ok
            and cancel_ok
            and expr_ok and bowl_ok and energy_ok
            and click_no_expr_ok and sleep_click_ok and clock_ok
            and log_ok and mem_ctx_ok and memory_tag_ok and bridge_ok
            and ledger_ok and sound_ok and panel_ok and bubble_ok and menu_ok
            and shell_ok and settings_ok and bubble_font_ok
            and feishu_media_ok and feishu_image_ok):
        print("[自检] 失败：对话与联网不符合预期。")
        return 1

    print("[自检] 通过。")
    return 0


# ---------------------------------------------------------------------------
# 下面这几个是"记账 / 音效 / 新面板 / 飞书媒体"的检查体，抽出来是因为它们
# **不需要（或只需要极少）Qt 上下文**（记账尤其干净：纯函数喂样本序列；
# 飞书媒体虽然要转事件循环，但网络层全是桩，跑起来跟纯函数一样安全），
# 单独放一段以后加断言只要改一处。
# 每个都返回"问题清单"：空列表 = 通过。这样失败时能直接说出**是哪一条**不对，
# 而不是丢一个 False 让人回去猜。
# ---------------------------------------------------------------------------
def _ledger_problems(pet):
    """记账核心 + 峰谷时段：降=消费 / 升=充值不冲消费 / 乱序忽略 / 部分数据 / 校正 / 峰谷。"""
    from datetime import datetime, timedelta, timezone
    import pet_ledger as led
    bj = timezone(timedelta(hours=8))
    day = "2026-10-11"

    def at(hh, mm, ss=0):
        return datetime.strptime("%s %02d:%02d:%02d" % (day, hh, mm, ss),
                                 "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=bj).timestamp()

    def at2(day2, hh, mm=0):
        return datetime.strptime("%s %02d:%02d" % (day2, hh, mm),
                                 "%Y-%m-%d %H:%M").replace(tzinfo=bj).timestamp()

    bad = []
    book = led.empty_ledger()
    s = led.observe(book, 100.0, at=at(9, 0))
    if abs(s["amount"]) > 1e-9:
        bad.append("第一次观测不该有消费")
    s = led.observe(book, 97.5, at=at(10, 0))
    if abs(s["amount"] - 2.5) > 1e-9:
        bad.append("余额降 2.5 应记 2.5（实得 %s）" % s["amount"])
    s = led.observe(book, 500.0, at=at(11, 0))
    if abs(s["amount"] - 2.5) > 1e-9:
        bad.append("充值不该冲掉已有消费（实得 %s）" % s["amount"])
    if abs(s["increase"] - 402.5) > 1e-9:
        bad.append("充值应单独累计（实得 %s）" % s["increase"])
    back = led.observe(book, 1.0, at=at(10, 30))
    if abs(back["amount"] - 2.5) > 1e-9:
        bad.append("更早的样本必须忽略（数字不能倒跳）")
    summary = led.day_summary(book, day, now=at(23, 0))
    if not summary or not summary["partial"]:
        bad.append("首观测 9:00 → 这天应标成「部分数据」")
    r = led.reconcile(book, day, credits=402.5)
    if r is None or abs(r["amount"] - 2.5) > 1e-9:
        bad.append("校正公式不对（100 期初 + 402.5 到账 - 500 期末 = 2.5）")
    if led.reconcile(book, day, other_debits=9999.0) is not None:
        bad.append("算出来是负数时校正应当被拒绝")
    if led.reconcile(book, "2000-01-01", 1.0) is not None:
        bad.append("没有观测记录的日期不该能校正")
    days = led.recent_days(book, 7, now=at(12, 0))
    if len(days) != 7 or days[-1][0] != day:
        bad.append("近 7 天列表不对")
    # ---- 峰谷时段（口径：工作日 9-12 / 14-18 高峰；周末与法定节假日全天谷价）----
    # 2026-10-12 是周一，2026-10-11 是周日，2026-10-10 是"调休上班的周六"，
    # 2026-10-01 是国庆 —— 四个日期正好把四条规则都覆盖到。
    for day2, hh, want_peak, tag in (
            ("2026-10-12", 8, False, "周一 08:00 谷价"),
            ("2026-10-12", 9, True, "周一 09:00 高峰（边界含）"),
            ("2026-10-12", 11, True, "周一 11:00 高峰"),
            ("2026-10-12", 12, False, "周一 12:00 谷价（边界不含）"),
            ("2026-10-12", 14, True, "周一 14:00 高峰"),
            ("2026-10-12", 18, False, "周一 18:00 谷价"),
            ("2026-10-11", 10, False, "周日全天谷价"),
            ("2026-10-10", 10, False, "调休上班的周六也是谷价"),
            ("2026-10-01", 10, False, "法定节假日全天谷价"),
            ("2026-08-22", 10, True, "周末规则生效前（2026-08-22）仍按高峰")):
        got = led.peak_state(now=at2(day2, hh))["peak"]
        if got != want_peak:
            bad.append("%s：应%s（实得%s）"
                       % (tag, "高峰" if want_peak else "谷价",
                          "高峰" if got else "谷价"))
    why = led.peak_state(now=at2("2026-10-11", 10))["why"]
    if "周末" not in why:
        bad.append("周末谷价的原因该写「周末」（实得 %s）" % why)
    why = led.peak_state(now=at2("2026-10-01", 10))["why"]
    if "节假日" not in why:
        bad.append("法定节假日谷价的原因该写「节假日」（实得 %s）" % why)
    # 倒计时：最容易错的是"跨周末"——周五 18:00 之后要到**周一 9:00** 才回高峰
    if abs(led.peak_state(now=at2("2026-10-12", 8, 30))["remain_sec"] - 1800) > 60:
        bad.append("周一 08:30 距回高峰应约 30 分")
    if abs(led.peak_state(now=at2("2026-10-12", 11, 30))["remain_sec"] - 1800) > 60:
        bad.append("周一 11:30 距进谷价应约 30 分")
    if abs(led.peak_state(now=at2("2026-10-16", 18))["remain_sec"] - 63 * 3600) > 60:
        bad.append("周五 18:00 距回高峰应约 63 小时（周末整段跳过）")
    return bad


def _sound_problems(pet):
    """音效：文件齐、音量缩放能渲染、开关 / 0 音量 / 事件开关都真的静音。"""
    import pet_sound as snd
    from pet_constants import SOUND_CACHE_DIRNAME, SOUND_FILES
    from pet_store import data_dir
    bad = []
    root = snd.sound_dir()
    for name in SOUND_FILES.values():
        path = root / name
        if not path.exists() or path.stat().st_size < 1024:
            bad.append("缺音效文件 %s（跑 make_sounds.py 生成）" % name)
    if not snd.available():
        bad.append("这台机器上放不了音（没有 winsound？）")
    if bad:
        return bad
    cache = data_dir(getattr(pet, "_config_path", None)) / SOUND_CACHE_DIRNAME
    snd.configure(True, 40, {}, cache)
    scaled = snd._scaled_path("press", 40)
    if not scaled or not Path(scaled).exists():
        bad.append("40% 音量的缓存没渲染出来")
    elif Path(scaled).read_bytes() == (root / "press.wav").read_bytes():
        # 注意：**不能比文件大小** —— WAV 的大小只由帧数决定，缩放振幅不改字节数。
        # 要比内容：40% 的那份样本值必须真的变小了。
        bad.append("40% 的缓存和原文件一模一样（音量没生效）")
    else:
        import struct
        import wave as _wave

        def _peak(path):
            with _wave.open(str(path), "rb") as fh:
                data = fh.readframes(fh.getnframes())
            return max(abs(v) for (v,) in struct.iter_unpack("<h", data))

        if _peak(scaled) >= _peak(root / "press.wav"):
            bad.append("40% 的缓存峰值没降下来")
    snd.configure(False, 70, {}, cache)
    if snd.play("press"):
        bad.append("总开关关掉后还在播")
    snd.configure(True, 0, {}, cache)
    if snd.play("press"):
        bad.append("音量 0 还在播")
    snd.configure(True, 70, {"press": False}, cache)
    if snd.play("press"):
        bad.append("单个事件关掉后还在播")
    return bad


def _bubble_problems(pet):
    """气泡的**层级**：别的程序窗口 > 气泡 > 桌宠（主人 2026-10-11 定的，别再改回去）。

    以前是"气泡主动躲开别的窗口"（会挪来挪去），主人要的是**被盖住**。
    现在的机制只有两条，所以这里就验这两条：
      ① 气泡与桌宠**都不带** WindowStaysOnTopHint —— 别的程序窗口才盖得住它们；
      ② 气泡的父窗口是桌宠 —— Windows 保证"附属窗口压在拥有者之上"，
         于是气泡永远在桌宠之上、又在别的窗口之下。
    """
    from PySide6.QtCore import Qt
    bad = []
    bubble = getattr(pet, "bubble", None)
    if bubble is None:
        return ["没有气泡对象（是不是 --no-talk 了）"]
    # 气泡的置顶状态**必须与桌宠一致**（主人 2026-07-11 的要求）：
    # 桌宠置顶 → 气泡也置顶；不置顶 → 气泡也不置顶，别的程序窗口才盖得住它。
    # 注意：这里**不能**断言"气泡不许置顶" —— 那个性质来自
    # "桌宠默认不置顶"（下面验 DEFAULT_CONFIG），气泡是跟着它走的。
    pet_top = bool(pet.windowFlags() & Qt.WindowStaysOnTopHint)
    bub_top = bool(bubble.windowFlags() & Qt.WindowStaysOnTopHint)
    if pet_top != bub_top:
        bad.append("气泡置顶(%s)与桌宠置顶(%s)不一致" % (bub_top, pet_top))
    # 桌宠自己置不置顶由**调用方**决定（自检这里就传了 on_top=True），
    # 所以断言要看**产品默认值**，而不是这个实例的标志。
    from pet_constants import DEFAULT_CONFIG
    if DEFAULT_CONFIG.get("always_on_top"):
        bad.append("配置默认成了「始终置顶」，应当是 False（别的窗口才盖得住）")
    if bubble.parent() is not pet:
        bad.append("气泡的父窗口不是桌宠 → 无法保证它压在桌宠之上")
    # 真的显示一次，确认它在桌宠上方那片区域、且不越出屏幕
    char = pet.char_rect()
    screen = pet.screen().availableGeometry()
    bubble.say("层级检查：这句该被别的窗口盖住。", char, screen, hold=0.0)
    if not bubble.isVisible():
        bad.append("气泡显示不出来")
    elif not screen.contains(bubble.frameGeometry()):
        bad.append("气泡跑到屏幕外面去了")
    bubble.hide()

    # 气泡的置顶状态必须**跟着桌宠走**（主人 2026-07-11 的要求）：
    # 桌宠置顶 → 气泡也置顶；不置顶 → 气泡不置顶（别的窗口才盖得住它）。
    try:
        from PySide6.QtWidgets import QApplication as _QApp
        was_top = bool(pet.windowFlags() & Qt.WindowStaysOnTopHint)
        for want in (not was_top, was_top):
            flags = pet.windowFlags()
            pet.setWindowFlags(flags | Qt.WindowStaysOnTopHint if want
                               else flags & ~Qt.WindowStaysOnTopHint)
            pet.show()
            for _ in range(2):
                _QApp.processEvents()
            pet.say("置顶状态检查")          # say 里会同步气泡的置顶
            for _ in range(2):
                _QApp.processEvents()
            pet_top = bool(pet.windowFlags() & Qt.WindowStaysOnTopHint)
            bub_top = bool(bubble.windowFlags() & Qt.WindowStaysOnTopHint)
            if pet_top != bub_top:
                bad.append("气泡置顶(%s)没跟着桌宠(%s)走" % (bub_top, pet_top))
        # 还原
        flags = pet.windowFlags()
        pet.setWindowFlags(flags | Qt.WindowStaysOnTopHint if was_top
                           else flags & ~Qt.WindowStaysOnTopHint)
        pet.show()
        bubble.hide()
    except Exception as err:
        bad.append("气泡置顶跟随检查抛异常：%s: %s" % (type(err).__name__, err))
    return bad


def _bubble_font_problems(pet):
    """气泡字号（2026-10-11 的「台词气泡字号」）：能改、改完真的变大、
    而且**超长台词 + 站在屏幕底部时气泡不许溢出屏幕**。

    这两条原本挂在双形态那节里，但它们验的是气泡本身、与形态无关，
    所以分享版（单形态）照留。这里用假余额、不联网。
    """
    bad = []
    real_chat = getattr(pet, "chat", None)
    try:
        # ⑨ 台词气泡字号能改，而且气泡会跟着变大
        if pet.bubble is not None:
            pet.bubble.set_font_px(BUBBLE_FONT_MIN)
            pet.say("字号检查：这句话用来量气泡。", hold=0.0)
            _QT_APP().processEvents()
            small = (pet.bubble.width(), pet.bubble.height())
            pet.bubble.set_font_px(BUBBLE_FONT_MAX)
            pet.say("字号检查：这句话用来量气泡。", hold=0.0)
            _QT_APP().processEvents()
            big = (pet.bubble.width(), pet.bubble.height())
            if not (big[0] > small[0] and big[1] > small[1]):
                bad.append("改气泡字号没让它变大：%s → %s" % (small, big))
            if int(pet.bubble._font.pixelSize()) != BUBBLE_FONT_MAX:
                bad.append("气泡字体的 pixelSize 没跟上设置")
            # ⑩ 超长台词 + 站在屏幕底部：气泡**不许溢出屏幕**
            _sc = pet.screen().availableGeometry()
            pet.move(600, _sc.bottom() - pet.height())
            _QT_APP().processEvents()
            pet.say("很长的一段话。" * 12, hold=0.0)
            for _ in range(3):
                _QT_APP().processEvents()
            _g = pet.bubble.frameGeometry()
            if not _sc.contains(_g):
                bad.append("台词气泡溢出了屏幕：气泡 %s ｜ 屏幕 %s" % (_g, _sc))
            pet.bubble.set_font_px(BUBBLE_FONT_PX)
            pet.dismiss_chat_bubble()
    except Exception as err:
        bad.append("气泡字号检查抛异常：%s: %s" % (type(err).__name__, err))
    finally:
        if real_chat is not None:
            pet.chat = real_chat
        pet.dismiss_chat_bubble()
    return bad

def _QT_APP():
    """拿当前那个 QApplication（自检里多处要 processEvents）。"""
    from PySide6.QtWidgets import QApplication
    return QApplication.instance()


def _menu_problems(pet):
    """右键菜单：功能入口在、**没有 emoji**、样式与本体的美术风格统一。

    主人 2026-10-11 的原话是"emoji 太土" —— 所以这条断言就是防止它再长回来。
    """
    bad = []
    menu = pet.build_menu()
    texts = [act.text() for act in menu.actions() if act.text()]
    joined = "\\n".join(texts)
    for need in ("小鲸鱼记账…", "设置…", "隐藏", "回到待机", "显示时钟"):
        if need not in joined:
            bad.append("菜单里少了「%s」" % need)
    # 音效设置/飞书/远程/API key 都搬进设置了，菜单里**不该**再有它们
    # （2026-07-11 主人要求删掉音效那一项）
    for gone in ("音效设置", "飞书接入", "远程控制", "API key"):
        if gone in joined:
            bad.append("菜单里还留着「%s」—— 它已经搬进「设置…」了" % gone)
    for text in texts:
        for ch in text:
            # emoji / 杂项符号 / 变体选择符 —— 菜单里一个都不许有
            if ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF or ord(ch) == 0xFE0F:
                bad.append("菜单文字里还有 emoji：%r" % text)
                break
    sheet = menu.styleSheet() or ""
    if "border-radius" not in sheet or "QMenu::item:selected" not in sheet:
        bad.append("菜单没上统一样式（pet_ui.style_menu 没生效）")
    # 子菜单（做个表情）也要上同一套样式，否则点开就露馅
    for act in menu.actions():
        sub = act.menu()
        if sub is not None and "border-radius" not in (sub.styleSheet() or ""):
            bad.append("子菜单没上样式：%r" % act.text())
    menu.close()
    return bad


def _settings_problems(pet):
    """设置窗口（主人 2026-10-11 要求）：六页都能开，飞书/远程/钥匙都搬进来了。

    菜单里那三项已经撤掉，所以这里是**唯一**的配置入口 —— 它要是打不开，
    主人就没法填 key / 开飞书 / 开远程了。
    """
    bad = []
    try:
        pet.open_settings()
    except Exception as err:
        return ["open_settings 抛异常：%s: %s" % (type(err).__name__, err)]
    win = getattr(pet, "_settings_win", None)
    if win is None:
        return ["open_settings 没造出窗口（_settings_win 还是 None）"]
    keys = set(getattr(win, "rail_buttons", {}) or {})
    # 分享版（SHARE_EDITION）不含局域网远程控制 → 不再要求「远程控制」那一页
    need_keys = (("chat", "feishu", "sound", "pet", "about")
                 if SHARE_EDITION
                 else ("chat", "feishu", "remote", "sound", "pet", "about"))
    for need in need_keys:
        if need not in keys:
            bad.append("设置里少了「%s」页" % need)
        else:
            try:
                win.open_page(need)
            except Exception as err:
                bad.append("打开「%s」页抛异常：%s: %s"
                           % (need, type(err).__name__, err))
    # 桌宠大小页是主人在设置里最常用的那个 —— 真按一次，看立即生效没有
    before = int(getattr(pet, "_pet_height", 0) or 340)
    target = 220 if abs(before - 220) > 40 else 460
    try:
        changed = pet.apply_pet_size(target)
        if not changed or int(pet._pet_height) != target:
            bad.append("apply_pet_size(%d) 没生效（记录高=%s）"
                       % (target, pet._pet_height))
        pet.apply_pet_size(before)          # 还原，别影响后面的检查
        if int(pet._pet_height) != before:
            bad.append("大小改回去也没生效")
    except Exception as err:
        bad.append("改大小抛异常：%s: %s" % (type(err).__name__, err))
    # 改大小必须走"**原图重新加载**"，不能缩放手里那张已经缩小过的 pixmap ——
    # 后者是二次重采样，会明显发糊（主人 2026-07-11 报的"改完模糊、重启才好"）。
    # 判据：挂了 reloader 之后，缩放出来的帧应当与"直接按新高度加载"的帧**逐像素相同**。
    art_src = getattr(pet, "_selftest_buckets", None)
    if art_src is not None:
        try:
            import whale_pet as _wp
            calls = []

            def _counting_reloader(h):
                calls.append(h)
                return _wp.load_pet_art(art_src, h)

            pet._asset_reloader = _counting_reloader
            probe_h = 300 if abs(before - 300) > 40 else 420
            pet.apply_pet_size(probe_h)
            if not calls:
                bad.append("改大小时没走「原图重新加载」（会发糊）")
            fresh = _wp.load_pet_art(art_src, probe_h)["frames"][0]
            if pet.frames and pet.frames[0].toImage() != fresh.toImage():
                bad.append("改大小后的立绘与「按新高度重新加载」的不一致（说明糊了）")
            # 饭碗比例：它是"画布空间"图层，构造时按显示宽度预缩放。
            # 改大小那条路若直接把画布原图塞回去，屏幕上就是一个巨大的碗
            # （主人 2026-07-11 报的）。判据：宽度必须等于同一个换算式的值，
            # 且**远小于**画布原图宽度。
            from pet_constants import AHOGE_CANVAS, BOWL_CANVAS_W
            bowl = getattr(pet, "bowl_full_pixmap", None)
            raw_bowl = _wp.load_pet_art(art_src, probe_h)["bowl_full_pixmap"]
            if bowl is not None and raw_bowl is not None:
                expect = max(6, int(round(
                    BOWL_CANVAS_W * (pet.pixmap.width() / float(AHOGE_CANVAS[0])))))
                if bowl.width() != expect:
                    bad.append("改大小后饭碗宽度 %d ≠ 应有 %d（比例失调）"
                               % (bowl.width(), expect))
                if bowl.width() >= raw_bowl.width() * 0.9:
                    bad.append("饭碗还是画布原图尺寸（漏了预缩放）")
            pet.apply_pet_size(before)
        except Exception as err:
            bad.append("清晰度检查抛异常：%s: %s" % (type(err).__name__, err))
        finally:
            pet._asset_reloader = None
    win.hide()
    return bad


def _shell_problems(pet):
    """两模式窗口（主人 2026-10-11 要求）：一条左栏 + 栏底一键切换 + 没有顶部操作栏。

    旧的两个窗口（build_chat_window / build_harness_console）已经不再被程序使用，
    所以这里验的是新壳 pet_shell —— 门面接口与旧的一致，调用点才不用改。
    """
    bad = []
    try:
        from PySide6.QtWidgets import QApplication as _QApp
        shell = pet._shell_window()
    except Exception as err:
        return ["建不出主壳窗口：%s: %s" % (type(err).__name__, err)]
    # ① 两个门面：接口必须在（缺一个老调用点就会 AttributeError）
    for name in ("add_me", "add_her", "add_note", "begin_reply", "stream",
                 "stream_full", "end_reply", "fail_reply", "add_error", "clear",
                 "set_busy", "open_window", "isVisible", "close"):
        if not hasattr(shell.chat, name):
            bad.append("聊天门面少了 %s()" % name)
    for name in ("refresh_sessions", "adopt_session", "current_target", "stream",
                 "tool", "note", "fail", "finish", "set_status", "set_busy",
                 "open_window"):
        if not hasattr(shell.work, name):
            bad.append("工作门面少了 %s()" % name)
    if not hasattr(shell.chat, "_rows_layout"):
        bad.append("聊天门面少了 _rows_layout（自检与清空逻辑都靠它）")
    # ② 左栏任务列表：磁盘上有几段就该有几段（+1 是"开一段新对话"）
    shell.work.refresh_sessions()
    want = _session_log.list_sessions() if SESSION_LOG_AVAILABLE else []
    if shell.work.listw.count() != 1 + len(shell.work._rows):
        bad.append("左栏条目数与 _rows 对不上")
    if want and not shell.work._rows:
        bad.append("左栏是空的（读不到会话日志？）")
    # ③ 模式一键切换：栏底按钮的文字跟着变
    shell.set_mode("chat")
    if "工作模式" not in shell.mode_btn.text():
        bad.append("聊天模式下栏底不是「切换工作模式」（实得 %r）"
                   % shell.mode_btn.text())
    shell.set_mode("work")
    if "聊天模式" not in shell.mode_btn.text():
        bad.append("工作模式下栏底不是「切换聊天模式」（实得 %r）"
                   % shell.mode_btn.text())
    shell.set_mode("chat")
    # ④ 顶部**不该**有操作栏：窗口里不能出现旧的"刷新列表"那种整条按钮行
    #    （允许卡片标题栏里的小按钮 —— 主人只是不要"上边栏那一排"）
    if getattr(shell, "topbar", None) is not None:
        bad.append("窗口顶上还挂着操作栏")
    # ⑤ 关掉之后 isVisible 必须是 False（调用方靠它决定要不要写日志）
    shell.chat.close()
    if shell.chat.isVisible():
        bad.append("关掉窗口后 chat.isVisible() 还是 True")

    # ⑥ 「隐藏」必须真的能藏 —— 开过这个窗口之后也要能藏。
    #    以前门面上少一个 hide()，hide_into_tray 在那里抛 AttributeError，
    #    后面那句 self.hide() 永远执行不到 → 桌宠再也藏不起来（主人报的"有时候"）。
    try:
        shell.chat.open_window(pet.char_rect(), pet.screen().availableGeometry())
        for _ in range(3):
            _QApp.processEvents()
        pet.hide_into_tray()
        for _ in range(3):
            _QApp.processEvents()
        if pet.isVisible():
            bad.append("开过两模式窗口之后，「隐藏」藏不起来")
        if shell.chat.isVisible():
            bad.append("「隐藏」之后两模式窗口还开着")
        if pet.bubble is not None and pet.bubble.isVisible():
            bad.append("「隐藏」之后气泡还在")
        # 隐藏期间自动冒泡也不该把她冒出来（提醒是唯一例外）
        pet.say("隐藏期间这句不该冒出来")
        for _ in range(3):
            _QApp.processEvents()
        if pet.isVisible() or (pet.bubble is not None and pet.bubble.isVisible()):
            bad.append("隐藏期间自动冒泡把她冒出来了")
        pet.show_from_tray()          # 还原：后面的检查还要用可见的桌宠
        shell.chat.hide()             # 用门面自己，别去碰可能已被清空的 pet.chat_window
        for _ in range(3):
            _QApp.processEvents()
    except Exception as err:
        import traceback
        bad.append("隐藏检查抛异常：%s: %s ｜ %s"
                   % (type(err).__name__, err,
                      traceback.format_exc().strip().splitlines()[-2]))

    # ⑦ 主/子会话识别：判据是 DSH 写进日志头部的 origin / parentSession /
    #    delegationDepth（实地核对过真实日志），这里用合成数据钉住这三条。
    try:
        import dsh_session_log as _slog
        from PySide6.QtCore import Qt as _Qt       # UserRole（副标题存在角色里）
        kind, parent, depth = _slog.classify_session(
            {"origin": "subagent", "parentSession": "session-a",
             "delegationDepth": 1})
        if (kind, parent, depth) != ("sub", "session-a", 1):
            bad.append("classify_session 认不出子会话：%s"
                       % ((kind, parent, depth),))
        if _slog.classify_session({"delegationDepth": 0})[0] != "main":
            bad.append("classify_session 把主会话认错了")
        # 老日志可能只有深度、没有 origin —— 只看 origin 会漏
        if _slog.classify_session({"delegationDepth": 2})[0] != "sub":
            bad.append("classify_session 漏了「只有深度」的子会话")

        def _stub(sid, kind, parent="", depth=0):
            return {"id": sid, "kind": kind, "parent": parent, "depth": depth,
                    "title": sid, "cwd": "C:\\x", "turns": 1,
                    "time_label": "今天 00:00"}

        groups = _slog.group_sessions([
            _stub("m1", "main"), _stub("s1", "sub", "m1", 1),
            _stub("s2", "sub", "s1", 2),      # 孙会话：要挂回根主会话
            _stub("s3", "sub", "gone", 1),    # 父被清理：不能丢
            _stub("m2", "main")])
        got = [(m["id"] if m else None, [s["id"] for s in kids])
               for m, kids in groups]
        want = [("m1", ["s1", "s2"]), ("m2", []), (None, ["s3"])]
        if got != want:
            bad.append("group_sessions 分组不对：%s（期望 %s）" % (got, want))

        # ⑧ 左栏真的把"子任务"标出来了。**注入假数据**，不依赖主人真实会话。
        orig_list = _slog.list_sessions
        try:
            _slog.list_sessions = lambda *a, **k: [
                _stub("m1", "main"), _stub("s1", "sub", "m1", 1)]
            shell.refresh_sessions()
            # 子会话**默认折叠**（主人 2026-10-11）：先验"折叠着"——
            # 不该有 └ 行，但主会话副标题要写明有几个子任务；然后再展开验画得对
            folded_texts = [shell.task_list.item(i).text()
                            for i in range(shell.task_list.count())]
            if any("└" in t for t in folded_texts):
                bad.append("子会话默认应当折叠，但左栏出现了 └ 行")
            folded_roles = [str(shell.task_list.item(i).data(_Qt.UserRole) or "")
                            for i in range(shell.task_list.count())]
            if not any("个子任务" in r for r in folded_roles):
                bad.append("折叠时没在副标题里写明有几个子任务")
            shell._expanded.add("m1")          # 展开之后再验那一行画得对不对
            shell.refresh_sessions()
            texts = [shell.task_list.item(i).text()
                     for i in range(shell.task_list.count())]
            subs = [t for t in texts if "└" in t]
            if len(subs) != 1:
                bad.append("左栏没标出子会话（带 └ 的行 = %d，应为 1）" % len(subs))
            if not any("对话一" in t for t in texts):
                bad.append("左栏没给主会话编号")
            roles = [str(shell.task_list.item(i).data(_Qt.UserRole) or "")
                     for i in range(shell.task_list.count())]
            if not any("子任务" in r for r in roles):
                bad.append("子会话那行的副标题没写「子任务」")
            if not any("主会话" in r for r in roles):
                bad.append("主会话那行的副标题没写「主会话」")
        finally:
            _slog.list_sessions = orig_list
            shell.refresh_sessions()          # 还原成真实列表
    except Exception as err:
        bad.append("主子会话识别检查抛异常：%s: %s" % (type(err).__name__, err))
    return bad


def _panel_problems(pet):
    """两个新面板能不能离屏建出来并渲染（喂假数据，不联网）。"""
    bad = []
    state = {
        "balance": {"total": 128.4, "granted": 8.4, "recharge": 120.0,
                    "currency": "CNY", "at": 1700000000.0},
        "today": {"day": "2026-10-11", "amount": 3.27, "partial": True,
                  "corrected": False, "increase": 0.0,
                  "first_at": 1700000000.0, "last_at": 1700003600.0},
        "recent": [("2026-10-09", None),
                   ("2026-10-10", {"amount": 12.4, "partial": False,
                                   "increase": 0.0, "first_at": 1700000000.0,
                                   "last_at": 1700086400.0}),
                   ("2026-10-11", {"amount": 3.27, "partial": True,
                                   "increase": 0.0, "first_at": 1700000000.0,
                                   "last_at": 1700003600.0})],
        "peak": {"offpeak": False, "label": "峰价时段",
                 "next_label": "进入谷价", "remain_sec": 3600,
                 "remain_text": "还有 1 小时"},
        "note": "自检",
        "warn": {"below": 20.0, "above": 5.0},
    }
    panel = build_balance_panel(pet, lambda: state, lambda: None,
                                lambda c, o: None, lambda b, a: True)
    panel.refresh()
    if "128.40" not in panel.big_value.text():
        bad.append("记账面板没显示总余额")
    if "3.27" not in panel.today_value.text():
        bad.append("记账面板没显示今日已用")
    if panel.detail_lay.count() < 2:
        bad.append("记账面板的逐日明细是空的")
    if panel.warn_below.text() != "20" or panel.warn_above.text() != "5":
        bad.append("记账面板没读回两条预警线（实得 %s / %s）"
                   % (panel.warn_below.text(), panel.warn_above.text()))
    # 面板"控制器"自己绝不能变成可见窗口（主人 2026-07-11 报的"窗口嵌套叠加"）：
    # Qt 会把子窗口跟着父窗口一起显示，所以它必须是不可见的那一个。
    # 判据用**基类**的 isVisible —— 面板类把 isVisible 转发给了里面的 frame，
    # 用转发值去数窗口会自己骗自己（踩过）。
    from PySide6.QtWidgets import QDialog as _QDialog
    panel.show()
    if _QDialog.isVisible(panel):
        bad.append("面板控制器自己也显示出来了（屏幕上会多一个空窗口）")
    if not panel.isVisible():
        bad.append("面板窗口没显示出来")
    panel.close()

    cfg = {"enabled": True, "volume": 55, "events": {"press": False}}
    dlg = build_sound_dialog(pet, lambda: cfg, lambda c: True,
                             lambda name: None, lambda c: None)
    dlg.reload()
    if dlg.volume.value() != 55:
        bad.append("音效面板没读回音量")
    if dlg.events["press"].isChecked():
        bad.append("音效面板没读回事件开关")
    dlg.close()

    # 自动观测余额的节拍（不真等 5 分钟，只验开关与间隔设对没设对）
    if pet.start_balance_watch(0) is not False:
        bad.append("间隔填 0 时不该起定时器")
    if pet.start_balance_watch(30) is not True:
        bad.append("自动观测该起得来")
    if pet._balance_timer.interval() != 30 * 60000:
        bad.append("自动观测的间隔没按配置设（实得 %s ms）"
                   % pet._balance_timer.interval())
    pet._balance_timer.stop()
    return bad


# ---------------------------------------------------------------------------
# 飞书媒体（2026-10-11 加）：手机拍照发过来 → 存盘；/shot → 截图发过去。
#
# **这一段绝不碰真飞书**：下载、上传、发送、抓屏全部打桩（monkeypatch）。
# 为什么敢在自检里建线程：这一段验的正是"跨线程那条链路"，不真起线程就
# 等于没验 —— 但线程里跑的每个网络调用都是桩，不会产生任何外部请求。
# ---------------------------------------------------------------------------
def _media_ev(message_type, content):
    """按**真实事件的形状**造一条归一化后的事件（走 normalize_event 的路径）。

    注意 content 要传"飞书原样的 JSON 字符串" —— 图片消息在真实事件里就是
    `{"image_key": "img_v2_..."}` 这种字符串，直接喂 dict 就绕过了解析那一步，
    而那一步正是"图片被静默忽略"的老 bug 所在。
    """
    import pet_feishu as _pf
    raw = content if isinstance(content, str) else json.dumps(
        content, ensure_ascii=False)
    return _pf.normalize_event({"sender": {"sender_id": {"open_id": "ou_me"},
                                          "sender_type": "user"},
                                "message": {"message_id": "om_x",
                                            "chat_id": "oc_selftest",
                                            "chat_type": "p2p",
                                            "message_type": message_type,
                                            "content": raw,
                                            "mentions": []}})


def _pump(app, pred, timeout=8.0):
    """转 Qt 事件循环直到 pred 为真。工作线程跑完了它才会真。

    为什么要转事件循环而不是 `time.sleep`：截图那条链路里，工作线程要等
    **主线程**（Qt 信号排过来的槽）把图抓完才继续 —— 主线程要是傻等，
    这条链就死锁了。所以自检必须一边等一边 processEvents。
    """
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(pred())


class _FeishuSelftestPet(QObject):
    """给飞书路由段用的**假桌宠**：只放路由真正会碰的那几个字段/方法。

    ⚠️ 路由方法（_on_feishu_message / _feishu_media / _feishu_take_shot）
    **不是抄一份**，而是从 LinkMixin 上借来绑到这个对象上的 —— 抄一份就等于
    验的是"抄得像不像"，改了真代码自检还照样绿（那是最没用的自检）。
    借过来的做法：`_FeishuSelftestPet._on_feishu_message = LinkMixin._on_feishu_message`，
    它们只依赖下面这十几个字段，不碰窗口，所以绑到普通对象上也跑得通。

    为什么不直接用真 WhalePet：这一段全是桩，真桌宠会把建窗口、定时器、
    网络客户端全拖进来；而且 `feishu_cfg` 得能按测试需要改。独立一个小类也让
    "这段到底依赖桌宠哪些接口"一眼可见 —— 以后谁改了那些接口名，这里会立刻炸。

    **为什么是 QObject 而不是普通 object**（2026-10-11 加）：要验"工作线程里
    绝不碰 Qt"，就得用**真桥**（build_feishu / _feishu_bridge）把整条链装配
    起来，而 QObject 的 parent 必须是 QObject。桥活在建它的那个线程（主线程），
    于是"工作线程 emit → 信号排队到主线程"这套机制才是真的在跑，而不是被
    桩绕过去。顺带也把这句钉住：这段自检依赖的"桌宠是个 QObject"是真实约束。
    """

    _config_path = None
    _feishu = None
    _remote = None
    _feishu_pending = None
    _feishu_chats = None
    _feishu_log = None
    _feishu_last_chat = ""

    def __init__(self, tmp_root, allow_sender=""):
        super().__init__()            # 无 parent：活在建它的线程（自检＝主线程）
        self._config_path = Path(tmp_root) / "whale_pet_config.json"
        self._feishu = None
        self._remote = None
        self._feishu_pending = {}
        self._feishu_chats = {}
        self._feishu_log = []
        self._feishu_last_chat = ""
        self._allow = allow_sender
        self.said = []

    # --- 桌宠那边被路由用到的接口（其余全部借 LinkMixin 的）---
    def feishu_cfg(self):
        return {"enabled": True, "app_id": "cli_x", "app_secret": "s",
                "domain": "feishu", "allow_harness": False,
                "allow_sender": self._allow}

    def say(self, text, **kw):
        self.said.append(str(text))


# 借真实现（见类文档：抄一份的自检等于没验）
def _bind_feishu_mixin():
    from pet_mixin_link import LinkMixin
    for name in ("_on_feishu_message", "_feishu_gate", "_feishu_media",
                 "_feishu_do_media", "_feishu_media_error", "_feishu_take_shot",
                 "_feishu_session", "_feishu_note", "_feishu_reply",
                 "_feishu_command", "_feishu_text", "_feishu_help",
                 "_feishu_status_text", "_feishu_session_list",
                 "_feishu_chat", "_feishu_task", "_feishu_publish",
                 "_remote_publish",
                 # 跨线程那三件（2026-10-11 加"飞书发图"一节时补）：
                 # _feishu_bridge 是**生产代码里真用的装配路径**（连 say_want），
                 # _on_feishu_say / _feishu_bubble 是那句气泡的两端。
                 # 漏借一个，新那节就会用桩把要验的那段绕过去。
                 "_feishu_bridge", "_on_feishu_say", "_feishu_bubble",
                 "_feishu_remember_chat"):
        func = getattr(LinkMixin, name, None)
        if func is not None:
            setattr(_FeishuSelftestPet, name, func)


_bind_feishu_mixin()


class _FakeFeishuClient(object):
    """假客户端：只记"被调了什么、参数是什么"，一个字节都不出网。"""

    def __init__(self, data=b"", ctype="image/png", fail=None, slow=0.0):
        self.data = data
        self.ctype = ctype
        self.fail = fail
        self.slow = slow            # >0 时下载故意慢一点，用来验"主线程没被堵"
        self.downloads = []
        self.uploads = []
        self.sent = []
        self.replies = []           # 真桥的发送线程会调 reply_text，记下来

    def download_resource(self, message_id, file_key, rtype="image"):
        self.downloads.append((message_id, file_key, rtype))
        if self.slow:
            time.sleep(self.slow)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.data, self.ctype

    def reply_text(self, message_id, text, chat_id=""):
        """真桥的发送线程走的是这个（不是 bridge.reply）。"""
        self.replies.append(str(text))
        if self.fail:
            raise RuntimeError(self.fail)
        return True

    def upload_image(self, path, image_type="message"):
        self.uploads.append((str(path), image_type))
        if self.fail:
            raise RuntimeError(self.fail)
        return "img_key_fake_from_selftest"

    def send_image(self, chat_id, path, reply_id="", image_type="message"):
        self.sent.append((chat_id, str(path), reply_id, image_type))
        if self.fail:
            raise RuntimeError(self.fail)
        return "img_key_fake_from_selftest"


class _FakeFeishuBridge(object):
    """假飞书桥：回复进列表、图片进列表、抓屏吐一张真 PNG 文件。"""

    def __init__(self, tmp_root, client=None, send_ok=True):
        self.client = client or _FakeFeishuClient()
        self.replies = []
        self.images = []
        self.said = []
        self.shot_files = []
        self.send_ok = send_ok
        self._tmp_root = Path(tmp_root)
        self._n = 0

    def reply(self, chat_id, message_id, text):
        self.replies.append((chat_id, message_id, text))
        return True

    def last(self):
        return self.replies[-1][2] if self.replies else ""

    def send_image(self, chat_id, path, message_id=""):
        self.images.append((chat_id, str(path), message_id))
        if not self.send_ok:
            return False
        # 真桥是"发完就删临时文件"；自检里也照做，这样"发完有没有留垃圾"
        # 这条能一起验（不能只验"排进队列了"，那漏掉的是清理这一步）
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def capture_screen_file(self, timeout=10.0):
        # 真抓屏是 Qt 才能做的事，自检**不真抓**（离屏平台下抓到的是黑图）；
        # 这里只吐一张写好的 1×1 PNG，用来验"后面的排队与清理"。
        self._n += 1
        f = self._tmp_root / ("fake_shot_%d.png" % self._n)
        f.write_bytes(base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            b"z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        self.shot_files.append(str(f))
        return str(f)


def _feishu_image_chain_problems(pet):
    """发图链路的硬骨头：**工作线程绝不碰 Qt** + 上传/发消息协议合规。

    为什么单开一节（2026-10-11 加，主人报"手机发图过来 → 程序未响应"之后）：

    * 真机证据：18:30:40 / 18:32:24 两张手机照片都已落盘（下载存盘是好的），
      紧接着 18:31:10 / 18:32:52 Windows 各记了一次 AppHangB1（"程序未响应"）；
    * 根因：`_feishu_do_media` 跑在工作线程里，却在最后直接调了 `self.say(...)`；
      `say` 最后会走到气泡的 `show()`，而**从工作线程调它会把 GUI 线程永久卡住**
      （本机逐调用验过，探针在 DSH 工作区的 `_probe/qt_affinity_probe.py`：`bubble.show()`
      和 `setWindowFlags()` 卡死，`setText/adjustSize/move/update` 不卡）。
    * **原来那节自检为什么没抓到**（这就是它的盲区）：
      ① 假桌宠的 `say` 只往列表里塞字符串，根本不碰 Qt —— 拿它验"会不会把界面
         卡死"等于没验；
      ② 断言只问"said 里有没有东西"，从来不问"这句话是在**哪个线程**里说的"。
      所以这一节：用**真桥**装配整条链、在**真工作线程**里跑，并且**记下 say
      被调到时是哪个线程**；再配一个主线程心跳，一边等一边数。

    另外顺手把"图发出去"那半段的协议钉住（纯打桩，不发任何请求）：
    multipart 交给标准库 email 解析器按 RFC 验，发图片消息那个请求的 URL/body
    用打桩抓下来看 —— content 必须是**字符串化 JSON**，写成 dict 飞书不报错、
    只是主人永远看不到图。
    """
    import shutil
    import tempfile

    import pet_feishu as pf
    from email import policy
    from email.parser import BytesParser
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QCoreApplication.instance()
    if app is None:
        return ["自检里没有 QApplication（这一段要转事件循环才能验跨线程）"]

    bad = []
    root = Path(tempfile.mkdtemp(prefix="whale_pet_feishu_chain_"))
    main_ident = threading.get_ident()
    # 主线程心跳：只要 GUI 线程还在转事件循环，这个数就会涨。
    # ⚠️ QTimer **必须留引用**（局部变量会被 GC，心跳永远是 0 —— 探针里踩过）
    beats = [0]
    beat = QTimer()
    beat.timeout.connect(lambda: beats.__setitem__(0, beats[0] + 1))
    beat.start(20)

    # ---- ① 真桥 + 真收图链路，从工作线程整条走一遍 ----
    p = _FeishuSelftestPet(root)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"C" * 4096
    # 下载故意慢 0.5 秒：网络活要是跑在主线程上，心跳立刻就停了
    client = _FakeFeishuClient(data=png_bytes, ctype="image/png", slow=0.5)
    said = []

    def _record_say(text, **kw):
        # 记线程：**这一条就是这节自检的核心断言**（旧那节只记字符串，
        # 所以工作线程碰 Qt 的 bug 从它眼皮底下溜过去了）
        said.append((threading.get_ident(), str(text)))

    p.say = _record_say
    p._feishu_bridge()            # 生产代码里真用的装配路径（内部会连 say_want）
    p._feishu.client = client     # 但客户端是桩：一个字节都不出网
    beats_before = beats[0]
    p._on_feishu_message(_media_ev("image", {"image_key": "img_v2_CHAINKEY"}))
    got = _pump(app, lambda: bool(said), timeout=8.0)
    beats_after = beats[0]
    # 回话是走真桥的发送队列的（另一条线程），再等它一下
    _pump(app, lambda: bool(client.replies), timeout=5.0)
    if not got and not said:
        bad.append("收图链路 8 秒内没走完（工作线程没冒泡 —— 卡住了？）")
    if beats_after - beats_before < 5:
        bad.append("收图那 0.5 秒里主线程心跳只涨了 %d 下 —— "
                   "下载这种网络活跑到主线程上去了"
                   % (beats_after - beats_before))
    if not said:
        bad.append("收图之后桌宠没冒气泡（say 一次都没被调到）")
    else:
        ident, text = said[0]
        if ident != main_ident:
            bad.append("★ **气泡是在工作线程里冒的**（真机 AppHangB1 就是这个："
                       "从工作线程调气泡 show() 会把 GUI 线程永久卡死）—— "
                       "必须走桥的 say_want 信号回主线程")
        if "收到一张照片" not in text:
            bad.append("气泡内容不对：%r" % (text,))
    if not client.downloads:
        bad.append("图片消息没走到下载分支（downloads 是空的）")
    elif client.downloads[0] != ("om_x", "img_v2_CHAINKEY", "image"):
        bad.append("下载参数不对：%r" % (client.downloads[0],))
    if not client.replies:
        bad.append("收图之后飞书那边没收到回话")
    elif "收到一张照片" not in client.replies[-1]:
        bad.append("飞书回话内容不对：%r" % (client.replies,))
    if p._feishu_pending.get("media"):
        bad.append("收图之后 pending[media] 没清空（下一条照片会被顶掉）")
    saved = sorted((Path(root) / "feishu_media").glob("*/*.png"))
    if len(saved) != 1:
        bad.append("应落盘 1 张图，实得 %d" % len(saved))

    # ---- ② 工作线程里的异常必须被抓住：不能把线程带走，也不能静默 ----
    # (a) 下载那一步抛了个"谁也想不到"的异常（不是 HTTP/token/404 那几类）：
    #     内层会把它翻成"下载图片失败：…"，主人照样收到一句回话。
    p2 = _FeishuSelftestPet(root)
    c2 = _FakeFeishuClient(data=png_bytes, ctype="image/png")

    def _boom(*_a, **_kw):
        raise ValueError("自检故意造的意外异常")

    c2.download_resource = _boom
    p2.say = lambda text, **kw: p2.said.append(str(text))
    p2._feishu_bridge()
    p2._feishu.client = c2
    p2._on_feishu_message(_media_ev("image", {"image_key": "img_v2_BOOM"}))
    _pump(app, lambda: bool(c2.replies), timeout=6.0)
    if not c2.replies:
        bad.append("下载抛意外异常时，飞书那边一个字都没回（异常把线程带走了）")
    elif "下载图片失败" not in c2.replies[-1]:
        bad.append("下载意外异常的回话没说清是下载失败：%r" % (c2.replies[-1],))
    if p2._feishu_pending.get("media"):
        bad.append("下载意外异常之后 pending[media] 没清空")

    # (b) **存盘**那一步抛非 OSError 的意外异常：会落到 _feishu_do_media 最外层
    #     那个兜底 except 上 —— 这一条才是"异常绝不把线程带走"的正面证明：
    #     内层只接 OSError（写盘失败的常规情形），别的异常必须由兜底接住。
    p3 = _FeishuSelftestPet(root)
    c3 = _FakeFeishuClient(data=png_bytes, ctype="image/png")
    p3.say = lambda text, **kw: p3.said.append(str(text))
    p3._feishu_bridge()
    p3._feishu.client = c3
    real_save = pf.save_media_bytes

    def _boom_save(*_a, **_kw):
        raise ValueError("自检故意造的意外异常（存盘那一步）")

    pf.save_media_bytes = _boom_save
    try:
        p3._on_feishu_message(_media_ev("image", {"image_key": "img_v2_BOOM2"}))
        _pump(app, lambda: bool(c3.replies), timeout=6.0)
    finally:
        pf.save_media_bytes = real_save
    if not c3.replies:
        bad.append("存盘抛意外异常时，飞书那边一个字都没回"
                   "（工作线程直接死了，之后所有收图都会静默失灵）")
    elif "意外错误" not in c3.replies[-1]:
        bad.append("兜底那层没把话说成「意外错误」：%r" % (c3.replies[-1],))
    if p3._feishu_pending.get("media"):
        bad.append("兜底之后 pending[media] 没清空")

    # ---- ③ multipart：按 RFC 7578 用标准库解析器验（不发任何请求）----
    body, ctype = pf._multipart([("image_type", "message")],
                                [("image", "x.png", "image/png", png_bytes)])
    if not ctype.startswith("multipart/form-data; boundary="):
        bad.append("multipart 的 Content-Type 不对：%r" % (ctype,))
    head = body[:body.find(png_bytes)] if png_bytes in body else body
    if b"\n" in head.replace(b"\r\n", b""):
        bad.append("multipart 头部有裸 LF（RFC 要求 CRLF 分隔）")
    if not body.endswith(b"--\r\n"):
        bad.append("multipart 结尾不是「--边界-- + CRLF」：%r" % (body[-24:],))
    msg = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body)
    if not msg.is_multipart():
        bad.append("标准库解析器认为这不是合法 multipart（body 结构坏了）")
    else:
        parts = msg.get_payload()
        names = [q.get_param("name", header="content-disposition")
                 for q in parts]
        if names != ["image_type", "image"]:
            bad.append("multipart 的字段名/顺序不对：%r" % (names,))
        got_file = [q for q in parts
                    if q.get_param("name", header="content-disposition") == "image"]
        if not got_file:
            bad.append("multipart 里没有 image 文件部分")
        else:
            if got_file[0].get_payload(decode=True) != png_bytes:
                bad.append("multipart 里的图片字节和原文件不一致")
            if got_file[0].get_content_type() != "image/png":
                bad.append("图片部分没带 Content-Type：%r"
                           % got_file[0].get_content_type())

    # ---- ④ 上传与"发图片消息"两个请求的形状（打桩抓参数，不出网）----
    captured = {}
    real_hm = pf._http_multipart

    def _fake_hm(url, fields, files, timeout=60, bearer=""):
        captured["url"] = url
        captured["fields"] = fields
        captured["files"] = [(n, fn, ct) for n, fn, ct, _d in files]
        captured["bearer"] = bool(bearer)
        return {"code": 0, "data": {"image_key": "img_v3_UP"}}, "{}"

    posts = []
    try:
        pf._http_multipart = _fake_hm
        up = pf.FeishuClient("cli_x", "s", on_event=lambda e: None,
                             on_log=lambda s: None)
        up._token, up._token_exp = "T", 9e18      # 别去真取 token
        img = root / "up.png"
        img.write_bytes(png_bytes)
        key2 = up.upload_image(str(img))
    finally:
        pf._http_multipart = real_hm
    if key2 != "img_v3_UP":
        bad.append("upload_image 没把返回体里的 image_key 取出来：%r" % (key2,))
    if not str(captured.get("url") or "").endswith("/open-apis/im/v1/images"):
        bad.append("上传的 URL 不对：%r" % (captured.get("url"),))
    if captured.get("fields") != [("image_type", "message")]:
        bad.append("上传表单里 image_type 不对：%r" % (captured.get("fields"),))
    if not captured.get("files") or captured["files"][0][0] != "image":
        bad.append("上传表单里文件字段名不是 image：%r" % (captured.get("files"),))
    if not captured.get("bearer"):
        bad.append("上传没带 Authorization")

    send = pf.FeishuClient("cli_x", "s", on_event=lambda e: None,
                           on_log=lambda s: None)
    send.upload_image = lambda path, image_type="message": "img_v3_STUB"
    send._post = lambda path, body: posts.append((path, body)) or {"code": 0}
    key3 = send.send_image("oc_x", str(root / "up.png"), reply_id="om_x")
    if key3 != "img_v3_STUB":
        bad.append("send_image 的返回值不是 image_key：%r" % (key3,))
    if not posts:
        bad.append("send_image 一个请求都没发出去")
    else:
        path, payload = posts[-1]
        # reply 挂在主人那条消息下面；content 必须是**字符串**化的 JSON
        if path != "/open-apis/im/v1/messages/om_x/reply":
            bad.append("图片消息的 reply URL 不对：%r" % (path,))
        if payload.get("msg_type") != "image":
            bad.append("图片消息的 msg_type 不是 image：%r" % (payload.get("msg_type"),))
        content = payload.get("content")
        if not isinstance(content, str):
            bad.append("图片消息的 content 不是字符串化 JSON（飞书会静默收下、"
                       "主人什么也看不到）：%r" % (content,))
        elif json.loads(content).get("image_key") != "img_v3_STUB":
            bad.append("content 里的 image_key 不对：%r" % (content,))

    # ---- ⑤ 失败提示：主人看到的那句话要说得清"下一步做什么" ----
    for err, want in (
            (RuntimeError("图片 12.0 MB 超过 10MB 上限，飞书不收"), "10MB"),
            (RuntimeError('上传被拒：code=234001 msg=x（返回体：{"code":234001}）'),
             "飞书那边拒了"),
            (RuntimeError("取 tenant_access_token 失败：code=10014"), "token"),
            (RuntimeError("连不上飞书（最多等 60 秒）：timed out"), "超时")):
        why = pf.image_failure_reason(err, "shot.png", 12345)
        if want not in why:
            bad.append("image_failure_reason 没把 %r 说清楚（实得 %r）"
                       % (str(err)[:30], why))

    beat.stop()
    shutil.rmtree(root, ignore_errors=True)
    return bad


def _feishu_media_problems(pet):
    """飞书媒体五条：图片路由 / 存盘规则 / 非图片回话 / /shot 命令 / 跨线程截图链。"""
    import shutil
    import tempfile

    import pet_feishu as pf
    import pet_bridges as pbr
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QCoreApplication.instance()
    if app is None:
        return ["自检里没有 QApplication（这一段要转事件循环才能验跨线程）"]

    bad = []
    root = Path(tempfile.mkdtemp(prefix="whale_pet_feishu_media_"))

    # ---- ① 命令：/shot 与它的别名（不能误伤普通中文）----
    for raw in ("/shot", "/screen", "/screenshot", "/截图", "截图"):
        if pf.parse_command(raw) != ("cmd", "shot", None):
            bad.append("%r 没被认成 /shot（实得 %r）"
                       % (raw, pf.parse_command(raw)))
    if pf.parse_command("看截图") != ("text", "看截图", None):
        bad.append("「看截图」被命令吃掉了（该当普通文字）")
    if pf.parse_command("帮我跑一下测试") != ("text", "帮我跑一下测试", None):
        bad.append("/shot 的别名误伤了普通句子")
    for raw, want in (("/help", "help"), ("/list", "list"), ("/harness", "harness"),
                      ("/status", "status"), ("/new", "new")):
        if pf.parse_command(raw) != ("cmd", want, None):
            bad.append("加了 /shot 之后老命令 %s 解析变了（实得 %r）"
                       % (raw, pf.parse_command(raw)))
    if pf.parse_command("3") != ("cmd", "pick", 3):
        bad.append("纯数字挑会话的解析被 /shot 影响了")

    # ---- ② 图片消息被路由到下载分支（打桩：断言"下载被调到、参数对"）----
    fake = _FakeFeishuBridge(root)
    fake.client.data = b"\x89PNG" + b"A" * 2048
    fake.client.ctype = "image/png"
    p1 = _FeishuSelftestPet(root)
    p1._feishu = fake
    ev = _media_ev("image", {"image_key": "img_v2_SELFTESTKEY123456"})
    if not ev or ev["message_type"] != "image" or not ev["content"]:
        bad.append("normalize_event 没把 message_type/content 带出来（实得 %r）"
                   % (ev,))
    p1._on_feishu_message(ev)
    got = _pump(app, lambda: bool(fake.client.downloads))
    if not got:
        bad.append("图片消息没走到下载分支（downloads=%r）" % fake.client.downloads)
    elif fake.client.downloads[0] != ("om_x", "img_v2_SELFTESTKEY123456", "image"):
        bad.append("下载参数不对（实得 %r，要 ('om_x', 'img_v2_SELFTESTKEY123456', "
                   "'image')）" % (fake.client.downloads[0],))
    # 回话 + 冒泡 + pending 清空：三件都要有
    _pump(app, lambda: len(fake.replies) >= 1 and not p1._feishu_pending.get("media"))
    if not fake.replies or "收到一张照片" not in fake.replies[-1][2]:
        bad.append("图片存好后没在飞书回话（replies=%r）" % (fake.replies,))
    else:
        line = fake.replies[-1][2]
        if "2049" not in line and "2.0" not in line and "KB" not in line:
            bad.append("回话里没带文件大小（实得 %r）" % line)
    if not p1.said:
        bad.append("图片存好后桌宠没冒气泡")
    if p1._feishu_pending.get("media"):
        bad.append("图片处理完 pending[media] 没清空（下一条会回错消息）")

    # ---- ③ 存盘路径与文件名规则（就用①落下来的那个文件来验）----
    found = sorted((Path(root) / "feishu_media").glob("*/*.png"))
    if len(found) != 1:
        bad.append("feishu_media 下应有 1 个落盘文件，实得 %d 个：%r"
                   % (len(found), [str(x) for x in found]))
    else:
        path0 = found[0]
        day_dir = path0.parent.name
        name = path0.name
        # 目录名必须是 8 位日期（用真实"今天"算，不是写死某一天）
        if day_dir != datetime.now().strftime("%Y%m%d"):
            bad.append("日期目录不对：实得 %r，要 %r"
                       % (day_dir, datetime.now().strftime("%Y%m%d")))
        # 名字必须长在 <配置目录>/feishu_media/<日期>/ 里面（不是别处）
        want_root = Path(root) / "feishu_media"
        if want_root not in path0.parents:
            bad.append("落盘位置不在 <配置目录>/feishu_media/<日期>/ 下：%r" % path0)
        # 文件名 = <8位日期>_<6位时分秒>_<image_key 前 12 位><扩展名>。
        # ⚠️ 时间戳本身带一个下划线，而且 image_key 里也带下划线
        # （img_v2_xxx）—— 所以**不能按 "_" 切段去验**：切出来的是
        # "img_v2" 而不是 key 的那一段（第一版就这么错，自检先报了个假失败）。
        # 时间戳那一段更**不能跟"现在"逐位比**：跨秒落盘时必然差一秒
        # （也在自检里当场踩到）。所以：前 8 位是今天 + 第 9 位是下划线 +
        # 后 6 位是数字，然后拿"去掉时间戳之后的尾巴"比 key 和扩展名。
        head = name.split("_", 1)[0]
        if head != datetime.now().strftime("%Y%m%d"):
            bad.append("文件名不是今天开头（实得 %r，要 %r）"
                       % (head, datetime.now().strftime("%Y%m%d")))
        mid = name[9:15]
        if len(mid) != 6 or not mid.isdigit():
            bad.append("文件名里没有 6 位时分秒（实得 %r）" % mid)
        if name[15] != "_":
            bad.append("时间戳与 image_key 之间没有下划线（实得 %r）" % name[:18])
        want_name = pf.media_filename("img_v2_SELFTESTKEY123456", "image/png")
        if name[16:] != want_name[16:]:
            bad.append("文件名里没带 image_key 前 12 位（实得 %r，同规则应为 %r）"
                       % (name, want_name))
        if not name.endswith(".png"):
            bad.append("按 image/png 该存成 .png（实得 %r）" % name)
        # 图片 bytes 必须原样落盘（不是被文本化、被截断）
        if path0.read_bytes() != fake.client.data:
            bad.append("落盘内容与下载到的字节不一致（%d vs %d）"
                       % (len(path0.read_bytes()), len(fake.client.data)))
    # 顺带把"扩展名表"和"造名函数"这两条纯规则喂死参数验一遍
    for ctype, want_ext in (("image/jpeg", ".jpg"), ("IMAGE/PNG; charset=binary", ".png"),
                            ("image/gif", ".gif"), ("application/octet-stream", ".png"),
                            ("", ".png")):
        if pf.ext_for_mime(ctype) != want_ext:
            bad.append("Content-Type %r 该映射成 %r，实得 %r"
                       % (ctype, want_ext, pf.ext_for_mime(ctype)))
    fixed = datetime(2026, 10, 11, 13, 45, 30)
    want_name = pf.media_filename("img_v2_ABCDEFGHIJKLMNOP", "image/jpeg",
                                  now=fixed)
    if want_name != "20261011_134530_img_v2_ABCDE.jpg":
        bad.append("文件名规则不对：实得 %r，要 '20261011_134530_img_v2_ABCDE.jpg'"
                   % want_name)
    dirt = pf.media_filename("../../evil/x", "image/png", now=fixed)
    if "/" in dirt or "\\" in dirt or ".." in dirt:
        bad.append("image_key 里的路径字符没洗净（实得 %r）" % dirt)

    # ---- ④ 非图片类型要回话，不能静默 ----
    for mtype, content, why in (
            ("file", {"file_key": "file_v2_x"}, "文件"),
            ("audio", {"file_key": "file_v2_a"}, "语音"),
            ("media", {"file_key": "file_v2_m"}, "视频"),
            ("sticker", {"file_key": "file_v2_s"}, "表情"),
            ("post", {"title": "标题", "content": []}, "富文本"),
            ("unknown_type", {}, "认不出的类型")):
        p = _FeishuSelftestPet(root)
        fb = _FakeFeishuBridge(root)
        p._feishu = fb
        p._on_feishu_message(_media_ev(mtype, content))
        if not fb.replies:
            bad.append("%s 消息被静默忽略了（一个字都没回）" % why)
        else:
            line = fb.last()
            if len(line) < 6:
                bad.append("%s 的回话太短、看不懂：%r" % (why, line))
            if mtype in ("file", "audio", "media", "sticker") and "收不了" not in line:
                bad.append("%s 的回话没说清「收不了」（实得 %r）" % (why, line))
    # 白名单外的图片消息：必须**安静**（不能因为图片走另一条路就漏过白名单）
    p2 = _FeishuSelftestPet(root, allow_sender="ou_boss")
    fb2 = _FakeFeishuBridge(root)
    p2._feishu = fb2
    p2._on_feishu_message(_media_ev("image", {"image_key": "img_v2_nope"}))
    app.processEvents()
    if fb2.replies or fb2.client.downloads:
        bad.append("白名单外的图片消息没被挡住（replies=%r downloads=%r）"
                   % (fb2.replies, fb2.client.downloads))

    # ---- ⑤ /shot 链路 + 跨线程部分能被构造出来不报错 ----
    # (a) 路由层：假桥，不真抓屏
    p3 = _FeishuSelftestPet(root)
    fb3 = _FakeFeishuBridge(root)
    p3._feishu = fb3
    p3._on_feishu_message(_media_ev("text", {"text": "/shot"}))
    app.processEvents()
    if not fb3.replies:
        bad.append("/shot 一句都没回（主人不知道接没接单）")
    elif "马上发给你" not in fb3.replies[0][2]:
        bad.append("/shot 第一句不是「接单」（实得 %r）" % fb3.replies[0][2])
    if not fb3.shot_files:
        bad.append("/shot 没调用抓屏接口（shot_files 是空的）")
    if not fb3.images:
        bad.append("/shot 抓完屏没把图片排进发送队列")
    else:
        chat_id, path, reply_id = fb3.images[-1]
        if chat_id != "oc_selftest" or reply_id != "om_x":
            bad.append("/shot 发图时的会话/回复 id 不对（实得 %r）"
                       % ((chat_id, reply_id),))
        if not path.endswith(".png"):
            bad.append("/shot 排队的是非 PNG：%r" % path)
        if Path(path).exists():
            bad.append("假桥发完图之后临时文件还在：%r" % path)
    if p3._feishu_pending.get("shot"):
        bad.append("/shot 收尾后 pending[shot] 没清空")

    # (b) 抓屏失败：要说清原因，不能装作发过了
    p4 = _FeishuSelftestPet(root)
    fb4 = _FakeFeishuBridge(root)
    fb4.capture_screen_file = lambda timeout=10.0: ""
    p4._feishu = fb4
    p4._on_feishu_message(_media_ev("text", {"text": "/shot"}))
    app.processEvents()
    if not fb4.replies or "/shot" not in fb4.replies[-1][2]:
        bad.append("抓屏失败时没给出「再发一次 /shot」这种能照做的话（实得 %r）"
                   % (fb4.replies,))
    if fb4.images:
        bad.append("抓屏失败却还是排了图片出去")

    # (c) 排不上队列：临时文件要当场删掉（别在 _tmp 里攒）
    p5 = _FeishuSelftestPet(root)
    fb5 = _FakeFeishuBridge(root, send_ok=False)
    p5._feishu = fb5
    p5._on_feishu_message(_media_ev("text", {"text": "/shot"}))
    app.processEvents()
    if not fb5.shot_files:
        bad.append("排不上队列那一路没抓到图（检查本身就没生效）")
    elif Path(fb5.shot_files[-1]).exists():
        bad.append("排不上发送队列时，临时截图没被删掉：%r" % fb5.shot_files[-1])
    if not fb5.replies or "队列" not in fb5.replies[-1][2]:
        bad.append("排不上队列时没说清（实得 %r）" % (fb5.replies,))

    # (d) **跨线程**：用真桥的信号握手，抓屏那次调用得真的回到主线程做完。
    #     这里不要求"抓到非空图"（离屏平台可能没有屏幕），只要求：
    #     · 在工作线程里调用不会卡死（能在超时内返回）；
    #     · 主线程确实被排到过（_shot_tick 涨了）。
    real = pbr.build_feishu(pet)
    real.client = _FakeFeishuClient()
    before = real._shot_tick
    box = {}

    def _worker():
        # 自检必须**从工作线程**调它 —— 这条链路的全部意义就是"工作线程不碰 Qt"，
        # 在主线程里调等于绕过了要验的那一段（而且主线程调用时会同步返回，
    # 那样"排到主线程"这个断言就永远成立，等于没验）。
        box["path"] = real.capture_screen_file(timeout=6.0)

    th = threading.Thread(target=_worker, daemon=True, name="selftest-shot")
    th.start()
    _pump(app, lambda: not th.is_alive(), timeout=8.0)
    th.join(timeout=2.0)
    if th.is_alive():
        bad.append("跨线程抓屏卡死了（工作线程 8 秒没返回）")
    if real._shot_tick <= before:
        bad.append("跨线程抓屏没排到主线程（_shot_tick 没涨：%d → %d）"
                   % (before, real._shot_tick))
    if "path" not in box:
        bad.append("跨线程抓屏那条线程没写出返回值（可能抛异常了）")
    elif box["path"]:
        # 真抓到了：文件得在，且必须落在配置目录下的 feishu_media/_tmp
        shot = Path(box["path"])
        if not shot.exists():
            bad.append("跨线程抓屏回了路径但文件不存在：%r" % shot)
        elif shot.parent.name != "_tmp" or shot.parent.parent.name != "feishu_media":
            bad.append("截图临时文件没放在 <配置目录>/feishu_media/_tmp/（实得 %r）"
                       % shot)
        else:
            # 换台机器/换个平台可能真抓到（离屏平台通常抓不到，那条路也得走得通）
            print("      ↳ 跨线程抓屏真拿到了图：%s（%d 字节）"
                  % (shot.name, shot.stat().st_size))
            shot.unlink(missing_ok=True)
    else:
        # 抓不到不算失败 —— 但必须能说清"是这条路走通了、只是这台机器没图"，
        # 否则将来真机上 /shot 不出图时，没法区分"链路断"和"没屏幕"
        print("      ↳ 跨线程抓屏这条路走通了，但这台机器上没抓到图"
              "（离屏平台正常现象）")
    # 真桥的发图路径：队列项要被发送线程消费掉，并且临时文件被清掉
    sentinel = root / "sentinel_shot.png"
    sentinel.write_bytes(b"\x89PNG\r\n\x1a\n" + b"Z" * 64)
    if not real.send_image("oc_selftest", str(sentinel), "om_x"):
        bad.append("真桥的 send_image 没排进队列（返回 False）")
    else:
        ok = _pump(app, lambda: not sentinel.exists(), timeout=6.0)
        if not ok:
            bad.append("真桥发完图之后临时文件还在（%r）" % sentinel)
        if not real.client.sent:
            bad.append("真桥的发送线程没调用 client.send_image（sent=%r）"
                       % (real.client.sent,))
        elif real.client.sent[0][1] != str(sentinel):
            bad.append("真桥传给 client.send_image 的路径不对（实得 %r）"
                       % (real.client.sent[0],))

    # ---- 收尾：下载失败时要说清原因（token / 404 / 写盘）----
    for fail, want_word, why in (
            ("取 tenant_access_token 失败：code=10014 app secret invalid",
             "token", "token 过期"),
            ("HTTP 404：{\"code\":234001,\"msg\":\"resource not found\"}",
             "404", "下载 404"),
            ("HTTP 500：内部错误", "500", "服务端 5xx")):
        p6 = _FeishuSelftestPet(root)
        fb6 = _FakeFeishuBridge(root, client=_FakeFeishuClient(fail=fail))
        p6._feishu = fb6
        p6._on_feishu_message(_media_ev("image", {"image_key": "img_v2_fail"}))
        _pump(app, lambda f=fb6: bool(f.replies), timeout=4.0)
        if not fb6.replies:
            bad.append("%s 时一个字都没回" % why)
        elif want_word not in fb6.last():
            bad.append("%s 的回话里没提 %r（实得 %r）" % (why, want_word, fb6.last()))
    # 写盘失败（把配置目录的位置做成一个**文件**，mkdir 必失败）
    p7 = _FeishuSelftestPet(root)
    # data_dir(config) = config 的父目录 → 让父目录是一个普通文件，
    # feishu_media/ 就建不出来，正好走到"存盘失败"那一路
    (root / "nowhere").write_text("not a dir", encoding="utf-8")
    p7._config_path = root / "nowhere" / "whale_pet_config.json"
    fb7 = _FakeFeishuBridge(root)
    fb7.client.data = b"12345"
    p7._feishu = fb7
    p7._on_feishu_message(_media_ev("image", {"image_key": "img_v2_diskfail"}))
    _pump(app, lambda: bool(fb7.replies), timeout=4.0)
    if not fb7.replies:
        bad.append("写盘失败时一个字都没回")
    elif "存盘失败" not in fb7.last():
        bad.append("写盘失败没被识别成「存盘失败」（实得 %r）" % fb7.last())

    shutil.rmtree(root, ignore_errors=True)
    return bad
