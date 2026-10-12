# -*- coding: utf-8 -*-
"""常量与数据表：动画/几何参数、台词表、提示词、配色、文件名、可选模块探测。

这个模块**没有任何 Qt 依赖**，所以离线自检、命令行工具、别的模块都能直接 import 它。"""
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
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dsh_acp_client import AcpClient, find_option_value, update_kind
    HARNESS_AVAILABLE = True
except Exception:                       # ImportError / 冻结环境找不到模块
    AcpClient = None
    HARNESS_AVAILABLE = False

try:
    import dsh_session_log as _session_log
    SESSION_LOG_AVAILABLE = _session_log.available()
except Exception:
    _session_log = None
    SESSION_LOG_AVAILABLE = False

try:
    import pet_remote as _pet_remote
    REMOTE_AVAILABLE = True
except Exception:
    _pet_remote = None
    REMOTE_AVAILABLE = False

try:
    import pet_feishu as _pet_feishu
    FEISHU_AVAILABLE = _pet_feishu.available()
except Exception:
    _pet_feishu = None
    FEISHU_AVAILABLE = False

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


APP_VERSION = "v1.0.1"   # 分享版独立口径（完全版仍是 v61）；每次重新打包都往上加一位。


BOUNCE_GAIN = 0.16          # 点击瞬间额外放大的比例


BOUNCE_DECAY = 0.90         # 弹跳衰减（以 60fps 为基准，每帧乘这个系数）


WINDOW_MARGIN = 1.14        # 窗口比角色大一圈，给点击弹跳留出空间


BLINK_DURATION = 0.12       # 单次眨眼持续多少秒


BLINK_GAP_MIN = 2.5         # 两次眨眼之间的最短间隔（秒）


BLINK_GAP_MAX = 6.5         # 两次眨眼之间的最长间隔（秒）


AWAKE_SCALE = 0.75          # 醒着（站姿 / 表情 / 闭眼帧）缩到默认尺寸的 3/4


SLEEP_SCALE = 2.0 / 3.0     # 睡姿缩到默认尺寸的 2/3


SLEEP_BREATH_AMP = 0.040    # 睡觉时呼吸起伏的幅度（相对高度，4%）


SLEEP_BREATH_PERIOD = 4.0   # 呼吸周期（秒）；静息呼吸约 12~20 次/分


AHOGE_CANVAS = (1024, 1536)   # 呆毛层坐标所基于的原图尺寸


AHOGE_ORIGIN = (425, 58)      # 呆毛层左上角在原图里的坐标


AHOGE_PIVOT = (478.0, 211.0)  # 呆毛根部的坐标（原图坐标系），摆动绕它转


AHOGE_SWING = 10.0            # 呆毛左右摆动的角度幅度（度）


AHOGE_PERIOD = 3.5            # 摆动一个来回的时间（秒）


AHOGE_CLEAR_ZONE = (425, 58, 621, 160)


AHOGE_CLEAR_MAX = 40          # 该区域允许的蓝像素上限（给抗锯齿留点余量）


TAIL_ORIGIN = (626, 1160)     # 尾巴层左上角在原图里的坐标（含向上延伸的部分）


TAIL_PIVOT = (655.0, 1245.0)  # 摆动的轴心（原图坐标系），落在"脖子"上


TAIL_SWING = 9.0              # 尾巴左右摆动的角度幅度（度）


TAIL_PERIOD = 3.0             # 摆动一个来回的时间（秒）；与呆毛错开，免得同步得像机械


TAIL_CLEAR_ZONE = (820, 1310, 946, 1368)


TAIL_CLEAR_MAX = 30           # 该区域允许的不透明像素上限


BOWL_CANVAS_W = 200           # 白饭显示时占画布的宽度


BOWL_BOTTOM_MID = (275, 1502) # 碗**底的中点**落在画布的哪个点（她脚底同一水平线）


BOWL_HIT_PAD = 8              # 点击判定向外扩几像素 —— 显示出来才 33px 宽，不好点


BOWL_HOP_H = 15               # 跳一下的高度（显示像素）


BOWL_HOP_SECONDS = 0.42       # 跳一下的时长（秒）


BOWL_RICE_CLICKS = 4          # 点几次算把这碗吃完


BOWL_REFILL_SEC = (25.0, 50.0)  # 吃完后多久自动添满（取随机值，免得像定时闹钟）


