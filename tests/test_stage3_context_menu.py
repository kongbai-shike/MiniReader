"""阶段 3 交互项的专项测试（规格书第 8 节"阶段 3：交互逻辑"）。

本阶段只做三件事（分页制改造与滚轮改翻页已由用户决策明确不做）：
  1. 正文右键菜单：上一页 / 下一页 ｜ 重新加载本页，样式对齐 QQ 实测
     （底 #2b2b2b、字 #bdbdc0、项高 40px、圆角 6-8px、纯文字、约 135×148px）；
  2. 👁 / 📌 tooltip 随状态实时双态切换；
  3. 菜单弹出期间暂停移出隐身的 100ms 防抖，菜单关闭后恢复。

外加一项阶段 2 收尾检查：新增的「首行缩进」滑块必须和设置对话框既有控件
同一条 QSS（字号 ≥13px、手柄 ≥14px），不允许出现一个"裸"滑块。
"""

from __future__ import annotations

import os

# 必须在 minireader.qt 导入 Qt 之前设置。setdefault，调用方显式指定的平台优先。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from minireader.qt import (
    QApplication,
    QContextMenuEvent,
    QEvent,
    QPoint,
)
from minireader.ui.settings_dialog import SettingsDialog
from minireader.ui.window import (
    LEAVE_HIDE_DELAY_MS,
    READER_MENU_MIN_W,
    MiniWindow,
)

from .test_reading_features import _settle, make_long_book, pump
from .test_ui_offscreen import WindowTestCase, assert_at_leave_hide_floor

_app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# 菜单结构 / 样式 / 接线
# ---------------------------------------------------------------------------


class TestReaderMenuStructure(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)
        self.window = self.make_window(path=self.long_path)
        self.menu = self.window.reader_menu

    def tearDown(self) -> None:
        self.menu.hide()
        pump()
        super().tearDown()

    def test_exactly_three_text_items_with_one_separator(self) -> None:
        actions = self.menu.actions()
        kinds = [(a.text(), a.isSeparator()) for a in actions]
        self.assertEqual(
            kinds,
            [("上一页", False), ("下一页", False), ("", True), ("重新加载本页", False)],
        )

    def test_items_are_plain_text_no_icon_no_shortcut(self) -> None:
        for action in self.menu.actions():
            if action.isSeparator():
                continue
            self.assertTrue(action.icon().isNull(), "规格：纯文字，无图标")
            self.assertTrue(action.shortcut().isEmpty(), "规格：无快捷键右列")

    def test_qss_matches_the_qq_measurements(self) -> None:
        qss = self.menu.styleSheet()
        self.assertIn("#2b2b2b", qss, "菜单底色")
        self.assertIn("#bdbdc0", qss, "菜单文字色")
        self.assertIn("min-height: 40px", qss, "项高 40px")
        self.assertIn("font-size: 13px", qss, "菜单文字 ≥13px")
        # 圆角 6-8px：菜单项 6、菜单外框 8。
        self.assertIn("border-radius: 8px", qss)
        self.assertIn("border-radius: 6px", qss)
        # #3a3a3a 同时是分隔线色和悬停高亮色。
        self.assertIn("QMenu::separator", qss)
        self.assertIn("QMenu::item:selected { background: #3a3a3a", qss)
        self.assertEqual(qss.count("#3a3a3a"), 3, "边框/悬停/分隔线三处")

    def test_menu_geometry_is_about_135_by_148(self) -> None:
        # 样式表作用在菜单上之后量 sizeHint，量的是 Qt 实际算出来的尺寸。
        hint = self.menu.sizeHint()
        self.assertGreaterEqual(hint.width(), READER_MENU_MIN_W, "整宽 ≈135")
        # 3×40 项 + 1px 分隔线（含 8px 上下 margin）+ 菜单上下 padding 12：141 上下。
        self.assertGreaterEqual(hint.height(), 132)
        self.assertLessEqual(hint.height(), 168, "实测约 148，允许字体度量偏差")

    def test_boundary_enabled_state_matches_the_bottom_capsules(self) -> None:
        # 第一章：上一页禁用（与底栏胶囊同一条规则），下一页可用，重载可用。
        self.window._on_reader_menu_about_to_show()
        self.assertFalse(self.prev_action().isEnabled())
        self.assertTrue(self.next_action().isEnabled())
        self.assertTrue(self.window.reload_page_action.isEnabled())

        # 中间章：两边都可用。
        self.window.step_chapter(1)
        self.window._on_reader_menu_about_to_show()
        self.assertTrue(self.prev_action().isEnabled())
        self.assertTrue(self.next_action().isEnabled())

        # 末章：下一页禁用。
        self.window.goto_chapter(5)
        self.window._on_reader_menu_about_to_show()
        self.assertTrue(self.prev_action().isEnabled())
        self.assertFalse(self.next_action().isEnabled())

    def test_actions_actually_turn_pages(self) -> None:
        self.assertEqual(self.window.chapter_index, 0)
        self.next_action().trigger()
        pump()
        self.assertEqual(self.window.chapter_index, 1)
        self.prev_action().trigger()
        pump()
        self.assertEqual(self.window.chapter_index, 0)

    def test_all_items_disabled_without_a_book(self) -> None:
        bare = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(bare)
        bare._quit_on_close = False
        bare._on_reader_menu_about_to_show()
        self.assertFalse(bare.prev_page_action.isEnabled())
        self.assertFalse(bare.next_page_action.isEnabled())
        self.assertFalse(bare.reload_page_action.isEnabled())

    def prev_action(self):
        return self.window.prev_page_action

    def next_action(self):
        return self.window.next_page_action


