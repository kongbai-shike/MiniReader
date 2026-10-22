"""隐私模式（stealth privacy mode）的状态机，以及"确定"时的内部冲突检测。

tests/test_ui_offscreen.py 管的是窗口长什么样；这里管的是**时间**：进模式的同一个
按键序列不能把窗口弹回来、松开唤醒键之后还能继续看、鼠标移出去再移进来不会自己出现。
这一整套都由注入的按键状态和注入的光标驱动，所以这里既不注册全局热键，也不读真实键盘
（边角决策 8/9）。

文件下半部分是冲突对话框：两个键撞车时「确定」要把本次改动的键全部回滚并弹小窗口，
不撞车时要保存、出绿字、**不关窗**（锁定决策 5/6）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader import win32  # noqa: E402
from minireader.qt import (  # noqa: E402
    QApplication,
    QDialog,
    QEvent,
    QLabel,
    QPoint,
    QRect,
    install_message_filter,
)
from minireader.storage import Library, ProgressStore, Settings, WAKE_FIELDS  # noqa: E402
from minireader.ui import settings_dialog as dialog_module  # noqa: E402
from minireader.ui import window as window_module  # noqa: E402
from minireader.ui.settings_dialog import SettingsDialog  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

# 和 test_ui_offscreen.py 一样：offscreen 没有字体也没有窗口管理器，这两类告警是常态。
_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

_app = QApplication.instance() or QApplication([])


class _FakeCursor:
    """窗口藏着的时候，它对 QCursor 只有一个要求：pos()。

    换成假光标而不是真的把鼠标挪走：真光标在测试里既不可靠（CI 上没有指针），也会
    把鼠标从跑测试的人手里抢走。
    """

    point = QPoint(0, 0)

    @staticmethod
    def pos() -> QPoint:
        return QPoint(_FakeCursor.point)


class FakeTray:
    """只记录窗口问了它什么。隐私模式的回家路必须有托盘，所以它得存在。"""

    def __init__(self) -> None:
        self.title = None
        self.hidden_state = None
        self.opacity = None
        self.shutdown_called = False
        self.stealth_state = None
        self.stealth_spec = None
        self.stealth_wake = None

    def set_title(self, title: str) -> None:
        self.title = title

    def set_hidden_state(self, hidden: bool) -> None:
        self.hidden_state = bool(hidden)

    def set_stealth_state(self, on: bool, spec: str = "", wake: str = "") -> None:
        self.stealth_state = bool(on)
        self.stealth_spec = spec
        self.stealth_wake = wake

    def set_opacity(self, percent: int) -> None:
        self.opacity = int(percent)

    def shutdown(self) -> None:
        self.shutdown_called = True


def _dispose_window(window: MiniWindow) -> None:
    """和 test_ui_offscreen._dispose_window 同样的收尾：停掉所有事件源再排队销毁。"""
    window._quit_on_close = False
    window._geom_timer.stop()
    window._progress_timer.stop()
    window._snap_timer.stop()
    window._leave_hide_timer.stop()
    window._stealth_timer.stop()
    window._opacity_anim.stop()
    window.autoscroller.stop()
    window.close()
    window.deleteLater()


class StealthTestCase(unittest.TestCase):
    """一个沙箱化 %APPDATA%、一个假托盘、一根假唤醒键、一个假光标。"""

    def setUp(self) -> None:
        self._appdata = os.environ.get("APPDATA")
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-stealth-"))
        os.environ["APPDATA"] = str(self.tmp)

        self.settings = Settings(wheel_animation_ms=0)
        self.progress = ProgressStore.load()
        self.library = Library.load()
        self.windows: list[MiniWindow] = []

        # 假按键：True = 唤醒键「全部按着」。真实实现里是 GetAsyncKeyState 轮询，
        # 这里换成一根开关，才能一步一张地走状态机。
        self.keys_down = False
        self._real_keys_all_down = win32.keys_all_down
        self._real_is_key_down = win32.is_key_down
        win32.keys_all_down = lambda vks: bool(self.keys_down) and bool(list(vks))
        win32.is_key_down = lambda vk: bool(self.keys_down)

        # 假光标：window.py 是 from ..qt import QCursor，按名字取到的是模块属性，所以
        # 换 minireader.ui.window.QCursor 就够了。
        self._real_cursor = window_module.QCursor
        window_module.QCursor = _FakeCursor

    def tearDown(self) -> None:
        win32.keys_all_down = self._real_keys_all_down
        win32.is_key_down = self._real_is_key_down
        window_module.QCursor = self._real_cursor
        for window in self.windows:
            _dispose_window(window)
        _app.processEvents()
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        _app.processEvents()
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata

    # ---- 夹具 ----------------------------------------------------------

    def make_window(self) -> MiniWindow:
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        window.resize(480, 360)
        window.show()
        _app.processEvents()
        window.tray = FakeTray()
        return window

    def enter_privacy(self, window: MiniWindow) -> QRect:
        """以"鼠标已在窗口中间"的状态进隐私模式，返回进入前的矩形。"""
        rect = QRect(window.geometry())
        self.cursor_at(rect.center())
        window.toggle_stealth_mode()
        return rect

    def freeze(self, window: MiniWindow) -> None:
        """确认轮询开了，然后停掉它。

        真的让 50ms 计时器跑会不可复现（一次 processEvents 里跳几次取决于机器），
        测试要自己决定"下一次心跳"发生在什么时候。
        """
        self.assertTrue(window._stealth_timer.isActive())
        self.assertEqual(window._stealth_timer.interval(), 50)
        window._stealth_timer.stop()

    def cursor_at(self, point: QPoint) -> None:
        _FakeCursor.point = QPoint(point)

    def cursor_outside(self, rect: QRect) -> None:
        self.cursor_at(QPoint(rect.right() + 40, rect.top() - 40))

    def arm(self, window: MiniWindow) -> None:
        """把冷却走完：看到一次"唤醒键全松开"。"""
        self.keys_down = False
        window._stealth_tick()
        self.assertTrue(window._wake_armed)


class TestEnterAndLeave(StealthTestCase):
    def test_entering_hides_the_window_and_starts_the_poll(self) -> None:
        window = self.make_window()
        rect = self.enter_privacy(window)

        self.assertFalse(window.isVisible())
        self.assertTrue(window._stealth_mode)
        self.assertTrue(window._stealth_timer.isActive())
        self.assertEqual(window._stealth_rect, rect)
        self.assertFalse(window._wake_armed)
        self.assertFalse(window._stealth_awake)
        # Ctrl+Shift 的虚拟键码，按 spec 里的顺序解析。
        self.assertEqual(window._wake_codes, [0x11, 0x10])
        # 托盘：红点亮着，并且知道"怎么回去"和"怎么唤醒"。
        self.assertTrue(window.tray.stealth_state)
        self.assertEqual(window.tray.stealth_spec, self.settings.stealth_mode_key)
        self.assertEqual(window.tray.stealth_wake, "Ctrl+Shift")
        self.assertTrue(window.tray.hidden_state)

    def test_the_rect_is_captured_before_hiding(self) -> None:
        """窗口一藏起来 geometry() 就不可信了，所以矩形必须是进入那一刻的。"""
        window = self.make_window()
        window.move(140, 120)
        _app.processEvents()
        expected = QRect(window.geometry())
        self.enter_privacy(window)
        self.assertEqual(window._stealth_rect, expected)
        self.assertTrue(expected.isValid())

    def test_toggling_again_restores_the_window_and_the_tray(self) -> None:
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)

        window.toggle_stealth_mode()

        self.assertTrue(window.isVisible())
        self.assertFalse(window._stealth_mode)
        self.assertFalse(window._stealth_timer.isActive())
        self.assertFalse(window._wake_armed)
        self.assertFalse(window._stealth_awake)
        self.assertFalse(window._stealth_rect.isValid())
        self.assertFalse(window.tray.stealth_state)
        self.assertFalse(window.tray.hidden_state)

    def test_closing_in_privacy_mode_goes_back_to_normal_instead_of_quitting(self) -> None:
        """边角决策 1：看不见的进程比"点了 × 没退出"更吓人。"""
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)

        window.close()

        self.assertTrue(window.isVisible())
        self.assertFalse(window._stealth_mode)
        self.assertFalse(window._stealth_timer.isActive())
        self.assertFalse(window._really_quit)
        self.assertFalse(window.tray.stealth_state)
        self.assertFalse(window.tray.hidden_state)


class TestWakeStateMachine(StealthTestCase):
    def test_the_wake_key_must_be_released_once_before_revealing(self) -> None:
        """纯修饰键的时序坑：按 Ctrl+Shift+L 的那一下 Ctrl+Shift 还按着。"""
        window = self.make_window()
        self.keys_down = True  # 用户还没松手
        self.enter_privacy(window)
        self.freeze(window)
        rect = window._stealth_rect
        self.cursor_at(rect.center())

        window._stealth_tick()
        self.assertFalse(window._wake_armed)
        self.assertFalse(window.isVisible())

        self.keys_down = False
        window._stealth_tick()
        self.assertTrue(window._wake_armed)
        self.assertFalse(window.isVisible())

        self.keys_down = True
        window._stealth_tick()
        self.assertTrue(window.isVisible())
        self.assertTrue(window._stealth_awake)

    def test_a_wake_does_not_steal_focus(self) -> None:
        """边角决策 5：show() 但绝不 activateWindow()。"""
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)
        self.arm(window)
        calls: list[int] = []
        window.activateWindow = lambda: calls.append(1)  # type: ignore[method-assign]
        self.cursor_at(window._stealth_rect.center())

        self.keys_down = True
        window._stealth_tick()

        self.assertTrue(window.isVisible())
        self.assertEqual(calls, [])
        self.assertFalse(window._stealth_awake is False)

    def test_releasing_the_key_keeps_it_readable_until_the_mouse_leaves(self) -> None:
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)
        self.arm(window)
        rect = window._stealth_rect
        self.cursor_at(rect.center())

        self.keys_down = True
        window._stealth_tick()
        self.assertTrue(window.isVisible())

        # 松手：鼠标还在里面，窗口保持可翻阅。
        self.keys_down = False
        window._stealth_tick()
        self.assertTrue(window.isVisible())
        self.assertTrue(window._stealth_awake)

        # 移出：立刻隐身，并清掉唤醒状态。
        self.cursor_outside(rect)
        window._stealth_tick()
        self.assertFalse(window.isVisible())
        self.assertFalse(window._stealth_awake)

    def test_moving_back_in_does_not_reveal_it_again(self) -> None:
        """再移入必须重新"按住键 + 移入"——否则移出就失去意义。"""
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)
        self.arm(window)
        rect = window._stealth_rect
        self.cursor_at(rect.center())
        self.keys_down = True
        window._stealth_tick()
        self.assertTrue(window.isVisible())
        self.cursor_outside(rect)
        window._stealth_tick()
        self.assertFalse(window.isVisible())

        # 键已松开，只把鼠标移回来：不显现。
        self.keys_down = False
        self.cursor_at(rect.center())
        window._stealth_tick()
        self.assertFalse(window.isVisible())
        self.assertFalse(window._stealth_awake)

        # 重新按住：这次才回来。
        self.keys_down = True
        window._stealth_tick()
        self.assertTrue(window.isVisible())

    def test_an_empty_wake_key_enters_but_never_reveals(self) -> None:
        """边角决策 10：空唤醒键 = 永不显现。托盘和老板键仍是回家的路。"""
        self.settings.stealth_wake_key = ""
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)

        self.assertEqual(window._wake_codes, [])
        self.keys_down = True
        window._stealth_tick()
        self.assertTrue(window._wake_armed)  # 没有键要等，立刻上膛
        self.assertFalse(window.isVisible())

    def test_a_garbage_wake_key_behaves_like_an_empty_one(self) -> None:
        self.settings.stealth_wake_key = "NotAKey"
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)

        self.assertEqual(window._wake_codes, [])
        self.keys_down = True
        window._stealth_tick()
        self.assertFalse(window.isVisible())

    def test_the_poll_keeps_running_while_a_modal_dialog_is_open(self) -> None:
        """锁定决策 4：模态框不暂停轮询，否则设置页开着时窗口就再也叫不醒。"""
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)
        self.arm(window)
        window._dialog_open = True
        self.cursor_at(window._stealth_rect.center())

        self.keys_down = True
        window._stealth_tick()

        self.assertTrue(window.isVisible())

    def test_a_border_pixel_still_counts_as_inside(self) -> None:
        """压在右/下边框时 QRect.contains() 是 False，用户会觉得"我明明还在里面"。"""
        window = self.make_window()
        self.enter_privacy(window)
        self.freeze(window)
        rect = window._stealth_rect
        y = rect.center().y()

        self.cursor_at(QPoint(rect.right() + 1, y))
        self.assertTrue(window._cursor_in_stealth_rect())
        self.cursor_at(QPoint(rect.left() - 1, y))
        self.assertTrue(window._cursor_in_stealth_rect())
        self.cursor_at(QPoint(rect.right() + 2, y))
        self.assertFalse(window._cursor_in_stealth_rect())

    def test_the_boss_key_hands_over_to_privacy_without_showing_the_window(self) -> None:
        """边角决策 3：两种"藏"之间切换不该闪一帧窗口。"""
        window = self.make_window()
        window.hide_to_boss()
        self.assertFalse(window.isVisible())
        self.assertTrue(window.boss_hidden)

        shown: list[int] = []
        real_show = window.show
        window.show = lambda: (shown.append(1), real_show())[1]  # type: ignore[method-assign]

        window.toggle_stealth_mode()

        self.assertEqual(shown, [])
        self.assertFalse(window.isVisible())
        self.assertTrue(window._stealth_mode)
        self.assertFalse(window.boss_hidden)
        self.assertTrue(window._stealth_timer.isActive())
        self.assertTrue(window.tray.stealth_state)


class TestHotkeyConflictSave(StealthTestCase):
    """「确定」的两种结局：冲突就回滚并弹小窗口，不冲突就保存但继续开着。"""

    def setUp(self) -> None:
        super().setUp()
        self.opened: list[object] = []
        self._real_exec = dialog_module.exec_dialog

        def fake_exec(dialog) -> bool:
            self.opened.append(dialog)
            dialog.accept()
            return True

        dialog_module.exec_dialog = fake_exec
        self.dialog = SettingsDialog(Settings(), None)

    def tearDown(self) -> None:
        dialog_module.exec_dialog = self._real_exec
        self.dialog.deleteLater()
        super().tearDown()

    def _texts(self, widget) -> list[str]:
        return [label.text() for label in widget.findChildren(QLabel)]

    def test_a_wake_key_equal_to_the_privacy_chord_is_rolled_back(self) -> None:
        stealth = self.dialog.hotkey_edits["stealth_mode_key"].spec()
        # 唤醒键不在 HOTKEY_FIELDS 里，所以设置页的实时检查看不出这个重复——这正是
        # 「确定」还要再跑一次 find_conflicts 的原因。
        self.dialog.wake_edit.set_spec(stealth)
        self.assertTrue(self.dialog._ok_button.isEnabled())

        self.dialog.accept()

        self.assertEqual(len(self.opened), 1)
        conflict_dialog = self.opened[0]
        # 回滚：本次改过的唤醒键退回上次保存的写法。
        self.assertEqual(self.dialog.wake_edit.spec(), "Ctrl+Shift")
        self.assertFalse(self.dialog._saved_ok)
        self.assertIn("stealth_wake_key", conflict_dialog.edits)
        texts = self._texts(conflict_dialog)
        self.assertTrue(any("同一个组合键" in text for text in texts))
        self.assertTrue(any(WAKE_FIELDS[0][1] in text for text in texts))

    def test_editing_in_the_conflict_dialog_reaches_the_settings_page(self) -> None:
        self.dialog.wake_edit.set_spec(self.dialog.hotkey_edits["stealth_mode_key"].spec())
        self.dialog.accept()
        conflict_dialog = self.opened[0]

        conflict_dialog.edits["stealth_wake_key"].set_spec("Ctrl+Alt")

        self.assertEqual(self.dialog.wake_edit.spec(), "Ctrl+Alt")
        self.assertEqual(self.dialog.wake_edit.objectName(), "")

    def test_a_clean_save_keeps_the_dialog_open_and_says_saved(self) -> None:
        fired: list[int] = []
        self.dialog.saved.connect(lambda: fired.append(1))
        self.dialog.wake_edit.set_spec("Ctrl+Alt")

        self.dialog.accept()

        self.assertEqual(self.opened, [])
        self.assertEqual(fired, [1])
        self.assertTrue(self.dialog._saved_ok)
        self.assertEqual(self.dialog.save_note.text(), "键位已保存")
        self.assertFalse(self.dialog.save_note.isHidden())
        # 没关窗：accepted 没有被置上。
        self.assertEqual(int(self.dialog.result()), int(QDialog.Rejected))
        self.assertEqual(self.dialog._hotkey_baseline["stealth_wake_key"], "Ctrl+Alt")

    def test_closing_after_a_save_keeps_it_instead_of_reverting(self) -> None:
        self.dialog.wake_edit.set_spec("Ctrl+Alt")
        self.dialog.accept()

        self.dialog.reject()

        # 已保存过：✕ / 取消 按"确定"算，否则主窗会把主题和透明度预览撤回去。
        self.assertEqual(int(self.dialog.result()), int(QDialog.Accepted))

    def test_closing_without_a_save_still_cancels(self) -> None:
        self.dialog.wake_edit.set_spec("Ctrl+Alt")

        self.dialog.reject()

        self.assertEqual(int(self.dialog.result()), int(QDialog.Rejected))

    def test_an_empty_wake_key_warns_but_still_saves(self) -> None:
        self.dialog.wake_edit.set_spec("")

        self.assertIn("未设置唤醒键", self.dialog.stealth_note.text())
        self.assertFalse(self.dialog.stealth_note.isHidden())
        self.assertTrue(self.dialog._ok_button.isEnabled())

        self.dialog.accept()
        self.assertTrue(self.dialog._saved_ok)

    def test_an_unparseable_wake_key_blocks_ok_and_never_saves(self) -> None:
        self.dialog.wake_edit.set_spec("NotAKey")

        self.assertIn(WAKE_FIELDS[0][1], self.dialog.hotkey_note.text())
        self.assertFalse(self.dialog._ok_button.isEnabled())

        # 回车/默认按钮也走 accept()：写坏的键不能借这条路存下去。
        self.dialog.accept()
        self.assertEqual(self.opened, [])
        self.assertFalse(self.dialog._saved_ok)

    def test_apply_to_writes_the_wake_key(self) -> None:
        self.dialog.wake_edit.set_spec("Ctrl+Alt")
        out = self.dialog.apply_to(Settings())
        self.assertEqual(out.stealth_wake_key, "Ctrl+Alt")


if __name__ == "__main__":
    unittest.main()
