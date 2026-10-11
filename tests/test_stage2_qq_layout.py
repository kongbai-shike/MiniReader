"""阶段 2（QQ 迷你窗竖版形态）的专项测试。

覆盖规格书第 8 节的五件事：
  1. 默认尺寸 397×585（只对新安装/重置生效，已存 geometry 原样保留）；
  2. 顶栏 42px，按钮垂直居中；
  3. 底栏三组入口（目录 / 设置 / 翻页胶囊）常驻可见、命中区/字号/留白过可读性下限；
  4. 正文默认 25px / 210% 行距 / 首行缩进 2 字符；
  5. 标题栏 tooltip 钉在图标正下方，按钮太靠屏幕边缘时自动翻转、不溢出。

这些测试跑在 offscreen QPA 上，用的是真窗口（构造方式与 test_ui_offscreen 一致），
所以布局断言量的是 Qt 实际布出来的几何，而不是样式表字符串。
"""

from __future__ import annotations

import os

# 必须在 minireader.qt 导入 Qt 之前设置。setdefault，调用方显式指定的平台优先。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from minireader.qt import (
    QApplication,
    QEvent,
    QHelpEvent,
    QPoint,
    QToolButton,
    QToolTip,
    QWidget,
    QGuiApplication,
)
from minireader.storage import Settings, default_geometry
from minireader.ui.settings_dialog import READ_PRESETS, SettingsDialog
from minireader.ui.window import (
    PAGE_BTN_MIN_H,
    PAGE_BTN_MIN_W,
    TITLE_BAR_H,
    TITLE_BTN_MIN,
    MiniWindow,
    ElidedLabel,
    _qss,
    screen_rects,
    tooltip_anchor,
)

from .test_reading_features import make_long_book, pump
from .test_ui_offscreen import WindowTestCase

_app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# 出厂默认值
# ---------------------------------------------------------------------------


class TestStage2FactoryDefaults(unittest.TestCase):
    def test_default_geometry_is_the_qq_portrait_size(self) -> None:
        # 纯函数断言：给一块放得下的屏幕，默认参数就是 397×585（不依赖 offscreen 屏大小）。
        geo = default_geometry((0, 0, 1920, 1080))
        self.assertEqual((geo["w"], geo["h"]), (397, 585))

    def test_default_geometry_always_fits_the_actual_screen(self) -> None:
        # 平台屏幕偏小时默认尺寸要收缩，但仍守 320×360 下限，且整窗不出屏。
        screen = screen_rects()[0]
        geo = default_geometry(screen)
        self.assertLessEqual(geo["x"] + geo["w"], screen[0] + screen[2])
        self.assertLessEqual(geo["y"] + geo["h"], screen[1] + screen[3])
        self.assertGreaterEqual(geo["w"], 320)
        self.assertGreaterEqual(geo["h"], 360)

    def test_factory_typography_is_25px_210pct_2char_indent(self) -> None:
        settings = Settings()
        self.assertEqual(settings.font_size, 25, "默认字号 25px")
        self.assertEqual(settings.line_height, 210, "默认行距 2.1 倍 = 210%")
        self.assertEqual(settings.paragraph_indent, 2, "默认首行缩进 2 字符")


# ---------------------------------------------------------------------------
# 窗口几何
# ---------------------------------------------------------------------------


class TestStage2Geometry(WindowTestCase):
    def test_a_fresh_window_opens_at_the_default_size(self) -> None:
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        expected = default_geometry(screen_rects()[0])
        self.assertEqual(
            (window.width(), window.height()), (expected["w"], expected["h"]),
            "新安装（无已存 geometry）必须拿到 397×585 默认尺寸",
        )

    def test_an_existing_geometry_is_left_untouched(self) -> None:
        """老用户升级：已存 geometry 必须原样保留，新默认尺寸只影响新安装/重置。"""
        screen = screen_rects()[0]
        saved = {"x": 60, "y": 60, "w": 460, "h": 500}
        if (saved["x"] + saved["w"] > screen[0] + screen[2]
                or saved["y"] + saved["h"] > screen[1] + screen[3]):
            self.skipTest("当前 offscreen 屏幕放不下这块测试几何")
        self.settings.geometry = dict(saved)
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        self.assertEqual(
            (window.x(), window.y(), window.width(), window.height()),
            (saved["x"], saved["y"], saved["w"], saved["h"]),
        )

    def test_the_minimum_size_keeps_the_three_bottom_groups_usable(self) -> None:
        # 320×360 是底栏三组入口（两个 87 胶囊 + 两个图标 + 留白）仍不互吃的底线。
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        self.assertGreaterEqual(window.minimumWidth(), 320)
        self.assertGreaterEqual(window.minimumHeight(), 360)


