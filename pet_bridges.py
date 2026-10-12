# -*- coding: utf-8 -*-
"""桥：搜索、余额查询、Harness(ACP)、任务对话工作台、远程遥控、飞书接入、大模型聊天客户端。

共同点是「都要联网、都得把异步结果用 Qt 信号送回主线程」。"""
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
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)
# ⚠️ 可选模块的**私有名**必须显式导入：`import *` 不带下划线开头的名字，
#    而它们是在 pet_constants 的 try 块里绑定的（_session_log / _pet_remote /
#    _pet_feishu）。漏了不会报错到脸上 —— 运行到那行才 NameError，
#    再被 except 吞掉就变成"静默失灵"（拆分时真踩过：工作台列表永远空、飞书/远程打不开）。
from pet_constants import _pet_feishu, _pet_remote, _session_log
from pet_net import (describe_net_error, format_search_digest, parse_bing_results, reply_from_json, resolve_endpoint, sse_delta, sse_finish_reason)
from pet_store import (_warn)
from pet_time import (_cn_num, now_line)
from pet_windows import (chat_font)


def build_search(parent, cfg):
    """联网搜索：免 key，直接抓 Bing 的搜索结果页。

    为什么是 Bing：这台机器上 DuckDuckGo 和维基百科都连不通（大陆网络实况），
    Bing 是实测可达的那个。抓网页天生脆，所以抠不到结果就返回空串，
    让模型凭自己的知识答 —— 既不抛异常打断对话，也不编造检索结果。
    """
    from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

    class WebSearch(QObject):
        found = Signal(str)       # 已压成 prompt 片段的检索结果；搜不到就是空串
        failed = Signal(str)

        def __init__(self):
            super().__init__(parent)
            self.manager = QNetworkAccessManager(self)
            self._reply = None
            self._raw = b""

        def busy(self):
            return self._reply is not None

        def search(self, query):
            if self._reply is not None or not query:
                return False
            url = QUrl(str(cfg.get("search_base")
                           or DEFAULT_CONFIG["search_base"]))
            params = QUrlQuery()
            params.addQueryItem("q", query)      # 交给 Qt 编码，中文/空格都不用自己转义
            url.setQuery(params)
            request = QNetworkRequest(url)
            request.setHeader(QNetworkRequest.UserAgentHeader, SEARCH_UA)
            request.setRawHeader(b"Accept-Language", b"zh-CN,zh;q=0.9")
            request.setTransferTimeout(int(float(cfg.get("timeout", 30)) * 1000))
            self._raw = b""
            self._reply = self.manager.get(request)
            self._reply.readyRead.connect(self._on_read)
            self._reply.finished.connect(self._on_finished)
            return True

        def _on_read(self):
            if self._reply is not None:
                self._raw += bytes(self._reply.readAll())

        def _on_finished(self):
            reply = self._reply
            self._reply = None
            if reply is None:
                return
            error = reply.error()
            code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            error_name = getattr(error, "name", str(error))
            raw = self._raw
            reply.deleteLater()
            if error_name != "NoError" and not raw:
                self.failed.emit(describe_net_error(error_name, code))
                return
            digest = format_search_digest(
                parse_bing_results(raw.decode("utf-8", "ignore")))
            self.found.emit(digest)      # 空串也算"完成"，上层据此决定不做检索注入

    return WebSearch()


def parse_balance_reply(raw, code, error_name):
    """把余额接口的回包解析成人话，返回 (是否正常, 台词)。纯函数，自检直接喂。

    判断顺序是坑：HTTP 401/402 时 Qt 的 error() 通常是 **NoError**
    （状态码不是传输层错误）—— 所以业务状态码判断必须在 error_name 之前，
    否则 401 会落进网络错误分支，key 错了永远只看到"出了点状况"。
    """
    if code == 401:
        return False, "这把钥匙打不开门……主人检查一下 key 是不是填错了？"
    if code == 402:
        return False, "呜……饭钱花光了，余额不足，主人快去充值！"
    if error_name != "NoError":
        if code is None:
            return False, describe_net_error(error_name, code)
        return False, "钱包这边出了点状况（HTTP %s）……" % code
    if code != 200:
        return False, "钱包这边出了点状况（HTTP %s）……" % code
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except ValueError:
        return False, "钱包接口说了听不懂的话……"
    if not data.get("is_available", True):
        return False, "呜，账户显示不可用……主人去平台看看？"
    parts = []
    for b in data.get("balance_infos") or []:
        # balance_infos 是数组：账户可能有 CNY/USD 多个币种，要遍历
        cur = b.get("currency", "")
        text = "%s %s" % (b.get("total_balance", "?"), cur)
        try:
            granted = float(b.get("granted_balance") or 0)
        except ValueError:
            granted = 0.0
        if granted > 0:
            text += "（含赠送 %.2f）" % granted
        parts.append(text)
    if not parts:
        return False, "钱包是空的？接口没说余额……"
    return True, "饭钱还有 %s。" % "、".join(parts)


def balance_snapshot(raw, code, error_name):
    """从余额接口的返回里取出**可记账的快照**；拿不到就返回 None。

    干什么用：记账只能靠"观测余额"推（接口没有交易明细），所以每次查余额
    都要落一个观测点。**优先人民币** —— 「今日已用」得有统一口径，
    CNY/USD 混着记会串味（记账本自己按币种分本，见 pet_ledger）。
    """
    if code != 200 or error_name != "NoError":
        return None
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except ValueError:
        return None
    if not data.get("is_available", True):
        return None
    infos = [b for b in (data.get("balance_infos") or [])
             if isinstance(b, dict)]
    if not infos:
        return None
    pick = next((b for b in infos
                 if str(b.get("currency", "")).upper() == "CNY"), infos[0])
    try:
        total = float(pick.get("total_balance"))
    except (TypeError, ValueError):
        return None
    try:
        granted = float(pick.get("granted_balance") or 0)
    except (TypeError, ValueError):
        granted = 0.0
    return {"total": total, "granted": granted,
            "recharge": max(0.0, total - granted),
            "currency": str(pick.get("currency") or "CNY").upper(),
            "at": datetime.now().timestamp()}


