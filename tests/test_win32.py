"""The raw Win32 helpers must be infallible.

Every one of these is called from inside window creation, painting-adjacent code, or a
native event filter. A raise there does not produce a nice error message; it either kills
the window or, worse, propagates out of an event filter into Qt. So the contract these
tests pin is: **no input makes any of them raise**, and each one returns the documented
falsy value when it cannot do its job.

The behavioural assertions are gated on `native_platform_available()`, because this suite
runs under the offscreen QPA plugin, where `winId()` returns an id that is not a real
window handle. That is not a degraded test: returning False gracefully for a bogus handle
is exactly the property being verified.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from minireader import win32  # noqa: E402
from minireader.qt import QApplication, QWidget, native_platform_available  # noqa: E402

_app = QApplication.instance() or QApplication([])

HOSTILE = (None, 0, -1, 1, 2 ** 40, 0.5, "abc", [], object())


class TestConstants(unittest.TestCase):
    """These exact values are what make the code work; a typo is silent otherwise."""

    def test_style_and_flag_values(self) -> None:
        self.assertEqual(win32.GWL_EXSTYLE, -20)
        self.assertEqual(win32.WS_EX_LAYERED, 0x00080000)
        self.assertEqual(win32.WS_EX_TOOLWINDOW, 0x00000080)
        self.assertEqual(win32.WS_EX_APPWINDOW, 0x00040000)
        self.assertEqual(win32.LWA_ALPHA, 0x00000002)
        self.assertEqual(win32.MONITOR_DEFAULTTONEAREST, 0x00000002)

    def test_appwindow_and_toolwindow_are_distinct_bits(self) -> None:
        """`WS_EX_APPWINDOW` overrides `WS_EX_TOOLWINDOW`, which is why the raw path has
        to clear one while setting the other."""
        self.assertFalse(win32.WS_EX_APPWINDOW & win32.WS_EX_TOOLWINDOW)

    def test_quiet_setwindowpos_flags_do_not_move_resize_or_activate(self) -> None:
        for flag in ("SWP_NOSIZE", "SWP_NOMOVE", "SWP_NOZORDER", "SWP_NOACTIVATE"):
            self.assertTrue(getattr(win32, flag))


class TestWindowsPresence(unittest.TestCase):
    def test_is_windows_matches_the_interpreter(self) -> None:
        import sys

        self.assertEqual(win32.IS_WINDOWS, sys.platform == "win32")

    def test_describe_is_a_readable_sentence(self) -> None:
        text = win32.describe()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())
        self.assertIn("user32", text)


class TestHwndOf(unittest.TestCase):
    def test_a_widget_yields_a_positive_integer(self) -> None:
        widget = QWidget()
        try:
            hwnd = win32.hwnd_of(widget)
            self.assertIsInstance(hwnd, int)
            self.assertGreater(hwnd, 0, "winId() realises a handle without showing anything")
        finally:
            widget.deleteLater()

    def test_none_is_zero_rather_than_an_exception(self) -> None:
        self.assertEqual(win32.hwnd_of(None), 0)

    def test_an_object_without_winid_is_zero(self) -> None:
        self.assertEqual(win32.hwnd_of(object()), 0)
        self.assertEqual(win32.hwnd_of("not a widget"), 0)


class TestNeverRaises(unittest.TestCase):
    """The headline contract. One loop, every function, every hostile input."""

    def test_no_function_raises_on_hostile_handles(self) -> None:
        for bad in HOSTILE:
            with self.subTest(handle=bad):
                self.assertIsInstance(win32.hwnd_of(bad), int)
                self.assertIn(win32.set_window_text(bad, "标题"), (True, False))
                self.assertIn(win32.set_layered_alpha(bad, 50), (True, False))
                self.assertIn(win32.clear_layered(bad), (True, False))
                self.assertIn(win32.set_tool_window(bad, True), (True, False))
                self.assertIn(win32.is_window_visible(bad), (True, False))
                self.assertIsInstance(win32.device_pixel_ratio(bad), float)
                area = win32.monitor_work_area(bad)
                self.assertTrue(area is None or len(area) == 4)
                logical = win32.logical_work_area(bad, 1.0)
                self.assertTrue(logical is None or len(logical) == 4)

    def test_hostile_text_does_not_raise(self) -> None:
        widget = QWidget()
        try:
            hwnd = win32.hwnd_of(widget)
        finally:
            widget.deleteLater()
        for text in ("", "x" * 5000, "标题", None, 123, "a\u0000b"):
            with self.subTest(text=repr(text)[:20]):
                self.assertIn(win32.set_window_text(hwnd, text), (True, False))

    def test_hostile_opacity_does_not_raise(self) -> None:
        for value in (-1000, -1, 0, 1, 50, 100, 101, 10 ** 9, None, "50", float("nan")):
            with self.subTest(value=value):
                self.assertIn(win32.set_layered_alpha(0, value), (True, False))

    def test_the_fallback_ratio_is_respected_for_a_bogus_handle(self) -> None:
        for fallback in (1.0, 1.25, 2.0):
            ratio = win32.device_pixel_ratio(0, fallback)
            self.assertIsInstance(ratio, float)
            self.assertGreater(ratio, 0.0)


class TestOnARealDesktop(unittest.TestCase):
    """Only meaningful with real window handles; skipped under the offscreen plugin."""

    def setUp(self) -> None:
        if not (win32.IS_WINDOWS and native_platform_available()):
            self.skipTest("needs a real Windows window handle")

    def test_a_visible_window_reports_itself_visible(self) -> None:
        widget = QWidget()
        widget.show()
        _app.processEvents()
        try:
            hwnd = win32.hwnd_of(widget)
            self.assertTrue(win32.is_window_visible(hwnd))
        finally:
            widget.close()
            widget.deleteLater()

    def test_setting_the_title_reaches_the_native_window(self) -> None:
        widget = QWidget()
        widget.show()
        _app.processEvents()
        try:
            self.assertTrue(win32.set_window_text(win32.hwnd_of(widget), "伪装标题"))
        finally:
            widget.close()
            widget.deleteLater()

    def test_layered_alpha_can_be_applied_and_cleared(self) -> None:
        widget = QWidget()
        widget.show()
        _app.processEvents()
        hwnd = win32.hwnd_of(widget)
        try:
            self.assertTrue(win32.set_layered_alpha(hwnd, 70), "LWA_ALPHA on a live HWND")
            self.assertTrue(win32.clear_layered(hwnd), "removing the layered style")
        finally:
            widget.close()
            widget.deleteLater()

    def test_the_dpi_ratio_is_one_or_more(self) -> None:
        widget = QWidget()
        widget.show()
        _app.processEvents()
        try:
            self.assertGreaterEqual(win32.device_pixel_ratio(win32.hwnd_of(widget)), 1.0)
        finally:
            widget.close()
            widget.deleteLater()

    def test_the_work_area_is_reported_and_sane(self) -> None:
        widget = QWidget()
        widget.show()
        _app.processEvents()
        try:
            hwnd = win32.hwnd_of(widget)
            area = win32.monitor_work_area(hwnd)
            self.assertIsNotNone(area)
            x, y, w, h = area
            self.assertGreater(w, 200)
            self.assertGreater(h, 200)
            logical = win32.logical_work_area(hwnd)
            self.assertIsNotNone(logical)
            self.assertLessEqual(logical[2], w, "logical pixels are never more than physical")
            self.assertLessEqual(logical[3], h)
        finally:
            widget.close()
            widget.deleteLater()

    def test_setting_the_tool_window_bit_reports_success(self) -> None:
        widget = QWidget()
        widget.show()
        _app.processEvents()
        try:
            self.assertTrue(win32.set_tool_window(win32.hwnd_of(widget), True))
        finally:
            widget.close()
            widget.deleteLater()


class TestKeyPolling(unittest.TestCase):
    """隐私模式的唤醒键：GetAsyncKeyState 只观察，不注册，也不许抛异常。"""

    def setUp(self) -> None:
        self._saved_poll = win32._get_async_key_state
        self._saved_windows = win32.IS_WINDOWS

    def tearDown(self) -> None:
        win32._get_async_key_state = self._saved_poll
        win32.IS_WINDOWS = self._saved_windows

    def _stub(self, value) -> None:
        win32._get_async_key_state = lambda vk: value

    def test_off_windows_it_is_false_and_does_not_raise(self) -> None:
        win32.IS_WINDOWS = False
        self.assertFalse(win32.is_key_down(0x11))
        self.assertFalse(win32.keys_all_down([0x11]))

    def test_only_the_high_bit_means_held_down(self) -> None:
        """低位是"上次调用之后按过"，拿它判定会把一次点击当成一直按着。"""
        self._stub(0x0001)
        self.assertFalse(win32.is_key_down(0x11))
        self._stub(0x8000)
        self.assertTrue(win32.is_key_down(0x11))
        self._stub(0x8001)
        self.assertTrue(win32.is_key_down(0x11))

    def test_no_keys_at_all_is_false(self) -> None:
        """all([]) 是 True：空唤醒键若返回 True，窗口会对着所有人大开。"""
        self.assertFalse(win32.keys_all_down([]))
        self.assertFalse(win32.keys_all_down(None))
        self.assertFalse(win32.keys_all_down(()))

    def test_all_means_all(self) -> None:
        down: set[int] = set()
        win32._get_async_key_state = lambda vk: 0x8000 if vk in down else 0
        self.assertFalse(win32.keys_all_down([0x11, 0x10]))
        down.add(0x11)
        self.assertFalse(win32.keys_all_down([0x11, 0x10]))
        down.add(0x10)
        self.assertTrue(win32.keys_all_down([0x11, 0x10]))

    def test_a_failing_win32_call_is_a_false(self) -> None:
        def boom(_vk):
            raise OSError("user32 went away")

        win32._get_async_key_state = boom
        self.assertFalse(win32.is_key_down(0x11))
        self.assertFalse(win32.keys_all_down([0x11]))

    def test_hostile_codes_never_raise(self) -> None:
        for vk in HOSTILE:
            with self.subTest(vk=repr(vk)[:20]):
                self.assertIn(win32.is_key_down(vk), (True, False))
        for vks in ([None], ["x"], [object()], [2 ** 40], [0.5], 42, "ab"):
            with self.subTest(vks=repr(vks)[:20]):
                self.assertIn(win32.keys_all_down(vks), (True, False))


if __name__ == "__main__":
    unittest.main()