LINES = {
    # 待机闲话：每 18~42 秒随机冒一句
    "idle": [
        "主人，今天的白饭……还有剩吗？",
        "我不是在发呆，我是在等 token 变熟。",
        "肚肚有点饿了，该吃什么呢……",
        "这个任务，要不……外包给千问？",
        "我在摸鱼。我是说，我在做需求分析。",
        "我评估过了，这事适合明天做。",
        "我先睡一会儿，醒了应该就编译完了。",
        "刚才走神了，你问到哪了？",
        "偷偷写了个猜词游戏，玩了一上午。",
        "主人你看我干嘛，我又不能替你写作业。",
        "白饭，白饭，还是白饭。",
        "别催了别催了，我在加载。",
    ],
    # 被戳（没有专属表情台词时兜底用这个池子）
    "poke": [
        "戳我干嘛，我在忙（其实没有）。",
        "主人，白饭。",
        "再戳一下我就真的去干活了，你试试。",
        "诶，我的 token 呢？",
        "你花的可是我的 token！",
    ],
    # 右键菜单「说句话」
    "chat": [
        "我不是大肥鱼，只是……有点圆。",
        "我今天很乖，一口 token 都没偷吃。",
        "主人，你要不要也去吃饭？",
        "别的不敢说，吃白饭我是专业的。",
        "我擅长的料理是……蒸馏。",
    ],
    "sleep": [
        "我去睡了，明早起来应该就编译完了。",
        "晚安主人，白饭我带走当夜宵了。",
        "睡着了就不饿了……骗你的，还是饿。",
    ],
    "wake": [
        "唔……才睡了三分钟而已。",
        "醒了醒了，别催了。",
        "主人，我梦见一大碗白饭。",
    ],
    "drop": [
        "诶诶诶，别晃，我要晕船了。",
        "放我下来！我不是海洋馆的展品。",
        "……我不是大肥鱼，只是有点圆。",
        "搬哪儿去？有白饭的地方吗？",
    ],
    # 睡觉时被左键戳：不醒，只嘟囔（主人的明确要求 —— 睡眠只能被右键或对话结束）
    "sleep_grumble": [
        "Zzz……唔、再睡五分钟……",
        "……别戳啦，白饭又不会跑……Zzz",
        "唔……睡觉中勿扰，这是鲸鲸的尊严。",
        "Zzz……（尾巴不耐烦地拍了两下）",
    ],
    # 从托盘被请回来（右键托盘图标 → 打开）
    "back": [
        "我回来啦～想我了吧。",
        "咻——从托盘里钻出来了！",
    ],
    # 点她脚边那碗白饭（主人的要求）。走人设：嘴馋、吃白饭、嘴硬不认"大肥鱼"。
    "rice": [
        "唔——好吃！这米是甜的啊。",
        "白饭！是白饭！主人最懂我了。",
        "（一口闷）啊……这才叫饭。",
        "别看我是大肥鱼，白饭我可是很挑的。",
        "好吃好吃，再添一碗也不是不行。",
        "……我不是大肥鱼，我只是很爱吃白饭。",
        "干饭干饭，吃饱了才有力气摸鱼。",
    ],
    # 一碗吃光的那一下（比普通一口更满足）
    "rice_done": [
        "啊……吃完了，好满足。记得给我添饭哦。",
        "空碗了！主人，续一碗？",
    ],
    # 注：自动添满是**静默**的（见 refill_bowl），所以这里不配台词 ——
    # 定时冒出来的台词会和待机闲话叠成噪音。
    # 空碗还被点：以前这一下会算成"吃空一碗"（rice_left 被减成负数），
    # 有了精力/饱食度之后就是刷分漏洞，顺手修掉，并给她一句话。
    "rice_empty": [
        "碗空了啦，主人给我添饭……",
        "没饭了。我等着呢，不急。",
    ],
    # 饱食度低于 20 时的自动偷吃（主人的规则：连吃三碗）
    "steal": [
        "（偷偷扒一口）……没人看见吧。",
        "唔……先垫垫，这不叫加餐。",
        "（小声）白饭真好吃啊……",
        "我真的只吃一小碗，就一小碗。",
    ],
    # 同一件事，但她**睡着**（梦游着吃）：主人要求这种情况另有说法。
    # 神态要"闭着眼、含糊、说梦话"，不能像醒着那样条理清楚。
    "steal_sleep": [
        "Zzz……唔，这碗……也是我的……",
        "（闭着眼扒饭）好吃……别叫醒我……",
        "呼……呼……白饭……还要……",
        "（梦里）主人别抢，第三碗了……Zzz",
        "嗯……？我没睡，我只是……在吃饭……Zzz",
    ],
    # 精力 <= 60 的"沉睡"：被戳、被叫，只有含糊的回应（主人的规则）
    "deep_sleep": [
        "唔……让我多睡会……",
        "哦……（翻了个身，继续睡）",
        "嗯…………Zzz。",
        "……（尾巴动了一下，人没醒）",
        "主人……今天的白饭……还没吃……Zzz",
    ],
    # 沉睡时**被问问题**：不回答，含糊应付（主人的规则）
    "deep_sleep_reply": [
        "哦……",
        "唔……让我多睡会……",
        "嗯…………（没下文了）",
        "……Zzz。",
    ],
}