def build_balance_query(parent):
    """余额查询器：走 QNetworkAccessManager 异步请求，绝不卡 UI。

    和 chat/search 一样挂在 Qt 事件循环上 —— 同步 urllib 会把桌宠冻住几秒。
    """
    from PySide6.QtCore import QObject, QUrl, Signal
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

    class BalanceQuery(QObject):
        done = Signal(bool, str)          # (是否正常, 人话台词)
        snapshot = Signal(dict)           # 成功时另发一份结构化快照（给记账用）

        def __init__(self):
            super().__init__(parent)
            self.manager = QNetworkAccessManager(self)
            self._reply = None

        def busy(self):
            return self._reply is not None

        def query(self, key, base_url, timeout=15):
            if self._reply is not None or not key:
                return False
            request = QNetworkRequest(QUrl(base_url.rstrip("/") + BALANCE_PATH))
            request.setRawHeader(b"Authorization",
                                 ("Bearer " + str(key)).encode("utf-8"))
            request.setRawHeader(b"Accept", b"application/json")
            request.setTransferTimeout(int(timeout * 1000))
            self._reply = self.manager.get(request)
            self._reply.finished.connect(self._on_finished)
            return True

        def _on_finished(self):
            reply, self._reply = self._reply, None
            if reply is None:
                return
            err = reply.error()
            code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            raw = bytes(reply.readAll())
            reply.deleteLater()
            error_name = getattr(err, "name", str(err))
            # 顺序有意：先发快照（记账 + 富气泡），再发人话结果。
            # 失败时 _on_balance 会说话，成功时它保持安静（快照那条说得更全）。
            snap = balance_snapshot(raw, code, error_name)
            if snap:
                self.snapshot.emit(snap)
            self.done.emit(*parse_balance_reply(raw, code, error_name))

    return BalanceQuery()


def build_harness(parent):
    """派活给 Harness：把 ACP 客户端跑在独立线程，用 Qt 信号把更新抛回主线程。

    为什么非要独立线程：ACP 客户端是**阻塞式 IO**（读子进程 stdout + 等 Future），
    放主线程会把桌宠的动画整个冻住。QObject 留在主线程（线程亲和性正确），
    信号从工作线程 emit 出去会自动走队列连接 —— 这是 Qt 跨线程回主线程的标准形，
    也是 WebSearch / ChatClient 那两个先例的延续，只是它们用 QNetworkAccessManager，
    这里是子进程，所以用 threading.Thread。
    """
    from PySide6.QtCore import QObject, Signal

    class HarnessBridge(QObject):
        started = Signal()
        chunk = Signal(str)        # 模型说的正文片段（喂给打字机）
        event = Signal(str)        # 人类可读的事件行（工具开始/结束）
        usage = Signal(int, int)   # 上下文用量 used / size
        done = Signal(bool, str)   # ok, 正文或一句人话的错误
        # 会话一建立就报出来（id, cwd）。新开的那段必须被记住：
        # 否则主人说第二句话时又变成"再开一段"，工作台就永远只有一轮。
        session = Signal(str, str)

        def __init__(self):
            super().__init__(parent)
            self._busy = False
            self._lock = threading.Lock()

        def busy(self):
            with self._lock:
                return self._busy

        def start(self, text, exe, cli_js, cwd, group, session_id=""):
            with self._lock:
                if self._busy:
                    return False
                self._busy = True
            threading.Thread(target=self._run,
                             args=(text, exe, cli_js, cwd, group, session_id),
                             daemon=True, name="harness-acp").start()
            return True

        # ---- 以下都在工作线程里跑，全是阻塞调用，绝不能进主线程 ----
        def _connect(self, exe, cli_js, on_update=None):
            """起子进程 + 握手。握手三连是固定的，抽成一个免得两处写。

            `on_log` 接到 event 信号上（以前是 lambda s: None，直接丢掉）：
            DSH 子进程起不来时，**原因全在它的 stderr 里**
            （比如 "EPERM ... profiles\\acp\\cordis.yml"），丢掉之后主人只能看到
            一句"ACP 子进程关闭了 stdout"，等于没提示。
            """
            client = AcpClient(on_update=on_update,
                               on_log=lambda s: self.event.emit(str(s)),
                               exe=Path(exe), cli_js=Path(cli_js))
            client.start()
            client.initialize()
            client.authenticate()
            return client

        def _run(self, text, exe, cli_js, cwd, group, session_id=""):
            self.started.emit()
            buf = []
            outcome = None                  # (ok, 正文) —— 攒着，等 finally 再发
            client = None
            try:
                client = self._connect(
                    exe, cli_js,
                    on_update=lambda sid, u: self._on_update(u, buf))
                # session_id 有值 = 接着那段旧对话；没有 = 开一段新的。
                # resume 是"把持久会话重新组合起来"（文档原话：不重放旧更新），
                # 接上之后 prompt 就是在**同一个会话**里追加一轮 ——
                # 历史上下文她真的看得见。而 new_session 每次都是白纸。
                if session_id:
                    sess = client.resume_session(session_id, cwd=cwd)
                    sid = session_id        # resume 的返回里没有 sessionId，用请求的那个
                else:
                    sess = client.new_session(cwd=cwd)
                    sid = sess.get("sessionId")
                self.session.emit(sid, cwd)     # 让工作台把这段挂上/记住
                value = find_option_value(sess, "model", group)
                if value:
                    # value 是"装着 JSON 的不透明字符串"，必须原样回传（见 README）
                    client.set_config_option(sid, "model", value)
                else:
                    raise RuntimeError("没找到 %s 组的模型路由（要用主人登录的"
                                       "账号得在 harness_group 里填 deepseek-account）"
                                       % group)
                result = client.prompt(sid, text, timeout=1800)
                try:
                    client.close_session(sid, timeout=30)
                except Exception:
                    pass                          # 关不掉也别把干完的活丢了
                answer = "".join(buf).strip()
                reason = str(result.get("stopReason", ""))
                if answer:
                    outcome = (True, answer)
                else:
                    outcome = (False, "Harness 跑完了（%s），但没给出正文。"
                                      % (reason or "无 stopReason"))
            except Exception as err:
                outcome = (False, "%s: %s" % (type(err).__name__, err))
            finally:
                # 顺序是刻意的：先收子进程、再把 busy 复位、**最后**才发结果。
                # 反过来的话主人收到"干完了"的瞬间立刻派下一个活，
                # busy 还挂在 True 上，会被"上一个还没干完"误拒（实测踩过）。
                # 收子进程也是保命 —— 异常路径不收，dsh --profile acp 就成孤儿了。
                if client is not None:
                    try:
                        client.stop(timeout=5)
                    except Exception:
                        pass
                with self._lock:
                    self._busy = False
                if outcome is not None:
                    self.done.emit(*outcome)

        def _on_update(self, update, buf):
            """跑在工作线程里：只做"翻译 + emit"，绝不碰任何 UI 对象。"""
            kind = update_kind(update)
            if kind in ("agent_message_chunk", "agent_thought_chunk"):
                content = update.get("content") or {}
                piece = content.get("text", "") if isinstance(content, dict) else ""
                if piece:
                    buf.append(piece)
                    self.chunk.emit(piece)
            elif kind == "usage_update":
                self.usage.emit(int(update.get("used") or 0),
                                int(update.get("size") or 0))
            elif kind in ("tool_call", "tool_call_update"):
                title = str(update.get("title") or update.get("toolCallId") or "工具")
                status = str(update.get("status") or "")
                self.event.emit("%s%s" % (title, ("（%s）" % status) if status else ""))
            elif kind == "plan":
                entries = update.get("entries") or []
                if entries:
                    self.event.emit("计划 %d 步" % len(entries))

    return HarnessBridge()