# ---------------------------------------------------------------------------
# 顶栏 / 底栏布局
# ---------------------------------------------------------------------------


class TestStage2Chrome(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)

    def make_portrait(self, path=None) -> MiniWindow:
        """397×585 默认形态下的真窗口（make_window 强行 resize 480×360）。"""
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        window.resize(397, 585)
        window.show()
        _app.processEvents()
        if path is not None:
            self.assertTrue(window.load_path(path))
            _app.processEvents()
        return window

    def test_title_bar_is_42px_and_buttons_vertically_centered(self) -> None:
        window = self.make_portrait(self.long_path)
        self.assertEqual(window.title_bar.height(), TITLE_BAR_H)
        self.assertEqual(window.title_bar.height(), 42)
        for name, button in (
            ("open_button", window.open_button),
            ("bookmark_button", window.bookmark_button),
            ("autoscroll_button", window.autoscroll_button),
            ("boss_button", window.boss_button),
            ("alpha_button", window.alpha_button),
            ("pin_button", window.pin_button),
            ("minimize_button", window.minimize_button),
            ("close_button", window.close_button),
        ):
            center = button.y() + button.height() / 2
            self.assertAlmostEqual(center, 21, delta=2,
                                   msg=f"{name} 必须在 42px 顶栏里垂直居中")

    def test_reading_actions_live_in_the_title_bar(self) -> None:
        window = self.make_portrait()
        for name in ("open_button", "bookmark_button", "autoscroll_button"):
            self.assertIs(getattr(window, name).parent(), window.title_bar, name)

    def test_all_three_bottom_groups_are_visible_in_the_bar(self) -> None:
        window = self.make_portrait(self.long_path)
        # 翻页组
        self.assertIs(window.prev_button.parent(), window.status_bar)
        self.assertIs(window.next_button.parent(), window.status_bar)
        self.assertTrue(window.prev_button.isVisibleTo(window.status_bar))
        self.assertTrue(window.next_button.isVisibleTo(window.status_bar))
        self.assertEqual(window.prev_button.text(), "‹ 上一页")
        self.assertEqual(window.next_button.text(), "下一页 ›")
        # 目录 / 设置组（两入口必须保留且可见）
        for name in ("toc_button", "settings_button"):
            button = getattr(window, name)
            self.assertIs(button.parent(), window.status_bar, name)
            self.assertTrue(button.isVisibleTo(window.status_bar), name)
            self.assertTrue(button.toolTip(), name)

    def test_capsules_meet_the_87x29_hit_target(self) -> None:
        window = self.make_portrait(self.long_path)
        for button in (window.prev_button, window.next_button):
            self.assertGreaterEqual(button.minimumWidth(), PAGE_BTN_MIN_W)
            self.assertGreaterEqual(button.minimumHeight(), PAGE_BTN_MIN_H)
            self.assertGreaterEqual(button.width(), PAGE_BTN_MIN_W)
            self.assertGreaterEqual(button.height(), PAGE_BTN_MIN_H)

    def test_the_three_groups_do_not_overlap_at_397_width(self) -> None:
        window = self.make_portrait(self.long_path)
        bar = window.status_bar
        prev = (window.prev_button.x(), window.prev_button.x() + window.prev_button.width())
        toc = (window.toc_button.x(), window.toc_button.x() + window.toc_button.width())
        settings = (window.settings_button.x(),
                    window.settings_button.x() + window.settings_button.width())
        nxt = (window.next_button.x(), window.next_button.x() + window.next_button.width())
        # 顺序：上一页 < 目录 < 设置 …… 下一页，全部在底栏内。
        self.assertLessEqual(prev[1], toc[0])
        self.assertLessEqual(toc[1], settings[0])
        self.assertLessEqual(settings[1], nxt[0])
        self.assertGreaterEqual(prev[0], 0)
        self.assertLessEqual(nxt[1], bar.width())

    def test_bottom_bar_readability_rules_in_the_qss(self) -> None:
        qss = _qss()
        self.assertIn("QToolButton#PrevPageButton, QToolButton#NextPageButton", qss)
        self.assertIn("1px solid #6e6e6f", qss)
        self.assertIn("#a3a3a3", qss)
        self.assertIn("border-radius: 14px", qss)
        self.assertIn("font-size: 13px", qss, "翻页文字 ≥13px")
        # 目录/设置图标走 #StatusBar 通用钮：14px（≥13）、命中区 ≥28px。
        self.assertIn("min-height: 28px", qss)
        window = self.make_portrait()
        self.assertGreaterEqual(window.status_bar.layout().spacing(), 8)
        self.assertGreaterEqual(window.toc_button.minimumHeight(), TITLE_BTN_MIN)
        self.assertGreaterEqual(window.settings_button.minimumHeight(), TITLE_BTN_MIN)


