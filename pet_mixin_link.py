# -*- coding: utf-8 -*-
"""对外通道：派活给 Harness(ACP)、远程遥控、飞书接入。

三者共同点：都是「外面来的指令进主线程、结果再送回去」，所以放一层。"""
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
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_sound import play as play_sound      # 音效（异步、失败即静音）
from pet_sound import preview as sound_preview    # 音效页的「试听」要用它
# ⚠️ 可选模块的私有名同样带不走，必须显式导入：漏了不会当场报错，
#    运行到那行才 NameError，被 except 一吞就成"静默失灵"（拆分时踩过）。
from pet_constants import _pet_feishu, _pet_remote, _session_log
from pet_bridges import (build_feishu, build_harness, build_harness_console, build_remote)
import pet_ledger                     # 「关于」页要报记账本路径
import pet_store                      # 「关于」页要报数据目录
from pet_settings import build_settings
from pet_store import (_mask_url, append_chat_log, load_config, save_config_values)
from pet_time import (format_seconds)
# 设置窗口里各页的控件都走 pet_ui 这套统一风格（原来远程/飞书框里那些
# 各写各的 setStyleSheet 已经删掉了）
from pet_ui import (COLOR_BLUE, COLOR_DEEP, DANGER, OK_GREEN, card_style,
                    ghost_btn, hint, label, primary_btn, quiet_btn, section,
                    ui_font)