def render_transcript_html(turns, live=""):
    """把对话染成角色色，返回 HTML。

    纯文本进、HTML 出，**先 escape 再排版** —— 顺序反了就是把模型输出当 HTML 执行
    （不加载外链，XSS 谈不上，但排版会炸）。live 是还在流式的那一段，挂在末尾。
    角色色表：主人/助手/工具/错误 —— 错误单独给红色，别混进"工具流水"的灰里。
    """
    palette = {
        "主人": ("#1F3A93", "#E8F0FE"),
        "助手": ("#1F2933", "#F4F6F8"),
        "工具": ("#5F6B7A", "#EDF1F4"),
        "错误": ("#B4483C", "#FDECEA"),
    }
    parts = []
    items = list(turns) + ([("助手", live)] if live else [])
    for role, text in items:
        fg, bg = palette.get(role, ("#1F2933", "#F4F6F8"))
        body = html.escape(text).replace("\n", "<br>")
        parts.append(
            '<div style="margin:8px 2px;">'
            '<span style="color:%s;font-weight:bold;">%s</span>'
            '<div style="background:%s;color:%s;border-radius:8px;'
            'padding:7px 10px;margin-top:3px;">%s</div></div>'
            % (fg, html.escape(role), bg, fg, body))
    return "".join(parts) or "<p>（这段对话还没有内容 —— 在下面说句话就开始）</p>"