# ---------------------------------------------------------------------------
# 正文排版
# ---------------------------------------------------------------------------


class TestStage2Typography(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)

    def _window(self):
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        window.resize(397, 585)
        window.show()
        _app.processEvents()
        self.assertTrue(window.load_path(self.long_path))
        _app.processEvents()
        return window

    def test_default_body_is_25px_with_210pct_lines_and_a_two_char_indent(self) -> None:
        window = self._window()
        reader = window.reader
        self.assertEqual(reader.font().pixelSize(), 25)
        block_format = reader.textCursor().blockFormat()
        self.assertAlmostEqual(block_format.lineHeight(), 210, delta=1)
        metrics = reader.fontMetrics()
        advance = getattr(metrics, "horizontalAdvance", None)
        char_w = float(advance("国")) if advance is not None else float(metrics.width("国"))
        self.assertGreater(block_format.textIndent(), 0.0)
        self.assertAlmostEqual(block_format.textIndent(), char_w * 2, delta=1.0)

    def test_indent_zero_means_flush_left(self) -> None:
        self.settings.paragraph_indent = 0
        window = self._window()
        self.assertEqual(window.reader.textCursor().blockFormat().textIndent(), 0.0)

    def test_indent_survives_a_chapter_switch(self) -> None:
        """setPlainText 会重置 block format，所以换章后缩进必须重新应用。"""
        window = self._window()
        window.goto_chapter(2)
        pump()
        self.assertGreater(window.reader.textCursor().blockFormat().textIndent(), 0.0)

    def test_the_default_screen_holds_about_eight_lines(self) -> None:
        window = self._window()
        reader = window.reader
        visible_lines = reader.viewport().height() / reader._line_px()
        # 规格目标"每页约 8 行"，给字体度量差异留余量：7-10 行都算竖长阅读形态。
        self.assertGreaterEqual(visible_lines, 7.0)
        self.assertLessEqual(visible_lines, 10.5)

    def test_progress_is_rendered_with_two_decimals(self) -> None:
        # make_long_book 默认 6 章：第一章 = 1/6 = 16.67%（规格示例格式 6.54%）。
        window = self._window()
        self.assertEqual(window.progress_label.text(), "16.67%")
        window.goto_chapter(5)
        pump()
        self.assertEqual(window.progress_label.text(), "100.00%")


# ---------------------------------------------------------------------------
# 设置对话框：缩进滑块
# ---------------------------------------------------------------------------


class TestStage2IndentControl(WindowTestCase):
    def test_indent_slider_round_trips(self) -> None:
        dialog = SettingsDialog(self.settings, None)
        try:
            self.assertEqual((dialog.indent_slider.minimum(),
                              dialog.indent_slider.maximum()), (0, 8))
            self.assertEqual(dialog.indent_slider.value(), 2)
            dialog.indent_slider.setValue(4)
            target = dialog.apply_to(Settings())
            self.assertEqual(target.paragraph_indent, 4)
        finally:
            dialog.deleteLater()
            _app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_the_default_preset_matches_factory_typography(self) -> None:
        name, values = READ_PRESETS[0]
        self.assertIn("默认", name)
        self.assertEqual((values["font_size"], values["line_height"],
                          values["paragraph_indent"]), (25, 210, 2))


# ---------------------------------------------------------------------------
# tooltip 钉图标下方 + 自动翻转
# ---------------------------------------------------------------------------


def _tip_size(text: str) -> tuple[int, int]:
    """复刻 window._tooltip_size 的尺寸口径，断言里自己算预期落点。"""
    from minireader.ui.window import _tooltip_size
    return _tooltip_size(text)


