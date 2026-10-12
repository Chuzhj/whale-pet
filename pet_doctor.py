# -*- coding: utf-8 -*-
"""体检（`--doctor`）：把「飞书接入 / 飞书图片 / 远程控制 / 派活给 Harness」
能不能用、为什么不能用，逐项**真测**一遍并写成报告。

和 `--selftest` 的分工：

* `--selftest` 验的是"画面、状态机、素材对不对" —— 要 Qt、要素材、不联网；
* `--doctor`   验的是"对外几条通道通不通" —— **不要 Qt、不要素材**，
  联网和起子进程都真来一次。

为什么刻意不依赖 Qt：诊断工具不该依赖被诊断的东西。哪天 Qt 插件没打进去、
窗口都起不来，`--doctor` 仍然能把依赖、路径、端口、握手的结果写出来。

⚠️ 第六节会**真发一条图片消息到主人自己的会话**（这是故意的：`/shot`
那条路上失败过一次、当时什么线索都没留下）。只想看连接状态就别跑 `--doctor`，
或者看到第六节之前 Ctrl+C。

用法：
    python whale_pet.py --doctor                    # 报告打到控制台
    WhalePet.exe --doctor --doctor-log 体检.log     # GUI 版没有控制台，写文件
"""
import importlib
import socket
import sys
import traceback
from pathlib import Path

from pet_constants import (
    APP_VERSION, DSH_CLI_JS_REL, DSH_EXE, FEISHU_AVAILABLE, HARNESS_DEFAULT_CWD,
    HARNESS_AVAILABLE, REMOTE_AVAILABLE, SESSION_LOG_AVAILABLE,
)
from pet_store import config_path, load_config

# 体检要真 import 的模块（名字, 说明）
_DEPENDENCIES = (
    ("websockets", "飞书长连接"),
    ("zstandard", "任务对话浏览器解 zstd"),
    ("dsh_acp_client", "派活给 Harness 的协议客户端"),
    ("pet_remote", "远程控制的 HTTP 服务"),
    ("pet_feishu", "飞书接入（自己实现的协议）"),
)


class _Report:
    """攒报告行：控制台 + 文件双写，任何一根断了都不影响另一根。"""

    def __init__(self, log_path=None):
        self.lines = []
        self.log_path = Path(log_path) if log_path else None

    def say(self, text=""):
        self.lines.append(text)

    def flush(self):
        body = "\n".join(self.lines) + "\n"
        try:
            if sys.stdout is not None:
                sys.stdout.write(body)
                sys.stdout.flush()
        except Exception:
            pass
        if self.log_path is not None:
            try:
                self.log_path.write_text(body, encoding="utf-8")
            except OSError as err:
                sys.stderr.write("[体检] 写不了报告文件：%s\n" % err)


def _mask(value, keep=4):
    """密钥打码：报告可能被截图/贴群里。"""
    value = str(value or "")
    if not value:
        return "(空)"
    if len(value) <= keep + 2:
        return "****"
    return value[:keep] + "****" + value[-2:]


def _import_check(report, name, why):
    """真 import 一次（不是 find_spec）：返回 (ok, 版本或原因)。"""
    try:
        mod = importlib.import_module(name)
    except Exception as err:
        report.say("  ✗ %-16s import 失败：%s: %s"
                   % (name, type(err).__name__, err))
        for line in traceback.format_exc().strip().splitlines()[-3:]:
            report.say("      " + line.strip())
        return False, str(err)
    version = getattr(mod, "__version__", "")
    report.say("  ✓ %-16s %s%s" % (name, ("v" + version + " ") if version else "",
                                   "（%s）" % why))
    return True, version