class LinkMixin:
    def harness_cfg(self):
        """把配置里 Harness 相关的几项合成桥需要的参数。

        每次**现读**配置而不是缓存 —— 主人改完 json 立刻生效，
        不用重启桌宠（edit_api_key 的教训：写错文件等于改了没生效）。
        """
        conf = load_config(self._config_path)
        exe = Path(str(conf.get("dsh_exe") or "").strip() or str(DSH_EXE))
        return {
            "exe": exe,
            "cli_js": exe.parent / DSH_CLI_JS_REL,
            "cwd": (str(conf.get("harness_cwd") or "").strip()
                    or str(HARNESS_DEFAULT_CWD)),
            "group": str(conf.get("harness_group") or "deepseek-account"),
        }

    def _harness_busy(self):
        """Harness 那边现在有没有活在跑。

        抽出来是因为有三个地方要问同一件事（远程页面状态、飞书状态、
        飞书派活前的检查）。以前是各写一遍同样的表达式 —— 那种写法迟早分叉。
        """
        return self._harness is not None and self._harness.busy()

    def _harness_console(self):
        """懒建 Harness 工作台窗口（左边选对话 / 右边看历史接着说）。"""
        if self._harness_win is None:
            self._shell_window()
        return self._harness_win

    def _shell_window(self):
        """懒建**聊天与派活共用**的那个窗口（pet_shell，两模式一键切换）。

        为什么要合并（主人 2026-10-11 的要求）：以前是聊天窗 + 工作台两个窗口，
        同一件事（跟她说点什么）在两处长得不一样、还得自己切。现在是一个窗口：
        左边栏永远是任务列表，右边内容按模式切换，栏底那个按钮一键换模式。

        这里把两个**门面**挂回原来的两个名字上：
          * `self.chat_window` ← shell.chat
          * `self._harness_win` ← shell.work
        于是所有老调用点（add_her / finish / adopt_session…）一个字都不用改 ——
        这是把改动面压到最小的关键：新界面只负责长相，调用协议保持原样。
        """
        if self._shell is not None:
            return self._shell
        from pet_shell import build_shell
        self._shell = build_shell(
            self,
            on_chat_send=self.start_chat,
            on_chat_clear=self.clear_chat_display,
            # from_console=True：那句"主人说的"已经由窗口画进历史里了，
            # 桌宠这边不用再补一遍（远程派活才需要补）
            on_task_send=lambda text: self._harness_send(text, from_console=True),
            on_task_select=self._set_harness_target,
            get_task_target=lambda: self._harness_target,
            # 栏底那个「切换聊天/工作模式」点下去要广播回来，
            # 否则桌宠的 _harness_mode 会和窗口显示的模式脱节
            on_mode_change=self._on_shell_mode,
            on_task_delete=self.delete_task_session)
        self.chat_window = self._shell.chat
        self._harness_win = self._shell.work
        return self._shell

    def delete_task_session(self, session_id, title=""):
        """工作界面里删掉选中的任务：**先弹确认框**，确认了才真删。

        主人 2026-10-11 的要求："可以在桌宠工作界面上删除选定任务的所有内容，
        包括本地的任务文件夹，删除前确认"。

        "所有内容" = 任务文件夹 + 它派出去的**子会话（含各层，孙子辈也算）** +
        各层的投影缓存（标题那份 json）✓。
        三道保险：① `plan_delete` 只算不动，并先校验路径必须在 DSH 的 sessions 里；
        ② 确认框列出"删哪些、多大、几个子会话（含各层）"，默认按钮是**取消**（手滑不会删）；
        ③ 真删时**再校验一次**路径 ✓。
        """
        import dsh_session_log as _sdc
        try:
            info = _sdc.plan_delete(session_id)
        except Exception as err:
            self.say("删除前检查出错了：%s: %s" % (type(err).__name__, err))
            return False
        if not info.get("ok"):
            self.say("这段任务删不了：%s" % (info.get("reason") or "未知原因"))
            return False
        try:
            from PySide6.QtWidgets import QMessageBox
        except Exception:
            return False
        box = QMessageBox(self)
        box.setWindowTitle("删除这段任务？")
        box.setIcon(QMessageBox.Warning)
        box.setText("要删掉「%s」的全部内容吗？" % ((title or session_id)[:30]))
        lines = ["将删除：",
                 # 这一行只报**主目录**的文件数（含各层子会话的大数在"合计"那行），
                 # 两处混着写会让主人以为主目录有几百个文件。
                 "· 本地任务文件夹 %s（%d 个文件）"
                 % (info["dirs"][0].name, len(info.get("main_files") or info["files"]))]
        if info.get("sub_count"):
            # "含各层"是主人 2026-10-11 点名要的：子智能体自己还会再派活，
            # 孙子辈也一起删，这里得把规模说清（不然主人以为是漏删/多删）。
            lines.append("· 它派出去的 %d 个子会话（含各层，最深第 %d 层）"
                         % (info["sub_count"], info.get("sub_depth") or 1))
        lines.append("· 投影缓存（标题等索引信息，主会话与各层子会话各一份）")
        lines.append("合计约 %.1f MB（含以上全部）"
                     % (info["bytes"] / 1024.0 / 1024.0))
        lines.append("")
        lines.append("删除后不可恢复。")
        box.setInformativeText("\n".join(lines))
        yes = box.addButton("删除", QMessageBox.DestructiveRole)
        no = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(no)          # 默认停在"取消"上：手滑不会删
        box.exec()
        if box.clickedButton() is not yes:
            return False
        ok, msg = _sdc.delete_session(session_id)
        self.say(msg)
        try:
            self._shell_window().refresh_sessions()   # 那一行要立刻从列表里消失
        except Exception:
            pass
        return ok

    def _on_shell_mode(self, mode):
        """窗口里切了模式 → 同步桌宠这边的 `_harness_mode`。

        为什么必须同步：工作模式输入框里那句是**派活**（真的会改文件 / 跑命令），
        聊天模式才是闲聊。两边不一致就会出现"看着像聊天，实际在家里改文件"。

        重入保护：`open_harness_mode()` 自己也会让窗口切到工作模式，
        那次回调进来时状态已经一致 → 直接返回，不会来回转圈。
        """
        want = (str(mode) == "work")
        if want == bool(self._harness_mode):
            return
        if want:
            self.open_harness_mode()
        else:
            self.leave_harness_mode()

    def _set_harness_target(self, target):
        """工作台选了哪段对话 → 记到桌宠这边（派活时要拿它去 resume）。

        目标只有这一处数据源，窗口只管显示 ——
        否则迟早出现"窗口以为接的是 A、桌宠按 B 派活"的分叉。
        """
        self._harness_target = target

    def open_harness_mode(self):
        """进「派活」模式：开工作台窗口，一步到位。

        上一版是"先弹框选对话、再开聊天窗"，两步做的是同一件事：
        决定接哪段。现在左边那个列表就是那个选择，而且能**边看历史边决定** ——
        信息还更多（原选择框只给标题和时间，看不到正文）。
        """
        if not self.harness_cfg()["exe"].exists():
            self.say("没找到 Harness……路径对不上，我先不动。"
                     "可以在 whale_pet_config.json 里填 dsh_exe。")
            return
        self._harness_mode = True
        self._harness_console().open_window()
        self.say("工作台开好了，选一段对话接着说～")

    def leave_harness_mode(self):
        """退出派活模式。窗口**留着**：历史还能翻，
        只是不再往 Harness 派活了（看历史和能不能派活是两件事）。"""
        self._harness_mode = False
        self.say("Harness 模式关了，继续陪你聊天～")

    def toggle_harness_mode(self):
        if self._harness_mode:
            self.leave_harness_mode()
        else:
            self.open_harness_mode()

    def _harness_bridge(self):
        """懒建桥并接好信号。"""
        if self._harness is None:
            self._harness = build_harness(self)
            self._harness.chunk.connect(self._on_harness_chunk)
            self._harness.event.connect(self._on_harness_event)
            self._harness.usage.connect(self._on_harness_usage)
            self._harness.done.connect(self._on_harness_done)
            self._harness.session.connect(self._on_harness_session)
        return self._harness

    def _on_harness_session(self, sid, cwd):
        """桥报回"这段活跑在哪个会话上"。

        对**新开**的会话，这里要立刻把它记成目标 —— 主人说第二句话才是
        "接着这段说"，而不是又开一段。工作台那边同步把这一行选上。
        """
        cur = self._harness_target or {}
        if cur.get("id") != sid:
            # 走到这儿说明是**新开**的（接旧的会命中同一个 id 直接跳过）。
            # 标题先用占位：等日志刷出来，工作台选中那一行时会带回真名。
            self._set_harness_target({"id": sid, "cwd": cwd,
                                      "title": "（刚开的对话）"})
        if self._harness_win is not None:
            self._harness_win.adopt_session(sid, cwd)
        if self._harness_mode and self._harness_win is not None:
            self._harness_win.set_status("会话 %s… 干活中" % sid[:18])

    def _harness_send(self, text, from_console=False, target=None):
        """把一句话变成一个真正的 Harness 任务。

        from_console=True 表示这句话是主人**在工作台输入框里**打的 ——
        工作台已经把它画进历史了，这里不重复画（远程/飞书派活才需要补那一句）。

        target 显式给定时用它。飞书那边自己有"接哪段旧对话"的选择，
        和桌面工作台的选中项是**两份独立的状态** —— 手机挑的那段不该
        顺手改掉电脑上打开着的那段。
        """
        bridge = self._harness_bridge()
        if bridge.busy():
            self._harness_note("上一个活还没干完呢，别催……", "错误")
            self.say("上一个活还没干完呢，别催……")
            return
        if self.deep_sleep():
            # 和聊天同一条规矩：沉睡叫不醒、不干活、也不扣精力
            reply = random.choice(LINES["deep_sleep_reply"])
            self._harness_note(reply)
            self.say(reply)
            return
        if self.sleeping():
            self.wake_up(quiet=True)
        paths = self.harness_cfg()
        target = target or self._harness_target or {}
        sid = str(target.get("id") or "")
        # 接旧对话时用**那段对话自己的工作目录**，不能用默认的 ——
        # ACP 的 resume 要拿 cwd 校验会话工作区，给错了会被拒。
        cwd = str(target.get("cwd") or "") or paths["cwd"]
        append_chat_log(self._config_path, "主人", text)   # 和聊天同一本日志
        self.dismiss_chat_bubble()
        self._chat_buffer = ""
        console = self._harness_console()
        if not from_console:
            console.note(text, "主人")       # 远程派活：替它补上主人那句
        console.set_busy(True)
        console.set_status("接单！去 Harness 干活……")
        self.say("（接单！让鲸鲸去 Harness 干活……）", hold=0)
        self.set_expression("cheer")
        if not bridge.start(text, paths["exe"], paths["cli_js"],
                            cwd, paths["group"], sid):
            console.fail("没派出去：上一个任务还没收尾。")
            console.set_busy(False)

    def _harness_note(self, text, role="助手"):
        """往工作台补一行（窗口还没建就悄悄跳过，绝不为了记日志去建窗口）。"""
        if self._harness_win is not None:
            self._harness_win.note(text, role)

    def _on_harness_event(self, line):
        """工具调用流水：进工作台的历史里（一串灰字），不打断正文。"""
        self._remote_publish("tool", line)   # 远程页面也要看她在干嘛
        self._feishu_publish("tool", line, for_kind="harness")   # 飞书那边同理（自己限量）
        if self._harness_win is not None:
            self._harness_win.tool(line)

    def _on_harness_usage(self, used, size):
        """上下文用量：每变过 1% 才更新一次，不然工具跑多了会刷屏。"""
        if not size:
            return
        pct = int(used * 100 / size)
        if pct == self._harness_usage_pct:
            return
        self._harness_usage_pct = pct
        text = "token %d / %d（%d%%）· 白饭在变少" % (used, size, pct)
        if self._harness_win is not None:
            self._harness_win.set_status(text)
        self.say("（%s）" % text, hold=0)

    def _on_harness_done(self, ok, text):
        """收尾：工作台收流 + 重读历史，桌面上她冒一句预览。

        和聊天模式共用 _show_reply / _fail_reply（气泡驻留、_chat_last
        这些细节那两条已经处理好了，重写一遍迟早分叉）。
        """
        append_chat_log(self._config_path, "鲸鲸", text)   # 和聊天同一本日志
        if ok:
            play_sound("task")        # 任务结束音：只在**干成了**的时候响
        if self._harness_win is not None:
            self._harness_win.set_busy(False)
            self._harness_win.set_status("干完了。" if ok else "出错了。")
            if ok:
                # finish 会清流式缓存 + 刷列表 + 从日志重读 ——
                # 重读是关键：日志里那段带着完整工具流水和真实轮次，
                # 显示出来和"任务对话"里看到的完全一致。
                self._harness_win.finish(text)
            else:
                self._harness_win.fail(text)
        self._remote_publish("done" if ok else "err", text)
        self._feishu_publish("done" if ok else "err", text, for_kind="harness")
        if ok:
            self.set_expression("thanks")
            self.spend_energy(ENERGY_PER_CHAT)   # 干活也算一问一答的体力
            self.update_bars()
            self._show_reply(text)
        else:
            self._fail_reply("好像搞砸了：" + text)
            self.set_expression("cry")

    def _on_harness_chunk(self, piece):
        """派活流的增量：只进工作台（和远程页面），不碰聊天那条管线。

        不碰 _chat_buffer 是关键：那是闲聊的半成品，混进派活正文之后
        再回到闲聊，气泡里就会先冒出一截任务输出。
        """
        self._remote_publish("chunk", piece)   # 远程页面的打字机
        if self._harness_win is not None:
            self._harness_win.stream(piece)

    def _remote_bridge(self):
        if self._remote is None:
            self._remote = build_remote(self, self._remote_state,
                                        self._remote_sessions,
                                        self._remote_history)
            # ⚠️ 这一行是命脉：HTTP 服务把指令 emit 到这个信号上，
            # 没有监听者就等于"网页上发什么都是石沉大海"，而且**不报错**
            # （服务端照常回 202）。之前就是漏了它 —— 手机和电脑网页
            # 都聊不动、也不回复，症状完全不指向这里。
            self._remote.command.connect(self._on_remote_command)
        return self._remote

    def _remote_sessions(self):
        """给网页端的任务对话列表（只读文件，不碰 Qt —— 在 socket 线程里被调）。"""
        if not SESSION_LOG_AVAILABLE:
            return []
        try:
            rows = [r for r in _session_log.list_sessions() if r.get("cwd")]
        except Exception:
            return []
        # 只放网页需要的字段：log_path 是本机路径，没必要送到浏览器
        return [{"id": r["id"], "title": r["title"], "cwd": r["cwd"],
                 "turns": r["turns"], "time_label": r["time_label"]}
                for r in rows]

    def _remote_history(self, sid):
        """某段任务对话的正文，[[角色, 正文], ...]。

        **sid 走白名单**：先去会话列表里查有没有这个 id，查到才读它的日志。
        所以浏览器传什么进来都不可能读到别的文件（穿越防护在查找这一步，
        而不是在拼路径那一步 —— 拼路径过滤是防不住的）。
        """
        if not SESSION_LOG_AVAILABLE:
            return []
        try:
            rows = _session_log.list_sessions(include_empty=True)
        except Exception:
            return []
        row = next((r for r in rows if r.get("id") == sid), None)
        if row is None:
            return []
        try:
            turns = _session_log.read_transcript(row["log_path"],
                                                 include_tools=True)
        except Exception:
            return []
        # 网页一次别塞太多：取最近 300 条，够翻最近的进展了
        return [[role, text] for role, text in turns[-300:]]

    def _remote_state(self):
        """给远程页面的状态。只读数字和布尔、不碰任何 QWidget ——
        它在 socket 线程里被调（HTTP handler 直呼）。"""
        return {
            "version": APP_VERSION,
            "energy": self._energy, "energyMax": ENERGY_MAX,
            "satiety": self._satiety, "satietyMax": SATIETY_MAX,
            "sleeping": self.sleeping(), "deep": self.deep_sleep(),
            "busy": self._harness_busy(),
        }

    def _remote_publish(self, kind, text=""):
        """推一条事件给远程页面。服务没开就是一次布尔判断，
        不拖累聊天/派活这些本地热路径。"""
        if self._remote is not None and self._remote.is_running():
            self._remote.publish(kind, text)

    def remote_start(self, conf):
        """按一份配置把服务起起来，返回可打开的 URL。"""
        token = str(conf.get("remote_token") or "").strip()
        if not token:
            token = _pet_remote.gen_token()
        server = self._remote_bridge().start(
            token=token,
            port=int(conf.get("remote_port") or 8765),
            bind=str(conf.get("remote_bind") or "127.0.0.1"),
            allow_harness=bool(conf.get("remote_allow_harness")))
        self._remote_publish("say", "远程遥控开好了，我来啦～")
        return server.url()

    def remote_stop(self):
        if self._remote is not None:
            self._remote.stop()

    def _on_remote_command(self, cmd):
        """远程指令的落地处。信号跨线程排队过来的，这里已经在主线程。"""
        action = cmd.get("action")
        text = str(cmd.get("text") or "").strip()
        if action == "chat":
            # as_chat=True：网页上的"闲聊"就是闲聊。不加这一条，桌面工作台
            # 正开着时，手机发一句"在吗"会被劫成派活任务 ——
            # 同一个坑飞书那边也踩过（见 _feishu_chat），这里一并堵上。
            self.start_chat(text, as_chat=True)
        elif action == "harness":
            # 网页可以指定"接哪一段"（sid）。和飞书一样，这份选择是**远程
            # 自己的**，不去改桌面工作台当前选中的那段。
            sid = str(cmd.get("sid") or "").strip()
            target = None
            if sid:
                target = next((r for r in self._remote_sessions()
                               if r["id"] == sid), None)
                if target is None:
                    self._remote_publish("err", "那段对话找不到了（可能被清掉了）")
                    return
            self._harness_send(text, target=target)
        elif action == "feed":
            self.feed_bowl()
            self._remote_publish("say", "（有人隔着屏幕喂我吃饭……好开心）")
        elif action == "sleep":
            if not self.sleeping():
                self.toggle_sleep()
                self._remote_publish("say", "我去睡了，有事明天说……")
        elif action == "wake":
            if self.sleeping():
                self.toggle_sleep()
                self._remote_publish("say", "醒了醒了，这就来。")
        elif action == "talk":
            self.talk_once()

    # ------------------------------------------------------------------ 设置
    #
    # 「设置」是唯一的配置入口（主人 2026-10-11 定的）：API key / 飞书 /
    # 远程控制 / 音效 / 桌宠大小 / 关于 全收进这一个窗口。
    # 窗口壳在 pet_settings.build_settings，这里只负责给 pages 列表 +
    # 各页的内容工厂 —— 每个工厂返回一个 QWidget，页面之间互不知情。
    def open_settings(self, page=None):
        """打开统一设置窗口；page 给了就切到那一页。返回窗口（持引用防 GC）。

        旧的两个属性名在这里一起指向同一个窗口：老代码 / 老自检按
        `_key_dialog`、`_remote_dialog`、`_feishu_dialog` 找那几个框
        （`pet_selftest` 现在还在按 `_key_dialog` 验「点了关闭不带崩全局」）。
        让它们指向设置窗口，那批检查就还能跑，不用去改别人的自检文件。
        """
        if self._settings_win is None:
            self._settings_win = build_settings(self, self._settings_pages())
        self._key_dialog = self._settings_win
        self._remote_dialog = self._settings_win
        self._feishu_dialog = self._settings_win
        self._settings_win.open_page(page)
        return self._settings_win

    def _settings_pages(self):
        """(key, 竖栏标题, 页面工厂)。**顺序就是竖栏顺序**，工厂是懒调的。"""
        return [
            ("chat", "聊天与钥匙", self._api_key_page),
            ("feishu", "飞书接入", self._feishu_page),
            *([] if SHARE_EDITION else [("remote", "远程控制", self._remote_page)]),
            ("sound", "音效", self._sound_page),
            ("pet", "桌宠", self._pet_page),
            ("about", "关于", self._about_page),
        ]

    # --- 音效：复用右键菜单那个面板，embedded=True 就是"当成一页" ----
    def _sound_page(self):
        from pet_panels import build_sound_dialog
        from pet_settings import embedded_page
        page = build_sound_dialog(self, self._sound_cfg, self._save_sound_cfg,
                                  sound_preview, self._apply_sound_cfg,
                                  embedded=True)
        # 嵌进设置页：面板自己那张"页面底"已经铺好，外面只套一层容器
        # （QStackedWidget 的每一页必须是一个完整控件）
        return embedded_page(page)

    # --- 桌宠：大小 + 始终置顶 ----
    def _pet_page(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel,
                                       QSlider, QVBoxLayout, QWidget)
        from pet_ui import page_bg_style

        conf = load_config(self._config_path)
        try:
            current = int(self._pet_height or conf.get("pet_height") or 340)
        except (TypeError, ValueError):
            current = 340
        current = max(96, min(900, current))     # 和 apply_pet_size 的夹取范围一致

        page = QWidget()
        page_bg_style(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        size_card = QFrame()
        size_card.setObjectName("card")
        size_card.setStyleSheet(card_style(padding=12))
        size_lay = QVBoxLayout(size_card)
        size_lay.setContentsMargins(12, 10, 12, 10)
        size_lay.setSpacing(8)
        size_lay.addWidget(section("大小与字号"))

        def _row(caption, lo, hi, value, unit="px"):
            """一行：左侧小标题 + 滑块 + 右侧数值。标题放行内是为了省高度（页面不高）。"""
            row = QHBoxLayout()
            row.setSpacing(10)
            cap = label(caption, 12)
            cap.setMinimumWidth(132)
            row.addWidget(cap)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setSingleStep(2)
            sl.setValue(int(value))
            val = label("%d %s" % (int(value), unit), 13, bold=True)
            val.setMinimumWidth(62)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(sl, 1)
            row.addWidget(val)
            size_lay.addLayout(row)
            sl.valueChanged.connect(
                lambda v: val.setText("%d %s" % (int(v), unit)))
            return sl

        sl_size = _row("桌宠大小", 96, 900, current)
        sl_font = _row("台词气泡字号", BUBBLE_FONT_MIN, BUBBLE_FONT_MAX,
                       int(conf.get("bubble_font_px") or BUBBLE_FONT_PX))
        size_lay.addWidget(hint("上面是桌宠大小、下面是**台词气泡字号**；"
                                "气泡字号改完立刻按新字号重排。"
                                "点「应用」才生效并写进配置。"))
        status = hint("")
        self._pet_page_status = status      # 让 reload 别把刚写的反馈冲掉
        size_lay.addWidget(status)

        def on_apply():
            target = int(sl_size.value())
            px = int(sl_font.value())
            # apply_pet_size 返回"是不是真改了"（差不到 2% 时它不动手）：
            # 那种情况下也得把配置写上，否则下次启动又是旧高度
            changed = bool(self.apply_pet_size(target))
            font_changed = False
            if self.bubble is not None:
                font_changed = bool(self.bubble.set_font_px(px))
            ok = bool(save_config_values({"pet_height": target,
                                          "bubble_font_px": px},
                                         self._config_path))
            bits = []
            if changed:
                bits.append("缩放到 %d px" % target)
            else:
                bits.append("大小和现在差不多，没重新缩放")
            if font_changed:
                bits.append("气泡字号改成 %d px" % px)
            else:
                bits.append("气泡字号本来就是 %d px" % px)
            if not ok:
                status.setText("；".join(bits) + "；但配置没写进去"
                               "（目录只读或被占用？）下次启动会回到旧值。")
            else:
                status.setText("；".join(bits) + "；都记进配置了。")

        apply_btn = primary_btn("应用")
        apply_btn.clicked.connect(on_apply)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        size_lay.addLayout(btn_row)
        lay.addWidget(size_card)

        top_card = QFrame()
        top_card.setObjectName("card")
        top_card.setStyleSheet(card_style(padding=12))
        top_lay = QVBoxLayout(top_card)
        top_lay.setContentsMargins(12, 10, 12, 10)
        top_lay.setSpacing(6)
        top_lay.addWidget(section("始终置顶"))
        on_top = QCheckBox("让她永远待在最前面")
        on_top.setFont(ui_font(12, True))
        on_top.setChecked(bool(conf.get("always_on_top")))
        top_lay.addWidget(on_top)
        top_lay.addWidget(hint(
            "默认不置顶：别的程序窗口能正常盖住她和气泡，不会糊在人家界面上。"
            "勾上之后她永远在最前 —— 全屏看电影、玩游戏时也会压在上面，慎用。"))

        def on_top_changed(checked):
            value = bool(checked)
            # 立即生效：改窗口标志必须重新 show() 一次才会被系统采纳
            # （只设标志不 show 的话，任务栏和层级都要等下一次显隐才更新）
            flags = self.windowFlags()
            if value:
                self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
            else:
                self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
            self.show()
            # 气泡要**跟着一起变**（主人 2026-07-11 的要求）：桌宠置顶时气泡也置顶
            # （不然她被别的窗口盖住、只有气泡飘在最上面，看着很怪）；
            # 不置顶时气泡也不置顶 —— 别的程序窗口能正常盖住它。
            try:
                self.sync_bubble_topmost()
            except Exception:
                pass
            if save_config_values({"always_on_top": value}, self._config_path):
                status.setText("已%s，立即生效。" % ("置顶" if value else "取消置顶"))
            else:
                status.setText("已经立即生效，但配置没写进去（目录只读或被占用？）"
                               "下次启动会回到旧设置。")

        on_top.stateChanged.connect(on_top_changed)
        lay.addWidget(top_card)

        # 尾巴那张小卡：写清"谁说的话算数"。命令行参数优先于这里的设置，
        # 不写明白的话，主人用 --height 起了以后会以为设置窗口坏了。
        foot_card = QFrame()
        foot_card.setObjectName("card")
        foot_card.setStyleSheet(card_style(padding=12))
        foot_lay = QVBoxLayout(foot_card)
        foot_lay.setContentsMargins(12, 10, 12, 10)
        foot_lay.setSpacing(6)
        foot_lay.addWidget(section("优先级"))
        foot_lay.addWidget(hint(
            "这两项存的是「下次启动的默认值」。命令行显式给了 --height 或 "
            "--top 时以命令行为准（那两种情况是临时起一只来看效果的）。"))
        lay.addWidget(foot_card)
        lay.addStretch(1)

        def reload():
            """每次切回这一页，把勾选状态和滑块拉回配置里的真值。"""
            fresh = load_config(self._config_path)
            on_top.blockSignals(True)          # 回填不该触发"保存并生效"
            on_top.setChecked(bool(fresh.get("always_on_top")))
            on_top.blockSignals(False)
            # 状态行是"上一次操作的反馈"，切页回来不该被清掉，所以不碰它

        page.reload = reload
        return page

    # --- 关于：版本 + 三处路径 + 体检提示 ----
    def _about_page(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget
        from pet_ui import page_bg_style

        conf_file = self._config_path or config_path()
        rows = [
            ("版本", str(APP_VERSION)),
            ("配置文件", str(conf_file)),
            ("记账本", str(pet_ledger.ledger_path(conf_file))),
            ("数据目录", str(pet_store.data_dir(conf_file))),
        ]

        page = QWidget()
        page_bg_style(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        for caption, path in rows:
            card = QFrame()
            card.setObjectName("card")
            card.setStyleSheet(card_style(padding=10))
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(10, 8, 10, 8)
            card_lay.setSpacing(2)
            card_lay.addWidget(hint(caption))
            value = label(path, 12)
            value.setWordWrap(True)
            # 可选中：出问题时主人能直接把路径复制出来发给我，不用手抄
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            card_lay.addWidget(value)
            lay.addWidget(card)

        tip = QFrame()
        tip.setObjectName("card")
        tip.setStyleSheet(card_style(padding=12))
        tip_lay = QVBoxLayout(tip)
        tip_lay.setContentsMargins(12, 10, 12, 10)
        tip_lay.setSpacing(4)
        tip_lay.addWidget(section("排查用"))
        tip_lay.addWidget(hint(
            "命令行跑 `WhalePet.exe --doctor` 可以把三条对外通道"
            "（飞书、远程、ACP）逐项实测，不用开界面。"))
        lay.addWidget(tip)
        lay.addStretch(1)
        return page

    def open_remote_dialog(self):
        """右键菜单「远程控制…」：打开设置窗口并切到远程那一页。"""
        return self.open_settings("remote")

    def _remote_page(self):
        """远程遥控页：开关、范围、令牌，一眼看全，保存即生效。

        内容是原来那个独立对话框搬过来的（一个功能都没少），只是换成
        设置窗口里的一页：状态一行、三张卡片、一排按钮。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                                       QFrame, QHBoxLayout, QLabel, QLineEdit,
                                       QVBoxLayout, QWidget)
        from pet_ui import page_bg_style

        conf = load_config(self._config_path)
        page = QWidget()
        page_bg_style(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        def card(vertical=True):
            box = QFrame()
            box.setObjectName("card")
            box.setStyleSheet(card_style(padding=12))
            box_lay = (QVBoxLayout(box) if vertical else QHBoxLayout(box))
            box_lay.setContentsMargins(12, 10, 12, 10)
            box_lay.setSpacing(8)
            return box, box_lay

        def tip(text, color=COLOR_DEEP):
            lb = label(text, 12, color=color, wrap=True)
            lay.addWidget(lb)
            return lb

        running = bool(self._remote is not None and self._remote.is_running())
        status = tip(("服务开着：%s" % self._remote.server.url()) if running
                     else "服务没开。开了以后，手机/别的电脑用浏览器打开"
                          "那个地址就能跟她聊。")

        card1, c1 = card()
        enabled = QCheckBox("启用远程遥控")
        enabled.setFont(ui_font(12, True))
        enabled.setChecked(bool(conf.get("remote_enabled")))
        c1.addWidget(enabled)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(label("端口", 12))
        port = QLineEdit(str(conf.get("remote_port") or 8765))
        port.setFont(ui_font(12))
        row.addWidget(port, 1)
        row.addWidget(label("范围", 12))
        combo = QComboBox()
        combo.addItem("只在本机", "127.0.0.1")
        combo.addItem("局域网（手机连这个）", "0.0.0.0")
        combo.setCurrentIndex(1 if str(conf.get("remote_bind")) == "0.0.0.0" else 0)
        row.addWidget(combo, 1)
        c1.addLayout(row)
        lay.addWidget(card1)

        card2, c2 = card()
        harness_ok = QCheckBox("允许远程派活（= 允许在这台电脑执行代码，慎重）")
        harness_ok.setFont(ui_font(12))
        harness_ok.setChecked(bool(conf.get("remote_allow_harness")))
        c2.addWidget(harness_ok)
        lay.addWidget(card2)

        token = str(conf.get("remote_token") or "").strip()
        if not token:
            token = _pet_remote.gen_token()   # 现场生成，保存时落盘
        card3, c3 = card()
        c3.addWidget(hint("访问令牌（删掉配置里的 remote_token 就能换新的）："))
        tok = QLabel(token)
        tok.setFont(ui_font(12))
        tok.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tok.setStyleSheet("color:%s;background:transparent;" % COLOR_DEEP)
        c3.addWidget(tok)
        lay.addWidget(card3)

        tip("安全提示：拿到这个链接的人就能跟她聊、喂饭；勾了远程派活还能"
            "在这台电脑上执行任务。只发给自己信得过的设备；不在同一局域网时"
            "建议走 Tailscale 之类的组网工具 —— 本程序绝不主动暴露到公网。",
            DANGER)

        btns = QHBoxLayout()
        copy = ghost_btn("复制链接")
        save = primary_btn("保存并应用")
        btns.addWidget(copy)
        btns.addStretch(1)
        btns.addWidget(save)
        lay.addLayout(btns)
        lay.addStretch(1)

        def on_copy():
            url = (self._remote.server.url()
                   if (self._remote is not None and self._remote.is_running())
                   else "http://%s:%s/?t=%s" % (
                       _pet_remote._lan_ip() if combo.currentData() == "0.0.0.0"
                       else "127.0.0.1", port.text().strip(), token))
            QApplication.clipboard().setText(url)
            copy.setText("已复制！")

        def on_save():
            try:
                port_num = int(port.text().strip() or 8765)
                if not (1 <= port_num <= 65535):
                    raise ValueError
            except ValueError:
                status.setText("端口不对（要 1~65535 的数字）。")
                return
            vals = {"remote_enabled": bool(enabled.isChecked()),
                    "remote_port": port_num,
                    "remote_bind": combo.currentData(),
                    "remote_allow_harness": bool(harness_ok.isChecked()),
                    "remote_token": token}
            if not save_config_values(vals, self._config_path):
                # 写不进去（目录只读 / 文件被占用 / 盘满）：说清楚，并且
                # **不动正在跑的服务** —— 免得界面显示"已保存"、实际什么都没变。
                status.setText("配置没写进去（目录只读或被占用？），服务保持原样。")
                return
            self.remote_stop()             # 先停，再按新配置起 —— 改端口/令牌立即生效
            if vals["remote_enabled"]:
                url = self.remote_start(load_config(self._config_path))
                warn = ("；注意：监听 %s 且允许派活 = 局域网里拿到链接的设备"
                        "可以在这台电脑上执行代码" % vals["remote_bind"]
                        if (vals["remote_allow_harness"]
                            and vals["remote_bind"] not in ("127.0.0.1",))
                        else "")
                status.setText("服务开着：%s%s" % (_mask_url(url), warn))
            else:
                status.setText("服务已关闭。")
            save.setText("已保存")

        copy.clicked.connect(on_copy)
        save.clicked.connect(on_save)

        def reload():
            """切回这一页时只重算"服务开着没"（端口/令牌是输入框里的草稿，
            不能拿配置覆盖掉主人正在改的值）。"""
            live = bool(self._remote is not None and self._remote.is_running())
            status.setText(("服务开着：%s" % self._remote.server.url()) if live
                           else "服务没开。开了以后，手机/别的电脑用浏览器打开"
                                "那个地址就能跟她聊。")

        page.reload = reload
        return page

    def feishu_cfg(self):
        """把配置里飞书相关的几项合成一份能直接用的参数。

        每次**现读**配置而不是缓存 —— 和 harness_cfg 同一个理由：
        改完 json 立刻生效，不用重启桌宠。
        """
        conf = load_config(self._config_path)
        return {
            "enabled": bool(conf.get("feishu_enabled")),
            "app_id": str(conf.get("feishu_app_id") or "").strip(),
            "app_secret": str(conf.get("feishu_app_secret") or "").strip(),
            "domain": str(conf.get("feishu_domain") or "feishu").strip(),
            "allow_harness": bool(conf.get("feishu_allow_harness")),
            "allow_sender": str(conf.get("feishu_allow_sender") or "").strip(),
        }

    def _feishu_bridge(self):
        if self._feishu is None:
            self._feishu = build_feishu(self)
            self._feishu.command.connect(self._on_feishu_message)
            self._feishu.note.connect(self._feishu_note)
            # 工作线程要冒气泡时走这条信号（2026-10-11 修"手机发图过来 →
            # 程序未响应"时补）。**为什么必须这么绕**：气泡是 QWidget，
            # 只能在 GUI 线程碰。真机证据：18:30:40 / 18:32:24 两张手机照片
            # 都已落盘（说明下载存盘成功），紧接着 18:31:10 / 18:32:52
            # Windows 就记了两次 AppHangB1 —— 卡点正是落盘之后那句
            # `self.say(...)`（见 _feishu_do_media）。本机复现结论：
            # 从工作线程调 `SpeechBubble.show()` 或 `QWidget.setWindowFlags()`
            # 会把 GUI 线程**永久**卡住（工作区里的探针 `_probe/qt_affinity_probe.py`）。
            # 信号 + 自动排队回主线程是本项目既有的写法（和 command/note 同套路）。
            signal = getattr(self._feishu, "say_want", None)
            if signal is not None:
                signal.connect(self._on_feishu_say)
        return self._feishu

    def _on_feishu_say(self, text):
        """飞书桥的 say_want 落到这儿 —— **一定是主线程**（见 _feishu_bridge）。

        单独一个方法而不是直接把 self.say 接到信号上：这样"跨线程那一步"
        在代码里看得见，将来谁再往工作线程里塞 Qt 调用，grep 一下就能发现。
        """
        self.say(text)

    def _feishu_bubble(self, text):
        """在**工作线程**里请求冒一句气泡：只发信号，自己绝不碰 Qt。

        退路（桥没有 say_want）只可能出现在自检的桩桥上 —— 那些 say 不碰 Qt。
        真桥一定有 say_want（没有就把上面那条注释里的坑踩回来了）。
        """
        bridge = self._feishu
        signal = getattr(bridge, "say_want", None) if bridge is not None else None
        if signal is not None:
            signal.emit(str(text))
            return
        self.say(text)

    def _feishu_note(self, line):
        """飞书自己的日志（连接、断开、异常）。留最近几十行给设置窗口看。

        2026-10-11 起**同时落盘**（<配置目录>/feishu_log.txt）：飞书那边出问题
        大多发生在主人没开设置窗口的时候，只在内存里留 40 行等于没留。
        """
        self._feishu_log = (self._feishu_log + [line])[-40:]
        _pet_feishu.append_feishu_log(self._config_path, line)
        self._remote_publish("say", "· %s" % line)

    def _feishu_remember_chat(self, chat_id):
        """记下"最近一次说话的会话"，给 --doctor 的图片自测当收件人。

        为什么要有它：`--doctor` 那节要**真发一条图片消息**，而飞书发消息必须
        指定 receive_id（chat_id 或 open_id）。主人不一定填了白名单 open_id，
        那就把最后一次收到的 chat_id 存进配置 —— 也就是"主人自己的那个会话"。
        只在变化时写盘，不会每收一条消息就动一次配置。
        """
        chat_id = str(chat_id or "")
        if not chat_id or chat_id == getattr(self, "_feishu_last_chat", ""):
            return
        self._feishu_last_chat = chat_id
        try:
            save_config_values({"feishu_last_chat": chat_id}, self._config_path)
        except Exception as err:
            print("[飞书] 记不住最近的会话 id：%s: %s" % (type(err).__name__, err),
                  file=sys.stderr)

    def feishu_status(self):
        if self._feishu is None:
            return {"state": "idle", "label": "未启动", "online": False,
                    "error": "", "uptime": 0.0, "last_event_ago": -1.0}
        return self._feishu.status()

    def feishu_status_line(self, enabled=True):
        """对话框顶上那行状态。**一句人话，而且说清下一步点哪里。**

        为什么要单独抽一个方法（原来这里直接显示 status()["label"]）：
        「未启动」三个字对主人毫无信息量 —— 没启用？没保存？保存了没连上？
        三种情况长得一模一样。更难查的是：那行**每 1.5 秒刷一次**，
        而"保存/测试的结果"原来也写在同一行上，于是**报错信息活不过 1.5 秒**，
        主人最后只看到一句干巴巴的「未启动」（这就是主人来问这句的原因）。
        所以现在：状态归状态（这里，会刷），动作反馈归反馈（另一行，不刷）。
        """
        s = self.feishu_status()
        if s["state"] in ("idle", "stopped"):
            if not enabled:
                return ("未启动：勾上「启用飞书接入」、填好 App ID / Secret，"
                        "再点「保存并应用」")
            return ("未启动：配置里是启用的，但这次没连起来 —— "
                    "点「保存并应用」重试；还不行就看下面的日志")
        line = "连接状态：%s" % s["label"]
        if s["online"] and s["uptime"]:
            line += "（已连 %s）" % format_seconds(s["uptime"])
        if s["error"]:
            line += " · %s" % s["error"]
        return line

    def feishu_start(self, conf=None):
        """按配置把长连接起起来。返回 (ok, 一句人话)。"""
        conf = conf or self.feishu_cfg()
        if not FEISHU_AVAILABLE:
            return False, _pet_feishu.missing_reason()
        if not conf["app_id"] or not conf["app_secret"]:
            return False, "App ID / App Secret 还没填"
        self._feishu_bridge().start(conf["app_id"], conf["app_secret"],
                                    conf["domain"])
        return True, "长连接起好了，去飞书里找她说话吧"

    def feishu_stop(self):
        if self._feishu is not None:
            self._feishu.stop()

    def _feishu_reply(self, chat_id, reply_id, text):
        """回一条到飞书。**静默失败**：回不出去也不能反过来把桌宠带崩。"""
        if self._feishu is None or not text:
            return False
        ok = self._feishu.reply(chat_id, reply_id, text)
        self._remote_publish("say", text)     # 电脑那边开着远程页面的话也看得到
        return ok

    def _feishu_session(self, chat_id):
        """每条飞书会话（私聊 / 某个群）各有一份状态。

        为什么要按 chat 分开：主人可能既私聊她、又在一个群里 @ 她。
        两边的"现在是在闲聊还是在派活""接的是哪段对话"不该互相打架。
        """
        st = self._feishu_chats.get(chat_id)
        if st is None:
            st = {"mode": "chat", "rows": [], "choosing": False, "target": None}
            self._feishu_chats[chat_id] = st
        return st

    def _feishu_gate(self, d, what="消息"):
        """白名单 + 群聊两道闸。过了返回 True；没过**已经记好日志**。

        为什么抽出来（2026-10-11 加图片消息时）：原来这两道闸写在
        `_on_feishu_message` 里，而它开头那句 `if not text: return` 会让
        **图片消息（text 是空串）在到达闸门之前就被丢掉** —— 白名单等于被绕过
        了。现在闸门必须对所有类型先过一遍，所以它得是一个能被复用的东西。
        """
        allow = self.feishu_cfg()["allow_sender"]
        sender = str(d.get("sender_open_id") or "")
        if allow and sender != allow:
            # 不是白名单里的人：**安静地不理**。
            # 回一句"你不能用"反而把"这儿有个机器人、它能干活"告诉了陌生人。
            self._feishu_note("忽略非白名单发送者 %s" % (sender or "?"))
            return False
        # ---- 群聊另外两道闸（主人 2026-10-10 加）----
        # 私聊默认只有本人看得见，群聊不是：机器人被拉进群、或者消息被
        # 转发到群里之后，**群里所有人都能指挥她**。而 flywheel 那头
        # 挂着的是 feishu_allow_harness —— 也就是"在这台电脑上执行代码"。
        # 所以群聊必须：① 白名单填了；② 这条消息是 @ 她的。
        # 光靠"@ 才回"挡不住转发，所以白名单是硬条件。
        if str(d.get("chat_type") or "") == "group":
            if not allow:
                self._feishu_note("群消息但没填「只认这个人的消息」→ 忽略"
                                  "（填上 open_id 才在群里应答）")
                return False
            if not d.get("mention_names"):
                self._feishu_note("群消息没有 @ 她 → 忽略")
                return False
        return True

    def _on_feishu_message(self, d):
        """一条飞书消息的落地处。信号跨线程排队过来的，这里已经是主线程。"""
        chat_id = str(d.get("chat_id") or "")
        reply_id = str(d.get("message_id") or "")
        text = str(d.get("text") or "").strip()
        if not chat_id or not reply_id:
            return
        # 闸门对**所有消息类型**先过一遍（图片消息没有正文，不能被
        # 下面那句"没正文就返回"提前丢掉 —— 见 _feishu_gate 的注释）
        if not self._feishu_gate(d):
            return
        # 过了闸门就记下"最近说话的会话"：--doctor 那节的图片自测要靠它当收件人
        self._feishu_remember_chat(chat_id)
        kind, spec = _pet_feishu._read_media_spec(d)
        if kind != "text":
            # 图片 / 文件 / 语音 / 表情 / 视频：走媒体那条路
            self._feishu_media(chat_id, reply_id, kind, spec, d)
            return
        if not text:
            return
        st = self._feishu_session(chat_id)
        kind, name, arg = _pet_feishu.parse_command(text)
        if kind == "cmd":
            self._feishu_command(st, chat_id, reply_id, name, arg)
        else:
            self._feishu_text(st, chat_id, reply_id, text)

    # ---- 媒体消息（手机拍照发过来 / 截图发过去，2026-10-11 加）----
    def _feishu_media(self, chat_id, reply_id, kind, spec, d):
        """按消息类型分流：图片走下载存盘，其余至少回一句人话。

        `kind` / `spec` 由 pet_feishu._read_media_spec 从**原始 content JSON**
        里读出来（不再是"只看 text"）。

        图省钱的三条支路：
          * `image`  —— 下载 → 存盘 → 回话 + 冒泡；
          * 别的可识别类型 —— 明确说"这个我收不了"，并**告诉主人能收什么**；
          * 认不出来的 —— 说清"收到了什么类型"，别让人对着空气猜。
        白名单在进来之前已经过了（_on_feishu_message），这里不再判一次。
        """
        if kind == "image":
            if not spec:
                self._feishu_reply(chat_id, reply_id,
                                   "这条图片消息里没找到 image_key，收不下来。")
                return
            self._feishu_pending["media"] = {"chat_id": chat_id,
                                             "reply_id": reply_id,
                                             "kind": "image", "key": spec}
            # 下载是网络活（还要取 token），**扔工作线程**：这个槽在主线程里跑，
            # 阻塞它等于把整个桌宠冻住（拖不动、菜单点不开）—— 和发消息那边
            # 养发送线程是同一个理由。
            threading.Thread(target=self._feishu_do_media,
                             args=(chat_id, reply_id, d.get("message_id"), spec),
                             daemon=True, name="feishu-media").start()
            return
        if kind in ("file", "audio", "media", "sticker", "post"):
            self._feishu_reply(
                chat_id, reply_id,
                "这个我暂时收不了（类型：%s）—— 现在只认文字和图片。\n"
                "你在手机上拍张照发过来我就能存；想看电脑画面就发 /shot。"
                % kind)
            return
        self._feishu_reply(
            chat_id, reply_id,
            "这条消息我没看懂是什么类型（内容里没有 text，也没有认识的文件字段），"
            "先不收。文字和图片我都在。")

    def _feishu_do_media(self, chat_id, reply_id, message_id, image_key):
        """下载图片 → 存盘 → 回话/冒泡。**跑在工作线程里**（见调用点）。

        ⚠️ **铁律：这条路上一个 Qt 对象都不能直接碰。**
        真机 2026-10-11：手机发照片过来，程序"未响应"（Windows 事件日志两次
        AppHangB1，18:31:10 / 18:32:52），而两张照片都已经落盘
        （feishu_media/20261011/…634KB、…1114KB）—— 说明卡点就在落盘之后那句
        气泡。本机复现（工作区里的探针 `_probe/qt_affinity_probe.py`）：从工作线程调
        `SpeechBubble.show()` 会把 GUI 线程永久卡住，进程还在、界面不动，
        正是"程序未响应"的样子。所以冒泡一律走 `_feishu_bubble`（发信号回主线程）。

        失败按"主人下一步要做什么"说清楚：
          * token 过期 —— 重发一次即可（token 会自己刷新）；
          * 下载 404 —— 图片已过期 / image_key 不对，重拍一张；
          * 写盘失败 —— 电脑这边的问题（只读、盘满），说得明白点。
        最后再兜一层 try/except：这条线程上任何没预料到的异常都必须变成
        "一条飞书回话 + 一行日志"，绝不能让线程静悄悄死掉
        （线程死了 = 之后所有收图都默默失灵，而且一点提示都没有）。
        """
        try:
            try:
                data, ctype = self._feishu.client.download_resource(
                    message_id, image_key, "image")
            except Exception as err:
                self._feishu_reply(chat_id, reply_id, self._feishu_media_error(err))
                return
            if not data:
                self._feishu_reply(chat_id, reply_id,
                                   "图片下载回来是空的（0 字节），没收着。")
                return
            try:
                path = _pet_feishu.save_media_bytes(
                    data, image_key, ctype,
                    _pet_feishu.media_root(self._config_path))
            except OSError as err:
                # 单独接住：写盘失败（目录只读/盘满/权限）和网络失败是两回事，
                # 说给主人的话也不一样
                self._feishu_reply(chat_id, reply_id,
                                   "照片拿到了（%s），但存盘失败：%s\n"
                                   "可能是数据目录只读或者磁盘满了。"
                                   % (_pet_feishu._human_size(len(data)), err))
                return
            size = _pet_feishu._human_size(len(data))
            # ① 飞书里回一句（带上落盘路径和大小 —— 主人要的就是"存哪了、多大"）
            self._feishu_reply(chat_id, reply_id,
                               "收到一张照片，已存到 %s（%s）" % (path, size))
            # ② 桌面冒一句气泡（她本机也看得见"刚发生了一件事"）。
            #    气泡只放文件名和大小的短版：整条路径在气泡里会撑成一大块。
            #    **必须走信号回主线程**（见方法开头那段真机证据）。
            self._feishu_bubble("收到一张照片：%s（%s）" % (path.name, size))
        except Exception as err:
            # 兜底：谁也没预料到的异常。不能让它把工作线程带走 ——
            # 主人至少要收到一句人话，磁盘上至少要有一行线索。
            line = ("收图那条线程出了意外错误：%s: %s"
                    % (type(err).__name__, err))
            try:
                self._feishu_note(line)
                self._feishu_reply(
                    chat_id, reply_id,
                    "这张图我没收下来 —— 电脑这边出了个意外错误：\n%s\n"
                    "再发一次试试；还不行就把这句发给鲸鲸。" % line)
            except Exception:
                pass                  # 连报错都失败就只能认了，但别再往上抛
        finally:
            # 不管走哪条路都要清掉这一舱，否则下一条照片会被当成"还在下载中"
            self._feishu_pending["media"] = None

    def _feishu_media_error(self, err):
        """把下载异常翻成人话。故意**按关键词分流**而不是只看异常类型：

        urllib 把 404 包成 HTTPError、把网络问题包成 URLError，而飞书取 token
        失败是我这边抛的 RuntimeError —— 对主人来说，"token 过期"和"图片链接
        失效"要做的动作不一样（前者重发即可、后者得重新拍），所以值得分辨。
        """
        text = str(err)
        if "tenant_access_token" in text:
            return ("取 access token 失败（凭证过期或被改过）：%s\n"
                    "把这条消息再发一次就好；还不行就去设置里点「测试凭据」。"
                    % text)
        if "HTTP 404" in text or "not found" in text.lower():
            return ("下载图片失败：飞书回 404（图片已过期或 image_key 不对）。\n"
                    "在手机上重新拍一张发过来就行。\n（原话：%s）" % text[:200])
        if "HTTP" in text:
            return "下载图片失败：%s" % text[:300]
        return "下载图片失败：%s: %s" % (type(err).__name__, text[:200])

    def _feishu_take_shot(self, chat_id, reply_id):
        """`/shot`：截主屏 → 发到飞书。**这里已经在主线程了。**

        抓屏那一下调用桥的 `capture_screen_file`：主线程调用时它直接抓
        （QScreen.grabWindow 只能在 GUI 线程调）；上传和发消息则排进飞书发送
        线程 —— 网络活不占界面。失败一定回话（抓屏失败带原因、发图失败由桥
        在队列里补一条），绝不留下"回了一句马上发给你然后没了"。
        """
        if self._feishu is None:
            return
        if self._feishu_pending.get("shot"):
            self._feishu_reply(chat_id, reply_id, "上一张截图还在传呢，稍等～")
            return
        self._feishu_pending["shot"] = {"chat_id": chat_id,
                                        "reply_id": reply_id}
        # 先回一句：抓屏+上传可能要好几秒，飞书那头得先看到"接单了"
        self._feishu_reply(chat_id, reply_id, "好，我看看电脑屏幕，马上发给你～")
        shot = ""
        err = ""
        try:
            shot = str(self._feishu.capture_screen_file() or "")
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
        if not shot:
            # 失败原因由桥记在 _last_shot_error 里（同时已经写进飞书日志）。
            # **一定要把它说给主人**：只回一句"没截到图"，主人分不清是
            # "这台机器不让抓屏"还是"主线程刚才正忙"，只能反复试。
            why = str(getattr(self._feishu, "_last_shot_error", "") or "")
            self._feishu_reply(
                chat_id, reply_id,
                "没截到图%s。可能是系统不允许抓屏（远程桌面/受保护窗口），"
                "或者刚才主线程正忙 —— 等两秒再发一次 /shot。"
                % (("：" + (why or err)) if (why or err) else ""))
            self._feishu_pending["shot"] = None
            return
        # 交给发送队列：桥发完会**自己把临时文件删掉**（_tmp 不攒垃圾）。
        # 发失败时桥会在同一个队列里补一条"截图没发出去：<原因>"的回话，
        # 所以这里只管排队，不需要再回话（不然会两条重复）。
        if not self._feishu.send_image(chat_id, shot, reply_id):
            self._feishu_reply(chat_id, reply_id,
                               "截图抓到了，但没排上发送队列（飞书连接断了吗？）")
            try:
                os.remove(shot)          # 排不上就当场删，别留给下次
            except OSError:
                pass
        self._feishu_pending["shot"] = None

    def _feishu_command(self, st, chat_id, reply_id, name, arg):
        if name == "help":
            self._feishu_reply(chat_id, reply_id, self._feishu_help(st))
        elif name == "shot":
            # 截屏发到飞书（主人 2026-10-11 要的"手机看电脑画面"）。
            # 放在命令这一个分支里而不是单独一条路：命令解析已经是唯一入口，
            # 再开一条"看到 /shot 就..."的分支迟早和它分叉。
            self._feishu_take_shot(chat_id, reply_id)
        elif name == "status":
            self._feishu_reply(chat_id, reply_id, self._feishu_status_text(st))
        elif name == "harness":
            st["mode"] = "harness"
            st["choosing"] = True
            self._feishu_reply(chat_id, reply_id, self._feishu_session_list(st))
        elif name == "list":
            st["choosing"] = True
            self._feishu_reply(chat_id, reply_id, self._feishu_session_list(st))
        elif name == "new":
            st["target"] = None
            st["choosing"] = False
            self._feishu_reply(chat_id, reply_id, "好，下一条就给你开一段新对话。")
        elif name == "chat":
            st["mode"] = "chat"
            st["choosing"] = False
            self._feishu_reply(chat_id, reply_id, "回来陪你聊天了～")
        elif name == "pick":
            # 数字**只在"正在挑会话"时**才算挑；其余时候当普通内容，
            # 不然主人随口发一句"2"就被吃掉了。
            if arg == 0:
                st["target"] = None
                st["choosing"] = False
                self._feishu_reply(chat_id, reply_id, "好，开一段新的。")
            elif st["choosing"] and st["rows"] and 1 <= arg <= len(st["rows"]):
                row = st["rows"][arg - 1]
                st["target"] = {"id": row["id"], "cwd": row["cwd"],
                                "title": row["title"]}
                st["choosing"] = False
                self._feishu_reply(
                    chat_id, reply_id,
                    "接上了：%s\n之后你说的话都派到这段里；\n"
                    "发 /new 换新对话、/chat 回来闲聊。" % row["title"])
            else:
                self._feishu_text(st, chat_id, reply_id, str(arg))

    def _feishu_text(self, st, chat_id, reply_id, text):
        if st.get("mode") == "harness":
            self._feishu_task(st, chat_id, reply_id, text)
        else:
            self._feishu_chat(st, chat_id, reply_id, text)

    def _feishu_chat(self, st, chat_id, reply_id, text):
        """闲聊。走**同一条**聊天管线，不另写一套。"""
        reason, _echo = self.chat_blocked()
        if reason:
            self._feishu_reply(chat_id, reply_id, reason)
            return
        if self._feishu_pending.get("chat"):
            # 上一条还在等回复：**不能覆盖**那一格，否则它回来时
            # 找不到该回给谁，答案就凭空消失了。
            self._feishu_reply(chat_id, reply_id, "我还在想上一条呢，等我说完～")
            return
        self._feishu_pending["chat"] = {"chat_id": chat_id,
                                        "reply_id": reply_id}
        # as_chat=True：飞书说的"闲聊"就是闲聊。不加这一条的话，桌面
        # 工作台正开着时，一句"今天好累"会被劫去当任务派出去。
        self.start_chat(text, as_chat=True)

    def _feishu_task(self, st, chat_id, reply_id, text):
        """派活。同样走现有的 _harness_send，只是把"接哪段"从飞书这边带过去。"""
        if not self.feishu_cfg()["allow_harness"]:
            self._feishu_reply(
                chat_id, reply_id,
                "从飞书派活这个开关还没开 —— 它等于「让手机在这台电脑上"
                "执行代码」，所以默认关着。\n"
                "在电脑上右键鲸鲸 →「飞书接入…」勾上「允许从飞书派活」再试。")
            return
        if self._harness_busy():
            self._feishu_reply(chat_id, reply_id, "上一个活还没干完，等它收尾～")
            return
        if self.deep_sleep():
            # 派活只需要 Harness 的账号凭证，跟聊天那把 API key 无关，
            # 所以这里**不能**用 chat_blocked()（它会把"没配钥匙"也算上）。
            self._feishu_reply(chat_id, reply_id,
                               random.choice(LINES["deep_sleep_reply"]))
            return
        cur = st.get("target") or {}
        if self._feishu_pending.get("harness"):
            self._feishu_reply(chat_id, reply_id, "上一个活还在等结果呢，别急～")
            return
        self._feishu_pending["harness"] = {"chat_id": chat_id,
                                           "reply_id": reply_id,
                                           "tools": 0, "notes": 0}
        self._feishu_reply(chat_id, reply_id,
                           "接单！去 Harness 干活了（%s）。干完我回你～"
                           % (cur.get("title") or "新开一段"))
        self._harness_send(text, target=cur)
        if not self._harness_busy():
            # 没真的起来（比如刚好撞上另一个任务收尾）。别让 pending
            # 挂在那儿 —— 否则下一个任务的结果会回错会话。
            self._feishu_pending["harness"] = None

    def _feishu_publish(self, kind, text="", for_kind="chat"):
        """把桌宠这边的进展回推到飞书。

        只在**确实有一条飞书请求在等结果**时才推：不然她自己在本机聊天、
        或者主人在电脑上派的活，都会往飞书刷屏。

        kind 和远程页面那套完全一致（tool/done/err）—— 这样每一处都能
        紧挨着 `_remote_publish` 写一行，不用为飞书再想一套事件名。

        for_kind 区分"这条结果是回闲聊还是回派活"：两类请求各有各的槽
        （见 __init__ 的 _feishu_pending），所以谁在等就回给谁，
        不会再出现"派活的结果回给了闲聊那条消息"。
        """
        pend = self._feishu_pending.get(for_kind)
        if not pend or self._feishu is None:
            return
        if kind == "done":
            # 先清槽再回话：回话里如果又触发一轮 publish，不会重复发送
            self._feishu_pending[for_kind] = None
            self._feishu_reply(pend["chat_id"], pend["reply_id"], text)
        elif kind == "err":
            self._feishu_pending[for_kind] = None
            self._feishu_reply(pend["chat_id"], pend["reply_id"],
                               "（我这边出岔子了：%s）" % text)
        elif kind == "tool":
            # 工具流水只属于"派活"那件事
            if for_kind != "harness":
                return
            pend["tools"] = pend.get("tools", 0) + 1
            # 有节制地报进度：干得久的时候让主人知道她还在动，又不能把
            # 每条工具调用都刷成一条消息（一次任务能跑几十个）。
            if pend.get("notes", 0) < 3 and pend["tools"] in (10, 30, 60):
                pend["notes"] = pend.get("notes", 0) + 1
                self._feishu_reply(pend["chat_id"], pend["reply_id"],
                                   "还在干……（第 %d 个动作）" % pend["tools"])

    def _feishu_help(self, st):
        cur = st.get("target") or {}
        where = ("工作模式" if st.get("mode") == "harness" else "闲聊模式")
        if cur.get("id"):
            where += "（接着：%s）" % cur.get("title")
        return ("我是鲸鲸 🐋 直接说话就是闲聊；想让我干活先切工作模式。\n\n"
                "命令：\n"
                "  /shot     截一张电脑主屏发给你（别名 /screen /截图）\n"
                "  /harness  进工作模式（列出主任务，回序号挑一段接着干）\n"
                "  /list     重看主任务列表（子任务不占行）\n"
                "  /new      下次开一段新对话\n"
                "  /chat     回到闲聊\n"
                "  /status   精力、饱食、现在在干嘛\n"
                "  /help     这一页\n\n"
                "你发照片我存，发别的类型我会告诉你收不收得了。\n"
                "现在：%s" % where)

    def _feishu_status_text(self, st):
        if self._harness_busy():
            doing = "在 Harness 干活"
        elif self.deep_sleep():
            doing = "睡得很沉（精力太低，叫不醒）"
        elif self.sleeping():
            doing = "睡着了"
        else:
            doing = "待机"
        return ("鲸鲸 · %s\n精力 %d/%d · 饱食 %d/%d\n现在：%s\n"
                "这条会话：%s" % (
                    APP_VERSION, self._energy, ENERGY_MAX,
                    self._satiety, SATIETY_MAX, doing,
                    "工作模式" if st.get("mode") == "harness" else "闲聊模式"))

    def _feishu_session_list(self, st):
        """列 Harness 里的任务对话，让主人回个数字挑。**只列主任务。**

        数据来自**会话日志**（只读），不走 ACP —— 所以 Harness 没开着
        也照样能挑，和桌面工作台是同一个来源。

        ⚠️ **显示列表 = 编号列表**（主人 2026-10-11 追加要求的命门）：
        主人是**回数字**挑会话的，所以 `st["rows"]` 必须**就是屏幕上印出来的那一串**。
        收窄成"只有主任务"之后，编号必须跟着重排 —— 要是照旧按"含子会话"的顺序编号，
        主人回 3 就会挑到一段他根本看不见的子会话 ✗。
        所以这里先定 `rows`（= 主任务），再拿**同一个** rows 打印序号；
        中间不许再插别的过滤，`pick` 分支也是直接 `rows[arg-1]` 取（两端同源）。

        子任务（含孙子辈）**不占行**：它们是主任务派出去的活。桌面左栏另有
        "默认折叠、可展开"那一套，那边照旧显示子会话，**别把这边改法搬过去**。
        """
        if not SESSION_LOG_AVAILABLE:
            return "这台机器上读不了会话列表（缺 zstandard）。"
        try:
            rows = [r for r in _session_log.list_sessions() if r.get("cwd")]
        except Exception as err:
            return "读会话列表失败：%s: %s" % (type(err).__name__, err)
        # 用现成的分组把子会话挂回它的主任务，再**只取每组的主会话**。
        # 分组时深度 2+ 会顺着一路往上找到根主会话，所以"孙子辈"也归在同一个
        # 主任务下面 —— 这正是"当前目标恰好是子任务"要查的表。
        # 父被清掉、找不到主任务的孤儿组（main is None）同样不列：它不是
        # "主人自己开的对话"，也就不该出现在挑选列表里。
        try:
            groups = _session_log.group_sessions(rows)
        except Exception:
            groups = [(r, []) for r in rows if r.get("kind") != "sub"]
        mains = [m for m, _kids in groups if m is not None]
        st["rows"] = mains                 # ← 显示什么就存什么：编号靠它对齐
        if not mains:
            st["choosing"] = False
            return "现在还没有任务对话。\n直接说要干什么，我给你开一段新的。"
        # 当前接的**可能是个子任务**（桌宠会自动跟到派出去的活上）。列表里既然
        # 只有主任务，就得把这个子任务的**主任务**标成「当前」，否则主人会以为
        # "我明明没选它，当前目标怎么变成它了"。
        cur_id = str((st.get("target") or {}).get("id") or "")
        cur_main, cur_is_sub = cur_id, False
        for main, kids in groups:
            if main is None or not cur_id:
                continue
            if any(str(k.get("id") or "") == cur_id for k in kids):
                cur_main, cur_is_sub = str(main.get("id") or ""), True
                break
        out = ["Harness 里的任务对话（只列主任务，回一个序号）："]
        for i, r in enumerate(mains[:20], 1):
            rid = str(r.get("id") or "")
            mark = "〔当前〕" if rid and rid == cur_main else ""
            out.append("%2d. %s%s  · %s · %d 轮"
                       % (i, mark, r["title"], r["time_label"], r["turns"]))
            if cur_is_sub and rid == cur_main:
                # 额外一行小字：列表里看不出子任务那一层，不说明白就成了"莫名其妙"
                out.append("      ↑ 现在接的是它下面的子任务（不是它本身）")
        out.append("")
        out.append("（只列主任务；子任务挂在它下面，不单独占一行）")
        out.append("")
        out.append("回 0 = 开一段新对话；发 /chat 回闲聊。")
        return "\n".join(out)

    # --- 飞书：凭证、闸门、连接状态 ---
    def _feishu_page(self):
        """飞书接入页：凭证、闸门、连接状态与日志，保存即生效。

        内容是原来那个独立对话框搬过来的（一个功能都没少），换成设置窗口
        里的一页；那个 1.5 秒刷一次的状态/日志定时器跟着页面走。
        """
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame,
                                       QHBoxLayout, QLineEdit,
                                       QPlainTextEdit, QVBoxLayout, QWidget)
        from pet_ui import page_bg_style

        conf = load_config(self._config_path)
        page = QWidget()
        page_bg_style(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        def card(vertical=True):
            box = QFrame()
            box.setObjectName("card")
            box.setStyleSheet(card_style(padding=12))
            box_lay = (QVBoxLayout(box) if vertical else QHBoxLayout(box))
            box_lay.setContentsMargins(12, 10, 12, 10)
            box_lay.setSpacing(8)
            return box, box_lay

        def tip(text, color=COLOR_DEEP):
            lb = label(text, 12, color=color, wrap=True)
            lay.addWidget(lb)
            return lb

        if not FEISHU_AVAILABLE:
            tip("这台机器上缺依赖，飞书功能用不了：%s"
                % (_pet_feishu.missing_reason() if _pet_feishu
                   else "pet_feishu 模块没加载"),
                DANGER)

        status = tip(self.feishu_status_line(
            bool(conf.get("feishu_enabled"))))
        # 反馈单独一行：它是"刚才那下点了之后飞书怎么说"，
        # 不能被下面那个 1.5 秒的刷新冲掉（冲掉过一次，主人来问「未启动是什么
        # 意思」就是被这一条坑的）。
        feedback = tip("", OK_GREEN)

        card1, c1 = card()
        enabled = QCheckBox("启用飞书接入（桌宠一启动就连上去）")
        enabled.setFont(ui_font(12, True))
        enabled.setChecked(bool(conf.get("feishu_enabled")))
        c1.addWidget(enabled)

        app_id_box = QLineEdit(str(conf.get("feishu_app_id") or ""))
        app_id_box.setFont(ui_font(12))
        secret_box = QLineEdit(str(conf.get("feishu_app_secret") or ""))
        secret_box.setFont(ui_font(12))
        secret_box.setEchoMode(QLineEdit.Password)   # 屏幕旁边有人时不该整串露出来
        for caption, box in (("App ID", app_id_box),
                             ("App Secret", secret_box)):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(label(caption, 12))
            row.addWidget(box, 1)
            c1.addLayout(row)

        site_row = QHBoxLayout()
        site_row.setSpacing(8)
        site_row.addWidget(label("站点", 12))
        combo = QComboBox()
        combo.addItem("飞书（国内版）", "feishu")
        combo.addItem("Lark（国际版）", "lark")
        combo.setCurrentIndex(1 if str(conf.get("feishu_domain")) == "lark" else 0)
        site_row.addWidget(combo, 1)
        c1.addLayout(site_row)

        sender_row = QHBoxLayout()
        sender_row.setSpacing(8)
        sender_row.addWidget(label("只认这个人", 12))
        sender = QLineEdit(str(conf.get("feishu_allow_sender") or ""))
        sender.setFont(ui_font(12))
        sender.setPlaceholderText("留空 = 谁都能指挥她（open_id，可在日志里看到）")
        sender_row.addWidget(sender, 1)
        c1.addLayout(sender_row)
        lay.addWidget(card1)

        card2, c2 = card()
        harness_ok = QCheckBox("允许从飞书派活（= 允许用手机在这台电脑执行代码，慎重）")
        harness_ok.setFont(ui_font(12))
        harness_ok.setChecked(bool(conf.get("feishu_allow_harness")))
        c2.addWidget(harness_ok)
        lay.addWidget(card2)

        card3, c3 = card()
        c3.addWidget(section("怎么开（一次性，约 5 分钟）"))
        c3.addWidget(hint(
            "1. 打开 open.feishu.cn → 开发者后台 → 创建「企业自建应用」\n"
            "   （注册个飞书个人账号就行，不用真的加入某家公司）\n"
            "2. 左侧「添加应用能力」→ 添加「机器人」\n"
            "3. 「权限管理」里开通：im:message（收发消息）\n"
            "4. 「事件与回调」→ 订阅方式选长连接 → 添加事件\n"
            "   im.message.receive_v1（接收消息）\n"
            "5. 「版本管理与发布」→ 创建版本 → 申请发布\n"
            "6. 把 App ID / App Secret 填到上面 → 这里点保存\n\n"
            "长连接是本机主动连出去的，所以不需要公网 IP、不用内网穿透。\n"
            "注意：保存前先在飞书那边把长连接客户端跑起来，"
            "否则它保存订阅方式时会说「无在线长连接」。"))
        lay.addWidget(card3)

        card4, c4 = card()
        c4.addWidget(section("连接日志"))
        log = QPlainTextEdit()
        log.setReadOnly(True)
        log.setFixedHeight(110)
        log.setPlainText("\n".join(self._feishu_log[-12:]) or "（还没有日志）")
        c4.addWidget(log)
        lay.addWidget(card4)
        lay.addStretch(1)

        btns = QHBoxLayout()
        test = quiet_btn("测试凭据")
        save = primary_btn("保存并应用")
        btns.addWidget(test)
        btns.addStretch(1)
        btns.addWidget(save)
        lay.addLayout(btns)

        def on_test():
            feedback.setStyleSheet("color:%s;background:transparent;" % COLOR_DEEP)
            feedback.setText("正在问飞书……")
            self._feishu_bridge().test_credentials(
                app_id_box.text().strip(), secret_box.text().strip(),
                combo.currentData())

        def on_tested(ok, msg):
            feedback.setStyleSheet("color:%s;background:transparent;"
                                   % (OK_GREEN if ok else DANGER))
            feedback.setText(("凭据没问题：%s" % msg) if ok
                             else ("用不了：%s" % msg))

        def on_save():
            vals = {"feishu_enabled": bool(enabled.isChecked()),
                    "feishu_app_id": app_id_box.text().strip(),
                    "feishu_app_secret": secret_box.text().strip(),
                    "feishu_domain": combo.currentData(),
                    "feishu_allow_harness": bool(harness_ok.isChecked()),
                    "feishu_allow_sender": sender.text().strip()}
            if not save_config_values(vals, self._config_path):
                feedback.setStyleSheet("color:%s;background:transparent;" % DANGER)
                feedback.setText("配置没写进去（目录只读或被占用？），"
                                 "连接保持原样。")
                return
            self.feishu_stop()          # 先停再起 —— 换凭证立即生效
            if vals["feishu_enabled"]:
                ok, msg = self.feishu_start(load_config(self._config_path))
                feedback.setStyleSheet("color:%s;background:transparent;"
                                       % (OK_GREEN if ok else DANGER))
                feedback.setText(msg if ok else ("没起来：%s" % msg))
            else:
                feedback.setStyleSheet("color:%s;background:transparent;" % COLOR_DEEP)
                feedback.setText("飞书接入已关闭（配置已保存）。")
            save.setText("已保存")

        def refresh():
            # 页面不在最前（切到别的页 / 窗口藏了）就不刷：白刷还占主线程
            if not page.isVisible():
                return
            # 只刷状态行；feedback 是"刚才那下的结果"，留着不覆盖
            status.setText(self.feishu_status_line(
                bool(enabled.isChecked())))
            log.setPlainText("\n".join(self._feishu_log[-12:]) or "（还没有日志）")

        bridge = self._feishu_bridge()
        bridge.tested.connect(on_tested)
        test.clicked.connect(on_test)
        save.clicked.connect(on_save)
        timer = QTimer(page)
        timer.timeout.connect(refresh)
        timer.start(1500)

        page.reload = refresh
        return page

    def open_feishu_dialog(self):
        """右键菜单「飞书接入…」：打开设置窗口并切到飞书那一页。"""
        return self.open_settings("feishu")
