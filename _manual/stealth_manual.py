r"""防偷窥模式手动确认：用户点名的七条"看得见"的行为。

单测验的是"状态对、账本对"，这里验的是**坐在这台机器前能看见**的东西：

   1. 按 Ctrl+Shift+L → 窗口消失（真隐藏，不是把不透明度降到 1%）
   2. 鼠标划过窗口原来的位置 → 不现身（这是整个功能的由来）
   3. 再按一次 → 现身，不透明度是用户设置的那个值
   4. 托盘图标变了（红点），tooltip 里写着"按 Ctrl+Shift+L 解锁"
   5. 托盘菜单「显示窗口」→ 现身，并且自动退出防偷窥模式
   6. 老板键 hide() 之后按 Ctrl+Shift+L → 静默忽略（不越过老板键的意图）
   7. 防偷窥期间按老板键 → 现身，并且自动退出防偷窥模式

**这个脚本在 offscreen 下也能跑**，所以 1-7 全部是自动判定的；需要人眼的只有
"红点在 16px 下认不认得出"这一条，它由 6 张截图留证：

    _manual/stealth_shots/00_tray_icon_normal.png      正常态托盘图标 64px（对照组）
    _manual/stealth_shots/01_window_normal.png        正常态整窗（对照组）
    _manual/stealth_shots/02_tray_icon_on.png         防偷窥态托盘图标 64px
    _manual/stealth_shots/03_dialog_stealth_block.png 设置页「窗口」页的小组
    _manual/stealth_shots/04_dialog_no_recovery.png   没有任何恢复方式 → 整组禁用 + 红字
    _manual/stealth_shots/05_dialog_clash.png         与老板键撞键 → 红字 + 挡住「确定」

真的把热键按下去这一步 offscreen 做不到（没有键盘焦点、没有 native event），所以
第 1/3/6/7 条走的是**真实的分派表** `minireader.__main__.hotkey_actions`——也就是
HotkeyManager 按下热键后调用的那一张表，不是绕过它直接调 window 的方法。

跑法： py -3.12 _manual\stealth_manual.py
"""

from __future__ import annotations

import os
import sys
import tempfile

REAL_DESKTOP = os.environ.get("MR_MANUAL_REAL") == "1"
if not REAL_DESKTOP:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 控制台默认是 GBK，装不下 → 这种字符。替换而不是崩掉，脚本本身不靠打印判对错。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from minireader.qt import QApplication, QEvent, Qt  # noqa: E402

_app = QApplication.instance() or QApplication([])

from minireader.__main__ import hotkey_actions  # noqa: E402
from minireader.hotkeys import register_all  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.settings_dialog import SettingsDialog  # noqa: E402
from minireader.ui.tray import TrayIcon  # noqa: E402
from minireader.ui.window import (  # noqa: E402
    LEAVE_HIDE_DELAY_MS,
    LEAVE_HIDE_OPACITY,
    MiniWindow,
)

SHOTS = os.path.join(ROOT, "_manual", "stealth_shots")

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def pump(n: int = 6) -> None:
    for _ in range(n):
        _app.processEvents()


def wait(ms: int) -> None:
    """真的等（移出隐身的计时器是 QTimer.singleShot，processEvents 推不动它）。"""
    from minireader.qt import QtCore, QTimer

    loop = QtCore.QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    pump()


def shot(widget, name: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    widget.grab().save(path, "PNG")
    print(f"      截图 {path}")
    return path


def shot_page(dialog, name: str) -> str:
    """截对话框当前页：先滚到底，否则窗口页里那一组在折叠线以下，图上没有它。"""
    area = dialog._scroll
    bar = area.verticalScrollBar()
    bar.setValue(bar.maximum())
    pump(12)
    return shot(dialog, name)


def icon_signature(icon) -> tuple:
    """逐像素签名（不是 cacheKey——任意两张图的 cacheKey 都不同，比了等于没比）。"""
    image = icon.pixmap(16, 16).toImage()
    return tuple(
        image.pixel(x, y) for y in range(image.height()) for x in range(image.width())
    )


class _Recorder:
    """只记录 register() 收到了什么，不碰 Windows。"""

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}

    def unregister_all(self) -> None:
        self.registered.clear()

    def register(self, name: str, spec: str, hwnd: int):
        self.registered[name] = spec
        return True, ""


