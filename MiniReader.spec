# -*- mode: python ; coding: utf-8 -*-
"""MiniReader 的 PyInstaller 打包配置。

在项目根目录执行：
    py -3.12 -m PyInstaller MiniReader.spec --noconfirm --clean

三个开关走环境变量，让「目录模式 / 单文件模式」共用同一份 excludes 与插件裁剪清单
——两份配置各写一遍必然会漂移，那样对比出来的体积和启动时间就没有可比性了：

    MR_ONEFILE=1        打成单文件（默认打成目录）
    MR_CONSOLE=1        保留控制台窗口（默认无控制台）
    MR_DROP_OPENGL=1    丢掉 opengl32sw.dll（省约 20 MB，见下方说明）

为什么默认是目录模式而不是单文件：单文件每次启动都要把整个包解压到 %TEMP%，实测这
一步就是秒级开销，而且每次启动都会让杀软重新扫一遍新落地的几百个文件。目录模式没有
这一步。对比数据见 README 的「打包」一章。
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve()

ONEFILE = os.environ.get("MR_ONEFILE", "") not in ("", "0", "false", "False", "no")
CONSOLE = os.environ.get("MR_CONSOLE", "") not in ("", "0", "false", "False", "no")
DROP_SOFTWARE_OPENGL = os.environ.get("MR_DROP_OPENGL", "") not in (
    "", "0", "false", "False", "no",
)

ICON = ROOT / "assets" / "minireader.ico"
VERSION_FILE = ROOT / "build" / "version_info.txt"
for _needed, _hint in (
    (ICON, "py -3.12 tools\\make_icon.py"),
    (VERSION_FILE, "py -3.12 tools\\make_version_info.py"),
):
    if not _needed.exists():
        raise SystemExit(f"缺少 {_needed}\n先跑：{_hint}")

# ---------------------------------------------------------------------------
# 1. 排除清单
# ---------------------------------------------------------------------------
# 本项目只 import QtCore / QtGui / QtWidgets 三个 Qt 模块（minireader/qt.py 是唯一的
# 绑定入口，已由测试守住）。其余 Qt 模块被 PyQt5 的 hook 一并收进来，纯属体积负担。
#
# 这份清单是「宁少勿多」的：只排除确定用不到的整块。像 email / http / urllib /
# logging / xml 这些虽然也用不到，但排除它们的收益只有几百 KB，而一旦撞上某个间接
# import 就是打包版本直接崩溃——不值。
EXCLUDES = [
    # --- 其它 Qt 绑定：装都没装，但排掉可以让 PyInstaller 少找一圈 ---
    # 注意：这里排的是**整个另一种绑定**（本机绑定是 PyQt5），不是它下面的某个 Qt
    # 模块。PyQt5.QtNetwork 见下方说明——单实例要用，不能排；在装了 PySide6 的
    # 机器上打包时，应改用 PySide6 绑定并保留 PySide6.QtNetwork（已在 hiddenimports）。
    "PySide2", "PySide6", "PyQt6", "shiboken2", "shiboken6",
    # --- 用不到的 Qt 模块 ---
    # PyQt5.QtNetwork **必须保留**：main.py 的单实例握手（QLocalServer/QLocalSocket，
    # Windows 上是命名管道）就靠它。曾经这里排掉了它、bin 裁剪又丢了 Qt5Network.dll，
    # 结果是双击第二个实例不会退出、各自开窗。模块（.pyd）保留在 hiddenimports，
    # 运行库 Qt5Network.dll 保留在 KEEP_QT_BIN，两处缺一不可。
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets", "PyQt5.QtQuick3D",
    "PyQt5.QtWebEngine", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebChannel", "PyQt5.QtWebSockets", "PyQt5.QtWebView",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtSql",              # 书签用的是 Python 自带的 sqlite3，不是 QtSql
    "PyQt5.QtSvg", "PyQt5.QtPrintSupport",
    "PyQt5.QtBluetooth", "PyQt5.QtNfc", "PyQt5.QtPositioning", "PyQt5.QtLocation",
    "PyQt5.QtSensors", "PyQt5.QtSerialPort", "PyQt5.QtXml", "PyQt5.QtXmlPatterns",
    "PyQt5.QtDBus", "PyQt5.QtDesigner", "PyQt5.QtHelp", "PyQt5.QtTest",
    "PyQt5.QtOpenGL", "PyQt5.QtCharts", "PyQt5.QtDataVisualization",
    "PyQt5.QtTextToSpeech", "PyQt5.QtGamepad", "PyQt5.QtRemoteObjects",
    "PyQt5.Qt3DCore", "PyQt5.Qt3DRender", "PyQt5.Qt3DInput", "PyQt5.Qt3DLogic",
    "PyQt5.Qt3DAnimation", "PyQt5.Qt3DExtras", "PyQt5.uic",
    # --- 用不到的标准库 / 打包工具链 ---
    "tkinter",                  # 本项目是 Qt 程序，但很多环境下 tkinter 会被捎带进来
    "unittest",                 # 测试从源码跑，不进 exe
    "pydoc", "doctest", "pdb", "bdb", "lib2to3",
    "distutils", "setuptools", "pip", "pkg_resources", "wheel",
    "PyInstaller",
    # 注意：**不要**在这里加 ssl / _ssl / hashlib / _hashlib，也不要删
    # libcrypto-3.dll 和 libssl-3.dll。
    #
    # 看起来很划算——这是个全程离线的程序，用不到 TLS，删掉能省约 5.8 MB。
    # 但实测 hashlib.sha1 走的是 _hashlib.HASH（OpenSSL），不是内置的 _sha1：
    #
    #     >>> type(hashlib.sha1(b"x"))
    #     <class '_hashlib.HASH'>
    #
    # 而 book_id() 就是 sha1(path)。删掉 libcrypto 之后 sha1 会直接抛异常，
    # 于是每本书的 id 都没法算——阅读进度和书签会全部对不上号。为了 5.8 MB 换来
    # 这个，不值。
]

# ---------------------------------------------------------------------------
# 2. Qt 插件裁剪
# ---------------------------------------------------------------------------
# PyQt5 的 hook 会把 platforms / styles / imageformats / iconengines / sqldrivers
# 等一堆插件目录拷进来。本项目真正需要的只有两项：
#
#   platforms/qwindows.dll            —— 没有它 Qt 起不来，这是必需的
#   styles/qwindowsvistastyle.dll     —— 原生 Windows 外观；少了控件会退回 Fusion
#
# 另外保留 platforms/qoffscreen.dll 是**故意**的：它让打包好的 exe 能在没有显示器的
# 环境里跑 --check / --smoke（CI 或远程会话），排查打包问题时非常有用，代价只有 750 KB。
#
# 被丢掉的那些以及理由：
#   imageformats/*  —— 没有任何一处从文件加载图片。托盘图标和删除按钮的图标都是
#                      QPainter 现画的（ui/tray.py、ui/toc_panel.py），走的是内存里的
#                      QPixmap，不需要解码插件。PNG 解码器本身编译在 Qt5Gui 里，不是插件。
#   iconengines/qsvgicon —— 同上，没有 SVG。
#   sqldrivers/*    —— 书签用 Python 的 sqlite3；QtSql 已被排除。
#   platforms/qminimal, qwebgl —— 不需要。
KEEP_PLUGINS = {
    "platforms/qwindows.dll",
    "platforms/qoffscreen.dll",
    "styles/qwindowsvistastyle.dll",
}

# ---------------------------------------------------------------------------
# 2b. Qt5/bin 裁剪
# ---------------------------------------------------------------------------
# 第一版漏了这一层，实测打包出来仍有 Qt5Qml.dll(3.4 MB) / Qt5Quick.dll(4.0 MB)
# 等一堆用不到的 DLL——因为 PyQt5 的 hook 是把整个 Qt5\bin
# 目录当二进制收进来的，跟 excludes 里排掉的模块没关系。排除模块只影响 .pyd，
# 不影响这些 Qt 自己的 DLL。补上这一层之后 90.6 MB -> 73.5 MB（再关掉软件 OpenGL
# 兜底是 53.5 MB）。
#
# 例外是 Qt5Network.dll(约 1.3 MB)：单实例握手的原生实现，必须留下。
#
# 只留 QtCore / QtGui / QtWidgets 这条链，以及 C++ 运行库。留下 MSVCP140 / VCRUNTIME140
# 是因为 Qt5Core.dll 本身依赖它们，而目标机器不一定装了 VC++ 运行库——这几百 KB 是
# 「拷到别人电脑上能不能直接双击」的差别，不能省。
KEEP_QT_BIN = {
    "qt5core.dll",
    "qt5gui.dll",
    "qt5widgets.dll",
    # 单实例握手（QLocalServer/QLocalSocket）的原生运行库。excludes 里保留
    # PyQt5.QtNetwork 的同时必须把它一起留下：.pyd 只是 Python 绑定壳，真正干活
    # 的是这个 DLL；只留前者会在 import 时直接崩。
    "qt5network.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
}

# opengl32sw.dll（约 20 MB）是 Qt 的**软件** OpenGL 实现，也是整条兜底链的最后一环。
# 默认保留：Qt 5 的 windows 平台插件在初始化时会去摸 OpenGL，一台没有可用显卡驱动
# 的机器（虚拟机、部分远程桌面、刚装完系统的裸机）如果既没有它也没有别的兜底，
# 表现是**双击没反应**，而不是弹个错——这种问题最难查，20 MB 换它不值得省。
DROP_SOFTWARE_OPENGL = os.environ.get("MR_DROP_OPENGL", "") not in (
    "", "0", "false", "False", "no",
)

# ANGLE（libEGL / libGLESv2 / d3dcompiler_47，约 7.2 MB）是 Direct3D 那一路兜底。
# 默认丢掉：它不是最后一环，后面的 opengl32sw 还在；而留着它的收益只体现在
# 「有 D3D 但桌面 OpenGL 不可用」的窄缝里——那种机器本来就会落到软件渲染。
# 想要最保险的包就把 MR_KEEP_ANGLE=1。
KEEP_ANGLE = os.environ.get("MR_KEEP_ANGLE", "") not in ("", "0", "false", "False", "no")

# 这几个只作为「可选兜底」存在，默认按上面的开关决定去留。
_OPTIONAL_QT_BIN = {
    "opengl32sw.dll": lambda: not DROP_SOFTWARE_OPENGL,
    "libegl.dll": lambda: KEEP_ANGLE,
    "libglesv2.dll": lambda: KEEP_ANGLE,
    "d3dcompiler_47.dll": lambda: KEEP_ANGLE,
}

_QT = "PyQt5/Qt5/"


def _wanted(dest: str) -> bool:
    """这个打包条目要不要留下。dest 是包内路径（正斜杠）。"""
    if dest.startswith(_QT + "qml/") or dest.startswith(_QT + "translations/"):
        # QML 全用不到。translations 也用不到：程序里没有任何 QTranslator，
        # Qt 不会自动加载 .qm（QColorDialog / QFileDialog 的按钮本来就是英文的，
        # 这一点打包前后完全一致）。
        return False
    if _QT + "plugins/" in dest:
        return dest.split(_QT + "plugins/", 1)[1] in KEEP_PLUGINS
    if _QT + "bin/" in dest:
        name = dest.split(_QT + "bin/", 1)[1].lower()
        if name in KEEP_QT_BIN:
            return True
        chooser = _OPTIONAL_QT_BIN.get(name)
        return bool(chooser) and chooser()
    return True


def _trim(entries, what: str):
    kept, dropped_bytes, dropped_n = [], 0, 0
    for entry in entries:
        if _wanted(str(entry[0]).replace("\\", "/")):
            kept.append(entry)
        else:
            dropped_n += 1
            try:
                dropped_bytes += os.path.getsize(entry[1])
            except OSError:
                pass
    print(
        f"[MiniReader.spec] {what}: 留下 {len(kept)} 项，"
        f"丢掉 {dropped_n} 项（约 {dropped_bytes / 1048576:.1f} MB）"
    )
    return kept


# ---------------------------------------------------------------------------
# 3. 打包
# ---------------------------------------------------------------------------
a = Analysis(
    # 入口必须是根目录的 main.py（不是 tools/frozen_entry.py）：单实例握手
    # （signal_existing_instance 客户端 + SingleInstanceServer 服务端）、日志落盘、
    # 异常弹框这套桌面外壳都在 main.py 里，frozen_entry 直转 __main__ 会把整条
    # 单实例链绕过去——打包后双击第二个实例不会退出，会各自开窗。
    # main.py 全部用绝对包导入（from minireader...），直接当 PyInstaller 入口没有
    # relative-import 问题（frozen_entry 当年只是为相对导入的 __main__.py 设的）。
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # 托盘图标（ui/tray.py 优先读 assets/minireader.ico，见 app_paths.resource_path）。
    # 等价于命令行 --add-data "assets/minireader.ico;assets"；不带它时托盘会退回
    # QPainter 现画的兜底图标，exe 图标和托盘图标就不一致了。
    datas=[(str(ICON), "assets")],
    # QtNetwork 是在 minireader/qt.py 里用 importlib 动态导入的，静态分析看不到，
    # 必须显式声明，两种绑定各列一份：本机装哪个就打进哪个，另一份只产生一条
    # "hidden import not found" 提示（不影响构建）。
    hiddenimports=["PyQt5.QtNetwork", "PySide6.QtNetwork"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    # 只做 -O（去掉 assert 和 __debug__ 分支）。本项目运行时没有一处 assert（已核对），
    # 所以 optimize=2（顺带去掉 docstring）也是安全的，想再省一点可以改成 2。
    optimize=1,
)
a.binaries = _trim(a.binaries, "二进制")
a.datas = _trim(a.datas, "数据文件")

pyz = PYZ(a.pure)

_common = dict(
    name="MiniReader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx 关掉，而且是明确写出来的：UPX 压缩过的 exe 是杀软启发式规则的高危特征，
    # 而且压缩过的 Qt DLL 偶尔会加载失败。省下的十几 MB 不值得换来「用户双击没反应」
    # 或者「被 Windows Defender 拦下来」。详见 README 的杀软一节。
    upx=False,
    console=CONSOLE,
    icon=str(ICON),
    version=str(VERSION_FILE),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_common)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="MiniReader",
    )
