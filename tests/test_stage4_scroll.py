"""阶段 4：连续滚动模式（模式 B）。

核心层（格式表 / 压缩包 / ComicBook）由 `tests/test_comic.py` 守，模式 A 由
`tests/test_comic_ui.py` 守。这里只测阶段 4 新加的那一层：整本铺成一条竖列、
占位块高度、懒加载、进度反算、边界提示、和窗口的接线。

**为什么占位块高度单独测。** 模式 B 不抖动的唯一保证是"每一页在第一帧就有真实
宽高"。如果高度是解码之后才知道的，滚动条范围会在用户滚动时随图片到达而变大，
拇指被推着走。所以 `test_geometry` 里那几条断言的是**在解码之前**高度就正确。

**为什么边界提示要测"只发一次"。** 触控板一次轻扫连发十几个滚轮事件，
规格明确要求第二次及以后不响应，否则底栏会被同一句话刷屏。

    py -3.12 -m unittest tests.test_stage4_scroll -v
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import unittest
import zipfile
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core.book_base import KIND_COMIC, KIND_TEXT  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.qt import (  # noqa: E402
    QApplication,
    QPoint,
    QPointF,
    QWheelEvent,
    Qt,
    install_message_filter,
)
from minireader.ui.comic_cache import fit_size, probe_size, probe_sizes  # noqa: E402
from minireader.ui.comic_reader import (  # noqa: E402
    IMPLEMENTED_MODES,
    ComicReader,
    boundary_notice,
)
from minireader.ui.reader import ReaderView  # noqa: E402
from minireader.ui.strip_view import (  # noqa: E402
    LAZY_DEBOUNCE_MS,
    LAZY_RADIUS,
    WHEEL_STEP_PX,
    StripView,
)
from minireader.ui.window import MiniWindow  # noqa: E402

from .test_comic_ui import PAGE_SIZES, make_cbz, make_comic_folder, make_png  # noqa: E402
from .test_reading_features import _settle, pump  # noqa: E402
from .test_ui_offscreen import WindowTestCase, make_book  # noqa: E402

_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

_app = QApplication.instance() or QApplication([])


def wheel_on(widget, notches: int) -> None:
    """Send one wheel event to `widget`.

    `notches` follows the project's convention (`test_comic_ui.wheel_at`): a
    negative number scrolls toward the end of the book. Qt reports that as a
    negative `angleDelta().y()`.

    Sent to the widget the pointer would really be over - a page slot - because
    that is where the event lands in the running app and it is the delivery path
    the strip's filter exists for.
    """
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        pos,
        pos,
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    _app.sendEvent(widget, event)
    _app.processEvents()


# ---------------------------------------------------------------------------
# 占位块高度：模式 B 不抖动的唯一保证
# ---------------------------------------------------------------------------


class TestStripGeometry(unittest.TestCase):
    """槽位高度来自真实宽高比 + 当前缩放档，且**在解码之前**就正确。"""

    def setUp(self) -> None:
        self.strip = StripView()
        self.strip.resize(600, 400)
        self.strip.show()
        _app.processEvents()

    def tearDown(self) -> None:
        self.strip.stop()
        self.strip.deleteLater()
        _app.processEvents()

    def test_it_makes_one_slot_per_page(self) -> None:
        self.strip.set_pages([(40, 60)] * 7)
        self.assertEqual(self.strip.count(), 7)
        for index in range(7):
            self.assertIsNotNone(self.strip.slot(index))
        self.assertIsNone(self.strip.slot(7), "越界给 None，不给假槽位")

    def test_every_slot_has_a_real_height_before_any_image_is_decoded(self) -> None:
        """这条就是模式 B 存在的理由：高度不能等到解码才知道。"""
        sizes = [(40, 60), (60, 40), (50, 50), (30, 90)]
        self.strip.set_pages(sizes)
        for index, size in enumerate(sizes):
            slot = self.strip.slot(index)
            self.assertFalse(slot.has_page, "还没解码任何一页")
            self.assertGreater(self.strip.slot_height(index), 0, f"第 {index} 页高度为 0")
        # 高度必须真的按宽高比算，不能是"每页一样高"
        heights = [self.strip.slot_height(i) for i in range(4)]
        self.assertEqual(len(set(heights)), len(set(sizes)), "不同宽高比要给出不同高度")

    def test_the_height_matches_fit_size(self) -> None:
        """高度和模式 A 用的是同一个纯函数，两模式不会对同一页给出两种尺寸。"""
        self.strip.set_pages([(40, 60)])
        expected = fit_size((40, 60), (self.strip.viewport().width(), self.strip.viewport().height()), "width")
        self.assertEqual(self.strip.slot_height(0), max(1, expected[1]))

    def test_a_page_of_unknown_size_still_gets_a_block(self) -> None:
        """探不到尺寸的页（坏图/坏头）给一屏高，而不是 0 高。

        0 高会让它从滚动条范围里消失——一张坏页不该把整本书的排版弄坏。
        """
        self.strip.set_pages([(0, 0), (40, 60)])
        self.assertGreater(self.strip.slot_height(0), 0)
        self.assertGreater(self.strip.slot_height(1), 0)

    def test_the_offsets_are_the_running_sum_of_the_heights(self) -> None:
        self.strip.set_pages([(40, 60), (60, 40), (50, 50)])
        running = 0
        for index in range(3):
            self.assertEqual(self.strip.page_top(index), running)
            running += self.strip.slot_height(index)
        self.assertEqual(self.strip.total_height(), running)

    def test_zooming_recomputes_every_height(self) -> None:
        """"original" 档下高度就是图片自己的高，和宽度适应不同。"""
        # 视口够大，宽度适应会把 40x60 放大吗？不会——`fit_size` 从不放大，
        # 所以宽度适应下 40x60 就是 40x60，和 original 一样。要看出差别，
        # 必须让页面**比视口宽**，这样宽度适应才会把它缩下来。
        self.strip.set_pages([(4000, 6000)])
        width_fit = self.strip.slot_height(0)
        self.strip.set_zoom("original")
        original = self.strip.slot_height(0)
        self.assertEqual(original, 6000, "原始尺寸下高度就是图片自己的高")
        self.assertLess(width_fit, original, "宽度适应必须把超宽页缩下来")

    def test_a_width_fitted_page_is_never_enlarged(self) -> None:
        """`fit_size` 从不放大：小图在宽度适应下保持原样。"""
        self.strip.set_pages([(40, 60)])
        self.assertEqual(self.strip.slot_height(0), 60)

    def test_an_unknown_zoom_falls_back_to_width(self) -> None:
        self.strip.set_zoom("nonsense")
        self.assertEqual(self.strip.zoom, "width")

    def test_a_resize_relayouts_without_decoding(self) -> None:
        """宽度适应的高度依赖视口宽，所以缩放窗口必须重算——但只是算术。

        用一张**比视口宽**的页才看得出差别：`fit_size` 从不放大，小图在两种
        视口宽度下都是原尺寸。
        """
        self.strip.set_pages([(4000, 6000)])
        before = self.strip.slot_height(0)
        self.strip.resize(300, 400)
        _app.processEvents()
        after = self.strip.slot_height(0)
        self.assertLess(after, before, "视口变窄，宽度适应的页应该变矮")
        self.assertFalse(self.strip.slot(0).has_page, "重排不该触发解码")


class TestStripScrollRange(unittest.TestCase):
    """滚动条范围：**立刻**就是对的，不能等 Qt 的延迟布局。"""

    def setUp(self) -> None:
        self.strip = StripView()
        self.strip.resize(600, 400)
        self.strip.show()
        _app.processEvents()

    def tearDown(self) -> None:
        self.strip.stop()
        self.strip.deleteLater()
        _app.processEvents()

    def test_the_range_is_right_immediately_after_set_pages(self) -> None:
        """这条守的是一个实测到的真缺陷。

        `setWidgetResizable(True)` 下 QScrollArea 要到**下一轮**事件循环才重算范围，
        在那之前 maximum 是 0，`setValue()` 会被静默夹到 0。于是"在第 200 页切进
        模式 B"会显示第 1 页、而底栏还写着 200，第一次滚轮也纹丝不动。
        """
        self.strip.set_pages([(40, 60)] * 10)
        bar = self.strip.verticalScrollBar()
        expected = max(0, self.strip.total_height() - self.strip.viewport().height())
        self.assertEqual(bar.maximum(), expected, "不能等 pump 之后才对")

    def test_scroll_to_page_lands_on_that_page_even_before_a_pump(self) -> None:
        """上一条的用户可见后果：切进模式 B 必须真的停在那一页。

        这里**不 pump**：`scroll_to_page()` 之后立刻检查，验的正是"范围已经
        算好了，不用等 Qt 的延迟布局"。

        用 20 页而不是 10 页：第 9 页的 top 必须真的落在 maximum 之内，否则
        滚动条会把它夹住，这条就测不到"范围算好了"而是测到"夹取生效了"。
        """
        self.strip.set_pages([(120, 180)] * 20)
        self.assertGreater(self.strip.verticalScrollBar().maximum(), self.strip.page_top(8),
                           "夹具前提：第 9 页得在可达范围内")
        self.strip.scroll_to_page(8)
        self.assertEqual(self.strip.verticalScrollBar().value(), self.strip.page_top(8))
        self.assertIn(8, self.strip.visible_range(), "第 9 页必须在可见范围内")

    def test_a_book_shorter_than_the_viewport_has_no_scroll_range(self) -> None:
        self.strip.set_pages([(40, 60)])
        self.assertEqual(self.strip.verticalScrollBar().maximum(), 0)


class TestStripPosition(unittest.TestCase):
    """进度反算：视口中心线所在的页。"""

    def setUp(self) -> None:
        self.strip = StripView()
        self.strip.resize(600, 400)
        self.strip.show()
        self.strip.set_pages([(120, 180)] * 12)
        _app.processEvents()

    def tearDown(self) -> None:
        self.strip.stop()
        self.strip.deleteLater()
        _app.processEvents()

    def test_the_current_page_is_the_one_under_the_middle(self) -> None:
        """不是"顶部那一页"，是**中心线**那一页——规格定的规则。"""
        self.strip.scroll_to_page(0)
        centre = self.strip.verticalScrollBar().value() + self.strip.viewport().height() // 2
        self.assertEqual(self.strip.current_index(), self.strip._index_at(centre))

    def test_scrolling_changes_the_current_page(self) -> None:
        self.strip.scroll_to_page(0)
        first = self.strip.current_index()
        self.strip.verticalScrollBar().setValue(self.strip.page_top(6))
        _app.processEvents()
        self.assertGreater(self.strip.current_index(), first)

    def test_the_visible_range_covers_what_is_on_screen(self) -> None:
        self.strip.scroll_to_page(4)
        first, last = self.strip.visible_range()
        self.assertLessEqual(first, 4)
        self.assertGreaterEqual(last, 4)

    def test_the_band_is_the_visible_range_widened_by_the_radius(self) -> None:
        self.strip.scroll_to_page(6)
        first, last = self.strip.visible_range()
        band_first, band_last = self.strip.band()
        self.assertEqual(band_first, max(0, first - LAZY_RADIUS))
        self.assertEqual(band_last, min(self.strip.count() - 1, last + LAZY_RADIUS))

    def test_an_empty_strip_answers_without_raising(self) -> None:
        empty = StripView()
        try:
            self.assertEqual(empty.count(), 0)
            self.assertEqual(empty.visible_range(), (0, -1))
            self.assertEqual(empty.band(), (0, -1))
            self.assertEqual(empty.current_index(), 0)
            self.assertEqual(empty.total_height(), 0)
            empty.scroll_to_page(3)  # 不该抛
            empty.stop()
        finally:
            empty.deleteLater()


class TestStripBoundary(unittest.TestCase):
    """到底/到顶的提示，以及"只发一次"。"""

    def setUp(self) -> None:
        self.strip = StripView()
        self.strip.resize(600, 400)
        self.strip.show()
        self.strip.set_pages([(120, 180)] * 8)
        _app.processEvents()
        self.seen: list[int] = []
        self.strip.boundaryReached.connect(self.seen.append)
        self.slot = self.strip.slot(0)

    def tearDown(self) -> None:
        self.strip.stop()
        self.strip.deleteLater()
        _app.processEvents()

    def _to_bottom(self) -> None:
        bar = self.strip.verticalScrollBar()
        bar.setValue(bar.maximum())
        _app.processEvents()

    def _to_top(self) -> None:
        bar = self.strip.verticalScrollBar()
        bar.setValue(bar.minimum())
        _app.processEvents()

    def test_the_first_notch_past_the_bottom_reports_it(self) -> None:
        self._to_bottom()
        wheel_on(self.slot, -1)
        self.assertEqual(self.seen, [-1])

    def test_a_second_notch_at_the_bottom_stays_silent(self) -> None:
        """触控板一次轻扫十几个事件，同一个边界只能说一次。"""
        self._to_bottom()
        for _ in range(10):
            wheel_on(self.slot, -1)
        self.assertEqual(self.seen, [-1], "到底之后连滚不许刷屏")

    def test_the_first_notch_past_the_top_reports_it(self) -> None:
        self._to_top()
        wheel_on(self.slot, 1)
        self.assertEqual(self.seen, [1])

    def test_a_second_notch_at_the_top_stays_silent(self) -> None:
        self._to_top()
        for _ in range(10):
            wheel_on(self.slot, 1)
        self.assertEqual(self.seen, [1])

    def test_leaving_the_end_re_arms_it(self) -> None:
        """滚回中间再滚到底，必须重新说一次——否则第二次到底就没反应了。"""
        self._to_bottom()
        wheel_on(self.slot, -1)
        self.assertEqual(self.seen, [-1])
        self.strip.verticalScrollBar().setValue(self.strip.page_top(3))
        _app.processEvents()
        self._to_bottom()
        wheel_on(self.slot, -1)
        self.assertEqual(self.seen, [-1, -1], "离开边界后要能再报一次")

    def test_a_wheel_in_the_middle_scrolls_instead_of_reporting(self) -> None:
        self.strip.verticalScrollBar().setValue(self.strip.page_top(3))
        _app.processEvents()
        before = self.strip.verticalScrollBar().value()
        wheel_on(self.slot, -1)
        self.assertEqual(self.seen, [], "中间滚动不是边界")
        self.assertGreater(self.strip.verticalScrollBar().value(), before, "该真的滚下去")

    def test_scrolling_up_from_the_middle_works(self) -> None:
        self.strip.verticalScrollBar().setValue(self.strip.page_top(4))
        _app.processEvents()
        before = self.strip.verticalScrollBar().value()
        wheel_on(self.slot, 1)
        self.assertLess(self.strip.verticalScrollBar().value(), before)
        self.assertEqual(self.seen, [])

    def test_one_notch_moves_by_the_documented_step(self) -> None:
        self.strip.verticalScrollBar().setValue(self.strip.page_top(4))
        _app.processEvents()
        before = self.strip.verticalScrollBar().value()
        wheel_on(self.slot, -1)
        self.assertEqual(self.strip.verticalScrollBar().value(), before + int(WHEEL_STEP_PX))

    def test_a_wheel_over_a_slot_reaches_the_strip(self) -> None:
        """这条守的是一个实测到的投递陷阱。

        Qt 只把滚轮事件投给**指针下最深的那层控件**，且**不向父控件传播**；
        而 QScrollArea 自己的滚轮处理是从它内部的 viewport 驱动的。所以没有
        `StripView.eventFilter` 的话，鼠标停在页面上滚轮就什么都不做——正好是
        用户最常做的动作。
        """
        slot = self.strip.slot(2)
        self.strip.verticalScrollBar().setValue(self.strip.page_top(2))
        _app.processEvents()
        before = self.strip.verticalScrollBar().value()
        wheel_on(slot, -1)
        self.assertGreater(self.strip.verticalScrollBar().value(), before)

    def test_a_zero_delta_wheel_is_ignored(self) -> None:
        pos = QPointF(10.0, 10.0)
        event = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, 0), Qt.NoButton,
                            Qt.NoModifier, Qt.NoScrollPhase, False)
        _app.sendEvent(self.slot, event)
        self.assertFalse(event.isAccepted(), "没有位移的滚轮不该被吃掉")
        self.assertEqual(self.seen, [])


class TestStripPlaceholderText(unittest.TestCase):
    def test_an_unloaded_slot_says_which_page_it_is(self) -> None:
        strip = StripView()
        try:
            strip.resize(400, 300)
            strip.set_pages([(40, 60)] * 3)
            self.assertEqual(strip.placeholder(1), "第 2 页")
            self.assertEqual(strip.slot(1).message, "第 2 页")
        finally:
            strip.stop()
            strip.deleteLater()

    def test_clearing_a_slot_puts_the_placeholder_back(self) -> None:
        strip = StripView()
        try:
            strip.resize(400, 300)
            strip.set_pages([(40, 60)] * 3)
            strip.set_page(1, None, "这一页读不出来")
            self.assertEqual(strip.slot(1).message, "这一页读不出来")
            strip.clear_page(1)
            self.assertEqual(strip.slot(1).message, "第 2 页")
            self.assertFalse(strip.slot(1).has_page)
        finally:
            strip.stop()
            strip.deleteLater()


# ---------------------------------------------------------------------------
# 批量探测：让"第一帧就正确"付得起
# ---------------------------------------------------------------------------


class TestBatchProbe(unittest.TestCase):
    """`probe_sizes()` 必须和逐页 `probe_size()` 给出**完全一样**的答案。

    它是模式 B 打开速度的关键（300 页实测 252 ms -> 24 ms），但快没有意义
    如果答案不一样——占位块高度错一页，滚动条就抖一次。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(os.environ["TEMP"]) / "mr-stage4-probe"
        cls.tmp.mkdir(exist_ok=True)
        cls.cbz = make_cbz(cls.tmp / "batch.cbz", pages=8)

    @classmethod
    def tearDownClass(cls) -> None:
        # `rmtree`, not `glob("*").unlink()`: this fixture makes a *folder* of pages
        # (`test_it_works_for_a_folder_too`), and unlinking a directory raises
        # PermissionError on Windows.
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_it_agrees_with_the_one_page_at_a_time_path(self) -> None:
        book = open_book(self.cbz)
        batch = probe_sizes(book, range(8))
        for index in range(8):
            self.assertEqual(batch[index], probe_size(book, index), f"第 {index} 页")
            self.assertEqual(batch[index], PAGE_SIZES[index % len(PAGE_SIZES)])

    def test_it_covers_every_index_asked_about(self) -> None:
        book = open_book(self.cbz)
        batch = probe_sizes(book, [7, 0, 3])
        self.assertEqual(set(batch), {0, 3, 7})

    def test_an_empty_request_is_an_empty_answer(self) -> None:
        book = open_book(self.cbz)
        self.assertEqual(probe_sizes(book, []), {})

    def test_an_out_of_range_page_is_unknown_rather_than_missing(self) -> None:
        """缺键会逼调用方去猜；(0,0) 是明确的"不知道"。"""
        book = open_book(self.cbz)
        self.assertEqual(probe_sizes(book, [99]), {99: (0, 0)})

    def test_a_broken_page_does_not_take_the_others_down(self) -> None:
        path = self.tmp / "mixed.cbz"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("page_1.png", make_png(40, 60))
            zf.writestr("page_2.png", b"not an image at all")
            zf.writestr("page_3.png", make_png(50, 50))
        book = open_book(path)
        batch = probe_sizes(book, range(3))
        self.assertEqual(batch[0], (40, 60))
        self.assertEqual(batch[1], (0, 0), "坏页报不知道")
        self.assertEqual(batch[2], (50, 50), "后面的好页照常测出来")

    def test_it_works_for_a_folder_too(self) -> None:
        folder = make_comic_folder(self.tmp / "folder", pages=3)
        book = open_book(folder)
        batch = probe_sizes(book, range(3))
        for index in range(3):
            self.assertEqual(batch[index], PAGE_SIZES[index % len(PAGE_SIZES)])

    def test_it_does_not_decode_anything(self) -> None:
        """只读文件头。真解码 300 页会很慢，而且这里没有任何缓存可查。"""
        book = open_book(self.cbz)
        probe_sizes(book, range(8))
        self.assertFalse(book.sizes_known(), "探测只报尺寸，不替书做决定")
        for page in book.pages:
            self.assertEqual((page.width, page.height), (0, 0))