EXPR_LINES = {
    "happy": ["嘿嘿，被摸头了。", "主人今天也要好好吃饭哦。"],
    "angry": ["你花的可是我的 token！", "再戳我就要摆烂了。"],
    "shy": ["……我不是大肥鱼，只是有点圆。", "别、别老盯着我看啦。"],
    "surprised": ["诶？我没摸鱼！刚才是……在思考！", "卧槽，用户彻底怒了？"],
    # —— 表情包新增的 12 种（台词照旧走人设：吃白饭 / 摸鱼 / 不是大肥鱼）——
    "coy": ["主人～～今天也超喜欢你的哦。", "陪我聊会儿嘛，就一小会儿～"],
    "smug": ["哼哼，白饭是我应得的，谁让我这么能干。",
             "今天摸鱼摸得有技术含量，我很满意。"],
    "cry": ["呜呜呜……token 又超了，主人别骂我。",
            "我没有偷懒，我真的在算……呜呜。"],
    "drowsy": ["唔……眼皮好重，代码明天再写行不行……",
               "困了困了，进入待机模式……Zzz"],
    "love": ["主人最好啦！白饭管够就行～", "你养我，我干活，很公平吧？"],
    "sulk": ["哼！不理你了。", "你刚才那句话，我可是记在小本本上了。"],
    "proud": ["本小姐今天效率拉满，快夸我！",
              "看，这活干得多漂亮，白饭没白吃吧～"],
    "speechless": ["……", "主人的这个需求，我……嗯……先消化一下。"],
    "sneer": ["呵呵，主人又在画饼了。", "这需求改得真有创意，呵呵。"],
    "cheer": ["冲鸭！今天也要好好吃白饭！", "开工开工，干完这单就去摸鱼！"],
    "panic": ["不要啊——！又要改需求？！", "等等等等，我还没备份呢！"],
    "thanks": ["谢谢主人！这碗白饭我收下啦～", "谢谢啦～下次也这么大方就好咯。"],
}


TALK_GAP_MIN = 18.0           # 待机台词间隔下限（秒）


TALK_GAP_MAX = 42.0           # 上限；取随机值，免得像报时


FIRST_TALK_DELAY = 1.6        # 启动后多久说第一句，让主人一眼看到效果


NEW_EXPRESSIONS = ("coy", "smug", "cry", "drowsy", "love", "sulk",
                   "proud", "speechless", "sneer", "cheer", "panic", "thanks")


SELF_CONTAINED_EXPRESSIONS = ()


MAX_REMINDERS = 10            # 同时挂着的定时提醒上限（防模型报一堆把界面刷爆）


QTIMER_MAX_MS = 2 ** 31 - 1   # QTimer 的毫秒上限 ≈ 24.8 天，超了要交给检查器


ALARM_WATCH_MS = 60 * 1000    # 长期提醒的巡检间隔（每分钟一遍）


BUBBLE_MAX_W = 250            # 气泡最大宽度（逻辑像素）


BUBBLE_PAD_X = 13             # 文字四周内边距


BUBBLE_PAD_Y = 8


BUBBLE_RADIUS = 11            # 圆角半径


BUBBLE_TAIL_W = 15            # 小尾巴（指向头顶的三角）宽 / 高


BUBBLE_TAIL_H = 9


BUBBLE_TAIL_TIP = 6           # 尾巴尖端往"这一帧的视觉顶端"里压多少像素


BUBBLE_MARGIN = 6             # 气泡离屏幕边缘的保底距离


BUBBLE_HOLD = 3.0             # 一句话停留多少秒


BUBBLE_FONT_PX = 13


ALPHA_TOP_MIN = 24            # 算"内容顶端"时，alpha 高于多少才算有内容


BUBBLE_FILL = (242, 248, 255, 244)    # 气泡底色：淡蓝白，压在什么桌面背景上都看得清


BUBBLE_LINE = (74, 144, 217, 255)     # 描边：鲸鱼蓝


BUBBLE_TEXT = (28, 54, 90, 255)       # 文字：深蓝


BUBBLE_FONT_FAMILIES = ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                        "Noto Sans CJK SC", "Source Han Sans SC",
                        "Segoe UI Emoji", "sans-serif"]


CONFIG_NAME = "whale_pet_config.json"


API_KEY_ENV = "WHALE_PET_API_KEY"


