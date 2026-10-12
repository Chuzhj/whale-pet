# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import get_package_paths

# Qt 的插件目录必须**显式**打进去 —— 这是实测踩出来的坑：
# PyInstaller 的 PySide6 hook 这次一个插件都没收集到（打包清单里 qwindows /
# qoffscreen 全是 0），后果是：
#   * platforms\ 缺失 → 打出来的 exe 根本开不了窗口；
#     而 --version / --help 照样正常退出（它们在建 QApplication 之前就返回了），
#     所以这个故障极具欺骗性，必须专门查插件清单才发现。
#   * tls\ 缺失 → QtNetwork 走 HTTPS 调模型接口可能直接失败。
# 这里自己扫一遍：源是 PySide6 包下的 plugins\，目标是包内的
# PySide6\plugins\<子目录>（PyInstaller 的运行时 hook 就按这个位置找插件）。
# 用 get_package_paths 而不是写死路径，换机器/换虚拟环境照样能打。
#
# **只挑用得上的子目录**：全量收集过一次，包从 58MB 涨到 182MB ——
# 因为 sceneparsers / assetimporters / sqldrivers 那些插件会顺着依赖把
# Qt6Quick3D、Qt6Qml、QtSql 一大堆拖进来，而这些本程序一个都不用。
_QT_PLUGIN_DIRS = (
    "platforms",          # qwindows（开窗口）/ qoffscreen（离屏自检）—— 缺了程序起不来
    "tls",                # QtNetwork 走 HTTPS 调模型接口要它
    "imageformats",       # jpeg/gif/webp 之类的解码器（PNG 是 QtGui 内置的）
    "iconengines",        # SVG 图标
    "styles",             # 原生控件样式
    "networkinformation", # QNetworkInformation（网络状态）
)
_qt_plugins = os.path.join(get_package_paths('PySide6')[1], 'plugins')
qt_plugins = [
    (os.path.join(_qt_plugins, sub, name),
     os.path.join('PySide6', 'plugins', sub))
    for sub in _QT_PLUGIN_DIRS
    for name in sorted(os.listdir(os.path.join(_qt_plugins, sub)))
    if os.path.isfile(os.path.join(_qt_plugins, sub, name))
]


a = Analysis(
    ['whale_pet.py'],
    pathex=[],
    binaries=qt_plugins,
    datas=[('assets', 'assets')],
    # v50：派活给 Harness 的协议客户端。whale_pet.py 里是 try/except 导入，
    # 万一静态分析没跟到（或打包目录变了），缺了它功能会整体降级而不报错 ——
    # 所以在这里点名一次，宁可打包失败也要比静默丢功能强。
    # v51：任务对话浏览器（dsh_session_log + 它唯一的三方依赖 zstandard）。
    # v52：远程遥控（pet_remote，纯标准库）。
    # v57：飞书接入（pet_feishu 自己实现了飞书长连接的协议，只依赖 websockets）。
    #      注意**不要**把 lark-oapi 写进来：我们没用它（import 一次要 10~56 秒、
    #      一万多个模块），写进来只会白白让包涨几十 MB。
    # v61：源码按职责拆成多模块（常量/存储/时间/标记/网络/素材/窗口/桥/widget/
    #      5 个 mixin/自检）。它们都是 whale_pet.py 顶部的普通 import，静态分析
    #      本来就跟得到；这里照样点名，是为了"哪天漏了就打包失败"，
    #      而不是让主人在某个菜单点下去才发现功能没了。
    hiddenimports=['dsh_acp_client', 'dsh_session_log', 'zstandard',
                   'pet_remote', 'pet_feishu', 'websockets',
                   'pet_constants', 'pet_store', 'pet_time', 'pet_tags', 'pet_net',
                   'pet_assets', 'pet_windows', 'pet_bridges', 'pet_widget',
                   'pet_mixin_anim', 'pet_mixin_life', 'pet_mixin_chat',
                   'pet_mixin_link', 'pet_mixin_ui', 'pet_selftest', 'pet_doctor', 'pet_sound', 'pet_ledger', 'pet_panels', 'pet_ui', 'pet_shell', 'pet_settings'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WhalePet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)