def send_enter(window) -> bool:
    """往窗口塞一个真的 Enter 事件（隐藏窗口本来收不到，这里是最坏情况的对照）。"""
    event = make_enter_event()
    if event is None:  # pragma: no cover - 绑定差异
        return False
    window.enterEvent(event)
    pump()
    return True


def make_enter_event():
    """Qt 6 的 enterEvent 只收 QEnterEvent，Qt 5 收 QEvent——两个绑定的签名不一样。"""
    from minireader.qt import BINDING

    if BINDING == "PySide6":
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QEnterEvent

        point = QPointF(10.0, 10.0)
        return QEnterEvent(point, point, point)
    return make_event("Enter")


def make_event(kind: str):
    """QEvent 的两种拼法：Qt 6 的嵌套枚举 vs Qt 5 的扁平 int。

    不能用 `enum_value()` 把枚举拆成 int——PySide6 的 QEvent 构造器只收 Type 枚举，
    传 int 会 TypeError。
    """
    nested = getattr(QEvent, "Type", None)
    if nested is not None and hasattr(nested, kind):
        return QEvent(getattr(nested, kind))
    flat = getattr(QEvent, kind, None)
    if flat is None:
        return None
    try:
        return QEvent(flat)
    except TypeError:  # pragma: no cover - 绑定差异
        return None


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mr-stealth-manual-")
    os.environ["APPDATA"] = tmp

    book = os.path.join(tmp, "sample.txt")
    with open(book, "w", encoding="utf-8") as fh:
        fh.write("第一章 开始\n")
        fh.write("".join(f"这是第 {i} 段的正文。\n" for i in range(1, 41)))

    settings = Settings(mouse_leave_hide=True, disguise_title="季度报告")
    window = MiniWindow(settings, ProgressStore.load(), Library.load())
    window._quit_on_close = False
    window.resize(900, 620)
    if REAL_DESKTOP:
        # 真桌面上跑：让窗口参与布局和绘制，但不出现在屏幕上（不闪、不抢焦点）。
        # 这样截图能用系统字体，而不是 offscreen 下那种"没有字体目录"的方框。
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    pump()
    window.load_path(book)
    pump()

    tray = TrayIcon(title=window.window_title())
    window.attach_tray(tray)
    pump()
    actions = hotkey_actions(window, window.settings)

    print("[0] 起点")
    user_opacity = window.opacity_percent()
    check("窗口看得见", window.isVisible(), f"isVisible={window.isVisible()}")
    check("不透明度 = 用户设置值", user_opacity == window.settings.target_opacity(active=True),
          f"{user_opacity}%")
    check("防偷窥模式默认关（重启即关，不持久化）", not window._stealth_mode)
    check("托盘 tooltip 是伪装标题", tray.toolTip() == "季度报告", tray.toolTip())
    normal_icon = icon_signature(tray.icon())
    os.makedirs(SHOTS, exist_ok=True)
    tray.icon().pixmap(64, 64).save(os.path.join(SHOTS, "00_tray_icon_normal.png"), "PNG")

    recorder = _Recorder()
    register_all(recorder, window.settings.hotkeys(), 0)
    check("快捷键按 Settings 字段名注册，且分派表认得这些名字",
          recorder.registered.get("stealth_mode_key") == "Ctrl+Shift+L"
          and set(recorder.registered) <= set(actions),
          f"{recorder.registered.get('stealth_mode_key')!r}；注册了 "
          f"{sorted(recorder.registered)}")

    shot(window, "01_window_normal.png")

    print("\n[1] 按 Ctrl+Shift+L → 窗口消失（真隐藏，不是降到 1%）")
    actions["stealth_mode_key"]()
    pump()
    hidden_opacity = window.windowOpacity()
    check("isVisible() 变成 False", not window.isVisible(), f"isVisible={window.isVisible()}")
    check("模式标记为开", window._stealth_mode)
    check("不透明度**没有**被改成 1%（差值就是证据：这是 hide()，不是渐变）",
          abs(hidden_opacity - LEAVE_HIDE_OPACITY) > 0.01,
          f"windowOpacity={hidden_opacity:.2f}，LEAVE_HIDE_OPACITY={LEAVE_HIDE_OPACITY}")
    check("托盘切到防偷窥图标（逐像素不同）",
          icon_signature(tray.icon()) != normal_icon)
    check("托盘 tooltip 写明怎么解锁",
          "防偷窥" in tray.toolTip() and "Ctrl+Shift+L" in tray.toolTip(),
          tray.toolTip())
    check("托盘「显示窗口」可用、「隐藏窗口」不可用",
          tray._show_action.isEnabled() and not tray._hide_action.isEnabled())
    os.makedirs(SHOTS, exist_ok=True)
    tray.icon().pixmap(64, 64).save(os.path.join(SHOTS, "02_tray_icon_on.png"), "PNG")
    print(f"      截图 {os.path.join(SHOTS, '02_tray_icon_on.png')}")

    print("\n[2] 鼠标划过原来的位置 → 不现身")
    if send_enter(window):
        check("硬塞一个 Enter 事件也不现身", not window.isVisible() and window._stealth_mode)
    leave = make_event("Leave")
    if leave is not None:
        window.leaveEvent(leave)
    wait(LEAVE_HIDE_DELAY_MS + 120)
    pump()
    check("移出隐身计时器到点后仍然不现身（hide() 之下没有 enter 可触发）",
          not window.isVisible() and window._stealth_mode)
    check("移出隐身没有留下 1% 的脏底", abs(window.windowOpacity() - LEAVE_HIDE_OPACITY) > 0.01,
          f"windowOpacity={window.windowOpacity():.2f}")

    print("\n[3] 再按一次 → 现身")
    actions["stealth_mode_key"]()
    pump()
    check("窗口回来了", window.isVisible(), f"isVisible={window.isVisible()}")
    check("不透明度回到用户设置值",
          window.opacity_percent() == window.settings.target_opacity(active=True),
          f"{window.opacity_percent()}%")
    check("模式自动关", not window._stealth_mode)
    check("托盘图标回到原样（逐像素等于起点）", icon_signature(tray.icon()) == normal_icon)
    check("托盘 tooltip 回到伪装标题", tray.toolTip() == "季度报告", tray.toolTip())

    print("\n[4] 托盘菜单「显示窗口」→ 现身且自动退出模式")
    actions["stealth_mode_key"]()
    pump()
    check("先进入防偷窥", not window.isVisible() and window._stealth_mode)
    tray._show_action.trigger()
    pump()
    check("托盘能叫回来", window.isVisible())
    check("并且自动退出防偷窥模式", not window._stealth_mode)
    check("tooltip 一起复位", tray.toolTip() == "季度报告", tray.toolTip())

    print("\n[5] 防偷窥期间按老板键 → 现身且自动退出")
    actions["stealth_mode_key"]()
    pump()
    actions["boss_key"]()
    pump()
    check("老板键是又一条回家路", window.isVisible())
    check("模式自动关（不留红着图标却窗口在眼前的状态）", not window._stealth_mode)
    check("老板键自己的账本没被弄脏", not window.boss_hidden)

    print("\n[6] 老板键 hide() 之后按 Ctrl+Shift+L → 静默忽略")
    actions["boss_key"]()
    pump()
    check("老板键把窗口藏了", not window.isVisible() and window.boss_hidden)
    actions["stealth_mode_key"]()
    pump()
    check("窗口仍然藏着（没有越过老板键的意图）", not window.isVisible() and window.boss_hidden)
    check("没有进入防偷窥模式", not window._stealth_mode)
    check("托盘没有变红", icon_signature(tray.icon()) == normal_icon)
    check("没有写警告文字", window.warn_label.text() == "", window.warn_label.text())
    actions["boss_key"]()
    pump()
    check("老板键自己还是能正常叫回来", window.isVisible() and not window.boss_hidden)

    print("\n[7] 设置页「窗口」页的小组")
    dialog = SettingsDialog(window.settings, parent=None)
    if REAL_DESKTOP:
        dialog.setAttribute(Qt.WA_DontShowOnScreen, True)
    dialog.show()
    pump()
    titles = [title for title, _ in __import__(
        "minireader.ui.settings_dialog", fromlist=["NAV_ITEMS"]).NAV_ITEMS]
    dialog._nav.setCurrentRow(titles.index("窗口"))
    pump(12)
    check("快捷键编辑框在「窗口」页，和鼠标移出隐身同一个小组",
          dialog.stealth_edit is dialog.hotkey_edits["stealth_mode_key"])
    check("默认：组可用、没有红字",
          dialog.stealth_widget.isEnabled() and dialog.stealth_note.text() == "")
    # 位置也要对：托盘复选框之下、置顶之上——"从属关系"是靠顺序表达的。
    check("位置在「显示托盘图标」之下、「总在最前」之上",
          dialog.tray_check.y() < dialog.stealth_widget.y() < dialog.on_top_check.y(),
          f"tray={dialog.tray_check.y()} stealth={dialog.stealth_widget.y()} "
          f"ontop={dialog.on_top_check.y()}")
    check("小块有自己的小标题",
          any(label.text() == "防偷窥模式" for label in dialog.stealth_widget.findChildren(
              __import__("minireader.qt", fromlist=["QLabel"]).QLabel)))
    shot_page(dialog, "03_dialog_stealth_block.png")

    dialog.tray_check.setChecked(False)
    dialog.hotkey_edits["boss_key"].set_spec("")
    dialog.stealth_edit.set_spec("")
    pump()
    check("没有任何恢复方式 → 整组禁用", not dialog.stealth_widget.isEnabled())
    check("红字说明理由，且红字本身没被灰掉",
          "至少需要一种恢复方式" in dialog.stealth_note.text() and dialog.stealth_note.isEnabled(),
          dialog.stealth_note.text())
    shot_page(dialog, "04_dialog_no_recovery.png")

    dialog.tray_check.setChecked(True)
    dialog.hotkey_edits["boss_key"].set_spec("Ctrl+Shift+`")
    dialog.stealth_edit.set_spec("Ctrl+Shift+`")
    pump()
    check("和老板键撞键 → 红字点名冲突", "冲突" in dialog.stealth_note.text(), dialog.stealth_note.text())
    check("并且挡住「确定」", not dialog._ok_button.isEnabled())
    shot_page(dialog, "05_dialog_clash.png")

    dialog.stealth_edit.set_spec("Ctrl+Alt+H")
    pump()
    applied = dialog.apply_to(Settings())
    check("改完能写回 Settings.stealth_mode_key", applied.stealth_mode_key == "Ctrl+Alt+H",
          applied.stealth_mode_key)
    check("写回后没有 stealth_mode 这样的布尔字段（模式不落盘）",
          not hasattr(applied, "stealth_mode"))
    dialog.close()
    dialog.deleteLater()
    pump()

    window.close()
    tray.shutdown()
    pump()

    failed = [name for ok, name, _ in RESULTS if not ok]
    print(f"\n{'=' * 72}")
    print(f"手动确认：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("未通过：")
        for name in failed:
            print(f"  - {name}")
    print(f"截图目录：{SHOTS}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