class TestContextMenuEvent(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)
        self.window = self.make_window(path=self.long_path)

    def tearDown(self) -> None:
        self.window.reader_menu.hide()
        pump()
        super().tearDown()

    def test_right_click_emits_the_global_position_and_pops_there(self) -> None:
        received = []
        self.window.reader.contextMenuRequested.connect(received.append)
        local = QPoint(50, 60)
        # QAbstractScrollArea 的真实右键目标是 **viewport**：它把 ContextMenu 事件
        # 转发给 QTextEdit，我们的 override 才会被调（直接发给 scrollarea 会被
        # 默认路由 ignore）。生产环境鼠标右键本来就落在 viewport，这里照实模拟。
        target = self.window.reader.viewport()
        global_pos = target.mapToGlobal(local)
        event = QContextMenuEvent(QContextMenuEvent.Mouse, local, global_pos)
        _app.sendEvent(target, event)
        self.assertTrue(event.isAccepted(), "事件必须被吃掉，不冒泡给默认菜单")
        self.assertEqual(received, [global_pos], "发出的必须是右键点的全局坐标")
        pump()
        self.assertTrue(self.window.reader_menu.isVisible(), "菜单真的弹出来了")
        self.assertEqual(self.window._context_menu_open, True)
        self.window.reader_menu.hide()
        pump()
        self.assertFalse(self.window._context_menu_open)


# ---------------------------------------------------------------------------
# 重新加载本页
# ---------------------------------------------------------------------------


class TestReloadCurrentChapter(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)
        self.window = self.make_window(path=self.long_path)

    def test_it_picks_up_new_content_from_disk_without_losing_chapter(self) -> None:
        self.window.goto_chapter(2)
        pump()
        self.assertEqual(self.window.book.chapter_count, 6)
        # 外部追加新章（模拟文件被更新）。
        with self.long_path.open("a", encoding="utf-8") as fh:
            fh.write("\n第7章 追加的章节\n" + "这是磁盘上新追加的正文。" * 100)

        self.window.reload_current_chapter()
        pump()
        self.assertEqual(self.window.book.chapter_count, 7, "读到了磁盘新版本")
        self.assertEqual(self.window.chapter_index, 2, "留在当前章，没被弹回开头")
        self.window.goto_chapter(6)
        pump()
        self.assertIn("新追加的正文", self.window.reader.toPlainText())

    def test_it_keeps_the_viewport_anchor_when_text_is_unchanged(self) -> None:
        # 滚到中间取一个字符锚点；原文件不变地重读，视口必须停在同一句话。
        bar = self.window.reader.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        pump()
        anchor_before = self.window.reader.first_visible_char()
        self.assertGreater(anchor_before, 0)

        self.window.reload_current_chapter()
        pump(6)
        self.assertEqual(
            self.window.reader.first_visible_char(), anchor_before, "锚点字符不变"
        )

    def test_a_missing_file_reports_a_warning_and_keeps_the_old_book(self) -> None:
        self.long_path.unlink()
        old_book = self.window.book
        self.window.reload_current_chapter()
        pump()
        self.assertIs(self.window.book, old_book, "重读失败不许把书清空")
        self.assertEqual(self.window.chapter_index, 0)
        self.assertTrue(self.window.warn_label.text(), "必须给用户一句警告")


# ---------------------------------------------------------------------------
# 右键菜单 × 移出隐身 共存
# ---------------------------------------------------------------------------