def _probe_port(bind, port):
    """真去 bind 一次端口：返回 (ok, 一句话)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((bind, int(port)))
        return True, "端口能绑上"
    except OSError as err:
        return False, "%s: %s" % (type(err).__name__, err)
    finally:
        s.close()


def _probe_acp_profile(report):
    """DSH 的 acp profile 目录在不在、**写不写得进去**。

    为什么单列这一项：DSH 每次起 ACP 都会重写 `profiles/acp/cordis.yml`
    （prepareProfile 里是 writeFileSync）。这个目录不可写时，子进程直接退出，
    桌宠只看到一句"ACP 子进程关闭了 stdout" —— 极难查。先自己写一个探针文件，
    就能把"写不进去"这一条单独指出来。
    """
    import os
    home = __import__("dsh_session_log").dsh_home()   # 分享版：自动识别（含别人电脑上的位置）
    prof = home / "profiles" / "acp"
    say = report.say
    say("  DSH_HOME：%s" % home)
    say("  acp profile：%s（存在=%s）" % (prof, prof.exists()))
    if not prof.exists():
        return False, "profile 目录不存在"
    for name in ("cordis.yml", "cordis.patch.yml", "package.json"):
        f = prof / name
        say("      %-18s %s" % (name, "在" if f.exists() else "**缺**"))
    probe = prof / ".whale-pet-write-probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        say("  ✓ 这个目录可写（DSH 每次起 ACP 都要往里写 cordis.yml）")
        return True, ""
    except OSError as err:
        say("  ✗ **这个目录写不进去**：%s: %s" % (type(err).__name__, err))
        say("      DSH 起 ACP 时会重写 cordis.yml，写不进去就是子进程立刻退出 ——")
        say("      这就是「派活不可用」的原因，跟桌宠本身无关。")
        return False, str(err)


def _probe_harness(report, cfg):
    """真起一次 ACP 握手（start → initialize → authenticate）。"""
    if not HARNESS_AVAILABLE:
        report.say("  ✗ 协议客户端没加载（见上面的 import 结果）")
        return False
    exe = Path(str(cfg.get("dsh_exe") or "").strip() or str(DSH_EXE))
    cli_js = exe.parent / DSH_CLI_JS_REL
    cwd = str(cfg.get("harness_cwd") or "").strip() or str(HARNESS_DEFAULT_CWD)
    report.say("  Harness 主程序：%s（存在=%s）" % (exe, exe.exists()))
    report.say("  协议脚本 cli.js：%s" % cli_js)
    report.say("       （asar 内的文件 os.path.exists 恒为 False，属正常）")
    report.say("  工作目录：%s（存在=%s）" % (cwd, Path(cwd).exists()))
    if not exe.exists():
        report.say("  ✗ 主程序不在 —— 要么 DSH 装在别处，要么配置里的 dsh_exe 要改")
        return False
    from dsh_acp_client import AcpClient
    profile_ok, profile_why = _probe_acp_profile(report)
    logs = []
    client = AcpClient(on_update=None, on_log=logs.append,
                       exe=exe, cli_js=cli_js)
    import time
    t0 = time.time()
    try:
        client.start()
        client.initialize()
        client.authenticate()
        report.say("  ✓ 握手成功（start → initialize → authenticate，%.1f 秒）"
                   % (time.time() - t0))
        ok = True
    except Exception as err:
        ok = False
        report.say("  ✗ 握手失败（%.1f 秒）：%s: %s"
                   % (time.time() - t0, type(err).__name__, err))
        if not profile_ok:
            report.say("      ↑ 上面已经指出根因：DSH 的 acp profile **写不进去**"
                       "（%s）—— 子进程一启动就退出了。" % profile_why)
        for line in traceback.format_exc().strip().splitlines()[-4:]:
            report.say("      " + line.strip())
    finally:
        try:
            client.stop(timeout=5.0)
        except Exception:
            pass
    if logs:
        report.say("  子进程最后的输出（最多 25 行）：")
        for line in logs[-25:]:
            report.say("      " + str(line)[:200])
    return ok


def _probe_feishu_ws(report, cfg):
    """真起一次飞书长连接（不发言、只握手），拿状态和最后一条错误。"""
    import time
    import pet_feishu
    logs = []
    client = pet_feishu.FeishuClient(
        str(cfg.get("feishu_app_id") or ""),
        str(cfg.get("feishu_app_secret") or ""),
        on_event=lambda ev: None,
        on_log=logs.append,
        domain=("lark" if str(cfg.get("feishu_domain")).lower() == "lark"
                else "feishu"))
    ok = client.start()
    if not ok:
        st = client.status()
        report.say("  ✗ 起不来：%s" % (st.get("error") or st.get("label")))
        return False
    deadline = time.time() + 15.0
    while time.time() < deadline:
        st = client.status()
        if st.get("online") or st.get("state") in ("offline", "error"):
            break
        time.sleep(0.5)
    st = client.status()
    online = bool(st.get("online"))
    report.say("  %s 状态=%s（%s）%s"
               % ("✓" if online else "✗", st.get("state"), st.get("label"),
                  ("，错误：%s" % st["error"]) if st.get("error") else ""))
    for line in logs[-8:]:
        report.say("      " + str(line)[:200])
    try:
        client.stop()
    except Exception:
        pass
    return online


def _probe_remote_server(report, cfg):
    """真起一次远程服务，带令牌请求一次 /api/state，再关掉。"""
    import http.client
    import pet_remote
    # 用一个空闲端口测，别占用主人真正在用的那个
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    token = pet_remote.gen_token()
    server = pet_remote.RemoteServer(
        emit_fn=lambda d: None,
        state_fn=lambda: {"energy": 1.0, "satiety": 1.0, "version": APP_VERSION},
        token=token, port=port, bind="127.0.0.1", allow_harness=False)
    try:
        server.start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/state?t=" + token)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        ok = resp.status == 200 and b"energy" in body
        report.say("  %s 服务起在 127.0.0.1:%d，带令牌请求 /api/state → HTTP %d"
                   % ("✓" if ok else "✗", port, resp.status))
        if not ok:
            report.say("      响应：%s" % body[:160])
        return ok
    except Exception as err:
        report.say("  ✗ 起服务/请求失败：%s: %s" % (type(err).__name__, err))
        return False
    finally:
        try:
            server.stop()
        except Exception:
            pass


def _make_test_png(width=240, height=120):
    """纯标准库造一张测试图（体检用）。

    为什么不塞一张 1×1 的图：主人要在飞书里**看到**点什么才算"这条路通了"，
    1×1 在手机上就是一个小点、看不出有没有发出来。这里画一张带彩色竖条的
    小图（30 行，zlib+struct 都是标准库）—— `--doctor` 依旧不依赖 Qt、
    不依赖 assets/ 里的素材。
    """
    import struct
    import zlib
    raw = bytearray()
    for y in range(height):
        raw.append(0)                     # 每行开头是 PNG 的 filter 类型：0=None
        for x in range(width):
            raw += bytes((x * 255 // max(1, width - 1),
                          y * 255 // max(1, height - 1),
                          128 if (x // 16) % 2 else 220))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _probe_feishu_image(report, cfg, cfg_path):
    """真上传一张图、真发一条图片消息。返回 '通过' / '失败' / '没测'。

    ⚠️ 这一步**会真发一条图片消息到主人自己的会话**（所以在报告中明写），
    而且只在 `--doctor` 里跑 —— `--selftest` 里绝不允许出现它。

    为什么要有这一节：2026-10-11 主人报"`/shot` 有回话、图传不到手机"，
    而当时的失败**只在内存日志里**，事后完全查不出根因。有了这一节，
    下次一条命令就知道是"上传被拒"还是"发消息被拒"，还能看到飞书原话。
    """
    import pet_feishu
    say = report.say
    say()
    say("【六】飞书图片：本机生成测试图 → 真上传 → 真发一条图片消息")
    say("      ！提醒：这一步会**真发一条图片消息到你自己的会话**。")
    if not cfg.get("feishu_app_id") or not cfg.get("feishu_app_secret"):
        say("  ✗ 没填 App ID / App Secret —— 跳过（先做第五节）")
        return "没测"

    # 收件人：优先"最近一次跟她说的话所在会话"（收消息时自动记下来的），
    # 退而求其次用白名单里那个 open_id。两个都没有就只能让主人先做一步。
    chat_id = str(cfg.get("feishu_last_chat") or "").strip()
    open_id = str(cfg.get("feishu_allow_sender") or "").strip()
    if chat_id:
        target, id_kind, who = chat_id, "chat_id", "最近跟她说过话的那个会话"
    elif open_id:
        target, id_kind, who = open_id, "open_id", "白名单里那个 open_id（你自己）"
    else:
        say("  ！不知道发给谁：既没有「最近说话的会话」，也没填"
            "「只认这个人的消息」（open_id）。")
        say("     做法二选一：① 先在飞书里给她随便发一条消息（她就记住了）；"
            "② 在「设置 → 飞书接入」里填上你的 open_id。然后重跑 --doctor。")
        return "没测"
    say("  收件人：%s（%s，%s）" % (target, id_kind, who))

    # 测试图放在应用自己的 _tmp 里（和数据目录同一套口径），发完就删
    tmp_dir = pet_feishu.media_root(cfg_path, tmp=True)
    png = Path(tmp_dir) / "doctor_image_test.png"
    logs = []
    client = pet_feishu.FeishuClient(
        cfg["feishu_app_id"], cfg["feishu_app_secret"],
        on_event=lambda ev: None, on_log=logs.append,
        domain=("lark" if str(cfg.get("feishu_domain")).lower() == "lark"
                else "feishu"))
    state = "失败"
    try:
        png.parent.mkdir(parents=True, exist_ok=True)
        raw = _make_test_png()
        png.write_bytes(raw)
        say("  测试图：%s（%d 字节，%dx%d，纯标准库现造）"
            % (png, len(raw), 240, 120))
        # ---- 第一步：上传换 image_key ----
        try:
            key = client.upload_image(str(png))
            say("  ✓ 上传成功：image_key=%s" % key)
        except Exception as err:
            say("  ✗ **上传失败**（图从这一步就出不去）：%s: %s"
                % (type(err).__name__, err))
            say("      翻成人话（飞书里会给主人的那句）：%s"
                % pet_feishu.image_failure_reason(err, str(png), len(raw)))
            say("      ↑ 这就是根因所在：先修上传（多半是权限/格式/大小），"
                "发消息那步还没轮到。")
            return "失败"
        # ---- 第二步：把 image_key 发成一条图片消息 ----
        try:
            client.send_image(target, str(png), receive_id_type=id_kind)
            say("  ✓ 图片消息已发出 —— 去飞书那个会话里看看有没有这张彩色小图")
            state = "通过"
        except Exception as err:
            say("  ✗ **发图片消息失败**（上传是好的）：%s: %s"
                % (type(err).__name__, err))
            say("      翻成人话（飞书里会给主人的那句）：%s"
                % pet_feishu.image_failure_reason(err, str(png), len(raw)))
        if logs:
            say("  客户端日志（最多 8 行）：")
            for line in logs[-8:]:
                say("      " + str(line)[:200])
    except Exception as err:
        say("  ✗ 这一节自己抛了异常：%s: %s" % (type(err).__name__, err))
        for line in traceback.format_exc().strip().splitlines()[-3:]:
            say("      " + line.strip())
    finally:
        try:
            png.unlink(missing_ok=True)
        except OSError:
            pass
    return state


def run_doctor(log_path=None, config_file=None):
    """体检主流程。返回 0 = 三项都通；1 = 至少有一项不通（或被关着）。

    config_file 显式给定时用它（体检"某一份配置"是常见需求：
    比如运行目录里那份和 exe 旁边那份不是同一个文件）。
    """
    report = _Report(log_path)
    say = report.say
    say("=" * 68)
    say("鲸鲸桌宠 · 对外通道体检（%s）" % APP_VERSION)
    say("=" * 68)

    # ---------------- 一、运行环境 ----------------
    say()
    say("【一】运行环境")
    say("  打包运行（frozen）：%s" % bool(getattr(sys, "frozen", False)))
    say("  可执行文件：%s" % sys.executable)
    meipass = getattr(sys, "_MEIPASS", "")
    say("  解包目录 _MEIPASS：%s" % (meipass or "(源码运行，没有)"))
    say("  当前工作目录：%s" % Path.cwd())
    for entry in sys.path[:4]:
        say("  sys.path 之一：%s" % entry)

    # ---------------- 二、配置 ----------------
    cfg_path = Path(config_file) if config_file else config_path()
    say()
    say("【二】配置（%s，存在=%s）" % (cfg_path, cfg_path.exists()))
    cfg = load_config(cfg_path)
    say("  远程：enabled=%s  bind=%s:%s  allow_harness=%s  token=%s"
        % (cfg.get("remote_enabled"), cfg.get("remote_bind"),
           cfg.get("remote_port"), cfg.get("remote_allow_harness"),
           _mask(cfg.get("remote_token"))))
    say("  飞书：enabled=%s  app_id=%s  secret=%s  domain=%s  allow_sender=%s"
        % (cfg.get("feishu_enabled"), cfg.get("feishu_app_id") or "(空)",
           _mask(cfg.get("feishu_app_secret")), cfg.get("feishu_domain"),
           cfg.get("feishu_allow_sender") or "(空 = 谁都能私聊她；群消息一律忽略)"))
    say("  Harness：dsh_exe=%s  cwd=%s  group=%s"
        % (cfg.get("dsh_exe") or "(用默认路径)",
           cfg.get("harness_cwd") or "(用默认工作区)",
           cfg.get("harness_group")))
    say("  对话：api_key=%s  provider=%s  model=%s"
        % (_mask(cfg.get("api_key")), cfg.get("provider"), cfg.get("model") or "(预设)"))

    # ---------------- 三、依赖（真 import） ----------------
    say()
    say("【三】依赖能不能真的 import（不是「只探测」）")
    results = {}
    for name, why in _DEPENDENCIES:
        results[name], _ = _import_check(report, name, why)
    # 这一条是关键：available() 只做 find_spec，可能"说能用、import 却失败"
    try:
        import pet_feishu
        probe = pet_feishu.available()
        real = results.get("pet_feishu", False)
        say("  find_spec 说飞书可用=%s；真 import 结果=%s%s"
            % (probe, real, "" if probe == real else "   ← 两者不一致，以真 import 为准"))
        if not probe:
            say("      missing_reason：%s" % pet_feishu.missing_reason())
    except Exception as err:
        say("  find_spec 对比失败：%s" % err)

    # ---------------- 四、菜单可见性 ----------------
    say()
    say("【四】右键菜单里会出现哪些入口")
    say("  派活给 Harness…  %s（HARNESS_AVAILABLE=%s）"
        % ("会出现" if HARNESS_AVAILABLE else "**不会出现**", HARNESS_AVAILABLE))
    say("  远程控制…        %s（REMOTE_AVAILABLE=%s）"
        % ("会出现" if REMOTE_AVAILABLE else "**不会出现**", REMOTE_AVAILABLE))
    say("  飞书接入…        总是会出现（缺依赖时由对话框说明缺什么）")
    say("  任务对话浏览器    %s（SESSION_LOG_AVAILABLE=%s）"
        % ("可用" if SESSION_LOG_AVAILABLE else "**不可用**", SESSION_LOG_AVAILABLE))

    # ---------------- 五、飞书实测 ----------------
    say()
    say("【五】飞书：真连一次（凭据 + 长连接握手）")
    feishu_ok = False
    if not results.get("pet_feishu"):
        say("  ✗ 模块 import 不了，先解决依赖")
    elif not cfg.get("feishu_app_id") or not cfg.get("feishu_app_secret"):
        say("  ✗ 没填 App ID / App Secret —— 去飞书开放平台建应用，"
            "再把两项填进「飞书接入…」")
    else:
        try:
            import pet_feishu
            domain = (pet_feishu.LARK_DOMAIN
                      if str(cfg.get("feishu_domain")).lower() == "lark"
                      else pet_feishu.FEISHU_DOMAIN)
            ok, msg = pet_feishu.check_credentials(
                cfg["feishu_app_id"], cfg["feishu_app_secret"], domain)
            say("  %s 凭据自检（取 tenant_access_token）：%s"
                % ("✓" if ok else "✗", msg))
            if ok:
                feishu_ok = _probe_feishu_ws(report, cfg)
        except Exception as err:
            say("  ✗ 自检抛异常：%s: %s" % (type(err).__name__, err))

    # ---------------- 六、飞书图片真测 ----------------
    image_state = "没测"
    if results.get("pet_feishu"):
        try:
            image_state = _probe_feishu_image(report, cfg, cfg_path)
        except Exception as err:
            say("  ✗ 图片那一节抛异常：%s: %s" % (type(err).__name__, err))
            image_state = "失败"

    # ---------------- 七、远程实测 ----------------
    say()
    say("【七】远程控制：真试端口 + 真起一次服务并请求一次")
    remote_ok = False
    if not results.get("pet_remote"):
        say("  ✗ 模块 import 不了，先解决依赖")
    else:
        bind = str(cfg.get("remote_bind") or "127.0.0.1")
        port = int(cfg.get("remote_port") or 8765)
        port_ok, why = _probe_port(bind, port)
        say("  %s 配置的端口 %s:%s → %s"
            % ("✓" if port_ok else "✗", bind, port, why))
        remote_ok = _probe_remote_server(report, cfg)
        if not cfg.get("remote_enabled"):
            say("  ！注意：配置里 remote_enabled=False —— **服务当前是关着的**。"
                "菜单入口在，但要在「远程控制…」里勾上启用再保存。")
        if bind not in ("127.0.0.1", "localhost", "::1"):
            say("  ！监听 %s = 同一局域网都能连；不用时改回 127.0.0.1" % bind)
        if not cfg.get("remote_token"):
            say("  令牌是空的 → 第一次开启时会自动生成并写回配置")

    # ---------------- 八、Harness 实测 ----------------
    say()
    say("【八】派活给 Harness：真起一次 ACP 握手")
    harness_ok = _probe_harness(report, cfg)

    # ---------------- 九、结论 ----------------
    say()
    say("=" * 68)
    say("结论")
    say("  飞书接入   ：%s" % ("可用" if feishu_ok else "**不可用**（看第五节）"))
    say("  飞书图片   ：%s" % (
        {"通过": "可用（刚真发了一张图）",
         "失败": "**不可用**（看第六节，那里有飞书原话）",
         "没测": "没测（没收件人 / 没填凭据，见第六节）"}[image_state]))
    say("  远程控制   ：%s"
        % ("可用" if remote_ok else "**不可用**（看第七节）")
        + ("" if cfg.get("remote_enabled") else "，且当前是关着的"))
    say("  派活 Harness：%s" % ("可用" if harness_ok else "**不可用**（看第八节）"))
    say("=" * 68)
    report.flush()
    # 图片那一节"失败"要算进飞书通道的结论里：飞书能连上但发不出图，
    # 对主人来说就是"飞书图片功能不可用"，不能报成绿的。
    if image_state == "失败":
        feishu_ok = False
    return 0 if (feishu_ok and remote_ok and harness_ok) else 1