# ---------------------------------------------------------------------------
# ComicReader：模式 B 的接线
# ---------------------------------------------------------------------------


class TestComicReaderScrollMode(unittest.TestCase):
    """不建窗口，只问 Reader 自己。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(os.environ["TEMP"]) / "mr-stage4-reader"
        cls.tmp.mkdir(exist_ok=True)
        cls.cbz = make_cbz(cls.tmp / "reader.cbz", pages=12)

    @classmethod
    def tearDownClass(cls) -> None:
        for item in cls.tmp.glob("*"):
            item.unlink()
        cls.tmp.rmdir()

    def setUp(self) -> None:
        self.book = open_book(self.cbz)
        self.reader = ComicReader()
        self.reader.resize(600, 400)
        self.reader.show()
        self.reader.show_book(self.book, index=0)
        _app.processEvents()

    def tearDown(self) -> None:
        self.reader.stop()
        self.reader.deleteLater()
        _app.processEvents()

    def _to_scroll(self) -> None:
        self.reader.set_mode("scroll")
        _app.processEvents()

    def test_scroll_is_now_implemented(self) -> None:
        self.assertIn("scroll", IMPLEMENTED_MODES)
        self.assertIn("scroll", ComicReader.IMPLEMENTED_MODES)

    def test_entering_the_mode_lays_out_every_page(self) -> None:
        self._to_scroll()
        self.assertEqual(self.reader.mode, "scroll")
        self.assertEqual(self.reader._strip.count(), 12)

    def test_entering_the_mode_probes_every_page_size(self) -> None:
        """占位块高度的前提：进模式时就把尺寸补齐，而不是边滚边补。"""
        self.assertFalse(self.book.sizes_known(), "打开时还不知道任何一页的尺寸")
        self._to_scroll()
        self.assertTrue(self.book.sizes_known(), "进模式 B 必须补齐全部尺寸")
        for index, page in enumerate(self.book.pages):
            self.assertEqual((page.width, page.height), PAGE_SIZES[index % len(PAGE_SIZES)])

    def test_the_scrollbar_is_the_strips_own_bar(self) -> None:
        """AutoScroller 只认 value()/maximum()/setValue()，必须是真条。"""
        self._to_scroll()
        self.assertIs(self.reader.scrollbar(), self.reader._strip.verticalScrollBar())

    def test_there_is_no_scrollbar_in_single_page_mode(self) -> None:
        self.assertIsNone(self.reader.scrollbar())

    def test_the_viewport_switches_with_the_mode(self) -> None:
        """边缘缩放的过滤器装在 viewport() 上，模式 B 必须换成真占地方的那个。"""
        self.assertIs(self.reader.viewport(), self.reader._page_view)
        self._to_scroll()
        self.assertIs(self.reader.viewport(), self.reader._strip)
        self.reader.set_mode("single")
        self.assertIs(self.reader.viewport(), self.reader._page_view)

    def test_switching_the_mode_announces_the_viewport_change(self) -> None:
        """窗口靠这个信号搬家那个事件过滤器；不发就等于模式 B 边缘缩放静默失效。"""
        seen: list[int] = []
        self.reader.viewportChanged.connect(lambda: seen.append(1))
        self._to_scroll()
        self.assertEqual(len(seen), 1)
        self.reader.set_mode("single")
        self.assertEqual(len(seen), 2)

    def test_it_only_decodes_the_band_around_the_viewport(self) -> None:
        """懒加载：12 页只解视口附近那几个，不是整本。"""
        self._to_scroll()
        loaded = self.reader._loaded
        self.assertTrue(loaded, "总得加载点什么")
        self.assertLess(len(loaded), 12, "不该把整本都解出来")
        first, last = self.reader._strip.band()
        self.assertTrue(loaded <= set(range(first, last + 1)), "加载的必须落在 band 内")

    def test_the_decoded_slots_actually_show_a_page(self) -> None:
        self._to_scroll()
        index = min(self.reader._loaded)
        self.assertTrue(self.reader._strip.slot(index).has_page)

    def test_scrolling_loads_the_new_band_and_unloads_the_old(self) -> None:
        """滚远了要卸载，否则 300 页会把内存吃光——缓存是 MB 计的。"""
        self._to_scroll()
        before = set(self.reader._loaded)
        bar = self.reader._strip.verticalScrollBar()
        bar.setValue(self.reader._strip.page_top(11))
        _app.processEvents()
        _settle(LAZY_DEBOUNCE_MS + 40)
        after = set(self.reader._loaded)
        self.assertNotEqual(before, after, "滚到书末必须换一批")
        self.assertIn(11, after, "当前页必须被加载")
        self.assertFalse(after & {0}, "第 1 页滚出范围后该被卸载")
        self.assertFalse(self.reader._strip.slot(0).has_page)

    def test_an_unloaded_slot_keeps_its_height(self) -> None:
        """卸载只丢像素，不丢占位块——丢高度就等于滚动条会跳。"""
        self._to_scroll()
        heights = [self.reader._strip.slot_height(i) for i in range(12)]
        bar = self.reader._strip.verticalScrollBar()
        bar.setValue(self.reader._strip.page_top(11))
        _app.processEvents()
        _settle(LAZY_DEBOUNCE_MS + 40)
        self.assertEqual([self.reader._strip.slot_height(i) for i in range(12)], heights)

    def test_the_current_page_follows_the_scroll(self) -> None:
        self._to_scroll()
        self.assertEqual(self.reader.index, 0)
        self.reader._strip.verticalScrollBar().setValue(self.reader._strip.page_top(6))
        _app.processEvents()
        self.assertGreater(self.reader.index, 0)

    def test_scrolling_emits_the_scrolled_signal(self) -> None:
        """窗口靠 `scrolled` 同步 chapter_index、底栏和进度保存。"""
        self._to_scroll()
        seen: list[int] = []
        self.reader.scrolled.connect(lambda: seen.append(1))
        self.reader._strip.verticalScrollBar().setValue(self.reader._strip.page_top(5))
        _app.processEvents()
        self.assertTrue(seen, "页码变了就要发 scrolled")

    def test_entering_the_mode_lands_on_the_current_page(self) -> None:
        """这条守的是一个实测到的真缺陷：进模式 B 会显示第 1 页，而页码还是旧值。

        根因是 QScrollArea 的滚动范围要到下一轮事件循环才算出来，`setValue()`
        在此之前被静默夹到 0。
        """
        self.reader.show_book(self.book, index=8)
        _app.processEvents()
        self._to_scroll()
        self.assertEqual(self.reader.index, 8)
        first, last = self.reader._strip.visible_range()
        self.assertLessEqual(first, 8, "屏幕上必须真的是第 9 页")
        self.assertGreaterEqual(last, 8, "屏幕上必须真的是第 9 页")

    def test_leaving_the_mode_keeps_the_page(self) -> None:
        """切回模式 A 要停在同一页——底栏报的是哪页，屏幕上就该是哪页。"""
        self._to_scroll()
        self.reader._strip.verticalScrollBar().setValue(self.reader._strip.page_top(7))
        _app.processEvents()
        index = self.reader.index
        self.reader.set_mode("single")
        _app.processEvents()
        self.assertEqual(self.reader.index, index)
        self.assertEqual(self.reader.current_position().label, f"第 {index + 1}/12 页")

    def test_zoom_applies_to_the_strip(self) -> None:
        """换缩放档要重算占位块高度。用超宽页才看得出宽度适应和原始尺寸的差别。"""
        self.reader.show_book(open_book(make_cbz(self.tmp / "wide.cbz", pages=3)), index=0)
        for page in self.reader.book.pages:
            page.width, page.height = 4000, 6000
        self.reader._strip_signature = None
        self._to_scroll()
        width_fit = self.reader._strip.slot_height(0)
        self.reader.set_zoom("original")
        self.assertEqual(self.reader._strip.zoom, "original")
        self.assertEqual(self.reader._strip.slot_height(0), 6000)
        self.assertLess(width_fit, 6000, "宽度适应必须把超宽页缩下来")

    def test_zoom_while_scrolled_keeps_the_page(self) -> None:
        self._to_scroll()
        self.reader._strip.verticalScrollBar().setValue(self.reader._strip.page_top(6))
        _app.processEvents()
        index = self.reader.index
        self.reader.set_zoom("original")
        _app.processEvents()
        self.assertEqual(self.reader.index, index, "换缩放档不该把读者扔到别处")

    def test_switching_books_forgets_the_old_strip(self) -> None:
        other = open_book(make_cbz(self.tmp / "other.cbz", pages=3))
        self._to_scroll()
        self.assertEqual(self.reader._strip.count(), 12)
        self.reader.show_book(other, index=0)
        _app.processEvents()
        self.assertEqual(self.reader._strip.count(), 3, "新书要有自己的槽位数")
        self.assertTrue(self.reader._loaded <= {0, 1, 2})

    def test_showing_no_book_clears_the_strip(self) -> None:
        self._to_scroll()
        self.reader.show_book(None)
        _app.processEvents()
        self.assertEqual(self.reader._strip.count(), 0)
        self.assertEqual(self.reader._loaded, set())

    def test_a_book_with_known_sizes_is_not_probed_again(self) -> None:
        """`_page_sizes` 只测还不知道的页。

        这条守的是"同一份文档报出两种尺寸"那个老问题：PDF 的 pixmap 是页框乘
        RENDER_SCALE，拿渲染结果当尺寸会让第 1 页比其余页大一倍。`open_pdf()`
        已经用文档自己的页框填好了尺寸，所以这里必须一个字节都不去测。
        """
        book = open_book(self.cbz)
        sizes = [(612, 792), (600, 800), (620, 780)]
        # 全部 12 页都要填上，否则 `sizes_known()` 是 False，测的就不是
        # "已经知道的书不再探测"而是"部分未知"了。
        for index, page in enumerate(book.pages):
            page.width, page.height = sizes[index % len(sizes)]
        self.assertTrue(book.sizes_known())
        got = self.reader._page_sizes(book)
        self.assertEqual(got, [sizes[i % len(sizes)] for i in range(12)],
                         "已知道的尺寸必须原样带过来")
        # 而且没有把这些页当成"待测"而重新探测——真探测会把它们覆盖成 PNG 的
        # 真实尺寸（PAGE_SIZES），那样这条断言就会看到 (40, 60) 之类。
        self.assertNotIn((40, 60), got)

    def test_stop_cancels_the_strip_timer(self) -> None:
        self._to_scroll()
        self.reader._strip._timer.start()
        self.reader.stop()
        self.assertFalse(self.reader._strip._timer.isActive())


class TestBoundaryNoticeWording(unittest.TestCase):
    """文案只有一处实现，所以三处入口说出来的是同一句话。"""

    def test_the_bottom_says_the_last_page(self) -> None:
        self.assertEqual(boundary_notice(-1, 299, 300), "已经是最后一页了（第 300/300 页）")

    def test_the_top_says_the_first_page(self) -> None:
        self.assertEqual(boundary_notice(1, 0, 300), "已经是第一页了（第 1/300 页）")

    def test_it_reports_the_current_page_not_a_stale_one(self) -> None:
        self.assertIn("第 5/9 页", boundary_notice(-1, 4, 9))

    def test_a_degenerate_total_still_reads_sensibly(self) -> None:
        """0 页的书（理论上）不该说出"第 1/0 页"这种话。"""
        self.assertIn("/1 页", boundary_notice(1, 0, 0))

    def test_an_out_of_range_index_is_clamped(self) -> None:
        self.assertIn("第 3/3 页", boundary_notice(-1, 99, 3))


# ---------------------------------------------------------------------------
# 接进窗口之后
# ---------------------------------------------------------------------------


class ScrollWindowTestCase(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cbz_path = make_cbz(self.tmp / "comic.cbz", pages=12)
        self.text_path = make_book(self.tmp, name="novel.txt", chapters=3)

    def _scroll_window(self) -> MiniWindow:
        window = self.make_window(path=self.cbz_path)
        window._comic_reader.set_mode("scroll")
        pump()
        return window


class TestWindowScrollMode(ScrollWindowTestCase):
    def test_the_mode_button_reaches_continuous_scroll(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.cycle_comic_mode()
        pump()
        self.assertEqual(window._comic_reader.mode, "scroll")
        self.assertIn("连续滚动", window.comic_mode_button.toolTip())

    def test_the_progress_records_the_mode(self) -> None:
        window = self._scroll_window()
        window.persist_progress()
        self.assertEqual(self.progress.get(window.book.id).mode, "scroll")

    def test_a_saved_scroll_mode_is_restored_at_startup(self) -> None:
        """设置里存了连续滚动，开机就该是连续滚动，而且不再有"还没实现"的提示。"""
        self.settings.comic_default_mode = "scroll"
        window = self.make_window()
        window._comic_reader.notice.connect(window.set_warning)
        window.apply_comic_settings()
        pump()
        self.assertEqual(window._comic_reader.mode, "scroll")
        self.assertEqual(window.warn_label.text(), "")

    def test_the_bottom_bar_tracks_the_scrolled_page(self) -> None:
        window = self._scroll_window()
        window._comic_reader._strip.verticalScrollBar().setValue(
            window._comic_reader._strip.page_top(6)
        )
        pump()
        self.assertEqual(window.chapter_index, window._comic_reader.index)
        self.assertEqual(window.chapter_label.text(), f"第 {window.chapter_index + 1}/12 页")

    def test_scrolling_schedules_a_progress_save(self) -> None:
        window = self._scroll_window()
        window._progress_timer.stop()
        window._comic_reader._strip.verticalScrollBar().setValue(
            window._comic_reader._strip.page_top(5)
        )
        pump()
        self.assertTrue(window._progress_timer.isActive())

    def test_the_scrolled_page_is_written_to_the_store(self) -> None:
        window = self._scroll_window()
        window._comic_reader._strip.verticalScrollBar().setValue(
            window._comic_reader._strip.page_top(7)
        )
        pump()
        window.persist_progress()
        self.assertEqual(self.progress.get(window.book.id).page, window.chapter_index)

    def test_reopening_lands_on_the_scrolled_page(self) -> None:
        """这条是用户真正会遇到的症状：滚到中间重开，必须还在那里。"""
        window = self._scroll_window()
        window._comic_reader._strip.verticalScrollBar().setValue(
            window._comic_reader._strip.page_top(7)
        )
        pump()
        expected = window.chapter_index
        window.persist_progress()

        again = self.make_window(path=self.cbz_path)
        pump()
        self.assertEqual(again.chapter_index, expected)

    def test_the_edge_resize_filter_moves_to_the_strip(self) -> None:
        """模式 B 的边缘缩放热区装在 QScrollArea 上，不是留在 PageView 上。

        不搬的话，4px 边框的内侧 8px 被新的控件盖住，拖动会静默失效。
        """
        window = self.make_window(path=self.cbz_path)
        window._comic_reader.set_mode("scroll")
        pump()
        self.assertIs(window._filtered_viewport, window._comic_reader._strip)
        window._comic_reader.set_mode("single")
        pump()
        self.assertIs(window._filtered_viewport, window._comic_reader._page_view)

    def test_only_one_viewport_ever_carries_the_filter(self) -> None:
        """搬家的实现必须是"摘掉旧的再装新的"，不能只装不摘。"""
        window = self.make_window(path=self.cbz_path)
        page_view = window._comic_reader._page_view
        window._comic_reader.set_mode("scroll")
        pump()
        # 旧的那个不该还挂着这个过滤器：再切回去时不会重复安装。
        window._comic_reader.set_mode("single")
        pump()
        self.assertIs(window._filtered_viewport, page_view)


class TestWindowAutoscrollInScrollMode(ScrollWindowTestCase):
    """模式 B 下自动滚动必须可用，而且滚的是那根真的条。"""

    def test_the_autoscroller_is_bound_to_the_strips_bar(self) -> None:
        window = self._scroll_window()
        self.assertIs(window._autoscroll_bar, window._comic_reader._strip.verticalScrollBar())

    def test_the_toggle_actually_starts(self) -> None:
        window = self._scroll_window()
        window.toggle_autoscroll()
        pump()
        self.assertTrue(window.autoscroller.is_running(), "模式 B 有滚动条，自动滚动该能用")
        window.autoscroller.stop()

    def test_it_moves_the_strip(self) -> None:
        window = self._scroll_window()
        window._comic_reader._strip.verticalScrollBar().setValue(0)
        window.toggle_autoscroll()
        window.autoscroller.set_speed(400)
        window.autoscroller._clock = lambda: window.autoscroller._last + 1.0
        window.autoscroller._last = 0.0
        window.autoscroller._on_tick()
        self.assertGreater(window._comic_reader._strip.verticalScrollBar().value(), 0)
        window.autoscroller.stop()

    def test_switching_back_to_single_unbinds_it(self) -> None:
        """模式 A 没有可滚的条，必须解绑：否则它会继续滚隐藏的正文。

        解绑的结果是滚到窗口那根"停着的空条"上（范围 0,0），不是 None——
        `AutoScroller` 的 `start()` 要读 `bar.value()`，给它 None 会炸。
        空条让 `start()` 立刻判定"到底了"并安全收摊，行为上等价于"停用"。
        """
        window = self._scroll_window()
        strip_bar = window._comic_reader._strip.verticalScrollBar()
        window._comic_reader.set_mode("single")
        pump()
        self.assertIsNot(window._autoscroll_bar, strip_bar, "不许还挂在漫画的条上")
        self.assertEqual(window._autoscroll_bar.maximum(), 0, "换成了停着的空条")
        window.toggle_autoscroll()
        self.assertFalse(window.autoscroller.is_running())

    def test_reaching_the_end_says_the_last_page(self) -> None:
        """整本 = 一话，没有下一话可接。滚到底就说到底了，不许原地打转。"""
        window = self._scroll_window()
        bar = window._comic_reader._strip.verticalScrollBar()
        bar.setValue(bar.maximum())
        pump()
        window._on_autoscroll_end()
        pump()
        self.assertFalse(window.autoscroller.is_running())
        self.assertIn("已经是最后一页了", window.warn_label.text())

    def test_the_end_of_a_comic_does_not_turn_pages(self) -> None:
        """守住那个真实的坑：自动滚动到底后 `_on_autoscroll_end` 会翻页。"""
        window = self._scroll_window()
        bar = window._comic_reader._strip.verticalScrollBar()
        bar.setValue(bar.maximum())
        pump()
        before = window.chapter_index
        window._on_autoscroll_end()
        pump()
        self.assertEqual(window.chapter_index, before)

    def test_a_text_book_still_advances_at_the_chapter_end(self) -> None:
        """反向断言：文本那条路一个字节都没改。"""
        window = self.make_window(path=self.text_path)
        self.assertIsNotNone(window._autoscroll_bar, "文字书照常有滚动条")
        window._on_autoscroll_end()
        pump()
        self.assertEqual(window.chapter_index, 1, "文字书照常接下一章")


class TestWindowBoundaryCommands(ScrollWindowTestCase):
    """模式 B 下"上一页/下一页"是语义一致的边界提示。"""

    def test_next_page_at_the_end_says_so(self) -> None:
        window = self._scroll_window()
        bar = window._comic_reader._strip.verticalScrollBar()
        bar.setValue(bar.maximum())
        pump()
        window.goto_chapter(11)
        pump()
        window.step_chapter(1)
        pump()
        self.assertIn("已经是最后一页了", window.warn_label.text())

    def test_prev_page_at_the_start_says_so(self) -> None:
        window = self._scroll_window()
        window.goto_chapter(0)
        pump()
        window.step_chapter(-1)
        pump()
        self.assertIn("已经是第一页了", window.warn_label.text())

    def test_the_notice_names_the_current_page(self) -> None:
        window = self._scroll_window()
        window.goto_chapter(11)
        pump()
        window.step_chapter(1)
        pump()
        self.assertIn("第 12/12 页", window.warn_label.text())

    def test_single_page_mode_stays_silent(self) -> None:
        """模式 A 的按钮本来就是灰的，再多一句话就是噪音。"""
        window = self.make_window(path=self.cbz_path)
        window.step_chapter(-1)
        pump()
        self.assertEqual(window.warn_label.text(), "")

    def test_a_text_book_stays_silent(self) -> None:
        window = self.make_window(path=self.text_path)
        window.step_chapter(-1)
        pump()
        self.assertEqual(window.warn_label.text(), "")

    def test_the_context_menu_items_still_work(self) -> None:
        window = self._scroll_window()
        window.goto_chapter(0)
        pump()
        window.next_page_action.trigger()
        pump()
        self.assertEqual(window.chapter_index, 1)

    def test_the_menu_items_disable_at_the_ends_in_scroll_mode(self) -> None:
        window = self._scroll_window()
        window.reader_menu.aboutToShow.emit()
        self.assertFalse(window.prev_page_action.isEnabled())
        window.goto_chapter(11)
        pump()
        window.reader_menu.aboutToShow.emit()
        self.assertFalse(window.next_page_action.isEnabled())


class TestWindowScrollModeWheel(ScrollWindowTestCase):
    def test_the_wheel_scrolls_instead_of_turning_a_page(self) -> None:
        """模式 B 下滚轮走系统滚动。"""
        window = self._scroll_window()
        reader = window._comic_reader
        reader._strip.verticalScrollBar().setValue(0)
        pump()
        before_bar = reader._strip.verticalScrollBar().value()
        wheel_on(reader._strip.slot(0), -1)
        pump()
        self.assertGreater(reader._strip.verticalScrollBar().value(), before_bar)

    def test_the_wheel_still_turns_a_page_in_single_mode(self) -> None:
        """反向断言：模式 A 的滚轮翻页行为没变。"""
        window = self.make_window(path=self.cbz_path)
        reader = window._comic_reader
        wheel_on(reader, -1)
        pump()
        self.assertEqual(window.chapter_index, 1)

    def test_a_wheel_at_the_end_shows_the_notice(self) -> None:
        window = self._scroll_window()
        reader = window._comic_reader
        bar = reader._strip.verticalScrollBar()
        bar.setValue(bar.maximum())
        pump()
        wheel_on(reader._strip.slot(11), -1)
        pump()
        self.assertIn("已经是最后一页了", window.warn_label.text())


class TestWindowSharedPathsStillWork(ScrollWindowTestCase):
    """模式 B 下，给文字书写的那些共享路径仍然要优雅降级。"""

    def test_switching_to_a_text_book_keeps_both_readers(self) -> None:
        window = self._scroll_window()
        comic_reader = window._comic_reader
        window.load_path(self.text_path)
        pump()
        self.assertIs(window.reader, window._text_reader)
        self.assertIs(window._comic_reader, comic_reader)
        self.assertIsNotNone(window._autoscroll_bar, "文字书要拿回滚动条")

    def test_reloading_a_comic_in_scroll_mode_keeps_it_a_comic(self) -> None:
        window = self._scroll_window()
        window.goto_chapter(4)
        pump()
        window.reload_current_chapter()
        pump()
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIs(window.reader, window._comic_reader)
        self.assertEqual(window.chapter_index, 4)

    def test_a_bookmark_in_scroll_mode_is_labelled_by_page(self) -> None:
        window = self._scroll_window()
        window.goto_chapter(6)
        pump()
        window.add_bookmark()
        pump()
        marks = window.store.list_for(window.book.id)
        self.assertEqual(marks[0].label, "第 7 页")
        self.assertEqual(marks[0].chapter, 6)

    def test_the_status_bar_still_names_the_page(self) -> None:
        window = self._scroll_window()
        window.goto_chapter(3)
        pump()
        text = window.status_label.full_text()
        self.assertIn(f"page_{4}.png", text)

    def test_the_placeholder_does_not_raise(self) -> None:
        window = self._scroll_window()
        window._show_placeholder()
        pump()

    def test_the_content_alpha_preview_reaches_the_strip(self) -> None:
        """🎚 预览会调 reader 的 content alpha；模式 B 下不许抛。"""
        window = self._scroll_window()
        window._comic_reader.preview_content_alpha(128)
        self.assertAlmostEqual(window._comic_reader.content_opacity(), 128 / 255, places=2)

    def test_apply_style_reaches_the_strip(self) -> None:
        window = self._scroll_window()
        window.settings.fg = "#123456"
        window.apply_preferences()
        pump()

    def test_the_toc_still_opens(self) -> None:
        window = self._scroll_window()
        window.open_toc()
        pump()
        window.close_toc()
        pump()


class TestStage4StaysQtFreeInCore(unittest.TestCase):
    """阶段 4 加的批量读头路径也在 core/ 里，同样不许碰 Qt。"""

    BANNED = ("PyQt5", "PySide6", "minireader.qt", "QtCore", "QtWidgets")

    def test_the_new_core_helpers_do_not_import_qt(self) -> None:
        from minireader.core import archives, comic, images

        for module in (archives, comic, images):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for banned in self.BANNED:
                self.assertNotIn(banned, source, f"{module.__name__} 不许出现 {banned}")


if __name__ == "__main__":
    unittest.main()
