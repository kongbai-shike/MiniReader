"""第三阶段功能的端到端测试：目录抽屉、书签、上一/下一章、自动滚动、排版设置。

这些测试跑在 offscreen QPA 上，用的是**真正的窗口**，而不是把各模块分别测一遍：
前三份单测（test_toc_panel / test_store / test_autoscroll）证明每个零件自己是对的，
这里证明它们被接在一起之后仍然是对的——信号连错了、锚点丢了、按钮状态没同步，
都只有在这一层才看得见。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from minireader.qt import QApplication, QEvent, QLabel, Qt

from .test_ui_offscreen import (
    WindowTestCase,
    assert_at_leave_hide_floor,
    make_book,
)

from minireader.storage import Settings
from minireader.ui.settings_dialog import SettingsDialog
from minireader.ui.window import COMPAT_MIN_OPACITY

_app = QApplication.instance() or QApplication([])


def pump(times: int = 3) -> None:
    """把事件循环推几圈。

    `apply_style()` 和 `set_chapter()` 都是把"回到哪个字符"用 QTimer.singleShot(0)
    推迟到下一轮事件循环去做的（那样才能在一次绘制里完成，不会闪一下）。所以断言
    "落点对不对"之前必须把事件循环推过去，否则测的是移动之前的状态。
    """
    for _ in range(times):
        _app.processEvents()


def _settle(ms: int) -> None:
    """真实等 `ms` 毫秒，同时不停地推事件循环。

    防抖用的是真的 QTimer（100ms），它只有事件循环跑够时间才会 fire——`pump()` 只是
    立刻处理一轮待办，不会让时间前进。睡一小口、推一圈，是为了让"到点"真的发生。
    """
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        _app.processEvents()
        time.sleep(0.005)

# 够长，保证每一章都能滚动（否则"自动滚动"和"锚点"根本无从谈起）。
LONG = 400


def make_long_book(directory: Path, name: str = "long.txt", chapters: int = 6) -> Path:
    """每章的文字互不相同，且足够长到能滚。

    两个细节都是刻意的，第一版这里写错过：

    * 正文句子必须以「这是」开头。如果写成「第1章的正文…」，那一行的行首同样满足
      `^第N章` 的分章规则，于是正文被当成新章节的标题，切出来的是 12 个"标题章 + 空正文"
      的章节——正文一个字都没有，自动滚动自然也就没得滚。
    * "互不相同"是必须的：断言"跳到第 4 章"时得能确认正文真的换了，而不是停在同一段。
    """
    parts = []
    for i in range(1, chapters + 1):
        body = f"这是第{i}章独有的正文。"
        parts.append(f"第{i}章 标题{i}\n{body * LONG}")
    path = directory / name
    path.write_bytes("\n".join(parts).encode("utf-8"))
    return path


class ReadingFeatureCase(WindowTestCase):
    """给每个用例一本长书，并让窗口真的加载它。"""

    def setUp(self) -> None:
        super().setUp()
        self.long_path = make_long_book(self.tmp)
        self.window = self.make_window(path=self.long_path)

    # 章序号是 0 基的，所以第 i 章（0 基）对应文件名里写的"第 i+1 章"。测试里到处
    # 都要用到这个换算，写成函数比每次手算靠谱。
    @staticmethod
    def chapter_word(index: int) -> str:
        return f"第{index + 1}章"

    def show_bookmarks_tab(self) -> None:
        """切到书签页。

        `empty_visible()` 问的是"这块标签真的显示出来了吗"，而 QWidget.isVisible()
        在父控件（另一个标签页）没显示时一律是 False——所以不切页的话读到的是假的。
        """
        self.window.open_toc()
        self.window.toc_panel.tabs.setCurrentIndex(1)
        pump()

    def scroll_to_chapter_end(self, *, seconds: float = 1.0) -> None:
        """把正文推到章末，再推一拍收尾。

        必须推进假时钟：`_on_tick()` 的位移来自**真实经过的时间**，刚 start() 完就调
        它，elapsed 约等于 0，攒不够一个像素就会直接返回，"到底"也就永远不会被判定。
        """
        fake = _FakeClock()
        self.window.autoscroller._clock = fake
        self.window.toggle_autoscroll()
        bar = self.window.reader.verticalScrollBar()
        bar.setValue(bar.maximum())
        fake.advance(seconds)
        self.window.autoscroller._on_tick()
        pump()


class TestTocDrawer(ReadingFeatureCase):
    def test_the_drawer_starts_closed(self) -> None:
        self.assertFalse(self.window.toc_panel.isVisible())
        self.assertFalse(self.window.toc_button.isChecked())

    def test_opening_lists_every_chapter_and_highlights_the_current_one(self) -> None:
        self.window.open_toc()
        pump()
        self.assertTrue(self.window.toc_panel.isVisible())
        self.assertTrue(self.window.toc_button.isChecked())
        self.assertEqual(self.window.toc_panel.visible_chapter_count(), self.window.book.chapter_count)
        self.assertEqual(self.window.toc_panel.model.current_chapter(), 0)
        # 关掉抽屉不能让按钮一直亮着——那是"我明明关了它还显示开着"的经典 bug。
        self.window.close_toc()
        pump()
        self.assertFalse(self.window.toc_panel.isVisible())
        self.assertFalse(self.window.toc_button.isChecked())

    def test_toggle_is_symmetric(self) -> None:
        self.window.toggle_toc()
        self.assertTrue(self.window.toc_panel.isVisible())
        self.window.toggle_toc()
        self.assertFalse(self.window.toc_panel.isVisible())

    def test_the_drawer_never_dims_while_it_is_open(self) -> None:
        """失焦变淡对正在看目录的人来说是反效果，所以打开时要主动清掉这个提示位。"""
        self.window.toc_panel.set_opacity_hint(True)
        self.window.open_toc()
        self.assertFalse(self.window.toc_panel.opacity_dimmed())

    def test_choosing_a_chapter_jumps_there_and_closes_the_drawer(self) -> None:
        self.window.open_toc()
        self.window._on_toc_chapter(4)
        pump()
        self.assertEqual(self.window.chapter_index, 4)
        self.assertIn(self.chapter_word(4), self.window.reader.toPlainText())
        self.assertFalse(self.window.toc_panel.isVisible())

    def test_the_highlight_follows_the_reader(self) -> None:
        """翻到第 2 章再开目录，高亮必须已经在第 2 章上。"""
        self.window.goto_chapter(2)
        self.window.open_toc()
        self.assertEqual(self.window.toc_panel.model.current_chapter(), 2)

    def test_flipping_a_chapter_while_open_moves_the_highlight(self) -> None:
        self.window.open_toc()
        self.window.step_chapter(1)
        self.assertEqual(self.window.toc_panel.model.current_chapter(), 1)

    def test_opening_another_book_replaces_the_chapter_list(self) -> None:
        self.window.open_toc()
        first = self.window.toc_panel.visible_chapter_count()
        other = make_book(self.tmp, name="other.txt", chapters=2)
        self.assertTrue(self.window.load_path(other))
        pump()
        self.assertNotEqual(first, self.window.toc_panel.visible_chapter_count())
        self.assertEqual(self.window.toc_panel.visible_chapter_count(), 2)

    def test_the_drawer_eats_no_width_while_closed(self) -> None:
        """抽屉是并排布局里的一个兄弟控件，关着的时候不能占正文的地方。"""
        closed_width = self.window.reader.width()
        self.window.open_toc()
        pump()
        self.assertLess(self.window.reader.width(), closed_width)
        self.window.close_toc()
        pump()
        self.assertEqual(self.window.reader.width(), closed_width)

    def test_an_empty_book_still_opens_the_drawer(self) -> None:
        empty = self.tmp / "empty.txt"
        empty.write_bytes("没有章节标题的一段文字。".encode("utf-8"))
        self.assertTrue(self.window.load_path(empty))
        self.window.open_toc()
        self.assertTrue(self.window.toc_panel.isVisible())


class TestChapterNavButtons(ReadingFeatureCase):
    def test_prev_is_disabled_on_the_first_chapter(self) -> None:
        self.assertEqual(self.window.chapter_index, 0)
        self.assertFalse(self.window.prev_button.isEnabled())
        self.assertTrue(self.window.next_button.isEnabled())

    def test_next_is_disabled_on_the_last_chapter(self) -> None:
        self.window.goto_chapter(self.window.book.chapter_count - 1)
        self.assertFalse(self.window.next_button.isEnabled())
        self.assertTrue(self.window.prev_button.isEnabled())

    def test_clicking_next_advances_one_chapter(self) -> None:
        self.window.next_button.click()
        pump()
        self.assertEqual(self.window.chapter_index, 1)
        self.assertIn("第2章", self.window.reader.toPlainText())

    def test_clicking_prev_goes_back_one_chapter(self) -> None:
        self.window.goto_chapter(3)
        self.window.prev_button.click()
        pump()
        self.assertEqual(self.window.chapter_index, 2)

    def test_the_buttons_are_large_enough_to_hit(self) -> None:
        """点击目标不能小于可用性下限，手抖的用户和触屏都需要。

        24px 是触屏的推荐值，但这套界面是鼠标驱动的桌面程序，标题栏又要尽量薄，
        所以取 22px 作为硬下限——比 Qt 在 offscreen 下算出来的 19px 大，也不至于让
        标题栏明显变高。
        """
        for button in (self.window.prev_button, self.window.next_button,
                       self.window.toc_button, self.window.bookmark_button,
                       self.window.autoscroll_button):
            self.assertGreaterEqual(button.minimumWidth(), 22)
            self.assertGreaterEqual(button.minimumHeight(), 22)


class TestBookmarks(ReadingFeatureCase):
    def test_the_store_lives_inside_the_sandbox(self) -> None:
        """构造窗口时不能碰到用户真实的 %APPDATA%\\MiniReader\\reading.db。

        库路径是私有的，但这条断言正是"别污染真实配置"的守门人，值得直接看它。
        """
        self.assertTrue(str(self.window.store._db_path).startswith(str(self.tmp)))
        self.assertTrue(self.window.store.persistent)

    def test_a_bookmark_remembers_the_chapter_and_the_character(self) -> None:
        self.window.goto_chapter(2)
        expected = self.window.reader.first_visible_char()
        self.window.add_bookmark()
        items = self.window.store.list_for(self.window.book.id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].chapter, 2)
        self.assertEqual(items[0].offset, expected)
        self.assertEqual(items[0].label, "第3章 标题3")

    def test_the_excerpt_matches_what_is_on_screen(self) -> None:
        """"摘录"必须真的来自正文，否则书签列表里会出现一句书里没有的话。"""
        self.window.goto_chapter(1)
        pump()
        self.window.add_bookmark()
        item = self.window.store.list_for(self.window.book.id)[0]
        self.assertTrue(item.excerpt.startswith(f"这是{self.chapter_word(1)}独有的正文"))
        self.assertNotIn("\n", item.excerpt)
        body = self.window.book.chapter_body(1)
        for char in item.excerpt.rstrip("…"):
            self.assertIn(char, body)

    def test_the_excerpt_is_capped_and_ellipsised(self) -> None:
        self.window.add_bookmark()
        item = self.window.store.list_for(self.window.book.id)[0]
        self.assertLessEqual(len(item.excerpt), 60)
        self.assertTrue(item.excerpt.endswith("…"))

    def test_the_panel_shows_the_bookmark(self) -> None:
        self.window.open_toc()
        self.window.add_bookmark()
        pump()
        self.assertEqual(self.window.toc_panel.bookmark_count(), 1)
        self.assertFalse(self.window.toc_panel.empty_visible())
        row = self.window.toc_panel.bookmark_rows()[0]
        self.assertEqual(row.label.text(), "第1章 标题1")
        self.assertTrue(row.excerpt.text())

    def test_jumping_back_lands_on_the_same_text(self) -> None:
        """核心承诺：书签跳回去要回到"那句话"，而不是"那一像素"。

        存的是章内字符位置，所以换过字号之后再跳也必须准——如果实现改回存像素，
        这个断言就会在"先滚动一段再跳回"这条路径上失败。
        """
        self.window.goto_chapter(3)
        self.window.reader.scroll_to_char(2200)
        pump()
        anchored = self.window.reader.first_visible_char()
        self.assertGreater(anchored, 1000)          # 确认真的滚下去了
        self.window.add_bookmark()
        bookmark = self.window.store.list_for(self.window.book.id)[0]

        # 跑到别的地方、换个字号，再跳回来。
        self.window.goto_chapter(0)
        self.settings.font_size = 24
        self.window.apply_preferences()
        pump()
        self.window._on_bookmark_chosen(bookmark.id)
        pump()

        self.assertEqual(self.window.chapter_index, 3)
        landed = self.window.reader.first_visible_char()
        # 只能保证到"行"：锚点行本身会随字号重新折行，行首因此可能挪动不到一行。
        self.assertLessEqual(abs(landed - anchored), 40)
        self.assertIn(f"这是{self.chapter_word(3)}独有的正文", self.window.reader.toPlainText())

    def test_deleting_removes_it_from_disk_and_from_the_panel(self) -> None:
        self.show_bookmarks_tab()
        self.window.add_bookmark()
        bookmark = self.window.store.list_for(self.window.book.id)[0]
        self.window._on_bookmark_deleted(bookmark.id)
        pump()
        self.assertEqual(self.window.store.list_for(self.window.book.id), [])
        self.assertEqual(self.window.toc_panel.bookmark_count(), 0)
        self.assertTrue(self.window.toc_panel.empty_visible())

    def test_the_delete_button_on_the_row_works(self) -> None:
        self.window.open_toc()
        self.window.add_bookmark()
        row = self.window.toc_panel.bookmark_rows()[0]
        row.delete_button.click()
        pump()
        self.assertEqual(self.window.store.count(self.window.book.id), 0)

    def test_clicking_a_row_is_wired_to_the_jump(self) -> None:
        self.window.open_toc()
        self.window.add_bookmark()
        row = self.window.toc_panel.bookmark_rows()[0]
        row.chosen.emit(row.chosen_id)
        pump()
        self.assertEqual(self.window.chapter_index, 0)

    def test_deleting_something_already_gone_does_not_explode(self) -> None:
        self.window._on_bookmark_deleted(999999)
        self.assertIn("已经不在", self.window.warn_label.text())

    def test_bookmarks_are_kept_per_book(self) -> None:
        self.window.add_bookmark()
        other = make_long_book(self.tmp, name="second.txt")
        self.assertTrue(self.window.load_path(other))
        pump()
        self.assertEqual(self.window.store.count(), 1)          # 总数
        self.assertEqual(self.window.store.list_for(self.window.book.id), [])
        self.assertEqual(self.window.toc_panel.bookmark_count(), 0)
        self.window.add_bookmark()
        self.assertEqual(self.window.store.count(), 2)
        self.assertEqual(self.window.store.count(self.window.book.id), 1)

    def test_adding_a_bookmark_without_a_book_warns_instead_of_crashing(self) -> None:
        bare = self.make_window()          # 没有任何书
        bare.add_bookmark()
        self.assertIn("先打开一本书", bare.warn_label.text())
        self.assertEqual(bare.store.count(), 0)

    def test_bookmarks_survive_a_new_window(self) -> None:
        """数据库在磁盘上，重开一个窗口必须还能看见之前存的书签。"""
        self.window.add_bookmark()
        saved = self.window.store.list_for(self.window.book.id)
        self.window.close()
        pump()
        second = self.make_window(path=self.long_path)
        again = second.store.list_for(second.book.id)
        self.assertEqual([b.id for b in again], [b.id for b in saved])
        self.assertEqual(again[0].offset, saved[0].offset)

    def test_reloading_the_panel_does_not_leak_rows(self) -> None:
        """反复刷新不能把行控件越堆越多——每次 set_bookmarks 都要清掉旧的。"""
        self.window.open_toc()
        for _ in range(4):
            self.window.add_bookmark()
        self.assertEqual(self.window.toc_panel.bookmark_count(), 4)
        self.window.reload_bookmarks()
        self.window.reload_bookmarks()
        self.assertEqual(self.window.toc_panel.bookmark_count(), 4)


class TestAutoScroll(ReadingFeatureCase):
    def test_toggling_starts_and_the_button_shows_pause(self) -> None:
        self.window.toggle_autoscroll()
        self.assertTrue(self.window.autoscroller.is_running())
        self.assertTrue(self.window.autoscroll_button.isChecked())
        self.assertEqual(self.window.autoscroll_button.text(), "⏸")

    def test_toggling_again_stops_and_restores_the_button(self) -> None:
        self.window.toggle_autoscroll()
        self.window.toggle_autoscroll()
        self.assertFalse(self.window.autoscroller.is_running())
        self.assertFalse(self.window.autoscroll_button.isChecked())
        self.assertEqual(self.window.autoscroll_button.text(), "⏵")

    def test_it_actually_moves_the_text(self) -> None:
        """把时钟换成一个可以手推的假表，然后推进 2 秒。

        用真定时器就必须 sleep，测试会变慢又容易在负载高的时候抖；AutoScroller 的
        clock 参数注入就是为了这个（见它的 docstring）。
        """
        fake = _FakeClock()
        self.window.autoscroller._clock = fake
        self.window.autoscroller.set_speed(100)
        self.window.toggle_autoscroll()
        bar = self.window.reader.verticalScrollBar()
        start = bar.value()
        fake.advance(2.0)
        self.window.autoscroller._on_tick()
        self.assertAlmostEqual(bar.value() - start, 200, delta=2)

    def test_the_speed_comes_from_the_settings(self) -> None:
        self.settings.autoscroll_speed = 250
        self.window.apply_preferences()
        self.assertEqual(self.window.autoscroller.speed(), 250)

    def test_a_pixel_speed_is_kept_when_the_font_changes(self) -> None:
        """速度是按像素/秒存的，所以换字号之后视觉速度不变。

        如果当初把速度存成"每 N 毫秒滚一行"，字号从 17 调到 34 就会凭空快一倍。
        """
        before = self.window.autoscroller.speed()
        self.settings.font_size = 34
        self.window.apply_preferences()
        self.assertEqual(self.window.autoscroller.speed(), before)

    def test_hovering_the_text_pauses_and_leaving_resumes(self) -> None:
        self.window.toggle_autoscroll()
        self.assertTrue(self.window.autoscroller.is_running())
        viewport = self.window.reader.viewport()
        # 真的 QEvent，不是替身：eventFilter 最后会调 super().eventFilter()，
        # PyQt 对非 QEvent 参数会直接抛 TypeError。
        self.window.eventFilter(viewport, QEvent(QEvent.Enter))
        self.assertTrue(self.window.autoscroller.is_paused())
        self.assertTrue(self.window.autoscroller.is_running())   # 暂停≠停止
        self.window.eventFilter(viewport, QEvent(QEvent.Leave))
        self.assertFalse(self.window.autoscroller.is_paused())

    def test_hover_pause_can_be_turned_off(self) -> None:
        self.settings.autoscroll_pause_on_hover = False
        self.window.apply_preferences()
        self.window.toggle_autoscroll()
        self.window.eventFilter(self.window.reader.viewport(), QEvent(QEvent.Enter))
        self.assertFalse(self.window.autoscroller.is_paused())

    def test_reaching_the_end_of_a_chapter_moves_to_the_next_one(self) -> None:
        self.scroll_to_chapter_end()
        self.assertEqual(self.window.chapter_index, 1)
        self.assertIn(f"这是{self.chapter_word(1)}独有的正文", self.window.reader.toPlainText())
        # 换章之后必须继续滚，而不是悄悄停了。
        self.assertTrue(self.window.autoscroller.is_running())
        self.window.autoscroller.stop()

    def test_the_end_of_the_last_chapter_stops_and_says_so(self) -> None:
        self.window.goto_chapter(self.window.book.chapter_count - 1)
        pump()
        self.scroll_to_chapter_end()
        self.assertFalse(self.window.autoscroller.is_running())
        self.assertIn("最后一章", self.window.warn_label.text())

    def test_a_chapter_that_fits_on_one_screen_does_not_cascade(self) -> None:
        """短到不用滚的章节不能让滚动器一章一章地空转下去。"""
        tiny = self.tmp / "tiny.txt"
        tiny.write_bytes("第1章 短\n很短。\n第2章 也短\n也一样短。\n".encode("utf-8"))
        self.assertTrue(self.window.load_path(tiny))
        pump()
        self.scroll_to_chapter_end()
        self.assertLessEqual(self.window.chapter_index, 1)
        self.assertFalse(self.window.autoscroller.is_running())

    def test_starting_at_the_bottom_of_a_chapter_advances_instead_of_doing_nothing(self) -> None:
        self.window.goto_chapter(0)
        pump()
        self.scroll_to_chapter_end()
        self.assertEqual(self.window.chapter_index, 1)
        self.window.autoscroller.stop()

    def test_closing_the_window_stops_the_timer(self) -> None:
        """关窗后还留着 40ms 的定时器，会在解释器退出时对着已删除的滚动条开火。"""
        self.window.toggle_autoscroll()
        self.assertTrue(self.window.autoscroller.is_running())
        self.window.close()
        pump()
        self.assertFalse(self.window.autoscroller.is_running())


class TestTypographySettings(ReadingFeatureCase):
    def test_the_font_size_reaches_the_reader(self) -> None:
        before = self.window.reader.font().pixelSize()
        self.settings.font_size = 30
        self.window.apply_preferences()
        self.assertGreater(self.window.reader.font().pixelSize(), before)

    def test_the_colours_reach_the_reader(self) -> None:
        """正文控件本身透明；颜色分别落到字色和窗口底色的 paintEvent 输入上。

        背景归顶层 paintEvent 画（无边框 + WA_TranslucentBackground），所以不再断言
        正文控件里带底色——如果那里带底色，alpha=0 时就会挡住桌面。
        """
        self.settings.bg = "#f5efe0"
        self.settings.fg = "#3a352c"
        self.window.apply_preferences()
        self.assertIn("#3a352c", self.window.reader.styleSheet())
        self.assertIn("background: transparent", self.window.reader.styleSheet())
        self.assertEqual(self.window.bg_color.name(), "#f5efe0")

    def test_the_line_height_reaches_the_reader(self) -> None:
        """行距是通过 QTextBlockFormat 的 ProportionalHeight 实现的，只能这样验。"""
        self.settings.line_height = 260
        self.window.apply_preferences()
        pump()
        fmt = self.window.reader.textCursor().blockFormat()
        self.assertAlmostEqual(fmt.lineHeight(), 260, delta=1)

    def test_the_margin_reaches_the_reader(self) -> None:
        self.settings.margin = 48
        self.window.apply_preferences()
        # 页边距是文档边距（QTextDocument.setDocumentMargin），不是控件的 viewportMargins。
        self.assertEqual(self.window.reader.document().documentMargin(), 48)

    def test_changing_the_font_keeps_the_reader_on_the_same_text(self) -> None:
        """换字号最让人恼火的失败就是"读者被弹回本章开头"。

        阶段 2 起每个段落首行缩进 2 字符：首行折行位置因此变 2 个字，配合
        first_visible_char "精度到行"的量化，落点最多多偏一行（17px 下约 40 字），
        所以阈值放到 60 字（≈3 行）；它仍然钉住"没被弹到别的段落"，而对照实验里
        像素偏移动辄上千字。
        """
        self.window.reader.scroll_to_char(1500)
        pump()
        anchored = self.window.reader.first_visible_char()
        self.assertGreater(anchored, 500)
        for size in (22, 12, 17):
            self.settings.font_size = size
            self.window.apply_preferences()
            pump()
            landed = self.window.reader.first_visible_char()
            self.assertLess(abs(landed - anchored), 60, f"字号 {size} 时漂到了 {landed}")

    def test_character_anchoring_beats_restoring_the_pixel_offset(self) -> None:
        """把"存字符"和"存像素"放在同一个位置上比一次。

        这是"书签为什么存字符偏移而不是滚动条像素"的守门人。真实 Windows 平台实测
        （520x420、本章 3,759 字、锚点 2983）：字符锚点在 12/22/24/30 号字下漂移都是
        **0 字**；同一操作改成按像素恢复，会漂到 -1305 / -873 / +669 / -1730 字——读者
        直接跑到别的段落去了。如果实现被改回存像素，这条断言会立刻失败。
        """
        target = 3000
        original = self.settings.font_size
        self.addCleanup(setattr, self.settings, "font_size", original)
        for size in (24, 12):
            self.settings.font_size = 17
            self.window.reader.apply_style(self.settings)
            pump()
            self.window.reader.scroll_to_char(target)
            pump()
            base = self.window.reader.first_visible_char()
            pixel = self.window.reader.verticalScrollBar().value()

            # 字符锚点：换字号后回到同一个字符
            self.settings.font_size = size
            self.window.reader.apply_style(self.settings)
            pump()
            char_landed = self.window.reader.first_visible_char()
            self.assertLess(abs(char_landed - base), 40, f"字号 {size}：字符锚点漂了")

            # 对照：换字号后回到同一个像素偏移
            self.settings.font_size = 17
            self.window.reader.apply_style(self.settings)
            pump()
            self.window.reader.scroll_to_char(target)
            pump()
            keep_pixel = self.window.reader.verticalScrollBar().value()
            self.settings.font_size = size
            self.window.reader.apply_style(self.settings)
            pump()
            bar = self.window.reader.verticalScrollBar()
            bar.setValue(min(keep_pixel, bar.maximum()))
            pump()
            pixel_landed = self.window.reader.first_visible_char()
            self.assertGreater(
                abs(pixel_landed - base), 100,
                f"字号 {size}：按像素恢复居然没漂，那这条对照就没有说服力了",
            )

    def test_the_dialog_round_trips_through_the_window(self) -> None:
        """对话框 → Settings → 窗口，这条链路必须真的通。"""
        from minireader.ui.settings_dialog import READ_PRESETS, SettingsDialog

        name, values = READ_PRESETS[1]          # 护眼（米黄）
        dialog = SettingsDialog(self.window.settings, self.window)
        try:
            dialog._apply_preset(values)
            produced = dialog.settings()
            for key, value in values.items():
                self.assertEqual(getattr(produced, key), value, f"{name} 的 {key} 没写进去")
        finally:
            dialog.deleteLater()

    def test_the_dialog_puts_the_typography_into_the_real_settings(self) -> None:
        """按下确定时 apply_to 必须真的写进窗口手里那个 Settings 对象。"""
        from minireader.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.window.settings, self.window)
        try:
            dialog.size_slider.setValue(33)
            dialog.line_slider.setValue(210)
            dialog.margin_slider.setValue(30)
            dialog.apply_to(self.window.settings)
            self.window.apply_preferences()
            self.assertEqual(self.window.settings.font_size, 33)
            self.assertEqual(self.window.reader.font().pixelSize(), 33)
        finally:
            dialog.deleteLater()

    def test_the_built_in_presets_all_clear_the_contrast_bar(self) -> None:
        """预设是"一键回到能看的状态"，所以预设本身不能是低对比的。"""
        from minireader.ui.settings_dialog import READ_PRESETS
        from minireader.ui.theme import AA_NORMAL, contrast_ratio

        for name, values in READ_PRESETS:
            ratio = contrast_ratio(values["bg"], values["fg"])
            self.assertGreaterEqual(ratio, AA_NORMAL, f"预设 {name} 的对比度只有 {ratio:.1f}:1")

    def test_cancelling_the_dialog_undoes_the_typography_preview(self) -> None:
        original = self.window.reader.font().pixelSize()
        preview = type(self.settings)(**self.settings.__dict__)
        preview.font_size = 40
        self.window._preview_settings(preview)
        self.assertNotEqual(self.window.reader.font().pixelSize(), original)
        # 取消 = 把真设置重新贴一遍
        self.window.reader.apply_style(self.settings)
        self.assertEqual(self.window.reader.font().pixelSize(), original)

    def test_typography_preview_is_live_and_cancel_rolls_back_end_to_end(self) -> None:
        """美化不能动到这条链路：真实 SettingsDialog（open_settings 亲手建的那个）里
        拖字号 -> 真窗口正文立刻变，不等"确定"、不写设置；取消 -> open_settings 的
        回滚分支把正文和设置都恢复成打开前。"""
        from minireader.ui import window as window_module

        reader_size_before = self.window.reader.font().pixelSize()
        setting_size_before = self.settings.font_size
        chosen = 40 if setting_size_before != 40 else 36
        seen_during: dict = {}

        def fake_exec(dialog):
            # 真人拖字号滑杆：valueChanged -> _on_typography -> _preview ->
            # window._preview_settings -> reader.apply_style。
            dialog.size_slider.setValue(chosen)
            pump()
            seen_during["reader_size"] = self.window.reader.font().pixelSize()
            seen_during["setting_size"] = self.settings.font_size
            return False  # 点"取消"

        real_exec = window_module.exec_dialog
        window_module.exec_dialog = fake_exec
        try:
            self.window.open_settings()
        finally:
            window_module.exec_dialog = real_exec
        pump()

        self.assertEqual(
            seen_during.get("reader_size"), chosen, "对话框开着时正文就必须实时变"
        )
        self.assertEqual(
            seen_during.get("setting_size"), setting_size_before,
            "实时预览不许回写 Settings 对象",
        )
        self.assertEqual(
            self.window.reader.font().pixelSize(), reader_size_before,
            "取消后正文回到打开对话框前的字号",
        )
        self.assertEqual(
            self.settings.font_size, setting_size_before,
            "取消后设置对象保持原值（确定才写盘）",
        )


class TestTranslucentFallback(ReadingFeatureCase):
    """translucent_bg 开关：半透明路径 vs 兼容模式，以及切换时几何/阅读进度不丢。

    paintEvent 是绘制行为，offscreen 看不到像素，所以这里用替身 QPainter 记录它到底
    调了 fillPath（半透明，画圆角）还是 fillRect（兼容模式，铺纯色）——断言绘制路径，
    而不是断言画面。
    """

    def _paint_calls(self) -> list:
        """跑一遍 paintEvent，返回它实际调用的绘制方法名（fillPath / fillRect）。"""
        from minireader.ui import window as window_module

        calls: list[str] = []

        class SpyPainter:
            # paintEvent 会读 QPainter.Antialiasing，替身得提供同一个常量名。
            Antialiasing = 1

            def __init__(self, *_a, **_k):
                pass

            def setRenderHint(self, *_a, **_k):
                pass

            def fillPath(self, *_a, **_k):
                calls.append("fillPath")

            def fillRect(self, *_a, **_k):
                calls.append("fillRect")

        original = window_module.QPainter
        window_module.QPainter = SpyPainter
        try:
            self.window.paintEvent(None)
        finally:
            window_module.QPainter = original
        return calls

    def test_translucent_mode_fills_rounded_path_with_bg_alpha(self) -> None:
        """translucent_bg=True：paintEvent 走 fillPath，底色 alpha 跟着 bg_alpha。"""
        self.settings.translucent_bg = True
        self.settings.bg_alpha = 90
        self.window.apply_background()
        self.assertTrue(self.window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual(self.window.bg_color.alpha(), 90)
        self.assertEqual(self._paint_calls(), ["fillPath"])

    def test_compat_mode_skips_fillpath_and_uses_window_opacity(self) -> None:
        """translucent_bg=False：不 fillPath，改铺纯色，透明交给 setWindowOpacity。"""
        self.settings.translucent_bg = False
        self.settings.bg_alpha = 128
        self.window._sync_translucent_mode()
        self.assertFalse(self.window.testAttribute(Qt.WA_TranslucentBackground))
        # 兼容模式不画圆角路径
        self.assertEqual(self._paint_calls(), ["fillRect"])
        # 同一根"背景透明度"滑块：128/255 ≈ 50% 整窗不透明度
        self.window.apply_opacity(100, animate=False)
        self.assertAlmostEqual(self.window.windowOpacity(), 128 / 255, places=2)
        # 拖到 0：这条路径没有逐像素 alpha，只能整窗变淡，真设 0 会把文字一起抹掉、
        # 窗口变成找不回来的空壳，所以留 COMPAT_MIN_OPACITY 的下限。
        self.window.preview_bg_alpha(0)
        self.assertAlmostEqual(self.window.windowOpacity(), COMPAT_MIN_OPACITY / 100.0, places=2)
        # 拖回 255 = 完全不透明
        self.window.preview_bg_alpha(255)
        self.assertAlmostEqual(self.window.windowOpacity(), 1.0, places=2)

    def test_compat_checkbox_maps_inversely_to_translucent_bg(self) -> None:
        """复选框是"兼容模式"，和 translucent_bg 的极性相反——这条就是防写反的。

        UI 措辞与字段语义相反，是最容易被无意识改回去的地方：把 setChecked 写成
        `s.translucent_bg`、或把 apply_to 写成 `= isChecked()`，界面看上去都"正常"，
        但勾选和字段的含义正好反过来。所以勾选 / 取消 / 恢复默认三条路径各钉一条。
        """
        dialog = SettingsDialog(self.settings, None)
        try:
            dialog.compat_check.setChecked(True)
            self.assertFalse(dialog.apply_to(Settings(**self.settings.__dict__)).translucent_bg)

            dialog.compat_check.setChecked(False)
            self.assertTrue(dialog.apply_to(Settings(**self.settings.__dict__)).translucent_bg)

            # 恢复默认后：出厂 translucent_bg=True → 复选框应为未勾选
            dialog.compat_check.setChecked(True)
            dialog._restore_defaults()
            self.assertEqual(dialog.compat_check.isChecked(), not Settings().translucent_bg)
            self.assertFalse(dialog.compat_check.isChecked())
        finally:
            dialog.deleteLater()

    def test_switching_mode_keeps_geometry_and_reading_position(self) -> None:
        """切换 translucent_bg 会重建原生窗口，位置/尺寸/阅读进度必须保持不变。"""
        reader = self.window.reader
        self.window.setGeometry(120, 90, 500, 380)
        reader.verticalScrollBar().setValue(4000)
        pump()
        anchor = reader.first_visible_char()
        self.assertGreater(anchor, 0, "先得真的滚下去，锚点才有意义")

        self.settings.translucent_bg = False
        self.window.apply_preferences()      # 会触发 _sync_translucent_mode
        pump()
        self.assertFalse(self.window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual(
            (self.window.x(), self.window.y(), self.window.width(), self.window.height()),
            (120, 90, 500, 380),
        )
        self.assertEqual(reader.first_visible_char(), anchor)

        # 切回半透明模式同样不丢
        self.settings.translucent_bg = True
        self.window.apply_preferences()
        pump()
        self.assertTrue(self.window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual(
            (self.window.x(), self.window.y(), self.window.width(), self.window.height()),
            (120, 90, 500, 380),
        )
        self.assertEqual(reader.first_visible_char(), anchor)


class TestMiniLayoutAndOpacityAxes(ReadingFeatureCase):
    """仿 QQ 迷你窗的顶栏/底栏，以及"背景 / 内容"两个独立透明度维度。

    这些用例基本是**接线**测试：按钮存在但连错槽，界面上看起来一模一样，只有点下去
    才知道。另一半钉的是两个维度互不串味——这正是拆开它们的全部意义。
    """

    def test_the_top_bar_controls_are_wired_to_their_slots(self) -> None:
        window = self.window
        window.set_boss_hint(True, self.settings.boss_key)

        # 👁 现在不是老板键，而是"鼠标移出隐身"的开关：checkable，点一下写设置并落盘。
        before = bool(window.settings.mouse_leave_hide)
        window.boss_button.click()                  # 👁 移出隐身开关
        pump()
        self.assertEqual(bool(window.settings.mouse_leave_hide), not before)
        self.assertEqual(window.boss_button.isChecked(), not before, "按下状态要跟着走")
        self.assertFalse(window.boss_hidden, "👁 不再走老板键那条路")
        window.boss_button.click()
        pump()
        self.assertEqual(bool(window.settings.mouse_leave_hide), before)

        before = bool(window.settings.always_on_top)
        window.pin_button.click()                   # 📌 置顶
        pump()
        self.assertEqual(bool(window.settings.always_on_top), not before)
        self.assertEqual(window.pin_button.isChecked(), not before, "按下状态要跟着走")
        window.pin_button.click()
        pump()
        self.assertEqual(bool(window.settings.always_on_top), before)

        window.alpha_button.click()                 # 🎚 透明度浮层
        pump()
        self.assertIsNotNone(window._opacity_popup)
        self.assertTrue(window._opacity_popup.isVisible())
        window._opacity_popup.hide()
        pump()
        self.assertFalse(window._opacity_popup.isVisible())

        window.minimize_button.click()              # — 最小化
        pump()
        self.assertTrue(window.isMinimized())
        window.showNormal()
        pump()

        # × 直接接 close()；常驻托盘模型下它只隐藏不退出，真正的退出走托盘菜单的
        # 退出（window.quit_app），那部分已由托盘那组用例覆盖，这里只钉住它没接错槽。
        self.assertEqual(window.close_button.text(), "×")

    def test_the_bottom_bar_has_the_toc_and_settings_buttons(self) -> None:
        window = self.window

        window.toc_button.click()                   # ☰ 章节目录
        pump()
        self.assertTrue(window.toc_panel.isVisible())
        window.toc_button.click()
        pump()
        self.assertFalse(window.toc_panel.isVisible())

        # ⚙ 走的是 open_settings。真弹一个模态对话框会把事件循环卡住，所以把
        # SettingsDialog 换成一个立刻"取消"的替身——点下去仍然经过真实的按钮→槽路径。
        from minireader.ui import window as window_module

        opened = []

        class FakeDialog:
            def __init__(self, settings, parent=None, **callbacks):
                opened.append(callbacks)

            def setWindowFlag(self, *_a, **_k):
                pass

            def exec_(self):
                return 0                            # 取消

            def apply_to(self, s):
                raise AssertionError("取消时不该写设置")

            def deleteLater(self):
                pass

        original = window_module.SettingsDialog
        window_module.SettingsDialog = FakeDialog
        try:
            window.settings_button.click()
            pump()
        finally:
            window_module.SettingsDialog = original
        self.assertEqual(len(opened), 1, "⚙ 必须打开设置对话框")
        self.assertTrue(callable(opened[0].get("on_background")), "背景滑块要有预览回调")
        self.assertTrue(callable(opened[0].get("on_content")), "内容滑块要有预览回调")
        self.assertTrue(callable(opened[0].get("on_leave_hide")), "移出隐身复选框要能同步 👁")

    def test_the_bottom_right_labels_track_the_chapter_and_progress(self) -> None:
        window = self.window
        self.assertEqual(window.chapter_label.text(), "第 1/6 章")
        self.assertIn("%", window.progress_label.text())
        window.goto_chapter(2)
        pump()
        self.assertEqual(window.chapter_label.text(), "第 3/6 章")

    def test_the_two_sliders_span_zero_to_a_hundred(self) -> None:
        dialog = SettingsDialog(self.settings, None)
        try:
            for name in ("bg_alpha_slider", "content_slider"):
                slider = getattr(dialog, name)
                self.assertEqual((slider.minimum(), slider.maximum()), (0, 100), name)
        finally:
            dialog.deleteLater()

    def test_content_alpha_reaches_the_reader_effect_only(self) -> None:
        self.settings.translucent_bg = True
        self.settings.content_alpha = 128
        self.window.apply_content_alpha()
        self.assertAlmostEqual(self.window.reader.content_opacity(), 128 / 255, places=2)
        # 满不透明时把 effect 摘掉：graphics effect 会让正文走离屏合成，白白丢掉 ClearType。
        self.settings.content_alpha = 255
        self.window.apply_content_alpha()
        self.assertEqual(self.window.reader.content_opacity(), 1.0)
        self.assertIsNone(self.window.reader.graphicsEffect())

    def test_background_and_content_alpha_do_not_touch_each_other(self) -> None:
        self.settings.translucent_bg = True
        self.settings.content_alpha = 128
        self.window.apply_content_alpha()
        self.settings.bg_alpha = 255
        self.window.apply_background()
        effect_before = self.window.reader.content_opacity()

        # 改背景 alpha：正文 effect 一动不动
        self.settings.bg_alpha = 40
        self.window.apply_background()
        self.assertEqual(self.window.bg_color.alpha(), 40)
        self.assertEqual(self.window.reader.content_opacity(), effect_before)

        # 改内容 alpha：窗口底色一动不动
        self.settings.content_alpha = 200
        self.window.apply_content_alpha()
        self.assertEqual(self.window.bg_color.alpha(), 40, "内容透明度不能碰背景")
        self.assertEqual(self.settings.bg_alpha, 40)
        self.assertAlmostEqual(self.window.reader.content_opacity(), 200 / 255, places=2)

    def test_the_content_slider_is_disabled_in_compat_mode(self) -> None:
        self.settings.translucent_bg = True
        dialog = SettingsDialog(self.settings, None)
        try:
            self.assertTrue(dialog.content_slider.isEnabled())
            dialog.compat_check.setChecked(True)
            self.assertFalse(dialog.content_slider.isEnabled(), "兼容模式做不到分离")
            self.assertIn("无法单独调节", dialog.content_note.text())
            dialog.compat_check.setChecked(False)
            self.assertTrue(dialog.content_slider.isEnabled())
        finally:
            dialog.deleteLater()

    def test_alpha_zero_leaves_the_text_on_the_desktop(self) -> None:
        """半透明模式 alpha=0：背景全透、文字清晰，窗口仍然在、还能操作。"""
        self.settings.translucent_bg = True
        self.settings.bg_alpha = 0
        self.settings.content_alpha = 255
        self.window.apply_background()
        self.window.apply_content_alpha()
        self.window.preview_bg_alpha(0)
        pump()
        self.assertEqual(self.window.bg_color.alpha(), 0)
        self.assertTrue(self.window.isVisible(), "全透的是背景，不是窗口")
        self.assertIn("这是", self.window.reader.toPlainText(), "文字必须还在")
        self.assertEqual(self.window.reader.content_opacity(), 1.0)

    def test_compat_mode_opacity_never_goes_below_the_floor(self) -> None:
        """兼容模式整窗变淡：真到 0 会把文字一起抹掉、窗口找不回来，所以留 5% 下限。"""
        self.settings.translucent_bg = False
        self.window._sync_translucent_mode()
        for percent in (0, 1, -30):
            self.window.apply_opacity(percent, animate=False)
            # 断言 _target_opacity（整百分比，和平台无关）而不是 windowOpacity()：offscreen
            # 平台会把不透明度量化到 1/255 的台阶，5% 读回来是 0.047。
            self.assertEqual(self.window.opacity_percent(), COMPAT_MIN_OPACITY, str(percent))
            self.assertGreater(self.window.windowOpacity(), 0.0, "窗口不能真的变成全透明")

    def test_toggling_pin_keeps_geometry_and_reading_position(self) -> None:
        """📌 重建原生窗口：几何和阅读位置（用首个可见字符做锚点）都必须原样回来。"""
        reader = self.window.reader
        self.window.setGeometry(140, 60, 520, 400)
        reader.verticalScrollBar().setValue(3000)
        pump()
        anchor = reader.first_visible_char()
        self.assertGreater(anchor, 0, "先得真的滚下去，锚点才有意义")
        before = (self.window.x(), self.window.y(), self.window.width(), self.window.height())

        self.window.toggle_on_top()
        pump()
        self.assertEqual(
            (self.window.x(), self.window.y(), self.window.width(), self.window.height()), before
        )
        self.assertEqual(reader.first_visible_char(), anchor)

    def test_cancelling_the_settings_rolls_back_both_alphas(self) -> None:
        """预览只动画面不写盘；取消 = 背景和内容两个维度都回到打开对话框前的值。"""
        from minireader.ui import window as window_module

        self.settings.translucent_bg = True
        self.settings.bg_alpha = 255
        self.settings.content_alpha = 255
        self.window.apply_background()
        self.window.apply_content_alpha()

        class FakeDialog:
            def __init__(self, settings, parent=None, on_preview=None,
                         on_background=None, on_content=None, on_leave_hide=None):
                # 模拟用户拖动两根滑块，然后按"取消"。
                on_background(10)
                on_content(20)

            def setWindowFlag(self, *_a, **_k):
                pass

            def exec_(self):
                return 0                            # 取消

            def apply_to(self, s):
                raise AssertionError("取消时不该写设置")

            def deleteLater(self):
                pass

        original = window_module.SettingsDialog
        window_module.SettingsDialog = FakeDialog
        try:
            self.window.open_settings()
        finally:
            window_module.SettingsDialog = original
        pump()

        self.assertEqual(self.settings.bg_alpha, 255, "预览不能写盘")
        self.assertEqual(self.settings.content_alpha, 255, "预览不能写盘")
        self.assertEqual(self.window.bg_color.alpha(), 255, "取消后背景回到原值")
        self.assertEqual(
            self.window.reader.content_opacity(), 1.0, "取消后内容透明度回到原值"
        )


class TestQQMiniStyleAlignment(ReadingFeatureCase):
    """阶段 1 纯样式对齐 QQ 迷你窗：圆角、悬停块、tooltip、底栏可读性、进度弱化。

    钉的是数值下限，不是像素级截图——后续调色只要不破这些底线就随便调。
    """

    def test_bg_radius_matches_qq(self) -> None:
        from minireader.ui.window import BG_RADIUS

        self.assertEqual(BG_RADIUS, 9, "QQ 迷你窗实测圆角约 9px")

    def test_title_button_hit_target_and_hover_block(self) -> None:
        import re

        from minireader.ui.window import TITLE_BTN_MIN, _qss

        self.assertGreaterEqual(TITLE_BTN_MIN, 26, "QQ 实测悬停块 26×26")
        qss = _qss()
        self.assertIn("border-radius: 6px", qss, "悬停块圆角 ~6px")
        # QQ 规格：26×26 圆角高亮块，字形不变色。:hover 规则里只许出现背景，不许出现
        # color——一旦有人顺手加了 color，图标就会跟着变色，偏离规格。
        hover = re.search(r"QToolButton:hover\s*\{([^}]*)\}", qss).group(1)
        self.assertIn("background", hover)
        self.assertNotIn("color", hover, "悬停只换高亮块，图标字形颜色不变")

    def test_status_bar_readability_floor(self) -> None:
        """底栏硬性要求：高 29-36px、按钮命中区 ≥28px、元素间距 ≥8px、文字不弱化。"""
        from minireader.ui.theme import PALETTE
        from minireader.ui.window import _qss

        self.assertGreaterEqual(self.window.status_bar.height(), 29)
        self.assertLessEqual(self.window.status_bar.height(), 36)
        self.assertGreaterEqual(self.window.status_bar.layout().spacing(), 8)
        qss = _qss()
        self.assertIn("#StatusBar QToolButton", qss)
        self.assertIn("min-height: 28px", qss, "底栏按钮命中区高度下限")
        self.assertIn("font-size: 14px", qss, "底栏图标 ≥14px（文字随之为 14 ≥ 13）")
        # 底栏按钮直接用正文色：可读性规格要求亮度不低于正文的 70%，100% 最保险。
        self.assertIn(f"#StatusBar QToolButton {{\n        font-size: 14px", qss)
        self.assertIn(f"QToolButton {{ color: {PALETTE['fg']}", qss)

    def test_progress_is_the_only_weakened_element(self) -> None:
        """弱化只针对进度（#67676a、≥11px），章节名和其他标签不跟着弱。"""
        from minireader.ui.theme import PALETTE
        from minireader.ui.window import _qss

        self.assertEqual(self.window.progress_label.objectName(), "ProgressLabel")
        self.assertEqual(self.window.chapter_label.objectName(), "StatusLabel")
        self.assertIn(f"#ProgressLabel {{ color: {PALETTE['faint']}; font-size: 11px;", _qss())

    def test_faint_is_weak_but_not_invisible(self) -> None:
        """进度色刻意不进 TEXT_PAIRS（AA 4.5 是对正文的门槛，装饰性弱化不适用），
        但它必须仍过 AA_LARGE 3.0——弱化不等于看不见。"""
        from minireader.ui import theme

        self.assertNotIn("faint", [key for _role, fg, _bg in theme.TEXT_PAIRS for key in (fg,)])
        ratio = theme.contrast_ratio(theme.PALETTE["faint"], theme.PALETTE["bg"])
        self.assertGreaterEqual(ratio, theme.AA_LARGE)

    def test_tooltip_qss_lands_on_the_application(self) -> None:
        """QToolTip 是顶层窗口，只认应用级样式表；窗口创建后必须已安装且幂等。"""
        sheet = _app.styleSheet()
        self.assertIn("MiniReaderTooltip", sheet)
        self.assertIn("QToolTip", sheet)
        self.assertIn("#262626", sheet)
        self.assertEqual(sheet.count("MiniReaderTooltip"), 1, "重复建窗不得重复追加")

    def test_dialog_stylesheet_respects_the_12px_floor(self) -> None:
        """设置对话框美化规格：任何文字不得小于 12px，分组标题 14px 加粗。"""
        import re

        from minireader.ui.settings_dialog import _stylesheet

        sizes = [int(m) for m in re.findall(r"font-size:\s*(\d+)px", _stylesheet())]
        self.assertTrue(sizes, "样式表里必须显式声明字号")
        self.assertGreaterEqual(min(sizes), 12, "对话框内不得出现小于 12px 的文字")
        sheet = _stylesheet()
        self.assertIn("font-size: 14px; font-weight: bold", sheet, "分组标题 14px 加粗")
        self.assertIn("border-radius: 8px", sheet, "分组框圆角 8px")

    def test_dialog_ok_button_is_the_primary(self) -> None:
        from minireader.ui.settings_dialog import _PRIMARY, SettingsDialog

        dialog = SettingsDialog(self.settings, None)
        try:
            self.assertEqual(dialog._ok_button.objectName(), "PrimaryButton")
            self.assertIn(_PRIMARY, dialog.styleSheet(), "主按钮橙色必须进样式表")
        finally:
            dialog.deleteLater()