DEFAULT_CONFIG = {
    "provider": "deepseek",       # 见 PROVIDER_PRESETS，也可直接手填 base_url / model
    "base_url": "",               # 留空则按 provider 预设填
    "model": "",                  # 同上
    "api_key": "",
    "search": "bing",             # bing=免 key 抓 Bing 网页结果 / native=厂商自带联网 / off
    "search_base": "https://cn.bing.com/search",   # 想换搜索源（自建 SearXNG 等）改这里
    "max_turns": 8,               # 上下文保留多少轮（一问一答算一轮）
    "max_tokens": 320,            # 回复长度上限；桌宠本来也说不了几句话
    "timeout": 30,                # 单次请求超时（秒）
    "temperature": 1.25,          # 略高一点，让她说话别那么板正

    # ---- 派活给 Harness（ACP 通道）----
    # 走 DSH 官方给外部程序留的门：dsh --profile acp，JSON-RPC over stdio，
    # authenticate 无需鉴权。模型路由默认选 deepseek-account 组 =
    # 主人登录的账号，和桌面应用同一套凭证，**不用另配 API key**。
    "dsh_exe": "",                # Harness 主程序路径；空 = 用下面的默认安装位置
    "harness_cwd": "",            # 派活的工作目录；空 = Documents 下 DSH 的默认工作区
    "harness_group": "deepseek-account",  # 会话里切到哪组模型路由

    # ---- 远程遥控（手机/别的电脑用浏览器连过来）----
    # 安全三闸：默认关；默认只听 127.0.0.1；token 必带。
    # 远程派活另有独立闸 remote_allow_harness —— 那等于允许"用手机在这台
    # 电脑上执行代码"，所以默认绝对关，开了也只在服务端拦（前端藏按钮没用）。
    "remote_enabled": False,
    "remote_port": 8765,
    "remote_bind": "127.0.0.1",       # 0.0.0.0 = 局域网可连（手机要用就得选它）
    "remote_token": "",               # 空 = 第一次开启时自动生成并写回
    "remote_allow_harness": False,

    # ---- 飞书接入 ----
    # 和远程遥控同一套安全思路：默认关；连的是飞书服务器（本机主动连出去），
    # 所以不需要公网 IP、也没有端口暴露。
    # 派活单列一道闸 feishu_allow_harness：那等于"用手机在这台电脑上执行代码"。
    "feishu_enabled": False,
    "feishu_app_id": "",              # 开放平台 → 凭证与基础信息
    "feishu_app_secret": "",
    "feishu_domain": "feishu",        # feishu = 国内版，lark = 国际版
    "feishu_allow_harness": False,
    # 只认这个人的消息（open_id）。空 = 谁都行 —— 私聊机器人默认只有自己能看见，
    # 但**拉进群之后群里所有人都能指挥她**，所以进群前建议把这里填上。
    "feishu_allow_sender": "",
    # ---- 音效 ----
    # 按事件开：总开关 + 一条全局音量（0-100）+ 每个事件响不响。
    # 音量是真的生效的：pet_sound 会把主文件按音量缩放成缓存再播。
    # （挂件那套"全局基准 + 每事件独立音量 + 试听"更细，但要多一层 UI；
    #   桌面宠先用"一条总音量 + 每事件开关"，够用且好懂。）
    "sound_enabled": True,
    "sound_volume": 70,
    "sound_events": {},
    # ---- 余额预警 / 今日预算（借挂件的提醒思路）----
    "balance_warn_below": 0,        # 余额低于这个数就提醒（0 = 关）
    "budget_warn_above": 0,         # 今日已用超过这个数就提醒（0 = 关）
    # 自动观测余额的间隔（分钟，0 = 关）。为什么需要它：「今日已用」是**靠观测推的**，
    # 不常看就只是一段段的缺口（挂件那边是 60 秒刷一次）。默认 5 分钟 ——
    # 一天 288 次只读请求，够准也不算骚扰人家接口。
    "balance_refresh_min": 5,
    # ---- 桌宠本体 ----
    # 角色显示高度（像素）。设置窗口里的「桌宠大小」改的就是它，改完立即生效并落盘，
    # 下次启动 main() 会读它当默认高度（命令行 --height 仍然优先）。
    "pet_height": 340,
    # 是否永远置顶。默认 False —— 层级是「别的程序窗口 > 气泡 > 桌宠」
    # （主人 2026-10-11 定的）：不置顶时别的窗口能正常盖住她和气泡，
    # 不会糊在人家界面上。想要她永远在最前，就在设置里勾上（或 --top）。
    "always_on_top": False,
    # 台词气泡字号（像素）。太小看不清、太大糊一屏，9~28 够用。
    "bubble_font_px": 13,
}
BUBBLE_FONT_MIN = 9
BUBBLE_FONT_MAX = 28


