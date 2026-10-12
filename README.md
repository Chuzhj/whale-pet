# 鲸鲸桌宠 · WhalePet

一只住在 Windows 桌面上的蓝色鲸鱼女仆。用 PySide6 写的桌宠：会待机、会眨眼、会饿、
会记账，接得上大模型 API 陪聊，也能通过飞书远程使唤她。

![鲸鲸](assets/01_front.png)

当前版本 **v1.0.1**（分享版 · 单形态）。不想装 Python？到
[Releases](../../releases) 页面下载打包好的 exe。

## 它是什么

一个纯 Python + Qt 的桌面宠物，没有游戏引擎、没有前端框架。窗口是透明无边框的置顶
窗口，角色靠逐帧换图做动画（待机呼吸、眨眼、睡觉、进食），呆毛和尾巴是拆出来单独摆动的
部件。除此之外还接了几条实用的通道：大模型聊天、余额记账、飞书收发消息、任务派发。

## 功能

**桌宠本体**

- 透明无边框置顶窗口，可拖动、可调大小（设置里一个滑块，默认 340 px 高）
- 待机呼吸、眨眼（5 帧）、睡觉、吃饭等多套动画；呆毛与尾巴独立摆动
- 16 张表情图：开心、害羞、生气、困、惊讶、爱、得意、委屈、想哭、无语……
- 台词气泡自动排版，不溢出屏幕；字号可调（9~28 px，默认 13）

**交互与界面**

- 右键菜单：和鲸鲸说句话 / 小鲸鱼记账 / 查看提醒 / 显示时钟 / 派活给 Harness /
  做个表情 / 去睡觉 / 回到待机 / 设置… / 隐藏
- 两模式窗口：**聊天模式** ↔ **工作模式**，一键切换
- 工作模式左栏列出任务会话，自动区分主任务与子任务（子任务默认折叠、右键展开）
- 设置窗口 5 页：聊天与钥匙 / 飞书接入 / 音效 / 桌宠 / 关于

**能力通道**

- **大模型聊天**：填自己的 API key 即可，内置 DeepSeek / 通义千问 / 智谱 / Moonshot /
  OpenAI / 本地 Ollama 六套预设，回答支持 Markdown 渲染（含表格）
- **小鲸鱼记账**：靠余额观测推算今日已用，自动判断峰谷时段
  （周一至周五 9–12、14–18 为峰，其余含节假日全谷）
- **提醒与时钟**：长期 / 短期提醒 + 桌面时钟
- **飞书接入**：手机发文字或图片给她，桌宠会存盘并回话；发 `/shot` 可以把当前屏幕截图发回手机
- **音效**：6 个 wav 由 `make_sounds.py` 用纯标准库现场合成，不依赖任何第三方音频素材
- **单实例保护**：同时只允许一只桌宠进程，第二次双击等于把已有那只叫到前台

## 快速开始

### 方式一：跑源码

```bash
pip install -r requirements.txt
python whale_pet.py
```

### 方式二：下载 exe

到 [Releases](../../releases) 下载 `WhalePet_share_*.zip`，解压后双击
`WhalePet_onedir\WhalePet.exe`。免安装，不需要 Python 环境。

> 压缩包里**只有可执行文件与 LICENSE**（约 71 MiB）。想要源码或自己改，直接 clone 本仓库。

### 自检与体检

改完代码建议先跑这两条：

```bash
python whale_pet.py --selftest              # 自检，退出码 0 表示通过
python whale_pet.py --selftest --selftest-log 自检.log
python whale_pet.py --doctor                # 体检：API / 飞书 / DSH 通道逐项真测
python whale_pet.py --version
```

## 重新打包 exe

```bash
pip install pyinstaller
python -m PyInstaller WhalePet_onedir.spec
```

产物在 `dist/WhalePet_onedir/`。（单文件版用 `WhalePet.spec`。）

## 目录结构

```
whale_pet.py           入口，python whale_pet.py 就能起
pet_constants.py       常量与默认配置（版本号 APP_VERSION 的唯一来源）
pet_shell.py           主窗口与整体装配
pet_mixin_*.py         UI / 聊天 / 动画 / 生命值 / 通道接入 五组 Mixin
pet_panels.py          各类面板（记账、提醒、时钟…）
pet_settings.py        设置窗口外壳（外壳 + 页面工厂）
pet_bridges.py         外部通道桥接
pet_feishu.py          飞书自实现协议（长连接收发）
pet_remote.py          远程控制 HTTP 服务
pet_doctor.py          体检（--doctor）
pet_selftest.py        自检（--selftest）
pet_store.py           配置与状态落盘
pet_ledger.py          记账
pet_sound.py           音效播放
dsh_*.py               DeepSeek Harness 接入
make_sounds.py         合成 6 个音效 wav
test_*.py              回归测试
assets/                立绘、表情、眨眼、睡姿、呆毛、尾巴、饭碗 + 音效
WhalePet*.spec         PyInstaller 打包配置（单文件 / onedir）
requirements.txt       依赖
```

## 关于密钥与隐私

**这个仓库里不含任何密钥。** API key、飞书 App ID / App Secret 只存在你自己机器上的
`whale_pet_config.json`（程序首次运行会生成），该文件以及运行状态、聊天记录、记忆、记账
本等个人数据全部已在 `.gitignore` 中排除。

## 环境要求

- **Windows 10 / 11** —— 大量使用 win32 API（置顶层级、托盘、窗口穿透），不跨平台
- **Python 3.10+**（开发环境为 3.13）
- `PySide6 >= 6.5`、`zstandard >= 0.21`、`websockets >= 11.0`

打包另需 `pyinstaller >= 6.0`、`pywin32 >= 306`。

## 已知限制

- 仅支持 Windows 10 / 11，没有 macOS / Linux 版本
- DSH（DeepSeek Harness）默认从「当前用户的标准安装位置」查找；装在别处时可用环境变量
  `DSH_APP_DIR` 指定，或在配置里填 `dsh_exe`
- 分享版是单形态版本（只有全身女仆装一种形态，无「切换形态」功能），且不含局域网远程控制

## License

MIT，见 [LICENSE](LICENSE)。