class TestMouseLeaveHide(ReadingFeatureCase):
    """鼠标移出窗口自动隐身（整窗压到 LEAVE_HIDE_OPACITY≈0，近全透但非零），以及它和
    老板键的分工。

    这里钉的是三件容易搞混的事：隐身是**临时覆盖**（settings.opacity 一个字节不动）、
    兼容模式也一样降到近全透下限（不走 COMPAT_MIN_OPACITY，也绝不能到真 0——alpha=0
    会让窗口点击穿透、移回鼠标唤不醒）、老板键的 hide() 和它不是同一回事。
    """

    def _leave(self, window) -> None:
        # 直接投递事件：offscreen 没有真实鼠标，造不出 "指针离开窗口" 的条件。
        window.leaveEvent(QEvent(QEvent.Leave))

    def _enter(self, window) -> None:
        window.enterEvent(QEvent(QEvent.Enter))

    def test_disabled_by_default_so_leaving_does_nothing(self) -> None:
        window = self.window
        self.assertFalse(window.settings.mouse_leave_hide, "默认关闭")
        window.settings.opacity = 80
        window.apply_opacity(80, animate=False)
        self._leave(window)
        _settle(160)
        self.assertAlmostEqual(window.windowOpacity(), 0.8, places=2, msg="没开就不许动")

    def test_leaving_for_a_hundred_ms_hides_the_whole_window(self) -> None:
        window = self.window
        window.settings.mouse_leave_hide = True
        window.settings.opacity = 80
        window.apply_opacity(80, animate=False)
        self._leave(window)
        self.assertGreater(window.windowOpacity(), 0.0, "100ms 之内还没到点")
        _settle(180)
        assert_at_leave_hide_floor(self, window, "到点后整窗（含文字）近全透，但非零可命中")

    def test_coming_back_within_the_debounce_cancels_the_hide(self) -> None:
        window = self.window
        window.settings.mouse_leave_hide = True
        window.settings.opacity = 80
        window.apply_opacity(80, animate=False)
        self._leave(window)
        _settle(40)                     # 不足 100ms 就回来
        self._enter(window)
        _settle(160)                    # 再等过一个完整防抖周期
        self.assertAlmostEqual(window.windowOpacity(), 0.8, places=2, msg="划过边缘不该闪")

    def test_entering_restores_the_users_own_opacity(self) -> None:
        window = self.window
        window.settings.mouse_leave_hide = True
        window.settings.opacity = 70
        window.apply_opacity(70, animate=False)
        self._leave(window)
        _settle(180)
        assert_at_leave_hide_floor(self, window)
        self._enter(window)
        self.assertAlmostEqual(window.windowOpacity(), 0.7, places=2)
        # 只是临时覆盖：用户的设置值不许被写坏。
        self.assertEqual(window.settings.opacity, 70)
        self.assertIsNone(window._opacity_before_hide)

    def test_the_boss_key_comes_back_to_the_users_opacity_not_zero(self) -> None:
        window = self.window
        window.settings.mouse_leave_hide = True
        window.settings.opacity = 60
        window.apply_opacity(60, animate=False)
        self._leave(window)
        _settle(180)
        assert_at_leave_hide_floor(self, window, "先处于移出隐身")

        window.hide_to_boss()               # 老板键：真隐藏，和移出隐身并存
        self.assertFalse(window.isVisible())
        window.show_from_boss()
        _settle(280)                        # 等滑行动画（OPACITY_MS=160）走完
        self.assertTrue(window.isVisible())
        self.assertIsNone(window._opacity_before_hide, "回来时不能带着移出隐身的账")
        self.assertAlmostEqual(window.windowOpacity(), 0.6, places=2, msg="不能恢复成 0")

    def test_compat_mode_also_drops_to_the_nonzero_floor_not_five_percent(self) -> None:
        window = self.window
        window.settings.translucent_bg = False
        window._sync_translucent_mode()
        window.settings.mouse_leave_hide = True
        window.apply_opacity(100, animate=False)
        self.assertGreaterEqual(window.windowOpacity(), COMPAT_MIN_OPACITY / 100.0)
        self._leave(window)
        _settle(180)
        # COMPAT_MIN_OPACITY 是普通滑块的下限，不是隐身的下限；但隐身下限同样不许到 0
        # （点击穿透），两种模式共用 LEAVE_HIDE_OPACITY。
        assert_at_leave_hide_floor(self, window, "兼容模式隐身也是整窗近全透")

    def test_the_window_still_takes_mouse_events_when_transparent(self) -> None:
        """隐身到近全透后必须仍然可命中，否则移入永远不触发、窗口回不来。

        两层保障：不允许设置任何鼠标穿透属性（WA_TransparentForMouseEvents /
        WindowTransparentForInput），且整窗 alpha 严格非零——alpha=0 时 Windows 自己
        就会让 layered window 穿透，那是这个修复针对的真 bug。
        """
        window = self.window
        self.assertFalse(window.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertFalse(
            bool(window.windowFlags() & Qt.WindowTransparentForInput),
            "不能设 WindowTransparentForInput",
        )
        window.settings.mouse_leave_hide = True
        self._leave(window)
        _settle(180)
        assert_at_leave_hide_floor(self, window)
        self._enter(window)                 # 还能收到 enter，说明没有被穿透
        self.assertGreater(window.windowOpacity(), 0.0)

    def test_restore_opacity_is_a_noop_without_a_pending_hide(self) -> None:
        window = self.window
        window.settings.opacity = 70
        window.apply_opacity(70, animate=False)
        self.assertIsNone(window._opacity_before_hide)
        window._restore_opacity_if_hidden()  # 不该抛，也不该动值
        self.assertAlmostEqual(window.windowOpacity(), 0.7, places=2)

    def test_present_window_brings_back_a_boss_hidden_window(self) -> None:
        """老板键藏起来后，托盘/第二实例的 present 也要能原样请回来。"""
        window = self.window
        window._boss_available = True
        window.hide_to_boss()
        self.assertFalse(window.isVisible())
        window.present_window()
        pump()
        self.assertTrue(window.isVisible())
        self.assertFalse(window.boss_hidden)

    def test_the_eye_button_and_the_setting_stay_in_step(self) -> None:
        window = self.window
        dialog = SettingsDialog(
            self.settings, None, on_leave_hide=window._sync_leave_hide_button
        )
        try:
            # 按钮 → 设置：点一下写进去并落盘；tooltip 同步翻成"关闭隐身"。
            self.assertFalse(window.boss_button.isChecked(), "默认关闭")
            self.assertEqual(window.boss_button.toolTip(), "开启隐身")
            window.boss_button.click()
            pump()
            self.assertTrue(window.settings.mouse_leave_hide)
            self.assertEqual(window.boss_button.toolTip(), "关闭隐身")

            # 设置 → 按钮：把设置改回去再同步，按钮要跟着松。
            window.settings.mouse_leave_hide = False
            window._sync_leave_hide_button()
            self.assertFalse(window.boss_button.isChecked())

            # 设置对话框的复选框一动，👁 立刻跟过去。
            dialog.leave_hide_check.setChecked(True)
            self.assertTrue(window.boss_button.isChecked())
            # 反向也要成立：复选框跟着设置走。
            dialog.leave_hide_check.setChecked(False)
            self.assertFalse(window.boss_button.isChecked())
        finally:
            dialog.deleteLater()

    def test_the_sliders_say_opacity_and_point_the_right_way(self) -> None:
        window = self.window
        window.settings.translucent_bg = True
        window.settings.bg_alpha = 255
        window.settings.content_alpha = 255
        window.open_opacity_popup()
        popup = window._opacity_popup
        captions = [label.text() for label in popup.findChildren(QLabel)]
        self.assertIn("背景不透明度 (0-100)", captions)
        self.assertIn("内容不透明度 (0-100)", captions)

        # 方向：0 = 完全透明，100 = 完全不透明。落盘仍然是 0-255。
        self.assertEqual((popup.bg_slider.minimum(), popup.bg_slider.maximum()), (0, 100))
        popup.bg_slider.setValue(0)
        pump()
        self.assertEqual(window.bg_color.alpha(), 0, "0 就是全透")
        popup.bg_slider.setValue(100)
        pump()
        self.assertEqual(window.bg_color.alpha(), 255, "100 就是全不透")
        popup.hide()
        pump()

        dialog = SettingsDialog(self.settings, None)
        try:
            texts = [label.text() for label in dialog.findChildren(QLabel)]
            self.assertTrue(any("背景不透明度 (0-100)" == t for t in texts), texts)
            self.assertTrue(any("内容不透明度 (0-100)" == t for t in texts), texts)
            self.assertFalse(any("背景透明度" in t for t in texts), "旧文案要换掉")
        finally:
            dialog.deleteLater()


class TestShortcutHygiene(unittest.TestCase):
    def test_ctrl_space_is_not_used(self) -> None:
        """Ctrl+Space 是中文 Windows 的输入法开关，抢过来会和输入法打架。"""
        from minireader import __main__ as entry

        source = Path(entry.__file__).read_text(encoding="utf-8")
        self.assertIn("Ctrl+Shift+Space", source)
        self.assertNotIn('("Ctrl+Space"', source)

    def test_no_tooltip_advertises_ctrl_space(self) -> None:
        """提示文字里不能出现 `Ctrl+空格`。

        这条是补出来的：快捷键本身早就改成了 Ctrl+Shift+Space，但自动滚动按钮的
        tooltip 还写着「Ctrl+空格 开始/暂停」——照着提示按下去只会切换输入法，滚动
        纹丝不动。快捷键的那条断言看的是 `__main__.py`，看不到 window.py 里的文案。
        """
        from minireader.ui import window as window_module

        source = Path(window_module.__file__).read_text(encoding="utf-8")
        self.assertIn("Ctrl+Shift+空格", source)
        self.assertNotIn("Ctrl+空格", source)

    def test_the_usage_text_lists_the_new_shortcuts(self) -> None:
        from minireader.__main__ import USAGE

        for needle in ("Ctrl+T", "Ctrl+B", "Ctrl+Shift+Space", "Ctrl+←"):
            self.assertIn(needle, USAGE)


class _GbkStdout:
    """只会 GBK 的假 stdout：不改本机环境就能复现中文 Windows 的 cp936 控制台。

    它同时是 spy：`reconfigure()` 被调用时记下来，并且真的把 encoding 换掉。所以
    "先强制 UTF-8、再写 USAGE"这个顺序如果被写反，写入仍然会在这里抛出——这条用例
    是有牙的，不是只断言"没抛异常"。
    """

    def __init__(self) -> None:
        self.encoding = "gbk"
        self.reconfigures: list[dict] = []
        self.writes: list[tuple[str, str]] = []   # (写入时的编码, 文本)

    def reconfigure(self, *, encoding=None, errors=None) -> None:
        self.reconfigures.append({"encoding": encoding, "errors": errors})
        if encoding is not None:
            self.encoding = encoding

    def write(self, text: str) -> int:
        self.writes.append((self.encoding, text))
        text.encode(self.encoding)          # GBK 编不出 ⚙ 就抛 UnicodeEncodeError
        return len(text)

    def flush(self) -> None:
        pass


class TestConsoleEncoding(unittest.TestCase):
    """每条命令行路径的输出编码必须一致。

    这条是打包时发现的：`--check` 重定向到文件是 UTF-8，`--smoke` 却是本地 ANSI
    代码页（中文 Windows 上是 GBK）——因为只有 `check()` 里调了
    `_force_utf8_console()`。从源码跑看不出来，环境里的 PYTHONIOENCODING 兜住了；
    PyInstaller 打出来的 exe 不带那个变量，同一台机器上同一个程序就给出两种编码
    的输出。修法是在 `main()` 开头调一次，覆盖所有路径。
    """

    def test_main_forces_utf8_on_every_path(self) -> None:
        from minireader import __main__ as entry

        calls: list[str] = []
        original = entry._force_utf8_console
        entry._force_utf8_console = lambda: calls.append("called")
        try:
            self.assertEqual(entry.main(["--help"]), 0)
        finally:
            entry._force_utf8_console = original
        self.assertEqual(len(calls), 1, "main() 开头必须调用一次 _force_utf8_console()")

    def test_help_output_is_utf8_without_pythonioencoding(self) -> None:
        """把 PYTHONIOENCODING 摘掉之后，输出仍然得是 UTF-8 字节。"""
        import os
        import subprocess
        import sys

        root = Path(__file__).resolve().parent.parent
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        result = subprocess.run(
            [sys.executable, "-m", "minireader", "--help"],
            cwd=str(root),
            env=env,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        # 不是 UTF-8 的话 decode 这一步就会抛 UnicodeDecodeError。
        text = result.stdout.decode("utf-8")
        self.assertIn("悬浮摸鱼阅读器", text)
        self.assertIn("老板键", text)

    def test_main_help_survives_a_gbk_stdout(self) -> None:
        """宿主只认 GBK 时，`--help` 必须跑完，而不是抛 UnicodeEncodeError。

        USAGE 里有一个 ⚙（U+2699），cp936 编不出来。修法是两层：`main()` 开头先
        `_force_utf8_console()` 把 stdout/stderr 换成 UTF-8；万一那一步没生效，写出
        路径也得自己写 UTF-8 字节。
        """
        import contextlib

        from minireader import __main__ as entry

        out = _GbkStdout()
        err = _GbkStdout()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(entry.main(["--help"]), 0)

        for name, stream in (("stdout", out), ("stderr", err)):
            self.assertEqual(len(stream.reconfigures), 1, f"{name} 必须被 reconfigure 一次")
            self.assertEqual(stream.reconfigures[0]["encoding"], "utf-8")
            self.assertEqual(stream.reconfigures[0]["errors"], "replace")
        self.assertTrue(out.writes, "USAGE 得真的写出去")
        self.assertEqual(
            {enc for enc, _ in out.writes},
            {"utf-8"},
            "写入不能发生在 reconfigure 之前",
        )
        self.assertIn("悬浮摸鱼阅读器", "".join(text for _, text in out.writes))

    def test_help_bytes_are_utf8_even_when_the_force_step_is_bypassed(self) -> None:
        """强制 UTF-8 那一步被绕过时，字节层写出的仍然得是 UTF-8。

        上一条用例依赖宿主流真的只认 GBK——本机有 PYTHONIOENCODING=utf-8 时它反而是
        因为别的原因通过的。这条自己造一条 GBK 文本流，所以在任何机器上都能复现你
        本地那次失败，并且直接断言落地的字节是 UTF-8。
        """
        import contextlib
        import io

        from minireader import __main__ as entry

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
        original = entry._force_utf8_console
        entry._force_utf8_console = lambda: None      # 模拟这道保险没生效
        try:
            with contextlib.redirect_stdout(stream):
                self.assertEqual(entry.main(["--help"]), 0)
        finally:
            entry._force_utf8_console = original
        stream.flush()                               # 不是 UTF-8 的话下一步就抛
        self.assertIn("悬浮摸鱼阅读器", raw.getvalue().decode("utf-8"))


class _FakeClock:
    """可控的单调时钟。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


if __name__ == "__main__":
    unittest.main()