class TestMenuSuspendsLeaveHide(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)
        self.window = self.make_window(path=self.long_path)
        # 放到确定位置：测试末尾要验证"指针仍在窗外→恢复计时"，QCursor 在 offscreen
        # 上恒报 (0,0)，窗口挪开后它才一定落在几何外。
        self.window.move(100, 100)
        self.window.settings.mouse_leave_hide = True
        self.window.settings.opacity = 80
        self.window.apply_opacity(80, animate=False)
        pump()

    def tearDown(self) -> None:
        self.window.reader_menu.hide()
        pump()
        super().tearDown()

    def _leave(self, window) -> None:
        window.leaveEvent(QEvent(QEvent.Leave))

    def _open_menu(self) -> None:
        self.window.reader_menu.popup(QPoint(200, 200))
        pump()
        self.assertTrue(self.window.reader_menu.isVisible())

    def test_leaving_while_menu_is_open_does_not_change_opacity(self) -> None:
        self._open_menu()
        self._leave(self.window)
        # 用户要求的硬指标：200ms（两个防抖周期）后 opacity 原样不动。
        _settle(200)
        self.assertAlmostEqual(
            self.window.windowOpacity(), 0.8, places=2,
            msg="菜单弹出时移出窗口，主窗不许突然隐身",
        )
        self.assertFalse(
            self.window._leave_hide_timer.isActive(), "菜单期间根本不该有计时在跑"
        )

    def test_a_timer_started_before_the_menu_is_cancelled(self) -> None:
        # 菜单弹出前 40ms 鼠标已经移出（100ms 定时器在跑），弹菜单必须把它撤掉。
        self._leave(self.window)
        _settle(40)
        self.assertTrue(self.window._leave_hide_timer.isActive())
        self._open_menu()
        self.assertFalse(self.window._leave_hide_timer.isActive())
        _settle(200)
        self.assertAlmostEqual(self.window.windowOpacity(), 0.8, places=2)

    def test_hiding_resumes_the_debounce_when_cursor_is_outside(self) -> None:
        self._open_menu()
        self._leave(self.window)
        _settle(200)
        self.assertAlmostEqual(self.window.windowOpacity(), 0.8, places=2)
        # 关菜单：offscreen 光标恒在 (0,0)，窗口在 (100,100)，判为窗外→重新计时。
        self.window.reader_menu.hide()
        pump()
        self.assertFalse(self.window._context_menu_open)
        self.assertTrue(self.window._leave_hide_timer.isActive(), "关闭后恢复防抖")
        _settle(LEAVE_HIDE_DELAY_MS + 80)
        assert_at_leave_hide_floor(self, self.window, "恢复后到点照样隐身")

    def test_about_to_show_handler_is_wired_to_the_real_popup(self) -> None:
        # 不直接调内部方法：真 popup 一次，证明标志位由 QMenu 信号驱动。
        self.assertFalse(self.window._context_menu_open)
        self._open_menu()
        self.assertTrue(self.window._context_menu_open)
        self.window.reader_menu.hide()
        pump()
        self.assertFalse(self.window._context_menu_open)


# ---------------------------------------------------------------------------
# 👁 / 📌 tooltip 双态
# ---------------------------------------------------------------------------


class TestDualStateTooltips(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window = self.make_window()

    def test_eye_tooltip_flips_with_the_setting_in_real_time(self) -> None:
        window = self.window
        self.assertFalse(window.settings.mouse_leave_hide, "出厂默认关")
        self.assertEqual(window.boss_button.toolTip(), "开启隐身")
        window.boss_button.click()
        pump()
        self.assertTrue(window.settings.mouse_leave_hide)
        self.assertEqual(window.boss_button.toolTip(), "关闭隐身")
        window.boss_button.click()
        pump()
        self.assertEqual(window.boss_button.toolTip(), "开启隐身")

    def test_eye_tooltip_follows_the_settings_dialog_checkbox(self) -> None:
        window = self.window
        dialog = SettingsDialog(
            self.settings, None, on_leave_hide=window._sync_leave_hide_button
        )
        try:
            dialog.leave_hide_check.setChecked(True)
            self.assertEqual(window.boss_button.toolTip(), "关闭隐身")
            dialog.leave_hide_check.setChecked(False)
            self.assertEqual(window.boss_button.toolTip(), "开启隐身")
        finally:
            dialog.deleteLater()
            pump(2)
            _app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_pin_tooltip_flips_with_always_on_top(self) -> None:
        window = self.window
        # 出厂默认 always_on_top=True。
        self.assertTrue(window.settings.always_on_top)
        self.assertEqual(window.pin_button.toolTip(), "取消置顶")
        window.pin_button.click()
        pump()
        self.assertFalse(window.settings.always_on_top)
        self.assertEqual(window.pin_button.toolTip(), "置顶")
        window.pin_button.click()
        pump()
        self.assertEqual(window.pin_button.toolTip(), "取消置顶")


# ---------------------------------------------------------------------------
# 设置对话框：首行缩进滑块必须沿用阶段 1 的统一样式
# ---------------------------------------------------------------------------


class TestIndentSliderStyle(WindowTestCase):
    def test_indent_slider_uses_the_same_qss_as_every_other_control(self) -> None:
        dialog = SettingsDialog(self.settings, None)
        try:
            qss = dialog.styleSheet()
            # 全局选择器同时管字号（QLabel 13px）和滑块手柄（14px、-5px 垂直外扩命中区）。
            self.assertIn("font-size: 13px", qss)
            self.assertIn("QSlider::handle:horizontal", qss)
            self.assertIn("width: 14px", qss, "手柄 ≥14px")
            self.assertIn("QSlider::groove:horizontal", qss)

            slider = dialog.indent_slider
            self.assertEqual((slider.minimum(), slider.maximum()), (0, 8))
            # 滑块挂在对话框上、吃到同一条样式表（不是一个没样式的裸控件）。
            self.assertIn(slider, dialog.findChildren(type(slider)))
            self.assertEqual(dialog.indent_value.text(), "2 字符", "默认 2 字符且带单位")
        finally:
            dialog.deleteLater()
            pump(2)
            _app.sendPostedEvents(None, QEvent.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
