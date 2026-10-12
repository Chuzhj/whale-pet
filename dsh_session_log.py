# -*- coding: utf-8 -*-
"""dsh_session_log —— 只读解析 DSH 的会话日志：列出任务对话 + 还原正文。

为什么不用 ACP：ACP 的文档写得很明白 ——
  * "No transcript replay" —— 它不给会话历史；
  * session/list 只返回 {sessionId, cwd}，**连标题都没有**（ACP 故意不暴露 title）。
所以"列出任务对话 + 点开看内容"只能读落盘文件。纯只读，不写任何东西。

数据在哪（两个来源，都以 $DSH_HOME 为根）：
  1. 会话日志  sessions/<workspace-slug>/<session-id>/session.v4.jsonl.zstd
     * zstd **多帧拼接**的追加日志 —— 每次落盘一个帧。最大的坑在这：
       只调 ZstdDecompressor().decompress() 只会解出**第一帧**
       （实测 54KB 的文件只还原出 244 字节、1 行），必须跨帧读。
     * 内容是 JSONL 事件流，type 有 session / user/message / assistant/message /
       tool/call / tool/result / session/title / turn/* / step/* ...
  2. 投影缓存  storages/session_projcache/sessions/<session-id>.json
     * 明文 JSON，record.rows.title.val 是持久化的任务名（比日志里的更"最终"）。
     * 本模块只拿它当标题的**首选来源**，读不到就退回日志里的 session/title，
       再退回首条用户消息 —— 三级兜底，哪个都在都能出名字。

依赖：zstandard（>=0.16，跨帧读需要 read_across_frames 参数）。纯标准库 + 这一个包。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    import zstandard as _zstd
except ImportError:                     # 缺包时整体降级，别让桌宠崩
    _zstd = None

USER_ROLE = "主人"
ASSISTANT_ROLE = "助手"
TOOL_ROLE = "工具"

TITLE_FALLBACK_LEN = 24                 # 拿首条提问当标题时截多长

# 会话是谁跑的。判据全部来自 DSH 自己写进日志头部的字段（2026-10-11 实地核对）：
#   origin           子会话是 "subagent"；**主会话连这个键都没有**
#   parentSession    子会话记着父会话 id；主会话没有
#   delegationDepth  主会话 0、直接子会话 1、再往下 2、3…
# 三个一起看才稳：老日志可能没写 origin，只看它就会把子会话当主会话。
SESSION_KIND_MAIN = "main"
SESSION_KIND_SUB = "sub"


def classify_session(header) -> tuple:
    """判断一个会话是**主智能体**跑的还是**子智能体**跑的。

    入参是日志里的那个头部事件（`type == "session"` 的第一条）。
    返回 `(kind, parent_id, depth)`：
      * kind      —— "main" / "sub"
      * parent_id —— 子会话的父会话 id（主会话为空串）
      * depth     —— 委托深度（主会话 0）
    纯函数、不碰文件，方便单测和复用。
    """
    if not isinstance(header, dict):
        return SESSION_KIND_MAIN, "", 0
    origin = str(header.get("origin") or "").strip().lower()
    parent = str(header.get("parentSession") or "").strip()
    try:
        depth = int(header.get("delegationDepth") or 0)
    except (TypeError, ValueError):
        depth = 0
    is_sub = (bool(parent)
              or origin in ("subagent", "sub-agent", "child", "sub")
              or depth > 0)
    return (SESSION_KIND_SUB if is_sub else SESSION_KIND_MAIN), parent, max(0, depth)


def group_sessions(items):
    """把会话列表整理成「主会话 + 它派出去的子会话们」。

    返回 `[(main_item or None, [sub_item, ...]), ...]`：
      * 主会话之间保持传入顺序（调用方已按最近活动排好）；
      * 子会话紧跟**它所属的那个主会话**（深度 2+ 会顺着 parentSession
        一路往上找到根主会话，所以孙会话也挂在同一个主会话下）；
      * 找不到父会话的（父被清理了）单独成组、`main_item is None` —— 
        **不能丢**，丢了主人就看不见自己派出去的活了。

    为什么不让调用方自己排：这条逻辑要同时被左栏列表和飞书那边的编号列表用，
    写两份早晚会分叉。
    """
    items = list(items or [])
    by_id = {}
    for it in items:
        sid = str(it.get("id") or "")
        if sid:
            by_id[sid] = it
    mains = [it for it in items if it.get("kind") != SESSION_KIND_SUB]
    subs = [it for it in items if it.get("kind") == SESSION_KIND_SUB]

    def root_main_id(sub):
        """顺着 parentSession 往上找根主会话；找不到返回空串。"""
        seen = set()
        cur = sub
        while True:
            pid = str(cur.get("parent") or "")
            if not pid or pid in seen:
                return ""
            seen.add(pid)
            nxt = by_id.get(pid)
            if nxt is None:
                return ""
            if nxt.get("kind") != SESSION_KIND_SUB:
                return str(nxt.get("id") or "")
            cur = nxt

    children = {}
    orphans = []
    for sub in subs:
        rid = root_main_id(sub)
        if rid:
            children.setdefault(rid, []).append(sub)
        else:
            orphans.append(sub)
    groups = [(m, children.get(str(m.get("id") or ""), [])) for m in mains]
    if orphans:
        groups.append((None, orphans))
    return groups


def available() -> bool:
    """zstandard 缺不缺。调用方据此决定要不要把入口藏起来。"""
    return _zstd is not None


# 运行期可以覆盖（main 会把配置里手填的 dsh_home 塞进来）
_HOME_OVERRIDE = None


def set_home(path) -> None:
    """手动指定 DSH 家目录（配置里的 dsh_home）。传空 = 回到自动识别。"""
    global _HOME_OVERRIDE
    _HOME_OVERRIDE = Path(path).expanduser() if path else None


def home_candidates():
    """**别人的电脑上** DSH 可能在哪：按"最可能"到"最不可能"排。

    为什么要猜：主人的机器上就是 ~/.dsh，但别人可能改了安装位置、
    或者是绿色版放在别处。凡是带 sessions 子目录的都认。
    """
    cands = []
    env = os.environ.get("DSH_HOME")
    if env:
        cands.append(Path(env).expanduser())
    home = Path.home()
    cands.append(home / ".dsh")                    # 官方默认
    for var, names in (("APPDATA", ("dsh", "DSH", ".dsh", "deepseek-harness")),
                       ("LOCALAPPDATA", ("dsh", "DSH", ".dsh",
                                         "deepseek-harness")),
                       ("USERPROFILE", (".dsh", "dsh"))):
        root = os.environ.get(var)
        if not root:
            continue
        for name in names:
            cands.append(Path(root) / name)
    cands.append(home / "Documents" / "deepseek-harness" / ".dsh")
    cands.append(home / "AppData" / "Roaming" / "DeepSeek Harness" / ".dsh")
    # 去重但保序
    out, seen = [], set()
    for c in cands:
        key = str(c).lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def dsh_home() -> Path:
    """DSH 的家目录。顺序：手动指定 → 环境变量 → 逐个候选（认带 sessions 的那个）
    → 退回 ~/.dsh。

    判据用"有没有 sessions 子目录"，而不是"目录存在" —— 别人机器上可能残留
    一个空的 dsh 目录（装过又卸了），按存在性认会指错地方、任务对话全空。
    """
    if _HOME_OVERRIDE is not None:
        return _HOME_OVERRIDE
    for c in home_candidates():
        try:
            if (c / "sessions").is_dir():
                return c
        except OSError:
            continue
    return Path.home() / ".dsh"


def sessions_root() -> Path:
    return dsh_home() / "sessions"


def projcache_dir() -> Path:
    return dsh_home() / "storages" / "session_projcache" / "sessions"


# ---------------------------------------------------------------- 解压
def _decompress_all(blob: bytes) -> str:
    """跨帧解压。这里是整个模块最关键的坑，单独抽出来并写明白。"""
    if _zstd is None:
        raise RuntimeError("缺少 zstandard 模块（pip install zstandard）")
    dctx = _zstd.ZstdDecompressor()
    try:
        # read_across_frames=True：一个 .zst 里可以拼着多个独立帧，
        # 默认只读第一帧 —— DSH 的日志就是多帧追加写的。
        return dctx.stream_reader(blob, read_across_frames=True).read().decode(
            "utf-8", "replace")
    except TypeError:
        raise RuntimeError("zstandard 版本太老：需要 >=0.16（跨帧读取）")


def _events(path: Path):
    """按序产出这个会话日志里的每个事件（dict）。解析失败的行跳过。"""
    raw = _decompress_all(path.read_bytes())
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _text_of(blocks, kinds=("text",)) -> str:
    """从内容块列表里抽正文。助手消息里混着 reasoning（内部思考），按类型过滤。"""
    out = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") in kinds and b.get("text"):
            out.append(str(b["text"]))
    return "\n".join(out).strip()


def _tool_brief(data: dict, limit: int = 80) -> str:
    name = str(data.get("name") or "工具")
    args = data.get("arguments")
    if isinstance(args, str):
        brief = args
    else:
        try:
            brief = json.dumps(args, ensure_ascii=False)
        except Exception:
            brief = str(args)
    brief = " ".join(brief.split())
    return "%s(%s)" % (name, brief[:limit] + ("…" if len(brief) > limit else ""))


def _is_human(ev: dict) -> bool:
    """这条 user/message 是不是**真人**发的。

    user/message 不等于用户输入：Harness 注入的运行时上下文、技能目录
    也走这个事件，source.kind 分别是 runtime-context / skill-catalog。
    不过滤的话"轮数"会翻三倍、正文里混满 system-reminder（实测踩到）。
    """
    src = (ev.get("data") or {}).get("source") or {}
    return src.get("kind") == "user"


def _title_from_projcache(session_id: str) -> str:
    """投影缓存里的任务名。读不到给空串，让上层走兜底。"""
    p = projcache_dir() / ("%s.json" % session_id)
    if not p.exists():
        return ""
    try:
        rec = json.loads(p.read_text(encoding="utf-8")).get("record") or {}
        val = ((rec.get("rows") or {}).get("title") or {}).get("val")
        return str(val).strip() if val else ""
    except Exception:
        return ""


def _time_label(mtime: float) -> str:
    lt = time.localtime(mtime)
    now = time.localtime()
    if lt[:3] == now[:3]:
        return "今天 %02d:%02d" % (lt[3], lt[4])
    return "%02d-%02d %02d:%02d" % (lt[1], lt[2], lt[3], lt[4])


# ---------------------------------------------------------------- 对外接口
def list_sessions(include_empty: bool = False):
    """扫出所有会话的元信息，按最近活动排序。

    返回 dict 列表：{id, cwd, title, mtime, time_label, turns, log_path,
    kind, parent, depth}。
      * turns 是"主人发话的次数"，可以当"这段对话有多少轮"的粗指标；
      * kind / parent / depth 说明这段会话是**主智能体**跑的（"main"）
        还是**子智能体**跑的（"sub"）—— 见 classify_session。
    空会话（一条用户消息都没有）默认不列 —— 没内容的"对话"只会添乱。
    **但子会话例外**：派出去的子智能体往往只有一条（派活那句）就干完了，
    按 turns==0 滤掉的话主人会以为活没派出去。所以子会话一律保留。
    """
    root = sessions_root()
    items = []
    if not root.exists():
        return items
    for ws in sorted(root.iterdir()):
        if not ws.is_dir():
            continue
        for sdir in sorted(ws.iterdir()):
            log = sdir / "session.v4.jsonl.zstd"
            if not sdir.is_dir() or not log.exists():
                continue
            try:
                info = _scan_log(log)
            except Exception:
                continue                       # 单个文件坏了不能拖垮整个列表
            if not include_empty and info["turns"] == 0 \
                    and info.get("kind") != SESSION_KIND_SUB:
                continue
            info["log_path"] = str(log)
            info["mtime"] = log.stat().st_mtime
            info["time_label"] = _time_label(info["mtime"])
            items.append(info)
    items.sort(key=lambda x: -x["mtime"])
    return items


def _scan_log(log: Path) -> dict:
    """一遍读完：id / cwd / 标题 / 轮数 / 主子身份。标题三级兜底（缓存→日志→首问）。"""
    sid = log.parent.name
    cwd = ""
    title = ""
    first_user = ""
    turns = 0
    kind, parent, depth = SESSION_KIND_MAIN, "", 0
    for ev in _events(log):
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "session":
            sid = str(ev.get("id") or sid)     # 头部里的 id 才是权威值
            cwd = str(ev.get("cwd") or "")
            kind, parent, depth = classify_session(ev)
        elif etype == "session/title":
            t = str(data.get("title") or "").strip()
            if t:
                title = t                      # 取最后一条（LLM 起的名会覆盖兜底名）
        elif etype == "user/message" and _is_human(ev):
            text = _text_of(data.get("content"))
            turns += 1
            if not first_user and text:
                first_user = text
    if not title:
        title = _title_from_projcache(sid)
    if not title and first_user:
        title = first_user[:TITLE_FALLBACK_LEN] + ("…" if len(first_user) > TITLE_FALLBACK_LEN else "")
    return {"id": sid, "cwd": cwd, "title": title or sid, "turns": turns,
            "kind": kind, "parent": parent, "depth": depth}


def read_transcript(log_path, include_tools: bool = True):
    """把一段会话还原成对话列表：[(role, text), ...]，按发生顺序。

    规则：
      * user/message（且 source.kind == user）→ 主人
      * assistant/message → 助手（只取 text 块，**丢掉 reasoning** —— 内部思考又长又吵）
      * tool/call      → 工具（一行摘要：名字 + 参数截断）；include_tools=False 可关
      * 其余事件（turn/step/token…）全是内部机制，不进对话
    """
    out = []
    for ev in _events(Path(log_path)):
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "user/message" and _is_human(ev):
            text = _text_of(data.get("content"))
            if text:
                out.append((USER_ROLE, text))
        elif etype == "assistant/message":
            text = _text_of((data.get("message") or {}).get("content"))
            if text:
                out.append((ASSISTANT_ROLE, text))
        elif etype == "tool/call" and include_tools:
            out.append((TOOL_ROLE, _tool_brief(data)))
    return out


# ---------------------------------------------------------------- 删除一段任务
def _inside(path, root) -> bool:
    """path 是不是在 root 里面。**删除功能的第一道保险**。

    用 `resolve()` 之后比前缀 —— 这样 `..` 和符号链接都绕不出去；
    只比字符串前缀会漏掉 `.../sessions/x/../../..` 这种（真删起来是灾难）。
    """
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
        return p == r or r in p.parents
    except OSError:
        return False


def find_session_dir(session_id: str):
    """按会话 id 找到它的落盘目录（sessions/<项目>/<id>）。找不到返回 None。"""
    root = sessions_root()
    if not root.exists():
        return None
    for ws in root.iterdir():
        if not ws.is_dir():
            continue
        cand = ws / session_id
        if cand.is_dir():
            return cand
    return None


def _cache_of(session_id: str):
    """这段会话的投影缓存文件（标题就存在那里）；不在 DSH 家目录里就不认。"""
    cache = projcache_dir() / ("%s.json" % session_id)
    return cache if cache.exists() and _inside(cache, dsh_home()) else None


def descendant_sessions(session_id: str):
    """把 `session_id` 的**所有后代会话**收齐 —— 子、孙、曾孙……任意深度。

    返回 `[(会话id, 目录, 深度), ...]`，**按深度从深到浅**排好；
    删除时照这个顺序走，就是"先删叶子、父目录最后删"。

    为什么必须是传递闭包（主人 2026-10-11 点名修的）：子智能体自己还会再派活，
    只按 `parent == 本会话` 收一层的话，**孙子辈的目录会留成孤儿** ——
    日志和投影缓存都还在，界面上却再也找不到入口，只能手工去磁盘里刨。

    两道保护：
      * **环保护 `seen`**：`parent` 是别人写进日志的字段，脏数据里出现过自指
        （A→A）和成环（A→B→A）。每个 id 只入队一次，环、自指、多个父指向同一处
        全都收敛，不会死循环 —— 没有它就是桌宠卡死。
      * **路径校验 `_inside`**：每个后代目录都要落在 `dsh_home()/sessions` 里面；
        不合格的既不收进清单、也**不再顺着它往下展开**（它下面的东西同样不该由我们删）。
    """
    home = dsh_home()
    kids = {}
    for row in list_sessions(include_empty=True):
        cid = str(row.get("id") or "")
        pid = str(row.get("parent") or "")
        if cid and pid:
            kids.setdefault(pid, []).append(cid)

    out = []
    seen = {session_id}                    # 起点先占位：自指也走不出去
    frontier = [(session_id, 0)]
    while frontier:
        cur, depth = frontier.pop(0)       # 队列：一层一层往外扩
        for cid in kids.get(cur, []):
            if cid in seen:                # 环 / 自指 / 重复指向 —— 一律跳过
                continue
            seen.add(cid)
            cdir = find_session_dir(cid)
            if cdir is None or not _inside(cdir, home / "sessions"):
                continue
            out.append((cid, cdir, depth + 1))
            frontier.append((cid, depth + 1))
    out.sort(key=lambda item: -item[2])    # 先深后浅：叶子在前，父在后
    return out


def plan_delete(session_id: str):
    """删除前的**清单**（给确认框用）。只是算，不动任何文件。

    返回 {ok, dirs, files, main_files, bytes, main_bytes, children,
    descendants, sub_count, sub_depth, reason}：
      * `descendants` / `children` —— **所有层级**的子会话（子、孙、曾孙……），
        按深度从深到浅排好；`children` 只放目录，`descendants` 带 id 和深度；
      * `sub_count` 是子会话**总数（含各层）**、`sub_depth` 是最深到第几层 ——
        确认框靠这两个数把规模说清楚；
      * `bytes` 是**总字节**（主目录 + 所有层子会话 + 各层投影缓存），
        `main_bytes` / `main_files` 只算主目录，好让文案分得清"谁占了多少"；
      * `reason` 非空表示不能删（找不到 / 不在 DSH 目录内）。
    """
    info = {"ok": False, "dirs": [], "files": [], "main_files": [], "bytes": 0,
            "main_bytes": 0, "children": [], "descendants": [],
            "sub_count": 0, "sub_depth": 0, "reason": ""}

    def _sweep(dirpath, into_main=False):
        """把这棵目录树的文件与字节数记进清单。

        `files` / `bytes` 是**要删的全部**（含各层子会话）；`main_*` 只累计主目录 ——
        确认框里"本地任务文件夹 N 个文件"说的是主目录那一份，
        大数字（含子会话）走"合计"那一行，两处不混。
        """
        for f in dirpath.rglob("*"):
            if not f.is_file():
                continue
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            info["bytes"] += size
            info["files"].append(f)
            if into_main:
                info["main_bytes"] += size
                info["main_files"].append(f)

    sdir = find_session_dir(session_id)
    if sdir is None:
        info["reason"] = "找不到这段任务的目录（可能已经被删了）"
        return info
    home = dsh_home()
    if not _inside(sdir, home / "sessions"):
        info["reason"] = "它不在 DSH 的 sessions 目录里，为安全起见不动它"
        return info
    info["dirs"].append(sdir)
    _sweep(sdir, into_main=True)
    # 子会话：**传递闭包**，一层层往下收齐（环保护与路径校验见 descendant_sessions）
    try:
        for cid, cdir, depth in descendant_sessions(session_id):
            info["descendants"].append((cid, cdir, depth))
            info["children"].append(cdir)
            info["sub_depth"] = max(info["sub_depth"], depth)
            _sweep(cdir)
    except Exception:
        pass                      # 列不出来也不该拦住删除（主目录照样删）
    info["sub_count"] = len(info["children"])
    # 投影缓存（明文 json，标题就存在那里）：主会话一份，**每个后代各一份**
    cache = _cache_of(session_id)
    if cache is not None:
        info["files"].append(cache)
        info["main_files"].append(cache)
        try:
            info["bytes"] += cache.stat().st_size
            info["main_bytes"] += cache.stat().st_size
        except OSError:
            pass
    for cid, _cdir, _depth in info["descendants"]:
        dcache = _cache_of(cid)
        if dcache is None:
            continue
        info["files"].append(dcache)
        try:
            info["bytes"] += dcache.stat().st_size
        except OSError:
            pass
    info["ok"] = True
    return info


def delete_session(session_id: str):
    """真删：主会话目录 + **所有层级**的子会话目录 + 各自的投影缓存。

    返回 (ok, 说明文字)。

    三道保险（删除功能的命门，一条都不许绕）：
      ① 每个目录在 `shutil.rmtree` 之前都要过 `_inside(..., sessions 根)` ——
         `plan_delete` 里那次是"算之前先拦"，这里是"动手之前再拦"，**两次都要在**；
      ② 删除顺序**先深后浅**：`plan_delete` 给的 `descendants` 已经按深度排好，
         叶子先走、父目录最后删 —— 父目录先没了的话，子目录的路径就悬空了；
      ③ 删不动的**只记账不中断**：某个子会话被别的进程占着，不影响主目录删掉。
    """
    import shutil

    info = plan_delete(session_id)
    if not info.get("ok"):
        return False, info.get("reason") or "不能删除"
    home = dsh_home()
    sdir = find_session_dir(session_id)
    if sdir is None or not _inside(sdir, home / "sessions"):
        return False, "路径校验没过，没动任何文件"
    gone, failed = 0, []
    # 先深后浅：info["descendants"] 已经是这个顺序（见 descendant_sessions）
    for _cid, cdir, _depth in info["descendants"]:
        if not _inside(cdir, home / "sessions"):
            continue
        try:
            shutil.rmtree(str(cdir))
            gone += 1
        except OSError as err:
            failed.append("%s（%s）" % (cdir.name, err))
    try:
        shutil.rmtree(str(sdir))
    except OSError as err:
        return False, "删不掉 %s：%s" % (sdir, err)
    # 投影缓存：主会话一份、每个后代各一份 —— 目录都没了，这些 json 没人会再读
    for cid in [session_id] + [d[0] for d in info["descendants"]]:
        cache = _cache_of(cid)
        if cache is None or not _inside(cache, dsh_home()):
            continue
        try:
            cache.unlink()
        except OSError:
            pass
    size_mb = info["bytes"] / 1024.0 / 1024.0
    msg = "删掉了这段任务（含 %d 个子会话，含各层；约 %.1f MB）" % (gone, size_mb)
    if failed:
        msg += "；有 %d 个子会话没删掉：%s" % (len(failed), "、".join(failed[:2]))
    return True, msg