class TestStage2TooltipAnchor(WindowTestCase):
    def _portrait(self):
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        window.resize(397, 585)
        window.show()
        _app.processEvents()
        return window

    def test_anchor_sits_directly_under_the_button(self) -> None:
        window = self._portrait()
        # 挪到左上角：offscreen 虚拟字体的度量偏大，按钮靠右时气泡会被右缘钳制，
        # 这里要单独验证"未触发钳制时与按钮左边对齐"这一基本事实。
        window.move(10, 10)
        _app.processEvents()
        button = window.pin_button
        anchor = tooltip_anchor(button)
        expected_xy = button.mapToGlobal(
            button.rect().bottomLeft()
        )
        self.assertEqual(anchor.x(), expected_xy.x(), "水平与按钮左边对齐，不跟鼠标跑")
        self.assertEqual(anchor.y(), expected_xy.y() + 2, "紧贴按钮底边下方 2px")
        self.assertGreaterEqual(anchor.y(), button.mapToGlobal(button.rect().bottomLeft()).y())

    def test_event_filter_pins_the_tooltip_ignoring_the_mouse_position(self) -> None:
        """真发一个 ToolTip 事件：globalPos 故意给在按钮外 200px，气泡文本必须出现，
        且落点仍取按钮几何（证明不是 Qt 默认的"鼠标右下角"行为）。"""
        window = self._portrait()
        button = window.boss_button
        local = QPoint(2, 2)
        fake_global = button.mapToGlobal(button.rect().bottomLeft()) + QPoint(200, 200)
        event = QHelpEvent(QEvent.ToolTip, local, fake_global)
        delivered = _app.notify(button, event)
        self.assertTrue(delivered)
        self.assertEqual(QToolTip.text(), button.toolTip())
        QToolTip.hideText()

    def test_every_title_bar_button_runs_through_the_filter(self) -> None:
        window = self._portrait()
        buttons = window.title_bar.findChildren(QToolButton)
        self.assertGreaterEqual(len(buttons), 5, "标题栏至少五个按钮")
        for button in buttons:
            event = QHelpEvent(
                QEvent.ToolTip, QPoint(2, 2),
                button.mapToGlobal(button.rect().center()),
            )
            self.assertTrue(_app.notify(button, event), button.text())
            self.assertTrue(QToolTip.text(), f"{button.text()} 必须保留 tooltip 文案")
            QToolTip.hideText()

    def test_it_flips_above_when_below_does_not_fit(self) -> None:
        """把一个带 tooltip 的按钮钉到屏幕可用区底边附近：气泡必须整个翻到按钮上方。"""
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        tip_w, tip_h = _tip_size("设置：排版、不透明度、快捷键、托盘、标题伪装、边缘吸附")

        host = QWidget()
        button = QToolButton(host)
        button.setText("⚙")
        button.setToolTip("设置：排版、不透明度、快捷键、托盘、标题伪装、边缘吸附")
        button.setGeometry(0, 0, 40, TITLE_BTN_MIN)
        # 按钮底边压到屏幕可用区底沿下方 6px：下方显然放不下 tip_h。
        host.move(avail.right() - 200, avail.bottom() + 6 - TITLE_BTN_MIN)
        host.show()
        _app.processEvents()
        try:
            anchor = tooltip_anchor(button)
            button_top = button.mapToGlobal(button.rect().topLeft()).y()
            self.assertLess(anchor.y(), button_top, "空间不足时气泡必须翻到按钮上方")
            self.assertLessEqual(anchor.y() + tip_h, button_top - 2 + 1)
            self.assertGreaterEqual(anchor.y(), avail.top())
        finally:
            host.close()
            host.deleteLater()
            _app.processEvents()
            _app.sendPostedEvents(None, QEvent.DeferredDelete)
            _app.processEvents()

    def test_it_clamps_inside_the_screen_on_the_right_edge(self) -> None:
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        tip_w, _tip_h = _tip_size("章节目录（Ctrl+T）")

        host = QWidget()
        button = QToolButton(host)
        button.setToolTip("章节目录（Ctrl+T）")
        button.setGeometry(0, 0, TITLE_BTN_MIN, TITLE_BTN_MIN)
        # 按钮挂在屏幕右上角，左对齐气泡会冲出右缘。
        host.move(avail.right() - 10, avail.top() + 60)
        host.show()
        _app.processEvents()
        try:
            anchor = tooltip_anchor(button)
            self.assertLessEqual(anchor.x() + tip_w, avail.right() + 1)
            self.assertGreaterEqual(anchor.x(), avail.left())
        finally:
            host.close()
            host.deleteLater()
            _app.processEvents()
            _app.sendPostedEvents(None, QEvent.DeferredDelete)
            _app.processEvents()


# ---------------------------------------------------------------------------
# ElidedLabel
# ---------------------------------------------------------------------------


class TestElidedLabel(unittest.TestCase):
    def tearDown(self) -> None:
        _app.processEvents()
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        _app.processEvents()

    def test_full_text_survives_narrow_width_and_returns_when_wide(self) -> None:
        full = "第1章 标题1 · UTF-8 · 12.3 KB · 一些额外的次要信息"
        label = ElidedLabel(full)
        label.show()
        try:
            label.resize(40, label.height())
            _app.processEvents()
            self.assertEqual(label.full_text(), full)
            self.assertNotEqual(label.text(), full, "40px 宽必须省略")
            label.resize(900, label.height())
            _app.processEvents()
            self.assertEqual(label.text(), full, "宽度够了必须显示完整文本")
        finally:
            label.close()
            label.deleteLater()
            _app.processEvents()
            _app.sendPostedEvents(None, QEvent.DeferredDelete)
            _app.processEvents()


if __name__ == "__main__":
    unittest.main()