def build_harness_console(parent, on_send, on_select, get_target):
    """Harness 工作台：**一屏解决"选哪段对话"和"在这段里说话"**。

    左边是所有任务对话（名字来自会话日志，只读、不过 ACP，所以 Harness 没开也能翻），
    右边是选中那段的历史记录 + 底部输入框。选哪段就在哪段里接着说 ——
    这就是把原来的「任务对话」窗口 + "派活前弹框选对话"合并成一个界面。

    为什么要合并：原来那两步是**同一件事被拆成两个界面**（先看历史、再决定接哪段），
    拆开之后主人得在两处之间来回对；合成一屏，"看到哪段、就在哪段说"是一步动作。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                                   QListWidget, QPushButton, QTextBrowser,
                                   QVBoxLayout)

    class HarnessConsole(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("Harness 工作台")
            # Qt.Tool = 附属小窗：不进任务栏、不参与"关掉最后窗口就退出"（钥匙框踩过）
            self.setWindowFlag(Qt.Tool, True)
            self.setModal(False)          # 看历史/说话不该抢死主人的焦点
            self.resize(960, 620)
            self._rows = []               # 列表项对应的会话元信息（第 0 项 = 新对话）
            self._turns = []              # 当前显示的历史记录
            self._live = ""               # 正在流式的那一段
            self._log_path = ""           # 当前会话的日志路径（干完活重读它）
            self._want_id = ""            # 下次刷新要选中的会话 id（新开的那段）

            head = QHBoxLayout()
            self.title = QLabel("Harness 工作台")
            self.title.setFont(chat_font(13, True))
            self.title.setStyleSheet("color:%s;" % COLOR_DEEP)
            refresh = QPushButton("刷新列表")
            refresh.setFont(chat_font(11))
            refresh.setCursor(Qt.PointingHandCursor)
            refresh.setStyleSheet(
                "QPushButton{background:%s;color:#FFFFFF;border:none;"
                "border-radius:8px;padding:4px 14px;}"
                "QPushButton:hover{background:#3C7FC6;}" % COLOR_BLUE)
            refresh.clicked.connect(self.refresh_sessions)
            head.addWidget(self.title)
            head.addStretch(1)
            head.addWidget(refresh)

            self.listw = QListWidget()
            self.listw.setFont(chat_font(12))
            self.listw.setFixedWidth(272)
            self.listw.setSpacing(3)
            self.listw.setStyleSheet(
                "QListWidget{background:#FFFFFF;border:1px solid %s;"
                "border-radius:10px;padding:6px;}"
                "QListWidget::item{border-radius:8px;padding:8px 9px;}"
                "QListWidget::item:selected{background:%s;color:#FFFFFF;}"
                % (COLOR_BLUE, COLOR_BLUE))
            self.listw.currentRowChanged.connect(self._on_row)

            self.view = QTextBrowser()
            self.view.setFont(chat_font(12))
            self.view.setStyleSheet(
                "QTextBrowser{background:#FFFFFF;border:1px solid %s;"
                "border-radius:10px;padding:10px;}" % COLOR_BLUE)
            self.view.setOpenExternalLinks(False)

            body = QHBoxLayout()
            body.addWidget(self.listw)
            body.addWidget(self.view, 1)

            self.status = QLabel("")
            self.status.setFont(chat_font(10))
            self.status.setWordWrap(True)
            self.status.setStyleSheet("color:%s;" % COLOR_BLUE)

            self.input = QLineEdit()
            self.input.setFont(chat_font(12))
            self.input.setPlaceholderText("在这段对话里说点什么…（回车派活）")
            self.input.setStyleSheet(
                "QLineEdit{background:#FFFFFF;border:1.5px solid %s;"
                "border-radius:12px;padding:8px 12px;color:%s;}"
                "QLineEdit:focus{border:1.5px solid %s;}"
                % (COLOR_BLUE, COLOR_DEEP, COLOR_DEEP))
            self.input.returnPressed.connect(self._emit)
            self.send_btn = QPushButton("派活")
            self.send_btn.setFont(chat_font(12, True))
            self.send_btn.setCursor(Qt.PointingHandCursor)
            self.send_btn.setStyleSheet(
                "QPushButton{background:%s;color:#FFFFFF;border:none;"
                "border-radius:12px;padding:8px 22px;}"
                "QPushButton:hover{background:#3C7FC6;}"
                "QPushButton:disabled{background:#BFD3E8;color:#F0F5FB;}"
                % COLOR_BLUE)
            self.send_btn.clicked.connect(self._emit)
            foot = QHBoxLayout()
            foot.addWidget(self.input, 1)
            foot.addWidget(self.send_btn)

            root = QVBoxLayout(self)
            root.addLayout(head)
            root.addLayout(body, 1)
            root.addWidget(self.status)
            root.addLayout(foot)
            self.setLayout(root)

        # ---------------------------------------------------------- 对外接口
        def open_window(self):
            self.refresh_sessions()
            self.show()
            self.raise_()
            self.input.setFocus()

        def refresh_sessions(self, keep_row=None):
            """重扫会话列表。keep_row 给定时尽量把选中项停回原来那段。

            每次刷新都重扫是有意的：干完一轮活之后，那段对话的轮数/时间都变了，
            列表要跟着更新（不然主人看到的是旧数据）。
            """
            want_row = keep_row if keep_row is not None else self.listw.currentRow()
            self.listw.blockSignals(True)      # 重建列表时别触发一串 _on_row
            self.listw.clear()
            self._rows = []
            self.listw.addItem("＋ 开一段新对话")
            if SESSION_LOG_AVAILABLE:
                try:
                    rows = [r for r in _session_log.list_sessions() if r.get("cwd")]
                except Exception:
                    rows = []
                for i, r in enumerate(rows, 1):
                    self.listw.addItem("对话%s  %s\n      %s · %d 轮"
                                       % (_cn_num(i), r["title"],
                                          r["time_label"], r["turns"]))
                    self._rows.append(r)
            self.listw.blockSignals(False)
            row = want_row
            if self._want_id:
                # 新开的那段一旦进了列表，就把它选上 —— 这一步是"接着说"的关键：
                # 不选它，主人第二句话就又被当成"再开一段"。
                for i, r in enumerate(self._rows, 1):
                    if r["id"] == self._want_id:
                        row = i
                        break
            row = min(max(row, 0), self.listw.count() - 1)
            self.listw.setCurrentRow(row)
            self._on_row(row)                  # 手动触发一次：blockSignals 把它挡掉了
            self._update_title()

        def current_target(self):
            return get_target()

        def adopt_session(self, session_id, cwd=""):
            """桥报回"这段活跑在哪个会话上"：刷一次列表并尽量选中它。

            对**新开**的会话尤其重要 —— 刚建出来时它还没有用户消息，
            list_sessions 会把空会话滤掉，所以这一次可能选不中；
            没关系，id 记在 _want_id 里，下一轮刷新（finish 时）就会选上。
            """
            self._want_id = session_id
            self.refresh_sessions()

        def stream(self, piece):
            """流式增量：挂在最后一段未定稿的回复上。"""
            self._live += piece
            self._render(scroll=True)

        def tool(self, line):
            self._turns.append(("工具", line))
            self._render(scroll=True)

        def note(self, text, role="助手"):
            self._turns.append((role, text))
            self._render(scroll=True)

        def fail(self, text):
            self._live = ""
            self._turns.append(("错误", text))
            self._render(scroll=True)

        def finish(self, text):
            """一轮干完：清掉流式缓存，刷列表，然后**从日志重读**当权威版本。

            为什么不直接把 text 追加进 _turns：日志里那一段带着完整的工具流水和
            真实轮次，重读一次显示的就和"任务对话"里看到的完全一致，
            不用维护第二套拼装逻辑。
            刷列表这一步不能省：新开的会话到这时才**有内容**（不再是空会话），
            才会出现在列表里被选上 —— 于是"第二句话接着说"就成立了。
            """
            self._live = ""
            if self._log_path:
                self._load_history()
            elif text:
                self._turns.append(("助手", text))
                self._render(scroll=True)
            self.refresh_sessions()

        def set_status(self, text):
            self.status.setText(text)

        def set_busy(self, busy):
            self.input.setEnabled(not busy)
            self.send_btn.setEnabled(not busy)
            self.send_btn.setText("干活中…" if busy else "派活")
            if not busy:
                self.input.setFocus()

        # ---------------------------------------------------------- 内部
        def _update_title(self):
            cur = get_target()
            self.title.setText("Harness 工作台 · 接的是：%s"
                               % (cur.get("title") if cur else "开一段新对话"))

        def _on_row(self, index):
            if index < 0:
                return
            # _want_id 只用于"下次刷新选谁"，一旦落到某一行就消耗掉 ——
            # 留着的话，主人手动点了别的对话，下一次刷新又会被拽回去。
            self._want_id = ""
            if index == 0:
                target, self._log_path = None, ""
            elif index - 1 < len(self._rows):
                r = self._rows[index - 1]
                target = {"id": r["id"], "cwd": r["cwd"], "title": r["title"]}
                self._log_path = r["log_path"]
            else:
                return
            on_select(target)
            self._update_title()
            if self._log_path:
                self._load_history()
            else:
                self._turns = []
                self._render()
                self.set_status("开一段新对话：说第一句话，她就替你在这里开一段。")

        def _load_history(self):
            try:
                self._turns = _session_log.read_transcript(self._log_path)
            except Exception as err:
                self._turns = [("错误", "读不出来：%s: %s"
                                % (type(err).__name__, err))]
            self._render(scroll=True)

        def _render(self, scroll=False):
            self.view.setHtml(render_transcript_html(self._turns, self._live))
            if scroll:
                bar = self.view.verticalScrollBar()
                bar.setValue(bar.maximum())

        def _emit(self):
            text = self.input.text().strip()
            if not text:
                return
            self.input.clear()
            cur = get_target()
            self._turns.append(("主人", text))
            if cur:
                self.set_status("接着「%s」干…" % (cur.get("title") or ""))
            else:
                self.set_status("给她开一段新对话…")
            self._render(scroll=True)
            on_send(text)

    return HarnessConsole()


def build_remote(parent, state_fn, sessions_fn=None, history_fn=None):
    """远程遥控桥：拥有 HTTP 服务，把 socket 线程里的指令排回主线程。

    为什么非要一层 QObject：HTTP 请求在 socket 线程里处理，绝不能直接摸窗口。
    把 Qt 信号发射器（command.emit）当回调递给服务，跨线程 emit 会自动排队到
    主线程 —— 和派活桥（build_harness）是同一个套路的反向：那次是"工作线程
    往回送更新"，这次是"外来线程往里送指令"，本质都是"跨线程 + 信号排队"。

    **注意：桥搭好之后一定要把 command 接到桌宠身上去**（在 _remote_bridge 里做）。
    曾经漏了这一步：HTTP 服务照常 202、信号也"发出去"了，只是**没有监听者**，
    于是手机网页上发什么都石沉大海 —— 而且不报任何错，极难查。
    """
    from PySide6.QtCore import QObject, Signal

    class RemoteBridge(QObject):
        command = Signal(dict)     # 桌宠要执行的动作（chat/harness/feed/…）

        def __init__(self):
            super().__init__(parent)
            self.server = None

        def start(self, token, port, bind, allow_harness):
            self.stop()                       # 重启语义：改端口/令牌后重开
            self.server = _pet_remote.RemoteServer(
                emit_fn=self.command.emit, state_fn=state_fn,
                token=token, port=port, bind=bind,
                allow_harness=allow_harness,
                # 这两个是给网页端"挑任务/看历史"用的只读回调：
                # 它们只读文件、不碰 Qt，所以在 socket 线程里直接调是安全的
                sessions_fn=sessions_fn, history_fn=history_fn,
                on_log=lambda s: None)
            self.server.start()
            return self.server

        def stop(self):
            if self.server is not None:
                self.server.stop()
                self.server = None

        def is_running(self):
            return self.server is not None and self.server.is_running()

        def publish(self, kind, text=""):
            if self.server is not None:
                self.server.publish(kind, text)

    return RemoteBridge()


def _file_size(path):
    """文件大小（字节）。读不到就返回 0 —— 只用来把话说清楚，不值得抛。"""
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def build_feishu(parent):
    """飞书桥：拥有一条长连接，把收到的消息排回主线程。

    和 build_remote 是同一个套路，方向也一致 —— 都是"外来线程往里送指令，
    靠 Qt 信号排队回主线程"。差别只在传输：那边是 HTTP 服务等别人连进来，
    这边是本机主动连出去（所以不需要公网 IP、没有端口暴露）。

    另外多一个 `tested` 信号：设置窗口里点「测试凭据」要发网络请求，
    必须扔到线程里，不然界面会定住几秒 —— 这个信号就是回话的口子。

    **回复也是同一个道理**：发消息走的是同步 urlopen（timeout 20s，
    取 tenant_access_token 还要再 15s），调用点却全在主线程的槽里。
    直接在槽里发，网络一慢就是"桌宠整体冻住十几秒"（拖不动、菜单点不开）。
    所以这里养一个常驻发送线程：主线程只入队，真正的 IO 在那边做，
    成败通过 note 信号回主线程写日志。
    """
    from PySide6.QtCore import QObject, Signal

    class FeishuBridge(QObject):
        command = Signal(dict)          # 一条飞书消息（已归一化）
        note = Signal(str)              # 连接/断线/异常之类的进展
        tested = Signal(bool, str)      # 「测试凭据」的回话
        # 抓屏用的两个信号（2026-10-11 加 /shot 时补）。**为什么必须是信号**：
        # 飞书那边的命令跑在工作线程（发送线程），而 Qt 的 QScreen.grabWindow
        # **只能在 GUI 线程调** —— 在工作线程里直接抓，轻则拿到空图，重则
        # "QObject used from another thread" 崩掉。Qt 的自动连接在这里正好
        # 是我们要的：本对象属于主线程，工作线程里 emit 出去会自动排队到
        # 主线程执行（和 command 从长连接线程排队回来是同一个机制）。
        #
        # 为什么不顺手用 QTimer.singleShot(0, ...)：它的接收者推断规则对
        # **普通函数/lambda** 不成立 —— 有可能就近在当前线程执行，那样就等于
        # 没跨线程。信号 + 显式槽是确定的。
        shot_want = Signal()            # 工作线程：请在 GUI 线程抓一张
        shot_done = Signal()            # GUI 线程：抓完了（给界面/将来留的口子）
        # 工作线程：请在 GUI 线程冒一句气泡（2026-10-11 修"手机发图过来桌宠
        # 未响应"时补）。**为什么必须有它**：气泡是 QWidget，只能在 GUI 线程碰。
        # 实测（工作区里的探针 `_probe/qt_affinity_probe.py`）：在工作线程里调
        # `SpeechBubble.show()` 或 `QWidget.setWindowFlags()`，GUI 线程会**永久**
        # 卡住 —— Windows 上就表现成"程序未响应"，事件日志里是 AppHangB1，
        # 而进程还在（所以看起来不像崩溃，像死机）。收图那条链路的实测日志
        # 与之完全对上：图片已经落盘，紧接着整个程序不响应了。
        say_want = Signal(str)          # 工作线程 → 主线程：冒一句气泡
 
        def __init__(self):
            super().__init__(parent)
            self.client = None
            self._send_q = queue.Queue()    # ("text"|"image", chat_id, 消息id, 正文/路径)
            self._sender = None
            self._sender_lock = threading.Lock()
            # 抓屏的握手位（跨线程）：_shot_tick 是"主线程已经处理过一次请求"
            # 的计数器，_capture_screen_file 靠比对它来判断自己那次有没有被处理。
            self._shot_lock = threading.Lock()
            self._shot_tick = 0
            self._shot_pixmap = None
            # 最近一次抓屏失败的原因（一句人话）。给调用方在"抓到空串"时
            # 补进回话里 —— 主人看到"没截到图：抓屏超时（10 秒）"才知道
            # 是主线程忙还是这台机器根本不给抓。
            self._last_shot_error = ""
            self._config_path = getattr(parent, "_config_path", None)
            self.shot_want.connect(self._on_shot_tick)

        # ------------------------------------------------------- 日志（落盘）
        def _log_line(self, text):
            """**所有**飞书进展都写进 <配置目录>/feishu_log.txt。

            为什么落盘：`note` 信号只把话送进内存里那个 40 行的环（设置窗口里
            才看得见）。/shot 传不出去、发图被飞书拒，多半发生在主人没开设置
            窗口的时候 —— 事后想查"当时报了什么"，磁盘上必须有东西。
            """
            _pet_feishu.append_feishu_log(self._config_path, text)

        def _note(self, text):
            """既写日志也走 note 信号（界面上那一栏照旧刷）。"""
            self._log_line(text)
            self.note.emit(text)

        def start(self, app_id, app_secret, domain):
            self.stop()                 # 重启语义：换凭证后重开
            self.client = _pet_feishu.FeishuClient(
                app_id, app_secret,
                on_event=self.command.emit,      # 跨线程 emit，自动排队到主线程
                on_log=self.note.emit,
                domain=domain)
            return self.client.start()

        def stop(self):
            if self.client is not None:
                self.client.stop()
                self.client = None

        # ---------------------------------------------------------- 抓屏（跨线程）
        def _on_shot_tick(self):
            """GUI 线程里执行：真的抓一张主屏。**只抓、不编码**。

            为什么不在这儿顺手 `save()`：PNG 编码一张 1920×1080 要 100~300ms，
            这是**主线程**，抓屏的瞬间主人可能正在拖桌宠（手一拖就卡一下）。
            编码挪到工作线程去做，主线程只花十几毫秒拷贝像素。
            """
            pix = None
            try:
                from PySide6.QtGui import QGuiApplication
                screen = QGuiApplication.primaryScreen()
                if screen is not None:
                    pix = screen.grabWindow(0)
            except Exception as err:
                self._last_shot_error = "抓屏抛异常：%s: %s" % (type(err).__name__, err)
                self._note("抓屏失败：%s: %s" % (type(err).__name__, err))
                pix = None
            if pix is None or pix.isNull():
                self._last_shot_error = self._last_shot_error or "这台机器没抓到画面（没有屏幕/受保护窗口）"
            with self._shot_lock:
                self._shot_pixmap = pix
                self._shot_tick += 1
            self.shot_done.emit()

        def capture_screen_file(self, timeout=10.0):
            """请 GUI 线程抓一张主屏并编码成 PNG，返回文件路径（失败返回空串）。

            **调用方在非 GUI 线程**（飞书发送线程）时：emit `shot_want` 把活排给
            主线程 → 等主线程把 `_shot_tick` 加一（**用计数器认领自己那一轮**，
            不用 Event：Event 只记"发生过"，万一下次请求恰好碰上上一轮迟到的
            通知，就会拿着旧图当新图发出去）→ 编码存盘。

            **调用方本来就在 GUI 线程**（现在的 /shot 就是：命令槽在主线程里跑）
            时：直接调 `_on_shot_tick`，不走信号。同线程的 emit 其实是直连、
            当场执行，行为一样 —— 但那样写埋着一个雷：哪天有人把连接改成队列式，
            主线程就会在这里**等自己**，那是永久卡死。显式分流就没有这个雷。

            返回空串的可能：没屏幕（离屏/远程桌面会话）、Qt 抓不到、
            超时（主线程正忙）、写盘失败 —— 每一种都会通过 note 说清原因，
            并且记在 `_last_shot_error` 里供调用方补进回话；
            绝不返回一个"看起来能用其实是空的"路径。
            """
            self._last_shot_error = ""
            from PySide6.QtCore import QThread
            with self._shot_lock:
                start = self._shot_tick       # 发请求**之前**的计数
                self._shot_pixmap = None
            if QThread.currentThread() is self.thread():
                self._on_shot_tick()          # 已经在 GUI 线程：直接抓
            else:
                self.shot_want.emit()         # 跨线程排队：真正的抓在主线程做
            deadline = time.time() + max(0.2, float(timeout))
            while True:
                with self._shot_lock:
                    ready = self._shot_tick > start
                    pix = self._shot_pixmap if ready else None
                    if ready:
                        self._shot_pixmap = None   # 别攥着整屏像素不放
                if ready:
                    break
                if time.time() >= deadline:
                    self._last_shot_error = ("抓屏超时（%.0f 秒，主线程正忙）"
                                             % timeout)
                    self._note("抓屏超时（%.0f 秒）：主线程可能正忙，再试一次"
                               % timeout)
                    return ""
                time.sleep(0.05)              # 小睡再问，别把发送线程转成死循环
            if pix is None or pix.isNull():
                # 具体原因 _on_shot_tick 已经写进 _last_shot_error / 日志了
                return ""
            path = self._shot_path()
            if not pix.save(path, "PNG"):
                self._last_shot_error = "截图存盘失败（目录只读或盘满？）：%s" % path
                self._note("截图存盘失败（目录只读或盘满？）：%s" % path)
                return ""
            return path

        def _shot_path(self):
            """截图临时文件：<配置目录>/feishu_media/_tmp/shot_<时间戳>_<短随机>.png。

            **名字必须唯一**：截图是"生成 → 上传 → 删掉"，名字撞上会出现
            "前一张被后一张删掉"或者"删到别人正在传的那张"。
            所以带毫秒时间戳 + 8 位随机十六进制（uuid4 才 8 位，够用且短）。
            """
            from pet_feishu import media_root
            tmp_dir = media_root(getattr(parent, "_config_path", None), tmp=True)
            try:
                tmp_dir.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                # 建不出来就让后面的 save() 去失败并说话（这里不提前返回空串：
                # 目录可能是被别的进程建了一半，真写一次才知道行不行）
                self.note.emit("临时目录建不出来（%s）：%s" % (tmp_dir, err))
            return str(tmp_dir / ("shot_%s_%s.png" % (
                datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3],
                uuid.uuid4().hex[:8])))

        # ---------------------------------------------------------- 发送线程
        def _ensure_sender(self):
            """懒起发送线程（常驻，不随 stop 退出 —— 它只在有活时醒来）。"""
            with self._sender_lock:
                if self._sender is not None and self._sender.is_alive():
                    return
                self._sender = threading.Thread(target=self._send_loop,
                                                daemon=True, name="feishu-send")
                self._sender.start()

        def _send_loop(self):
            """一条一条发，保持顺序（回复的先后不能乱）。

            队列里每项都带 `kind`：文字和图片**走同一个队列**是关键 ——
            分两条队列的话，"先回一句'截图来了'再发图"这种顺序就保不住了。

            图片发失败**必须让主人看见**：失败发生在这一条线程里，
            调用方（/shot 的槽）早就返回了，只有这里还知道"这条失败属于哪个
            会话、该回给谁"。所以失败时除了写日志，还往队列里补一条文字回话
            （同一队列 → 顺序自然，不用另外开线程）。
            """
            while True:
                item = self._send_q.get()
                kind = item[0] if item else ""
                client = self.client
                if client is None:
                    if kind == "image" and len(item) > 3 and item[3]:
                        # 已经停了：临时截图还是得删掉，不然 _tmp 里越攒越多。
                        # 这里**发不出去任何话**（连接都没了），但必须留痕：
                        # 以前这条是彻底静默的，主人在飞书里什么也等不到。
                        self._note("飞书连接当时是断的，这张图没发出去、临时文件已删：%s"
                                   % Path(item[3]).name)
                        self._drop_file(item[3])
                    continue            # 排队中的回复直接丢掉
                if kind == "text":
                    _kind, chat_id, message_id, text = item
                    try:
                        ok = client.reply_text(message_id, text, chat_id)
                    except Exception as err:
                        ok = False
                        self._note("飞书回复抛异常：%s: %s"
                                   % (type(err).__name__, err))
                    if not ok:
                        self._note("飞书回复没发出去")
                elif kind == "image":
                    _, chat_id, message_id, path = item
                    try:
                        key = client.send_image(chat_id, path, reply_id=message_id)
                    except Exception as err:
                        # 失败原因原样摆出来（上传被拒 / 图片过大 / 网络），
                        # 只写一句"发送失败"的话主人没法判断下一步做什么
                        why = _pet_feishu.image_failure_reason(
                            err, path, _file_size(path))
                        self._report_image_failure(chat_id, message_id, why)
                    else:
                        self._note("截图已发到飞书（image_key=%s，%s）"
                                   % (key, Path(path).name))
                    finally:
                        self._drop_file(path)

        def _report_image_failure(self, chat_id, message_id, why):
            """发图失败：写日志 + **在飞书里回一句人话**（绝不静默）。

            2026-10-11 主人报的"/shot 有回话但图传不到手机"就是死在这里：
            失败只进了内存里那个日志环，飞书那边一点动静都没有，
            主人只看到"马上发给你"，然后就没有然后了。
            """
            self._note("发图片失败：%s" % why)
            if chat_id:
                # 补一条文字回话进同一个队列（下一轮就被发出去，顺序在后）
                self._send_q.put(("text", chat_id, message_id,
                                  "截图没发出去 —— %s" % why))

        def _drop_file(self, path):
            """发完就删临时文件。删不掉只记一笔，绝不抛（不能因为清理失败
            把发送线程带走 —— 那条线程还担着所有后续回复）。"""
            try:
                if path:
                    Path(path).unlink(missing_ok=True)
            except OSError as err:
                self._note("临时文件删不掉（%s）：%s" % (path, err))

        def is_online(self):
            return self.client is not None and self.client.is_online()

        def status(self):
            if self.client is None:
                return {"state": "idle", "label": "未启动", "online": False,
                        "error": "", "uptime": 0.0, "last_event_ago": -1.0}
            return self.client.status()

        def reply(self, chat_id, message_id, text):
            """把一条回复**排进发送队列**（不阻塞主线程），返回是否排上了。

            返回值是"排上了"，不是"发出去了" —— 真成败由发送线程通过
            note 信号报到界面日志里（以前同步发、返回值也没人看，
            发失败就只剩模型接口那一行日志，主人根本不知道她没回）。
            """
            if self.client is None or not text:
                return False
            self._ensure_sender()
            self._send_q.put(("text", chat_id, message_id, text))
            return True

        def send_image(self, chat_id, path, message_id=""):
            """把一张本地图片排进发送队列（同步上传会卡住调用它的主线程）。

            排在文字后面是**故意的**：调用方先回一句"截图这就来"再排这张图，
            队列保序 → 手机上先看到那句话、再看到图。
            """
            if self.client is None or not path:
                return False
            self._ensure_sender()
            self._send_q.put(("image", chat_id, message_id, str(path)))
            return True

        def test_credentials(self, app_id, app_secret, domain):
            """点「测试凭据」：真去问一次飞书，但别卡住界面。"""
            def work():
                try:
                    ok, msg = _pet_feishu.check_credentials(
                        app_id, app_secret,
                        _pet_feishu.LARK_DOMAIN if domain == "lark"
                        else _pet_feishu.FEISHU_DOMAIN)
                except Exception as err:
                    ok, msg = False, "%s: %s" % (type(err).__name__, err)
                self.tested.emit(ok, msg)
            threading.Thread(target=work, daemon=True,
                             name="feishu-test").start()

    return FeishuBridge()


def build_chat(parent, cfg):
    """和大模型说话。

    用 Qt 自带的 QNetworkAccessManager，不装 requests、不开线程 ——
    网络 IO 挂在 Qt 事件循环上，界面不会卡住，打字的动画照常跑。
    """
    from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

    class ChatClient(QObject):
        chunk = Signal(str)       # 流式增量，用来做打字机效果
        done = Signal(str)        # 完整回复
        failed = Signal(str)      # 一句人话的错误提示

        def __init__(self):
            super().__init__(parent)
            self.cfg = dict(cfg)
            self.base_url, self.model, self.native_search = resolve_endpoint(cfg)
            self.manager = QNetworkAccessManager(self)
            self.history = []
            # 持久记忆上下文（长期记忆 + 最近对话，启动时加载，每轮都注入）。
            # 与 ask(extra_system) 的临时注入不同：那个只用于当轮检索结果，
            # 这个是桌宠的"记忆"，每次启动从日志文件里重建。
            self.memory_context = None
            self._reply = None
            self._buf, self._raw, self._answer = "", b"", ""
            # 这两个属性在 ask() 里重设，这里先给个初值：
            # _on_finished 有可能在 ask 之前被调用（老主机上先有 finished 才连槽）
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self._closed = False
            self._finish_reason = None

        def ready(self):
            """key / base_url / model 三样齐了才算能用。缺一样就整个功能关掉。"""
            return (bool(self.cfg.get("api_key")) and bool(self.base_url)
                    and bool(self.model))

        def busy(self):
            return self._reply is not None

        def reset(self):
            self.history = []

        def ask(self, text, extra_system=None):
            if self._reply is not None or not self.ready() or not text:
                return False
            self.history.append({"role": "user", "content": text})
            max_turns = int(self.cfg.get("max_turns", 8) or 8)
            self.history = self.history[-max_turns * 2:]    # 只留最近几轮，别把上下文撑爆

            messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
            # 本地时间每轮现取 —— 注入在 memory_context 之前，
            # 它是"此刻的事实"，优先级高于记忆里的旧对话
            messages.append({"role": "system", "content": now_line()})
            if self.memory_context:
                messages.append({"role": "system", "content": self.memory_context})
            if extra_system:
                # 检索结果作为**临时** system 注入，不进 history ——
                # 否则上一轮的搜索结果会一直黏在后面几轮对话里
                messages.append({"role": "system", "content": extra_system})
            messages += self.history

            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "max_tokens": int(self.cfg.get("max_tokens", 320) or 320),
                "temperature": float(self.cfg.get("temperature", 1.0) or 1.0),
            }
            if self.native_search and str(self.cfg.get("search")) == "native":
                payload["enable_search"] = True      # 通义千问这类厂商的自带联网开关

            request = QNetworkRequest(QUrl(self.base_url + "/chat/completions"))
            request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
            request.setRawHeader(
                b"Authorization",
                ("Bearer " + str(self.cfg.get("api_key", ""))).encode("utf-8"))
            # 传输超时 = 多久没有任何字节就放弃；流式对话下正合适
            request.setTransferTimeout(int(float(self.cfg.get("timeout", 30)) * 1000))

            self._buf, self._raw, self._answer = "", b"", ""
            # 增量解码器：一个中文字被 TCP 包切成两半时，逐包 decode 会丢掉
            # 半个字符（以前用 errors="ignore"，丢得无声无息）。解码器会把
            # 半截字节留在自己肚子里，等下一包到齐再吐出来。
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self._closed = False        # 服务端明确说过 [DONE]
            self._finish_reason = None
            body = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            self._reply = self.manager.post(request, body)
            self._reply.readyRead.connect(self._on_read)
            self._reply.finished.connect(self._on_finished)
            return True

        def _feed_text(self, text):
            """把一段**已解码**的文本按行喂给 SSE 解析（残片留在 _buf 里）。"""
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                piece = sse_delta(line)
                if piece is None:
                    reason = sse_finish_reason(line)
                    if reason:
                        self._finish_reason = reason
                    continue
                if piece == "":
                    self._closed = True          # data: [DONE]
                    continue
                self._answer += piece
                self.chunk.emit(piece)

        def _on_read(self):
            if self._reply is None:
                return
            raw = bytes(self._reply.readAll())
            self._raw += raw
            self._feed_text(self._decoder.decode(raw))

        def _on_finished(self):
            reply = self._reply
            self._reply = None
            if reply is None:
                return
            error = reply.error()
            code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            error_name = getattr(error, "name", str(error))
            raw = self._raw
            reply.deleteLater()

            # 收尾两个动作，缺一个都会"丢最后一段话"：
            # ① 解码器里还压着半截字节；② _buf 里那行没有尾换行的 data:。
            try:
                tail = self._decoder.decode(b"", True)
            except Exception:
                tail = ""
            if tail:
                self._buf += tail
            if self._buf:
                self._feed_text("\n")     # 补个换行，让最后一行走同一条路
            self._buf = ""

            if not self._answer and error_name != "NoError":
                self.failed.emit(describe_net_error(error_name, code))
                return
            if not self._answer:
                # 有的服务端/代理不吃 stream，会直接回一整块 JSON
                self._answer = reply_from_json(raw.decode("utf-8", "replace"))
            if not self._answer:
                self.failed.emit("她张了张嘴，没说出话来……要不再问一次？")
                return
            # 网络错误 / 没收到 [DONE] / finish_reason=length：正文可能是半截的。
            # **照样显示**（半句也比什么都没有强），但要留个痕，别让它看起来
            # 像"她说完了"。
            if error_name != "NoError" and not self._closed:
                _warn("流式回复中断（%s），已显示收到的 %d 字"
                      % (describe_net_error(error_name, code), len(self._answer)))
            elif self._finish_reason == "length":
                _warn("回复被 max_tokens 截断（finish_reason=length），"
                      "可在配置里调大 max_tokens")
            self.history.append({"role": "assistant", "content": self._answer})
            self.done.emit(self._answer)

    return ChatClient()
