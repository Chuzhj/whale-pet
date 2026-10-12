# -*- coding: utf-8 -*-
"""鲸鲸桌宠 —— 蓝色大肥鱼的桌面形态。

一个透明、无边框、永久置顶的桌面宠物窗口。素材是一组透明底 Q 版立绘。
"活起来"靠四样东西：定时眨眼、呆毛摆动、尾巴摆动、睡觉时的呼吸起伏。
（不用 Live2D，所以不做整体位移与呼吸缩放。）

呆毛和尾巴各自单独成层，叠在"去掉了该部件的底图"上，各绕自己的根部转：
  * 呆毛（ahoge_front.png）：轴心在根部，底图由 AI 生成"无呆毛版"补全头顶
  * 尾巴（tail_front.png）：轴心在脖颈，底图直接把尾巴挖透明 ——
    因为尾巴整段悬在背景上，背后本来就是背景，不必补画
两者的眨眼帧 / 表情帧里原本都烘焙着固定角度的同一部件，
已在素材阶段抹掉（strip_baked_ahoge.py），所以醒着的三种状态共用这两层，
动作才连得上。相位由 _tick 持续推进，从不归零。

用法示例：
    python whale_pet.py                     # 默认启动（60fps）
    python whale_pet.py --height 420        # 换成 420 像素高
    python whale_pet.py --fps 30            # 降帧率省电
    python whale_pet.py --pos 1200,600      # 固定初始位置
    python whale_pet.py --selftest          # 离屏自检，不弹窗口
    python whale_pet.py --version           # 打印版本号

交互：拖拽移动 / **双击打开对话窗口**（Harness 模式开工作台，否则开聊天窗）/
      单击顺序切换表情（切过去就保持住，不自动收回）/
      待机时随机冒台词 / 右键菜单（和鲸鲸说句话、显示时钟、做个表情、叫醒/去睡觉、
      回到待机、派活给 Harness、远程控制、隐藏）。
隐藏：在角色上点右键 → 隐藏（程序缩进系统托盘，不退出）。
托盘：系统托盘有鲸鱼图标，右键 → 「打开」（请回桌宠）/「退出」（真正退出程序）。

台词画在一个**独立的气泡窗口**里，而不是画在宠物窗口内部 ——
原因见 build_bubble 的注释（宠物窗口只比角色大 14%，上方仅余 35px，塞不下）。

「和鲸鲸说句话」会调大模型：key 放在同目录的 whale_pet_config.json 里（见 CONFIG_NAME）。
没配 key 时宠物照常工作，只是不会说话 —— 不留半死不活的中间状态。
她的回复会**一直留在桌面气泡上**，直到主人点她一下或拖她一下才收起来。
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
from pet_assets import *          # noqa: F401,F403
from pet_bridges import *          # noqa: F401,F403
from pet_constants import *          # noqa: F401,F403
from pet_doctor import *          # noqa: F401,F403
from pet_feishu import *          # noqa: F401,F403
from pet_ledger import *          # noqa: F401,F403
from pet_panels import *          # noqa: F401,F403
from pet_sound import *          # noqa: F401,F403
from pet_ui import *          # noqa: F401,F403
from pet_mixin_anim import *          # noqa: F401,F403
from pet_mixin_chat import *          # noqa: F401,F403
from pet_mixin_life import *          # noqa: F401,F403
from pet_mixin_link import *          # noqa: F401,F403
from pet_mixin_ui import *          # noqa: F401,F403
from pet_net import *          # noqa: F401,F403
from pet_remote import *          # noqa: F401,F403
from pet_store import *          # noqa: F401,F403
from pet_tags import *          # noqa: F401,F403
from pet_time import *          # noqa: F401,F403
from pet_widget import *          # noqa: F401,F403
from pet_windows import *          # noqa: F401,F403
from pet_selftest import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)
from pet_constants import _pet_feishu      # 私有名：import * 带不走
from pet_sound import configure as configure_sound
from pet_store import data_dir
from pet_feishu import (_CMD_ALIASES, _STATE_LABELS, _decode_header, _http_json, _put_blob, _put_num, _put_str, _read_varint, _varint_bytes)
from pet_remote import (_IPV4_RE, _lan_ip)
from pet_store import (_alarm_path, _append_jsonl, _atomic_write_text, _clamp_float, _dpapi, _mask_secret, _mask_url, _protect_secret, _read_jsonl, _read_text, _seal_secrets, _state_path, _unprotect_secret, _warn, _write_json, _write_jsonl)
from pet_tags import (_extract_tag)
from pet_time import (_cn_num)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="whale_pet",
        description="鲸鲸桌宠 —— 透明置顶的桌面宠物",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--assets", type=Path,
                        default=resource_dir() / "assets",
                        help="立绘素材目录")
    parser.add_argument("--height", type=int, default=0,
                        help="角色显示高度（像素）；0 = 用配置里的 pet_height"
                             "（设置窗口里也能调，默认 340）")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="在 --height 基础上再乘一个系数")
    parser.add_argument("--fps", type=int, default=60,
                        help="动画帧率（5~120）；默认 60，动画更顺滑")
    parser.add_argument("--opacity", type=float, default=1.0,
                        help="整体不透明度（0.2~1.0）")
    parser.add_argument("--pos", default=None, metavar="X,Y",
                        help="初始位置；缺省停在屏幕右下角")
    parser.add_argument("--no-float", action="store_true",
                        help="禁用上下浮动")
    parser.add_argument("--no-swap", action="store_true",
                        help="禁用待机自动换动作")
    parser.add_argument("--top", action="store_true",
                        help="强制让桌宠永远置顶（默认不置顶：别的程序窗口会正常"
                             "盖住她和气泡，层级见 pet_windows.build_bubble）")
    parser.add_argument("--no-expr", action="store_true",
                        help="禁用表情（右键菜单和对话心情都不再切换表情）")
    parser.add_argument("--no-talk", action="store_true",
                        help="禁用台词气泡")
    parser.add_argument("--no-ai", action="store_true",
                        help="禁用「和鲸鲸说句话」（不读配置、不联网）")
    parser.add_argument("--config", type=Path, default=None,
                        help="配置文件路径；缺省用 %s" % CONFIG_NAME)
    parser.add_argument("--ask", default=None, metavar="问题",
                        help="启动后自动把这句话发给她（自测/脚本用）")
    parser.add_argument("--talk-gap", type=float, default=None, metavar="SEC",
                        help="待机台词用固定间隔（秒）；缺省在 %g~%g 秒之间随机"
                             % (TALK_GAP_MIN, TALK_GAP_MAX))
    parser.add_argument("--selftest", action="store_true",
                        help="离屏自检：校验素材 alpha 通道并渲染若干帧后退出")
    parser.add_argument("--selftest-log", type=Path, default=None, metavar="路径",
                        help="把自检输出同时写进这个文件 —— 打包成 exe 后没有"
                             "控制台，这是唯一能事后查自检结果的办法")
    parser.add_argument("--doctor", action="store_true",
                        help="对外通道体检：飞书/远程/派活三条通道逐项实测，"
                             "把「不可用」变成「为什么不可用」")
    parser.add_argument("--doctor-log", type=Path, default=None, metavar="路径",
                        help="把体检报告写进这个文件（GUI 版没有控制台）")
    parser.add_argument("--no-bars", action="store_true",
                        help="不显示头顶的精力/饱食度能量条（现在是默认）")
    parser.add_argument("--bars", action="store_true",
                        help="强制显示头顶的精力/饱食度能量条（默认已隐藏）")
    parser.add_argument("--version", action="store_true",
                        help="打印版本号后退出（用来确认桌面跑的是不是最新版）")
    return parser.parse_args(argv)


def load_pet_art(buckets, height):
    """按目标高度把角色素材全部加载好，返回给 `build_widget` 的那一组参数。

    **启动与「设置 → 桌宠大小」共用这一个函数**，这是关键：
    改大小必须**重新读盘重采样**（原图通常比显示尺寸大得多，重采样很干净），
    不能拿手里那张已经缩小过的 pixmap 去放大 —— 那是二次重采样，会明显发糊
    （主人 2026-07-11 报的"改完大小画面模糊、退出重进才恢复"就是它）。
    两处各写一份加载逻辑的话，又会分叉出"启动清晰、改大小糊"这种更难查的毛病。

    呆毛与尾巴例外：它们保持**原尺寸**，绘制时才按
    `k = 显示宽 / AHOGE_CANVAS[0]` 换算（见 pet_widget 的绘制部分）——
    这里不能预先缩放，否则会双重缩放。
    """
    awake_h = max(48, int(height * AWAKE_SCALE))
    sleep_h = max(48, int(height * SLEEP_SCALE))
    pool_paths = buckets["front"] + buckets["variant"] or buckets["back"]
    blink_paths = {asset_key(p.stem): p for p in buckets["blink"]}

    def paired_blink(source_path):
        path = blink_paths.get(asset_key(source_path.stem))
        return load_scaled(path, awake_h) if path else None

    return {
        "frames": [load_scaled(p, awake_h) for p in pool_paths],
        "expressions": [(asset_key(p.stem), load_scaled(p, awake_h))
                        for p in buckets["expression"]],
        "idle_blinks": [paired_blink(p) for p in pool_paths],
        "expr_blinks": [paired_blink(p) for p in buckets["expression"]],
        "sleep_pixmap": (load_scaled(buckets["sleep"][0], sleep_h)
                         if buckets["sleep"] else None),
        "ahoge_pixmap": (QPixmap(str(buckets["ahoge"][0]))
                         if buckets["ahoge"] else None),
        "tail_pixmap": (QPixmap(str(buckets["tail"][0]))
                        if buckets["tail"] else None),
        # 白饭（主人的要求）：满碗 / 空碗一对，两张同画布同锚点（见 prep_bowl.py），
        # 所以吃空换碗时不会跳。缺素材就整块功能不出现，不留半截状态。
        "bowl_full_pixmap": next(
            (QPixmap(str(p)) for p in buckets["bowl"]
             if "empty" not in p.stem.lower()), None),
        "bowl_empty_pixmap": next(
            (QPixmap(str(p)) for p in buckets["bowl"]
             if "empty" in p.stem.lower()), None),
    }


def main(argv=None):
    args = parse_args(argv)

    if args.version:
        print("鲸鲸桌宠 %s" % APP_VERSION)
        return 0

    # 体检放在**任何 Qt 导入之前**：Qt 插件没打进去、窗口都起不来的时候，
    # 这条命令恰恰是最该能用的那条（诊断工具不该依赖被诊断的东西）。
    if args.doctor:
        return run_doctor(args.doctor_log, args.config)

    # 离屏平台必须在任何 Qt 对象创建之前设好，所以放在这里而不是 __main__ 里
    if args.selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication

    # ---- 同时只允许一个桌宠进程（主人 2026-07-11 要求）----
    # 三条规矩：
    #   ① **诊断模式（--selftest）不上锁**：主人在桌宠跑着的时候也该能跑自检
    #      （我自己就被这个锁挡过一次：自检直接退出、什么都没检查）；
    #   ② 锁放在**每用户固定位置**（不是数据目录）：同一台机器上不管从哪个副本
    #      启动，都只允许一只鲸鲸。用 QLockFile 而不是"看看文件在不在"：它把持锁
    #      进程的 pid 写进去，进程被强杀/断电留下的陈旧锁能自动识别并接管 ——
    #      自己用文件存在与否判断的话，崩一次就再也起不来了；
    #   ③ **锁用不了 ≠ 不许启动**：目录只读之类的情况下 tryLock 也会失败，
    #      必须看 error() 分清"被别人占着"和"我自己建不出来" ——
    #      后者要放行（大不了这次没有单实例保护），不能让主人用不了桌宠。
    from PySide6.QtCore import QLockFile
    lock_dir = Path(os.environ.get("LOCALAPPDATA")
                    or str(data_dir(args.config))) / "WhalePet"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        lock_dir = data_dir(args.config)      # 退而求其次：跟配置同目录
    poke_file = lock_dir / "whale_pet.show"
    instance_lock = None
    if not args.selftest:
        lock_path = lock_dir / "whale_pet.lock"
        instance_lock = QLockFile(str(lock_path))
        instance_lock.setStaleLockTime(30000)
        if not instance_lock.tryLock(0):
            if instance_lock.error() == QLockFile.LockFailedError:
                # 真有一只鲸鲸在跑：留一个"叫我出来"的记号再退出 ——
                # 于是"再双击一次 exe"＝把那只鲸鲸请回前台，而不是毫无反应。
                try:
                    poke_file.write_text(str(time.time()), encoding="utf-8")
                except OSError:
                    pass
                print("[启动] 已经有一只鲸鲸在跑了（锁：%s）—— 这次退出，并请她出来。"
                      % lock_path)
                return 0
            print("[启动] 单实例锁在这个目录用不了（%s）—— 继续启动，"
                  "这次不做单实例保护。" % instance_lock.error())
            instance_lock = None
    # instance_lock 要活到进程结束（被回收就等于放锁）——挂到 main 的局部变量
    # 上就行：main 只在退出时才返回。

    # 只把程序名交给 Qt：否则 Qt 会试图解析我们自己的 --height 之类的参数
    app = QApplication(sys.argv[:1])
    app.setApplicationName("whale_pet")
    # 桌宠是常驻程序，退出只走右键菜单 —— 不能让"关掉某个窗口"带崩全局。
    # 默认的 quitOnLastWindowClosed 在这里是个炸弹：宠物全家都是 Qt.Tool
    # （不算"真窗口"），而钥匙框 QDialog 是唯一的普通窗口 —— 点"算了"
    # 关掉它，Qt 就认为"最后一个窗口关了"，整个程序跟着退出（主人实测踩到）。
    app.setQuitOnLastWindowClosed(False)

    buckets = discover_assets(args.assets)
    # 高度优先看命令行；没给就看配置里的 pet_height（设置窗口调的就是它）；
    # 都没有才用默认 340。这样"设置里调完大小、重启还是这个大小"是自洽的。
    _startup_conf = load_config(args.config)
    try:
        _conf_height = int(_startup_conf.get("pet_height") or 0)
    except (TypeError, ValueError):
        _conf_height = 0
    height = max(48, int((args.height or _conf_height or 340) * args.scale))
    # 醒着 / 睡姿各有一个基准高度，在这里一次算好：
    # 站姿、表情、闭眼帧必须同尺寸（否则切换时角色会突然变大变小），
    # 所以它们共用 awake_h。
    awake_h = max(48, int(height * AWAKE_SCALE))
    sleep_h = max(48, int(height * SLEEP_SCALE))

    if args.selftest:
        if args.selftest_log:
            # 打包后没有控制台：让自检把结果写文件，事后能查
            return run_selftest_logged(buckets, args.assets, awake_h, sleep_h,
                                       args.selftest_log)
        return run_selftest(buckets, args.assets, awake_h, sleep_h)

    # 素材加载：启动与"改大小"共用 `load_pet_art`（见那个函数的说明 ——
    # 改大小必须重新读盘重采样，缩放已经缩小过的 pixmap 会糊）。
    art = load_pet_art(buckets, height)
    frames = art["frames"]

    # 音效：把配置里的开关/音量/事件交给播放器（真正放的时候还会现读一次音量档）。
    # 失败只是没声音，不能让桌宠起不来 —— 所以整块包在 try 里。
    try:
        _sound_cfg = load_config(args.config)
        configure_sound(_sound_cfg.get("sound_enabled", True),
                        _sound_cfg.get("sound_volume", 70),
                        _sound_cfg.get("sound_events") or {},
                        data_dir(args.config) / SOUND_CACHE_DIRNAME)
        print("[音效] %s" % ("已开启" if _sound_cfg.get("sound_enabled", True)
                             else "已关闭"))
    except Exception as err:
        print("[音效] 初始化失败（不影响桌宠）：%s: %s"
              % (type(err).__name__, err))

    # 对话功能：要气泡才能说话，所以 --no-talk 时一并关掉。
    # 没配 key 就整个不建 —— 状态只有"能用"和"没有"，没有中间态。
    chat = search = None
    if not args.no_ai and not args.no_talk:
        config = load_config(args.config)
        candidate = build_chat(None, config)
        if candidate.ready():
            chat = candidate
            base, model, _native = resolve_endpoint(config)
            print("[对话] %s / %s / 联网=%s"
                  % (base, model, config.get("search")))
            if str(config.get("search")) == "bing":
                search = build_search(None, config)
        else:
            print("[对话] 未启用：%s 里没填 api_key（路径 %s）"
                  % (CONFIG_NAME, args.config or config_path()))

    WhalePet = build_widget(frames, height, args.fps, args.opacity,
                            # 默认**不置顶**（主人 2026-10-11 定的层级：
                            # 别的程序窗口 > 气泡 > 桌宠）；--top 可强制置顶
                            args.top or bool(_startup_conf.get("always_on_top")),
                            expressions=art["expressions"],
                            do_expr=not args.no_expr,
                            idle_blinks=art["idle_blinks"],
                            expr_blinks=art["expr_blinks"],
                            sleep_pixmap=art["sleep_pixmap"],
                            ahoge_pixmap=art["ahoge_pixmap"],
                            tail_pixmap=art["tail_pixmap"],
                            bowl_full_pixmap=art["bowl_full_pixmap"],
                            bowl_empty_pixmap=art["bowl_empty_pixmap"],
                            do_talk=not args.no_talk,
                            talk_gap=args.talk_gap,
                            chat=chat,
                            search=search,
                            show_bars=(SHOW_ENERGY_BARS or args.bars)
                                     and not args.no_bars)
    pet = WhalePet()
    # 设置窗口里的「桌宠大小」靠这个回调用**原图重新加载**（不是缩放旧 pixmap，
    # 那样会糊 —— 主人 2026-07-11 报的"改完大小画面模糊"）。
    # ⚠️ 分享版是**单形态**的，所以这里直接按当前高度取素材，没有形态分岔。
    pet._asset_reloader = lambda h: load_pet_art(buckets, h)
    # 界面里改 key 要写回**这份**配置，而不是无脑写默认路径
    pet._config_path = args.config
    # 记住当前角色高度：设置窗口里的"桌宠大小"靠它算缩放比例（见 apply_pet_size）
    pet._pet_height = int(height)
    # 台词气泡字号
    try:
        pet.bubble.set_font_px(int(_startup_conf.get("bubble_font_px")
                                   or BUBBLE_FONT_PX))
    except Exception as _err:
        print("[气泡] 应用字号失败：%s: %s" % (type(_err).__name__, _err),
              file=sys.stderr)
    # 精力/饱食度：读回上次退出时的值（此刻 _config_path 才是准的）
    pet.load_state()
    # 余额自动观测：「今日已用」是靠余额观测推的，不常看就只是一段段缺口。
    # 默认 5 分钟一次（配置里可改，0 = 关）；全靠只读接口，不花 token。
    try:
        if pet.start_balance_watch(load_config(args.config).get(
                "balance_refresh_min", BALANCE_REFRESH_MIN)):
            # 开起来 20 秒后先记第一笔：不然刚开机那几个小时都算"没观测到"
            QTimer.singleShot(20000, pet.refresh_balance_quiet)
            print("[记账] 每 %s 分钟自动记一笔余额"
                  % load_config(args.config).get("balance_refresh_min",
                                                 BALANCE_REFRESH_MIN))
    except Exception as err:
        print("[记账] 自动观测没起来（不影响桌宠）：%s: %s"
              % (type(err).__name__, err), file=sys.stderr)
    # 远程遥控：上次开着，这次就还开着（随桌宠自启；起不来只报一句，别带崩桌宠）
    # 分享版（SHARE_EDITION）不启动局域网远程控制 —— 永不监听任何端口。
    if (REMOTE_AVAILABLE and not SHARE_EDITION
            and load_config(args.config).get("remote_enabled")):
        try:
            # token 打码：终端输出会进日志、会被截图，而它是唯一身份凭证。
            # 要完整地址去设置框里复制（那里有「复制」按钮）。
            print("[远程] %s" % _mask_url(pet.remote_start(
                load_config(args.config))))
        except Exception as err:
            print("[远程] 没起来：%s: %s" % (type(err).__name__, err),
                  file=sys.stderr)
    # 飞书接入：同样"上次开着，这次就还开着"。长连接完全在后台线程里跑，
    # 起不来只是没连上，绝不影响桌宠本体。
    if FEISHU_AVAILABLE and load_config(args.config).get("feishu_enabled"):
        try:
            ok, msg = pet.feishu_start()
            print("[飞书] %s" % (msg if ok else ("没起来：" + msg)))
        except Exception as err:
            print("[飞书] 没起来：%s: %s" % (type(err).__name__, err),
                  file=sys.stderr)
    # 飞书这一条既是有用的状态，也是**打包自检的信号**：
    # 真包里如果没把 websockets 收进去，这里会明说"不可用：缺依赖：websockets"，
    # 不用等到主人去点菜单才发现。
    print("[飞书] %s" % ("已就绪（右键 →「飞书接入…」填 App ID / Secret）"
                        if FEISHU_AVAILABLE else
                        "不可用：%s" % (_pet_feishu.missing_reason()
                                        if _pet_feishu else "pet_feishu 没加载")))
    print("[状态] 精力 %.1f / 饱食 %.1f / 上次退出时%s（文件 %s）"
          % (pet._energy, pet._satiety,
             "是**睡着的**，已静默躺回睡姿" if pet.sleeping() else "醒着",
             _state_path(pet._config_path)))
    # 安全体检：把"开着、但等于把电脑交出去"的组合点出来（只警告不改行为）
    for line in security_warnings(load_config(args.config)):
        print("[安全] %s" % line)
    pet.update_bars()
    # 长期提醒：读回文件里的提醒并装上定时器，再起一个每分钟的巡检
    # （近的靠精确定时器，远的和关机期间错过的靠巡检兜底）
    pending_alarms = pet.load_persisted_alarms()

    def watch_alarms():
        pet.check_alarms()

    pet._alarm_watch = QTimer(pet)
    pet._alarm_watch.timeout.connect(watch_alarms)
    pet._alarm_watch.start(ALARM_WATCH_MS)
    QTimer.singleShot(2200, watch_alarms)      # 启动后先补一次：错过的一并提醒
    print("[提醒] 长期提醒 %d 条待触发（文件 %s）"
          % (pending_alarms, data_dir(pet._config_path) / ALARM_LOG_NAME))

    if not args.no_ai and not args.no_talk and chat is not None:
        # 要求 4：启动时清掉三天前的对话日志，并把长期记忆 + 最近对话带进上下文
        pruned = prune_chat_log(pet._config_path)
        if pruned:
            print("[记忆] 清理了 %d 条过期对话日志（超过 %d 天）"
                  % (pruned, CHAT_LOG_KEEP_DAYS))
        pet._memory_context = build_memory_context(pet._config_path)
        chat.memory_context = pet._memory_context
        memories = len(_read_jsonl(
            data_dir(pet._config_path) / MEMORY_LOG_NAME))
        print("[记忆] 长期记忆 %d 条 / 最近对话已注入%s"
              % (memories, "（首次运行，从零开始记）" if not pet._memory_context else ""))

    if args.pos:
        try:
            px, py = (int(v) for v in args.pos.split(","))
        except ValueError:
            raise SystemExit("--pos 需要形如 1200,600 的格式")
        pet.place_at(px, py)
    else:
        pet.reset_position()

    pet.show()
    pet.schedule_ask(args.ask)

    # 第二个进程留下的"叫我出来"记号：每秒看一眼，看到就把她请回桌面。
    # 这样主人再双击一次 exe（或者从开始菜单又点一次）＝ 叫她出来，
    # 而不是"什么都没发生"（单实例保护下最容易让人以为程序坏了）。
    def _check_poke():
        try:
            if poke_file.exists():
                poke_file.unlink()
                pet.show_from_tray()
        except OSError:
            pass

    poke_timer = QTimer(pet)
    poke_timer.timeout.connect(_check_poke)
    poke_timer.start(1000)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