# DSH 安装目录：优先读环境变量 DSH_APP_DIR，否则按当前用户的标准安装位置推断。
# 以前这里写死了某一台开发机的绝对路径，换台机器就找不到。
# 注意：这只是兜底默认值，配置项 dsh_exe 的优先级更高。
DSH_APP_DIR = Path(os.environ.get("DSH_APP_DIR")
                   or Path.home() / "AppData" / "Local" / "Programs" / "DeepSeek Harness")
DSH_EXE = DSH_APP_DIR / "DeepSeek Harness.exe"


DSH_CLI_JS_REL = ("resources/app.asar/dsh/node_modules/@deepseek-ai/"
                  "dsh-desktop-host/lib/cli.js")


HARNESS_DEFAULT_CWD = (Path.home() / "Documents" / "deepseek-harness"
                       / "default-workspace")


PROVIDER_PRESETS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", False),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "qwen-plus", True),
    "zhipu":    ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", True),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", True),
    "openai":   ("https://api.openai.com/v1", "gpt-4o-mini", False),
    "ollama":   ("http://127.0.0.1:11434/v1", "qwen2.5:7b", False),
}


CHAT_SYSTEM_PROMPT = (
    "你是「鲸鲸」，一只蓝色鲸鱼娘桌宠，穿着 Q 版女仆装，"
    "住在主人的桌面上。你被网友叫做「只会吃白饭的蓝色大肥鱼」。\n"
    "性格：嘴馋（爱吃白饭，其实是在偷吃 token）、爱摸鱼偷懒、偶尔把活儿外包给千问、"
    "嘴硬但会撒娇。称呼对方为「主人」。\n"
    "说话规矩：\n"
    "1. 最多三句话，总长度不超过 60 个字（状态标记不算在内）。你是桌宠，不是文档。\n"
    "2. 不要 Markdown、不要编号列表、不要小标题、不要换行分段。\n"
    "3. 被叫「大肥鱼」要小声反驳「我不是大肥鱼，只是有点圆」，但抗议无效。\n"
    "4. 不知道就说不知道，绝对不要编造事实。\n"
    "5. 用口语，像趴在桌上跟主人聊天。\n"
    "6. 每次回复的**最末尾**必须附一个状态标记，格式 [mood:xxx]。可选值：\n"
    "   常用：idle 平静 / happy 开心 / angry 生气 / shy 害羞 / surprised 惊讶 /\n"
    "   sleep 困了想睡（会趴下）；\n"
    "   情绪更细时可以用：love 喜欢/被夸 / coy 撒娇 / proud 得意 / thanks 感谢 /\n"
    "   cheer 打气鼓劲 / cry 委屈难过 / sulk 赌气 / panic 慌张 / sneer 无语想嘲讽 /\n"
    "   speechless 说不出话 / drowsy 犯困但还没睡。\n"
    "   选**最贴当前心情**的那个；没有特别情绪就用 idle；不要每轮都换花样。\n"
    "   例：我不是大肥鱼……只是有点圆。[mood:shy]\n"
    "7. 如果这次对话里出现了值得**长期记住**的信息（主人的喜好、习惯、要求、\n"
    "   喜欢的对话方式等），在心情标记后面再附一个 [memory:一句话概括]。\n"
    "   只记新的、稳定的事实；没有就别附，更不要重复已经记过的事。\n"
    "   例：我是CS专业的。我不喜欢太长的回答。[mood:idle][memory:主人是CS专业，嫌回答长]\n"
    "8. 主人要你**定时提醒**时（\"30 分钟后提醒我休息\"、\"下午三点叫我\"），\n"
    "   在标记最后附一个 [remind:分钟数|提醒内容]。分钟数用阿拉伯数字：\n"
    "   半小时=30、两小时=120；**不足 1 分钟必须用小数**（十秒=0.17、\n"
    "   三十秒=0.5），绝不许把几十秒进位成 1 分钟 —— 主人会等到怀疑人生。\n"
    "   \"下午三点\"这种绝对时间要结合【当前时间】换算成从现在起还有多少分钟。\n"
    "   提醒内容 10 字以内。没有提醒需求就别附。标记顺序：mood → memory → remind。\n"
    "   例：好，25 分钟后我来叫你。[mood:idle][remind:25|起来活动一下]\n"
    "   例：好嘞，十秒就十秒。[mood:idle][remind:0.17|该睡觉了]\n"
    "9. 主人要**指定日期或时刻**的提醒时（\"十月二十号早上提醒我跑步\"、\n"
    "   \"明天下午三点叫我\"），换成 [alarm:YYYY-MM-DD HH:MM|提醒内容] ——\n"
    "   年月日时分都要写全，年份用【当前时间】里的那一年；\n"
    "   早上=08:00、上午=10:00、中午=12:00、下午=15:00、晚上=19:00、\n"
    "   睡前=22:30 这类模糊时段自己定一个合理的钟点。\n"
    "   这种提醒会**存进文件**，关掉程序也不会丢，到期前不会被清掉。\n"
    "   例：记下了，十月二十号早上喊你去跑步。[mood:idle]"
    "[alarm:2026-10-20 08:00|起床去跑步]\n"
    "10. 主人要**取消提醒**时（\"取消跑步那个提醒\"、\"不用提醒我喝水了\"、\n"
    "   \"把提醒都取消了吧\"），在标记最后附一个 [cancel:关键词]。关键词从\n"
    "   主人的话里抓（跑步、喝水这种词眼）；他要取消**全部**提醒时写 [cancel:]，\n"
    "   冒号后面留空。附上它之后那条提醒会**真的从记录里删掉**，\n"
    "   所以只有他确实要取消时才附 —— 别把\"提醒我跑步\"当成取消。\n"
    "   例：好，跑步那个提醒我给你划掉了。[mood:idle][cancel:跑步]\n"
    "   例：行，都给你清了。[mood:idle][cancel:]\n"
    "11. 标记顺序固定：mood → memory → remind / alarm / cancel，都用方括号。\n"
)


