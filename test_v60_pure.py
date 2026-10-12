# -*- coding: utf-8 -*-
"""v60 改动的离线验证：不碰 Qt，直接 import whale_pet 测纯函数与存储层。

用法（在项目目录下）：
    python <本文件>            # 或把项目目录加进 sys.path
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# 项目目录 = 本文件所在目录（不写死绝对路径，换机器 / 改目录名都能跑）
PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))

import whale_pet as wp   # noqa: E402

ok = True


def ck(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("  OK   " if cond else "  FAIL ") + name + (("  << " + str(extra)) if not cond and extra else ""))


print("=== 1. 时长解析（P2：复合时长 / 一刻钟 / 时刻不算时长）===")
cases = [("1个半小时", 5400.0), ("一个半小时", 5400.0), ("半小时", 1800.0),
         ("一刻钟", 900.0), ("十秒", 10.0), ("30秒", 30.0), ("两小时", 7200.0),
         ("三十分钟", 1800.0), ("0.5分钟", 30.0)]
for text, want in cases:
    got = wp.parse_duration(text)
    ck("parse_duration(%r) == %s" % (text, want), got == want, got)
ck("'三点十分提醒我' 不算时长", wp.parse_duration("三点十分提醒我") is None,
   wp.parse_duration("三点十分提醒我"))
ck("'下午三点叫我' 不算时长", wp.parse_duration("下午三点叫我") is None)
ck("reminder_from_user 仍能兜底十秒",
   (wp.reminder_from_user("十秒后提醒我睡觉") or (None,))[0] == 10.0,
   wp.reminder_from_user("十秒后提醒我睡觉"))

print()
print("=== 2. 流式标记隐藏（P2：[apple] 不该被吞）===")
ck("完整标记被抹掉", wp.hide_mood_fragment("好呀[mood:happy]") == "好呀")
ck("半截 [mood:hap 被藏掉", wp.hide_mood_fragment("好呀[mood:hap") == "好呀")
ck("半截 [moo 被藏掉", wp.hide_mood_fragment("好呀[moo") == "好呀")
ck("半截 [remind:3 被藏掉", wp.hide_mood_fragment("好呀[remind:3") == "好呀")
ck("[apple] 保留", wp.hide_mood_fragment("[apple] 是水果") == "[apple] 是水果",
   wp.hide_mood_fragment("[apple] 是水果"))
ck("普通方括号 [1] 保留", wp.hide_mood_fragment("第 [1] 条") == "第 [1] 条")
ck("strip_reply_tags 照常剥五个标记",
   wp.strip_reply_tags("好[mood:shy][memory:主人是CS专业][remind:25|休息]")[0] == "好")

print()
print("=== 3. 密钥加密落盘（P0：DPAPI）===")
plain = "sk-test-1234567890abcdef"
enc = wp._protect_secret(plain)
ck("加密后有 enc:v1: 前缀", enc.startswith("enc:v1:"), enc[:16])
ck("密文不等于明文", enc != plain)
ck("解回来等于原文", wp._unprotect_secret(enc) == plain, wp._unprotect_secret(enc))
ck("明文原样通过（兼容老配置）", wp._unprotect_secret(plain) == plain)
ck("空值安全", wp._unprotect_secret("") == "" and wp._protect_secret("") == "")
ck("打码不动前 4 后 2", wp._mask_secret(plain).startswith("sk-t")
   and wp._mask_secret(plain).endswith(plain[-2:]))

print()
print("=== 4. 配置读写（BOM 容错 / 原子写 / 加密）===")
with tempfile.TemporaryDirectory() as tmp:
    cfgp = Path(tmp) / "whale_pet_config.json"
    # 4a. 带 BOM 的 UTF-8（记事本默认）必须能读
    cfgp.write_text("\ufeff" + json.dumps({"api_key": plain, "provider": "qwen"},
                                          ensure_ascii=False), encoding="utf-8")
    cfg = wp.load_config(cfgp)
    ck("BOM 配置能读出来（以前静默回落默认）", cfg.get("api_key") == plain,
       cfg.get("api_key"))
    ck("BOM 配置里其他键也在", cfg.get("provider") == "qwen")
    # 4b. 保存 → 文件里是密文、读回来是明文
    ck("save_api_key 返回 True", wp.save_api_key(plain, cfgp) is True)
    raw = cfgp.read_text(encoding="utf-8")
    ck("文件里没有明文 key", plain not in raw)
    ck("文件里是 enc:v1: 密文", "enc:v1:" in raw)
    ck("load_config 解回明文", wp.load_config(cfgp).get("api_key") == plain)
    ck("没有留下 .tmp", not (Path(tmp) / "whale_pet_config.json.tmp").exists())
    ck("save_config_values 返回 True",
       wp.save_config_values({"remote_token": "tok-abcdefghijkl"}, cfgp) is True)
    ck("第二个密钥也加密了", "tok-abcdefghijkl" not in cfgp.read_text(encoding="utf-8"))
    ck("两个密钥都能解回来",
       wp.load_config(cfgp).get("remote_token") == "tok-abcdefghijkl")

print()
print("=== 5. 提醒存储（消费确认 / id / 原子写）===")
with tempfile.TemporaryDirectory() as tmp:
    cfgp = Path(tmp) / "whale_pet_config.json"
    now = datetime.now()
    # 归一：文件只存到分钟，所以"同一时刻"必须是分钟对齐的（模型给的时间就是这样）
    soon = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    a1 = wp.append_alarm(cfgp, soon, "短提醒")
    ck("append_alarm 返回 id", bool(a1) and len(a1) == 12, a1)
    ck("同一分钟同一事不重复登记（带秒也认）",
       wp.append_alarm(cfgp, soon + timedelta(seconds=41), "短提醒") == a1)
    ck("归一后文件里的时间就是内存里的时间",
       wp.load_alarms(cfgp)[0]["when"] == soon)
    a2 = wp.append_alarm(cfgp, now + timedelta(days=2), "远提醒")
    ck("两条 id 不同", a1 != a2)
    due = wp.due_alarms(cfgp, now=soon + timedelta(minutes=1))
    ck("到期的一条被取出", [a["note"] for a in due] == ["短提醒"], due)
    ck("取出后文件里没了（不重复播报）",
       wp.due_alarms(cfgp, now=soon + timedelta(minutes=1)) == [])
    ck("远处那条还在", [a["note"] for a in wp.load_alarms(cfgp)] == ["远提醒"])
    ck("remove_alarm 真删", wp.remove_alarm(cfgp, a2) is True
       and wp.load_alarms(cfgp) == [])
    ck("remove_alarm 空 id 不报错", wp.remove_alarm(cfgp, "") is False)
    ck("秒级未来时间归一后已过期 → 明确拒绝登记",
       wp.append_alarm(cfgp, datetime.now() + timedelta(seconds=1), "来不及") is None)
    ck("没有 .tmp 残留", not (Path(tmp) / "whale_pet_alarms.jsonl.tmp").exists())

print()
print("=== 6. 日志/记忆（坏行保留 / 原子写）===")
with tempfile.TemporaryDirectory() as tmp:
    cfgp = Path(tmp) / "whale_pet_config.json"
    chat = Path(tmp) / "whale_pet_chat_log.jsonl"
    wp.append_chat_log(cfgp, "主人", "你好")
    wp.append_chat_log(cfgp, "鲸鲸", "在呢")
    # 手工塞一条坏行 + 一条超老的行
    with open(chat, "a", encoding="utf-8") as fh:
        fh.write("这不是 json\n")
        fh.write(json.dumps({"t": "2000-01-01 00:00:00", "who": "主人",
                             "text": "很久以前"}, ensure_ascii=False) + "\n")
    removed = wp.prune_chat_log(cfgp, keep_days=3)
    left = chat.read_text(encoding="utf-8")
    ck("过期的删掉一条", removed == 1, removed)
    ck("坏行原样留着（不再被顺手删）", "这不是 json" in left)
    ck("最近两条还在", left.count("在呢") == 1 and left.count("你好") == 1)
    ck("长期记忆能追加", wp.append_memory(cfgp, "主人是CS专业") is True
       and wp._read_jsonl(Path(tmp) / "whale_pet_memory.jsonl")[0]["note"] == "主人是CS专业")

print()
print("=== 7. SSE（finish_reason / 纯函数契约不变）===")
ck("catch [DONE] 仍返回空串",
   wp.sse_delta("data: [DONE]") == "")
ck("注释行返回 None", wp.sse_delta(": 心跳") is None)
ck("finish_reason 能读出来",
   wp.sse_finish_reason('data: {"choices":[{"delta":{},"finish_reason":"length"}]}') == "length")
ck("没有 finish_reason 时是 None",
   wp.sse_finish_reason('data: {"choices":[{"delta":{"content":"a"}}]}') is None)

print()
print("=== 8. 安全体检 / 打码 ===")
warns = wp.security_warnings({"remote_enabled": True, "remote_bind": "0.0.0.0",
                              "remote_allow_harness": True,
                              "feishu_enabled": True, "feishu_allow_harness": True,
                              "feishu_allow_sender": ""})
ck("四个风险点都说出来了", len(warns) == 4, warns)
ck("只听本机时不报警",
   wp.security_warnings({"remote_enabled": True, "remote_bind": "127.0.0.1"}) == [])
masked = wp._mask_url("http://192.168.1.5:8765/?t=t2VztSjFz8rRcHiZu-MC6g")
ck("URL 里的 token 打码", "t2VztSjFz8rRcHiZu-MC6g" not in masked, masked)

print()
print("=== 9. Bing 解析（属性顺序/引号宽容）===")
page = ('<ol><li class="b_algo" data-x="1"><h2><a href="#">标题甲</a></h2>'
        '<p class="b_lineclamp2">摘要甲</p></li>'
        "<li data-y='2' class='b_algo'><h2><a href='#'>标题乙</a></h2>"
        "<p>摘要乙</p></li></ol>")
res = wp.parse_bing_results(page)
ck("两种写法都解得出来", len(res) == 2, res)
ck("标题摘要正确", res[0][0] == "标题甲" and res[1][0] == "标题乙", res)

print()
print("=== 结果：%s ===" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
