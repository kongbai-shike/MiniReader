"""Command-line entry point: `py -3.12 -m minireader [file.txt]`."""

from __future__ import annotations

import os
import sys

USAGE = """MiniReader - 悬浮摸鱼阅读器

用法:
    py -3.12 -m minireader              打开 MiniReader
    py -3.12 -m minireader 小说.txt      启动时直接打开这个文件
    py -3.12 -m minireader --check      只打印环境自检信息，不开窗口
    py -3.12 -m minireader --smoke [秒]  自检用：开窗口跑 N 秒后自动退出（默认 3）

快捷键（老板键可以在 ⚙ 设置里改）:
    Ctrl+Shift+`   老板键：整个窗口藏起来 / 叫回来
    托盘图标        左键单击/双击 = 显示窗口，右键 = 菜单（显示/隐藏/设置/退出）
    Esc            窗口有焦点时也能隐身（本地快捷键，不是全局的）
    Ctrl+T         章节目录（再按一次收起）
    Ctrl+B         在当前位置加一个书签
    Ctrl+Shift+Space  自动滚动 开始 / 暂停
    Ctrl+← / Ctrl+→   上一章 / 下一章
"""


def _force_utf8_console() -> None:
    """Make the Chinese report legible in a Windows console and when piped.

    A cmd.exe window defaults to code page 936, and a PowerShell capture decodes with
    whatever the shell guesses; either can turn the self-check into mojibake. Harmless
    everywhere else, and it never raises.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - output formatting must never break startup
        pass


def _write_utf8(text: str) -> None:
    """以 UTF-8 字节写出文本，不依赖 stdout 当前的编码。

    `_force_utf8_console()` 是第一条路：把 stdout reconfigure 成 UTF-8。这里是第二条，
    用来兜住它没生效的情况——环境不支持 reconfigure，或者调用方把它换成了别的实现。
    那时 `print(USAGE)` 会走宿主编码，而中文 Windows 上重定向到文件是 cp936，编不出
    U+2699（⚙），`--help` 就 traceback。

    有字节层就直接写 UTF-8 字节，这正是我们想要的输出；没有字节层的假流（StringIO
    之类）只能走文本层，那种流不会因为编码失败。
    """
    stream = sys.stdout
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text + "\n")
        return
    try:
        stream.flush()      # 先把文本层里排队的输出吐出去，免得和下面的字节乱序
    except Exception:       # noqa: BLE001 - 冲刷失败不该拦住输出本身
        pass
    buffer.write(text.encode("utf-8", "replace") + b"\n")
    buffer.flush()


def check() -> int:
    """Environment self-report. The first thing to ask for in a bug report."""
    _force_utf8_console()
    from . import __version__
    from .hotkeys import format_hotkey, make_manager, parse_hotkey
    from .qt import BINDING, QT_VERSION, QApplication, QWidget
    from .storage import HOTKEY_FIELDS, Settings, data_dir
    from .ui.tray import TrayIcon

    # A QApplication must exist before the platform plugin has a name to report -
    # without one, platformName() is "" and this printed "offscreen/minimal" even on a
    # real Windows desktop, which is exactly the wrong thing to tell a bug report.
    app = QApplication.instance() or QApplication([sys.argv[0]])

    print(f"MiniReader {__version__}")
    print(f"  python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"  Qt binding  : {BINDING} (Qt {QT_VERSION})")
    print(f"  Qt platform : {app.platformName()}")
    print(f"  frozen      : {bool(getattr(sys, 'frozen', False))}")
    print(f"  data dir    : {data_dir()}")

    settings = Settings.load()
    spec = settings.boss_key
    try:
        parse_hotkey(spec)
        parsed = f"解析为 {format_hotkey(spec)}"
    except Exception as exc:  # noqa: BLE001
        parsed = f"无法解析：{exc}"
    print(f"  boss key    : {spec!r} {parsed}")
    print(f"  window      : {settings.geometry}")
    print(
        f"  appearance  : 不透明度={settings.opacity}% "
        f"失焦变淡={'开/' + str(settings.dim_opacity) + '%' if settings.dim_on_blur else '关'} "
        f"吸附={'开/' + str(settings.snap_distance) + 'px' if settings.snap_enabled else '关'}"
    )
    print(
        f"  behaviour   : 托盘={'开' if settings.show_tray else '关'} "
        f"任务栏={'隐藏' if settings.hide_from_taskbar else '显示'} "
        f"置顶={'开' if settings.always_on_top else '关'} "
        f"标题伪装={settings.disguise_title or '（无）'}"
    )

    # Tray availability is reported, not assumed: on the offscreen QPA plugin there is
    # no system tray at all and the app has to keep working without one.
    tray_ok = TrayIcon.is_available()
    print(f"  tray        : {'可用' if tray_ok else '不可用（无系统托盘，托盘相关功能自动隐藏）'}")

    # Actually try it. winId() realises a native window handle without ever showing
    # one, so this reports whether the boss key can work rather than whether it might.
    manager = make_manager(lambda name: None)
    manager.attach(app)
    probe = QWidget()
    hwnd = int(probe.winId())
    ok, message = manager.register("probe", spec, hwnd)
    manager.unregister_all()
    manager.detach(app)
    probe.deleteLater()
    print(f"  global hk   : {'可用' if manager.available else '不可用（非 Windows 或没有真实窗口句柄）'}")
    if manager.available:
        print(f"  实测注册    : {'成功' if ok else '失败：' + message}")
        if not ok:
            print("                换一个 boss_key（比如 Alt+` 或 Ctrl+Alt+F9）再试")

    # Every bound chord gets a real registration attempt, because "registered in the
    # config file" and "actually reserved system-wide" are different things: another
    # running program holding the same chord makes the second one fail, and the only
    # place that shows up is here.
    bindings = settings.hotkeys()
    if bindings:
        print("  已配置快捷键 :")
        for name, _field, label in HOTKEY_FIELDS:
            chord = bindings.get(name)
            if not chord:
                print(f"    {label:<12} 未绑定")
                continue
            holder = QWidget()
            try:
                got, why = manager.register(f"probe_{name}", chord, int(holder.winId()))
            except Exception as exc:  # noqa: BLE001
                got, why = False, str(exc)
            print(f"    {label:<12} {format_hotkey(chord)}  {'可注册' if got else '失败：' + why}")
        manager.unregister_all()

    if not tray_ok and not manager.available:
        print("  提示        : 热键和托盘都不可用，老板键会主动拒绝隐藏")
    if sys.platform != "win32":
        print("  注意        : 本版本的全局热键只实现了 Windows（RegisterHotKey）")
    return 0


def main(argv: list[str] | None = None, *, app=None, on_ready=None) -> int:
    """Build and run the GUI.

    Two optional injection points exist for the desktop-app entry (root ``main.py``):

    * ``app`` - a QApplication the caller already created (so the single-instance
      handshake and the quit-on-close policy are in place before any widget exists).
      When omitted, this function creates one itself (``py -m minireader``).
    * ``on_ready(window, tray, app)`` - called once, after the window is shown and the
      hotkeys are registered; the entry uses it to attach the single-instance server.
    """
    # 在这里调用一次，覆盖 --help / --check / --smoke 以及正常启动的**所有**路径。
    #
    # 原先只有 check() 里调了它，于是出现过一个很隐蔽的不一致：`--check` 重定向到
    # 文件是 UTF-8，`--smoke` 却是本地 ANSI 代码页（中文 Windows 上是 GBK）。
    # 从源码跑时感觉不到——环境里的 PYTHONIOENCODING 兜住了；打包成 exe 之后
    # PyInstaller 不带那个变量，同一台机器上同一个程序，两种输出的编码就不一样了。
    _force_utf8_console()
    args = list(sys.argv[1:] if argv is None else argv)

    if "--help" in args or "-h" in args:
        # 不是 print()：USAGE 里有一个 ⚙，编不出来时 print 会抛 UnicodeEncodeError，
        # 而 `--help` 恰恰是用户在没有 UTF-8 环境的情况下来找路的那条命令。
        _write_utf8(USAGE)
        return 0
    if "--check" in args:
        return check()

    smoke_seconds = 0.0
    if "--smoke" in args:
        idx = args.index("--smoke")
        args.pop(idx)
        smoke_seconds = 3.0
        if idx < len(args):
            try:
                smoke_seconds = float(args.pop(idx))
            except ValueError:
                pass

    # The Qt import is the one dependency that can realistically be missing, and a raw
    # ModuleNotFoundError traceback tells the user nothing actionable. Every entry
    # point (run.bat, `py -m minireader`, a shortcut) funnels through here, so the
    # helpful message belongs here rather than in the launcher script.
    try:
        from .hotkeys import make_manager, register_all
        from .qt import QApplication, QKeySequence, QShortcut, Qt, enable_high_dpi, exec_app
        from .storage import HOTKEY_FIELDS, Library, ProgressStore, Settings
        from .ui.tray import make_tray
        from .ui.window import HOTKEY_LABELS, MiniWindow
    except ImportError as exc:
        _force_utf8_console()
        print(f"缺少依赖：{exc}", file=sys.stderr)
        print("", file=sys.stderr)
        print("MiniReader 需要一个 Qt 绑定，安装其中一个即可：", file=sys.stderr)
        # The full executable path, quoted: "python" is not always on PATH, and the
        # path can contain spaces.
        print(f'    "{sys.executable}" -m pip install PySide6', file=sys.stderr)
        print(f'    "{sys.executable}" -m pip install PyQt5', file=sys.stderr)
        return 2

    enable_high_dpi()
    if app is None:
        app = QApplication([sys.argv[0]])
    app.setApplicationName("MiniReader")
    app.setApplicationDisplayName("MiniReader")
    # 常驻托盘：关掉最后一个窗口绝不能结束进程，否则"✕ 隐藏到托盘"会直接杀掉自己。
    # 真正的退出只走托盘菜单的"退出"（window.quit_app → closeEvent 真关闭）。没有任何
    # 找回途径时 closeEvent 自己会真关，所以这里恒 False 也不会留下找不到的进程。
    app.setQuitOnLastWindowClosed(False)

    settings = Settings.load()
    progress = ProgressStore.load()
    library = Library.load()

    def on_hotkey(name: str) -> None:
        """One dispatcher for every global chord.

        `HotkeyManager` already coalesces the duplicate WM_HOTKEY that Windows delivers
        for a single press, so this fires exactly once per press.
        """
        if name == "boss":
            window.toggle_boss()
        elif name == "next":
            window.step_chapter(1)
        elif name == "prev":
            window.step_chapter(-1)
        elif name == "opacity_up":
            window.set_opacity_percent(min(100, settings.opacity + 10))
        elif name == "opacity_down":
            window.set_opacity_percent(max(30, settings.opacity - 10))

    manager = make_manager(on_hotkey)
    window = MiniWindow(settings, progress, library, hotkeys=manager)

    # The tray is created before show() so that a failure to get one is known before
    # the window can be hidden. `make_tray` returns None on a platform without a tray
    # (the offscreen QPA plugin, for one), and everything downstream tolerates that.
    tray = None
    if settings.show_tray:
        tray = make_tray(window, title=settings.disguise_title or "MiniReader")
        window.attach_tray(tray)
    if tray is None:
        window.update_boss_capability()

    window.show()
    app.processEvents()          # make sure the native window exists before we ask for its HWND

    # Global hotkeys. Registering needs a live HWND, so this happens after show().
    manager.attach(app)
    hwnd = int(window.winId())
    results = register_all(manager, settings.hotkeys(), hwnd, labels=HOTKEY_LABELS)
    window.set_boss_hint(bool(results.get("boss_key", (False, ""))[0]), settings.boss_key)
    window.update_boss_capability(results)
    if tray is not None and not results.get("boss_key", (False, ""))[0]:
        # Keeping the tray is what makes hiding safe here, so say why hiding still works.
        window.set_warning("老板键注册失败，可用托盘图标显示/隐藏")

    # Desktop-app shell hook (root main.py): the single-instance server is attached to
    # this window once everything it would present is wired up.
    if on_ready is not None:
        on_ready(window, tray, app)

    # A local shortcut is the honest fallback: it is NOT global, so it only works while
    # the window has focus, which is why it does not unlock hiding on its own.
    esc = QShortcut(QKeySequence(Qt.Key_Escape), window)
    esc.setContext(Qt.WidgetWithChildrenShortcut)
    esc.activated.connect(window.toggle_boss)
    window._esc_shortcut = esc  # keep a reference alive on the Python side

    # The reading shortcuts. Deliberately local rather than global: a global Ctrl+T would
    # be stolen from every other program on the machine, and these only make sense while
    # the reader is actually looking at MiniReader.
    #
    # Ctrl+Shift+Space rather than Ctrl+Space for auto-scroll: on a Chinese Windows the
    # bare Ctrl+Space is the IME on/off switch, and stealing it would make the IME fight
    # the reader - exactly the wrong trade for this app's audience.
    #
    # Each action is gated on `_dialog_open`. Qt's WidgetWithChildrenShortcut context
    # looks at focus, and the settings dialog is parented to the window, so without this
    # gate Ctrl+B while typing in a hotkey field would silently drop a bookmark.
    local = (
        ("Ctrl+T", "目录", lambda w: w.toggle_toc()),
        ("Ctrl+B", "加书签", lambda w: w.add_bookmark()),
        ("Ctrl+Shift+Space", "自动滚动", lambda w: w.toggle_autoscroll()),
        ("Ctrl+Left", "上一章", lambda w: w.step_chapter(-1)),
        ("Ctrl+Right", "下一章", lambda w: w.step_chapter(1)),
    )
    shortcuts = []
    for sequence, _label, action in local:
        sc = QShortcut(QKeySequence(sequence), window)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(
            lambda w=window, a=action: None if w._dialog_open else a(w)
        )
        shortcuts.append(sc)
    window._local_shortcuts = shortcuts  # same reason as above: keep them alive

    # Restore the last book, at the position it was left at. load_path() does the
    # resume lookup itself, so this is the same code path as opening from the dialog.
    target = next((a for a in args if not a.startswith("-")), None) or settings.last_book
    opened = False
    if target and os.path.isfile(target):
        opened = window.load_path(target)
        if not opened:
            print(f"[warn] 打不开：{target}", file=sys.stderr)
    elif target:
        window.set_warning(f"上次的书找不到了：{os.path.basename(target)}")
        print(f"[warn] 找不到文件：{target}", file=sys.stderr)

    if smoke_seconds > 0:
        from .qt import QTimer

        QTimer.singleShot(int(max(0.2, smoke_seconds) * 1000), app.quit)
        print(f"[smoke] 窗口已显示，{smoke_seconds:g} 秒后自动退出")
        print(f"[smoke] 平台={app.platformName()} 尺寸={window.width()}x{window.height()}")
        print(f"[smoke] 托盘={'有' if window.tray is not None else '无'} 不透明度={window.opacity_percent()}% "
              f"任务栏={'隐藏' if settings.hide_from_taskbar else '显示'} 标题伪装={window.window_title()!r}")
        print(f"[smoke] 可隐藏={'是' if window.can_hide() else '否（热键与托盘都不可用，已阻止）'}")
        # Per-chord, because a chord that fails to register is otherwise invisible until
        # the user presses it and nothing happens.
        for name, _field, label in HOTKEY_FIELDS:
            if name not in settings.hotkeys():
                continue
            got, why = results.get(name, (False, "未注册"))
            print(f"[smoke] 热键 {label:<12} {'已注册' if got else '失败：' + why}")
        # Report what was actually opened. Without this the smoke run "passes" while
        # having tested nothing, which is exactly what happened the first time: a
        # mistyped filename meant no book was loaded and nothing said so.
        if window.book is not None:
            book = window.book
            print(
                f"[smoke] 已打开={os.path.basename(book.path)} 章节={book.chapter_count} "
                f"当前={window.chapter_index + 1} 编码={book.encoding_label} 字数={len(book.text)}"
            )
            print(f"[smoke] 正文字号行高={settings.font_size}px/{settings.line_height}% 滚动范围={window.reader.max_offset()}px")
        else:
            print("[smoke] 已打开=无（没有传入文件，也没有可恢复的上一本）")

    return exec_app(app)


if __name__ == "__main__":
    raise SystemExit(main())