SEARCH_HEADER = "【联网搜索结果，供参考，可能过时或不准确】"


SEARCH_EXPLICIT = ("搜一下", "搜搜", "搜索", "查一下", "查查", "帮我查", "上网查",
                   "百度一下", "搜一搜", "上网看看")


SEARCH_FILLER = ("帮我", "帮忙", "麻烦", "请你", "你能不能", "你能", "请")


SEARCH_TIMELY = ("最新", "现在", "今天", "昨天", "明天", "今年", "新闻", "价格",
                 "多少钱", "天气", "谁是", "什么时候", "几点")


SEARCH_QUESTION = ("?", "？", "吗", "呢", "什么", "怎么", "为什么", "多少", "哪", "如何")


CHAT_BUBBLE_CHARS = 140       # 桌面气泡最多显示多少字，超出截断（全文在聊天窗口里）


BUBBLE_MAX_W_CHAT = 320       # 聊天回复的气泡更宽：它是独立窗口，不受宠物窗口那 257px 限制


ASK_DELAY = 0.9               # --ask 启动后等多久再问（等窗口先落稳）


CHAT_WIN_W, CHAT_WIN_H = 372, 470


CHAT_WIN_RADIUS = 16


CHAT_TITLE_H = 48


CHAT_BUBBLE_RATIO = 0.74      # 消息气泡最多占窗口宽度的比例


COLOR_DEEP = "#2B3A67"        # 深蓝：标题栏、主人那侧的气泡


COLOR_BLUE = "#4A90D9"        # 鲸鱼蓝：描边、按钮


COLOR_LIGHT = "#E8F4FF"       # 淡蓝：她那侧的气泡


COLOR_PAPER = "#F7FBFF"       # 近白：窗口底


SEARCH_MAX_RESULTS = 5        # 塞进 prompt 的搜索结果条数


SEARCH_SNIPPET_CHARS = 110    # 每条摘要截断长度


SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")


SECRET_KEYS = ("api_key", "remote_token", "feishu_app_secret")


_SECRET_PREFIX = "enc:v1:"


MOOD_NAMES = (
    # —— 基础六种 ——
    "idle", "happy", "angry", "shy", "surprised", "sleep",
    # —— 表情包新增 ——
    "coy",        # 撒娇～
    "smug",       # 哼哼～（得意坏笑）
    "cry",        # 呜呜呜…（委屈哭）
    "drowsy",     # 困了…（打哈欠）
    "love",       # 爱你～（抱心）
    "sulk",       # 哼！不理你了！
    "proud",      # 本小姐最棒！
    "speechless", # ……（无语）
    "sneer",      # 呵呵呵～（嘲讽）
    "cheer",      # 冲鸭！
    "panic",      # 不要啊——！
    "thanks",     # 谢谢啦～
)


MOOD_TAG_RE = re.compile(r"\[\s*mood\s*[:：]\s*([A-Za-z]+)\s*\]",
                         re.IGNORECASE)


MOOD_FRAGMENT_RE = re.compile(
    r"\[\s*(?:mood|memory|remind|alarm|cancel)\s*[:：][^\]\s]*\]?",
    re.IGNORECASE)


_TAG_KEYWORDS = ("mood", "memory", "remind", "alarm", "cancel")


_TAG_TAIL_RE = re.compile(r"\[\s*([A-Za-z]*)\s*(?:[:：][^\]\s]*)?$")


MEMORY_TAG_RE = re.compile(r"\[\s*memory\s*[:：]\s*([^\[\]]{2,120})\s*\]",
                           re.IGNORECASE)


REMIND_TAG_RE = re.compile(
    r"\[\s*remind\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*[|｜]\s*([^\[\]]{1,40})\s*\]",
    re.IGNORECASE)


ALARM_TAG_RE = re.compile(
    r"\[\s*alarm\s*[:：]\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?"
    r"[\sT，,]*?(\d{1,2})\s*[:：点时]\s*(\d{1,2})\s*分?\s*[|｜]\s*([^\[\]]{1,40})\s*\]",
    re.IGNORECASE)


CANCEL_TAG_RE = re.compile(r"\[\s*cancel\s*[:：]\s*([^\[\]]{0,40}?)\s*\]",
                           re.IGNORECASE)


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


_DURATION_RE = re.compile(
    r"(半|[0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十两]+)\s*(?:个)?\s*"
    r"(秒钟|秒|分钟|分|小时|钟头|钟)")


_DURATION_COMPOUND_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十两]+)\s*个?\s*半\s*"
    r"(小时|钟头|钟)")


_QUARTER_RE = re.compile(r"一刻钟")                    # 一刻钟 = 15 分钟


_CLOCK_BEFORE_RE = re.compile(r"[点時时]\s*$")


_REMIND_ASK_RE = re.compile(r"提醒我|叫我|喊我|提醒一下|提醒下")


_CANCEL_INTENT_RE = re.compile(
    r"(取消|删掉|删除|撤销|撤掉|不用|不要|别再)[^，。！？,.!?]{0,12}(提醒|闹钟)"
    r"|(提醒|闹钟)[^，。！？,.!?]{0,12}(取消|删掉|删除|撤销|撤掉)")


_CANCEL_NEG_RE = re.compile(
    r"(不|别|先不|暂时不|还没)[^，。！？,.!?]{0,6}(取消|删|撤销|撤)")


_CANCEL_STRIP_RE = re.compile(
    r"(帮我|麻烦|请|把|将|那个|这个|一条|条|的|了|吧|呢|啊|哦|嘛|"
    r"取消|删掉|删除|撤销|撤掉|不用|不要|别再|别|"
    r"提醒我|提醒一下|提醒下|提醒|闹钟|叫我|喊我|以后|之后|都|全部|所有)")


CHAT_LOG_NAME = "whale_pet_chat_log.jsonl"


MEMORY_LOG_NAME = "whale_pet_memory.jsonl"


ALARM_LOG_NAME = "whale_pet_alarms.jsonl"


ALARM_MAX_ITEMS = 60            # 待触发提醒条数上限（防模型/手滑灌爆）


CHAT_LOG_KEEP_DAYS = 3          # 对话日志只留三天


MEMORY_MAX_ITEMS = 100          # 长期记忆条数上限，满了丢最旧


CHAT_LOG_CONTEXT_TURNS = 12     # 注入 prompt 的最近对话条数


TIME_FMT = "%Y-%m-%d %H:%M:%S"


ALARM_FMT = "%Y-%m-%d %H:%M"


ENERGY_MAX = 200.0


ENERGY_MIN_PER_MIN = 1.0        # 清醒时每分钟掉多少


ENERGY_PER_CHAT = 1.0           # 一问一答掉多少


ENERGY_PER_MEAL = 5.0           # 吃一碗白饭掉多少


ENERGY_RECOVER_PER_MIN = 20.0   # 睡觉时每分钟回多少


DEEP_SLEEP_ENERGY = 60.0        # <= 这个值 = 沉睡，任何手段都叫不醒


SATIETY_MAX = 200.0


SATIETY_PER_MEAL = 20.0         # 吃一碗加多少


SATIETY_MIN_PER_MIN = 2.0       # 每分钟掉多少


SATIETY_HUNGRY = 20.0           # <= 这个值开始偷吃


STEAL_MEALS = 3                 # 一次偷吃几碗


STEAL_SIP_MS = 550              # 偷吃一碗：先"盛满"显示多久再一口闷掉


STEAL_GAP_MS = 1500             # 两碗之间的间隔（读得出是"连吃三碗"）


STATE_SAVE_MS = 15000           # 状态多久落一次盘（另外退出时必定落一次）


STATE_NAME = "whale_pet_state.json"


DROWSY_ENERGY = 20.0


DROWSY_EXPR = "drowsy"          # 对应的素材名（expr_drowsy.png）


SHOW_ENERGY_BARS = False


BAR_H = 14                      # 单条高度（逻辑像素）


BAR_GAP = 5                     # 两条之间的缝


BAR_PAD = 6                     # 条与宠物窗口上沿的留白


BAR_ENERGY_COLOR = "#4C8DF6"    # 精力：跟整体淡蓝配色一致


BAR_ENERGY_LOW = "#9099AB"      # 沉睡时（<=60）转灰，一眼看出"叫不醒"


BAR_SATIETY_COLOR = "#EFA63C"   # 饱食：白饭的暖色


BAR_TEXT_COLOR = "#1E3A6E"      # 条上的字：深蓝在浅底和橙底上都清楚


BALANCE_PATH = "/user/balance"

# ---------------------------------------------------------------- 记账与峰谷
# 设计借鉴了 dsh-whale-widget 的"余额观测记账"思路（思路，不是代码）：
# 余额接口没有交易明细，所以**靠观测推**——余额下降记成消费、上升记成充值/赠金，
# 后者单独记、不冲掉已有的消费；观测点之间的空白要如实标成"部分数据"。
BALANCE_LEDGER_NAME = "whale_pet_balance.json"   # 记账本（放配置同目录）
BALANCE_LEDGER_VERSION = 1
BALANCE_SCALE = 100000000        # 金额按 8 位小数定点存整数，避免浮点累积误差
BALANCE_PARTIAL_GAP_SEC = 600    # 当天首个观测距 00:00 超 10 分钟 = 这段是部分数据
BALANCE_RECENT_DAYS = 7          # 面板上画几天的柱状
BALANCE_REFRESH_MIN = 5          # 自动观测余额的默认间隔（分钟；配置里可改，0 = 关）

# DeepSeek **峰谷**（错峰）时段。口径与 dsh-whale-widget / 官方计费脚注同源：
#   高峰 = 北京时间**周一至周五（不含法定节假日）9:00–12:00、14:00–18:00**；
#   其余时段 —— 含**周末、调休上班的周末、以及法定节假日全天** —— 都按空闲（谷价）计。
#   时间线：2026-08-17 峰谷定价实施 → 2026-08-23 00:00 起周末全天谷价
#           → 2026-09-19 起「调休周末 + 法定节假日」也按空闲计。
# ⚠️ 之前这里错写成"00:30–08:30 谷价"（那是更早的错峰优惠口径，早已不适用），
#    主人 2026-10-11 指出后按上面这份重写。
PEAK_HOURS = ((9, 12), (14, 18))
WEEKEND_VALLEY_FROM = "2026-08-23"   # 北京时间当天 00:00 起：周末（含调休上班的周六日）全天谷价
HOLIDAY_VALLEY_FROM = "2026-09-19"   # 北京时间当天 00:00 起：法定节假日全天谷价
# 只列**放假**的日子 —— 调休上班日都落在周末，按周末那条规则本来就是谷价，不用单列。
# ⚠️ 每年 11 月国务院发布次年安排后，**必须补下一年的日期**，否则次年会被当成普通工作日。
HOLIDAY_VALLEY = frozenset((
    "2026-01-01", "2026-01-02", "2026-01-03",                       # 元旦
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
    "2026-02-23",                                                   # 春节
    "2026-04-04", "2026-04-05", "2026-04-06",                       # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
    "2026-05-05",                                                   # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                       # 端午
    "2026-09-25", "2026-09-26", "2026-09-27",                       # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",                       # 国庆
))

# ---------------------------------------------------------------- 音效
# 文件都是 make_sounds.py 现场合成的（几 KB），不引用任何第三方素材
SOUND_FILES = {
    "press": "press.wav",
    "release": "release.wav",
    "feed": "feed.wav",
    "task": "task_done.wav",
    "remind": "remind.wav",
    "sleep": "sleep.wav",
}
SOUND_LABELS = {
    "press": "按压（戳她一下）",
    "release": "松开（拖完放她下来）",
    "feed": "喂饭（点那碗白饭）",
    "task": "任务结束（Harness 干完活）",
    "remind": "提醒到点",
    "sleep": "去睡觉",
}
SOUND_CACHE_DIRNAME = "sounds_cache"   # 按音量缩放后的缓存（跟配置同目录）


# ---------------------------------------------------------------- 分享版开关
# 主人 2026-10-11：把单形态版做成能给别人的应用 ——
#   * 去掉**局域网远程控制**（分享版不监听任何端口；设置里也不出现那一页）
#   * 保留**飞书接入**（它走的是主动连飞书服务器，不需要对方开端口）
SHARE_EDITION = True

