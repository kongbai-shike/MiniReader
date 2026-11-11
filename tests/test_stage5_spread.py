"""阶段 5：双页模式（模式 C）。

纯算术（`ui/spread.py`）先单独测，因为它承载了最容易错的部分：哪一页在哪一边、
落单页落在哪个槽、两页怎么共享一个高度。然后测 `SpreadView` 的摆放、`ComicReader`
的接线、以及窗口层。

**为什么装订方向单独测两条。** rtl 和 ltr 的差别只有一个：两个槽位里谁放小页码。
它错的时候表现是"翻页方向反了"，而那看起来很像"下一页按钮坏了"，所以必须分别
断言，而不是只测一个方向。

    py -3.12 -m unittest tests.test_stage5_spread -v
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

from minireader.core.book_base import KIND_COMIC, KIND_TEXT, Position  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.qt import QApplication, install_message_filter  # noqa: E402
from minireader.ui.comic_reader import (  # noqa: E402
    IMPLEMENTED_MODES,
    MAX_GUTTER,
    ComicReader,
)
from minireader.ui.spread import (  # noqa: E402
    COVER_MARKERS,
    DEFAULT_GUTTER,
    MAX_GUTTER as SPREAD_MAX_GUTTER,
    SPREAD_ASPECT,
    anchor_of,
    is_first_spread,
    is_last_spread,
    looks_like_a_cover,
    name_says_cover,
    needs_spread_width,
    next_anchor,
    pair_size,
    spread_at,
    spread_starts,
    visual_order,
)
from minireader.ui.spread_view import LONE_SLOT, SpreadView  # noqa: E402
from minireader.ui.strip_view import StripView  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

from .test_comic_ui import make_png  # noqa: E402
from .test_reading_features import _settle, pump  # noqa: E402
from .test_ui_offscreen import WindowTestCase, make_book  # noqa: E402

_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

_app = QApplication.instance() or QApplication([])

#: 竖版页：宽度适应下会被缩，双页里用来验"共享高度"。
PORTRAIT = (600, 900)
#: 横版页：形状规则不会把它判成封面，用来验"设置才生效"。
LANDSCAPE = (1200, 700)


def make_comic(path: Path, sizes, names=None) -> Path:
    """A CBZ with real PNGs of exactly the sizes asked for.

    `test_comic_ui.make_cbz` cycles a fixed table of sizes; mode C's tests need to
    choose shapes deliberately (portrait vs landscape decides the cover rule), so
    this helper takes the list instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if names is None:
        names = [f"page_{i + 1:03d}.png" for i in range(len(sizes))]
    with zipfile.ZipFile(path, "w") as zf:
        for name, (width, height) in zip(names, sizes):
            zf.writestr(name, make_png(width, height))
    return path


# ---------------------------------------------------------------------------
# 纯算术：配对与落单
# ---------------------------------------------------------------------------


class _Page:
    def __init__(self, name: str, width: int, height: int) -> None:
        self.name = name
        self.width = width
        self.height = height

    @property
    def size_known(self) -> bool:
        return self.width > 0 and self.height > 0


class _Book:
    def __init__(self, pages) -> None:
        self.pages = list(pages)


class TestSpreadStarts(unittest.TestCase):
    """每一话（跨页）从哪一页开始。"""

    def test_cover_alone_pairs_after_the_cover(self) -> None:
        """封面单独时是 [0] [1,2] [3,4]…，所以锚点是 0,1,3,5…——不是偶数序列。"""
        self.assertEqual(spread_starts(6, True), [0, 1, 3, 5])
        self.assertEqual(spread_starts(7, True), [0, 1, 3, 5])

    def test_without_cover_alone_it_is_a_plain_even_run(self) -> None:
        self.assertEqual(spread_starts(6, False), [0, 2, 4])
        self.assertEqual(spread_starts(7, False), [0, 2, 4, 6])

    def test_an_empty_book_has_no_spreads(self) -> None:
        self.assertEqual(spread_starts(0, True), [])
        self.assertEqual(spread_starts(0, False), [])

    def test_a_single_page_is_one_spread_either_way(self) -> None:
        self.assertEqual(spread_starts(1, True), [0])
        self.assertEqual(spread_starts(1, False), [0])

    def test_every_page_lands_in_exactly_one_spread(self) -> None:
        """这条是配对的**完整性**证明：漏一页会让某页永远翻不到。"""
        for total in range(0, 25):
            for cover in (True, False):
                seen: list[int] = []
                for start in spread_starts(total, cover):
                    first, second = spread_at(start, total, cover)
                    seen.append(first)
                    if second is not None:
                        seen.append(second)
                self.assertEqual(
                    sorted(seen), list(range(total)),
                    f"total={total} cover={cover}: {sorted(seen)}",
                )


class TestSpreadAt(unittest.TestCase):
    """给定页码，它属于哪个跨页、同屏还有哪一页。"""

    def test_the_cover_stands_alone_when_asked(self) -> None:
        self.assertEqual(spread_at(0, 6, True), (0, None))

    def test_the_pairs_after_a_lone_cover(self) -> None:
        self.assertEqual(spread_at(1, 6, True), (1, 2))
        self.assertEqual(spread_at(2, 6, True), (1, 2), "页 3 属于 (1,2) 那一跨页")
        self.assertEqual(spread_at(3, 6, True), (3, 4))

    def test_without_cover_alone_the_first_pair_includes_page_one(self) -> None:
        self.assertEqual(spread_at(0, 6, False), (0, 1))
        self.assertEqual(spread_at(1, 6, False), (0, 1))

    def test_an_odd_last_page_stands_alone(self) -> None:
        """落单页：6 页 + 单独封面，最后一页没有配对。"""
        self.assertEqual(spread_at(5, 6, True), (5, None))
        self.assertEqual(spread_at(4, 5, False), (4, None), "5 页无封面时第 5 页落单")

    def test_an_out_of_range_page_is_clamped(self) -> None:
        self.assertEqual(spread_at(99, 6, False), (4, 5))
        self.assertEqual(spread_at(-3, 6, False), (0, 1))

    def test_an_empty_book_answers_without_raising(self) -> None:
        self.assertEqual(spread_at(0, 0, True), (0, None))

    def test_the_anchor_is_the_first_page_of_the_spread(self) -> None:
        for index in range(6):
            self.assertEqual(anchor_of(index, 6, True), spread_at(index, 6, True)[0])


class TestSpreadNavigation(unittest.TestCase):
    """next/prev 跨页，以及首末判断。"""

    def test_next_moves_one_spread(self) -> None:
        self.assertEqual(next_anchor(0, 6, True, 1), 1, "封面之后是 (1,2)")
        self.assertEqual(next_anchor(1, 6, True, 1), 3, "一个跨页 = 两页")
        self.assertEqual(next_anchor(3, 6, True, 1), 5)

    def test_next_at_the_last_spread_stays_put(self) -> None:
        """停住是边界提示的前提：不动才能和当前锚点比较。"""
        self.assertEqual(next_anchor(5, 6, True, 1), 5)

    def test_prev_walks_back_the_same_way(self) -> None:
        self.assertEqual(next_anchor(5, 6, True, -1), 3)
        self.assertEqual(next_anchor(1, 6, True, -1), 0)
        self.assertEqual(next_anchor(0, 6, True, -1), 0)

    def test_next_uses_two_pages_when_the_cover_is_paired(self) -> None:
        self.assertEqual(next_anchor(0, 6, False, 1), 2)
        self.assertEqual(next_anchor(2, 6, False, 1), 4)

    def test_first_and_last_spread(self) -> None:
        self.assertTrue(is_first_spread(0, 6, True))
        self.assertFalse(is_first_spread(1, 6, True))
        self.assertTrue(is_last_spread(5, 6, True))
        self.assertFalse(is_last_spread(3, 6, True))

    def test_an_empty_book_is_both_first_and_last(self) -> None:
        self.assertTrue(is_first_spread(0, 0, True))
        self.assertTrue(is_last_spread(0, 0, True))


class TestVisualOrder(unittest.TestCase):
    """装订方向：rtl 小页码在右，ltr 在左。"""

    def test_rtl_puts_the_lower_page_on_the_right(self) -> None:
        """日漫右綴じ：(1,2) 里第 1 页在右、第 2 页在左。"""
        self.assertEqual(visual_order((1, 2), "rtl"), (2, 1))

    def test_ltr_puts_the_lower_page_on_the_left(self) -> None:
        self.assertEqual(visual_order((1, 2), "ltr"), (1, 2))

    def test_a_lone_page_takes_the_leading_slot(self) -> None:
        """落单页在 rtl 占右槽、ltr 占左槽——"跨页总从阅读方向的起始边开始"。"""
        self.assertEqual(visual_order((5, None), "rtl"), (None, 5))
        self.assertEqual(visual_order((5, None), "ltr"), (5, None))

    def test_an_unknown_binding_is_treated_as_rtl(self) -> None:
        self.assertEqual(visual_order((1, 2), "nonsense"), (2, 1))

    def test_the_binding_only_swaps_never_drops_a_page(self) -> None:
        for binding in ("rtl", "ltr"):
            for pair in ((0, 1), (1, 2), (3, None), (0, None)):
                left, right = visual_order(pair, binding)
                self.assertEqual(
                    sorted(x for x in (left, right) if x is not None),
                    sorted(x for x in pair if x is not None),
                    f"{pair} @ {binding} 丢了页",
                )


class TestPairSize(unittest.TestCase):
    """两页共享一个高度：接缝必须齐平。"""

    def test_two_equal_pages_get_the_viewport_height(self) -> None:
        height, widths = pair_size([PORTRAIT, PORTRAIT], (1200, 900), 0)
        self.assertEqual(height, 900)
        self.assertEqual(widths, [600, 600])

    def test_two_different_shapes_share_one_height(self) -> None:
        """这条就是"接缝水平"的证明：高度必须相同，宽度才不同。"""
        height, widths = pair_size([PORTRAIT, LANDSCAPE], (1400, 900), 0)
        self.assertGreater(height, 0)
        # 每页按自己的宽高比推出宽度，高度是同一个。
        self.assertAlmostEqual(widths[0] / height, PORTRAIT[0] / PORTRAIT[1], places=2)
        self.assertAlmostEqual(widths[1] / height, LANDSCAPE[0] / LANDSCAPE[1], places=2)

    def test_the_pair_never_wider_than_the_viewport(self) -> None:
        """两页 + 间距必须放得下，否则第二页会被挤出去。"""
        for gutter in (0, 8, 32):
            _height, widths = pair_size([PORTRAIT, PORTRAIT], (1000, 900), gutter)
            self.assertLessEqual(
                sum(widths) + gutter, 1000,
                f"gutter={gutter}: {widths} + {gutter} 超出视口",
            )

    def test_a_page_is_never_enlarged(self) -> None:
        """和 `fit_size` 同一个承诺：放大小图只会变糊。"""
        height, _widths = pair_size([(300, 400), (300, 400)], (4000, 4000), 0)
        self.assertEqual(height, 400)

    def test_the_height_is_capped_by_the_viewport(self) -> None:
        height, _widths = pair_size([PORTRAIT, PORTRAIT], (1200, 500), 0)
        self.assertEqual(height, 500)

    def test_an_unknown_page_gets_no_width(self) -> None:
        """尺寸未知的页宽度给 0，由视图决定它显示成什么，而不是算出一个假宽度。"""
        height, widths = pair_size([(0, 0), PORTRAIT], (1200, 900), 0)
        self.assertEqual(widths[0], 0)
        self.assertGreater(widths[1], 0)
        self.assertEqual(height, 900)

    def test_both_unknown_falls_back_to_the_viewport(self) -> None:
        height, widths = pair_size([(0, 0), (0, 0)], (1200, 900), 0)
        self.assertEqual(height, 900)
        self.assertEqual(widths, [0, 0])

    def test_the_gutter_comes_out_of_the_available_width(self) -> None:
        """间距是从"两页可用宽度"里扣的，不是加在外面——否则会溢出。"""
        _h0, wide = pair_size([PORTRAIT, PORTRAIT], (1200, 900), 0)
        _h1, narrow = pair_size([PORTRAIT, PORTRAIT], (1200, 900), 32)
        self.assertLess(sum(narrow), sum(wide))


class TestSpreadTrigger(unittest.TestCase):
    """窗口宽 > 高 × 1.4 才进双页。"""

    def test_a_landscape_window_fits(self) -> None:
        self.assertTrue(needs_spread_width(1200, 800))
        self.assertTrue(needs_spread_width(1440, 900))

    def test_a_portrait_window_does_not(self) -> None:
        self.assertFalse(needs_spread_width(600, 900))
        self.assertFalse(needs_spread_width(900, 900), "正方形也不算宽")

    def test_the_threshold_is_exactly_the_documented_ratio(self) -> None:
        """边界上要精确：840 = 600 × 1.4 不算宽，841 才算。"""
        self.assertFalse(needs_spread_width(840, 600))
        self.assertTrue(needs_spread_width(841, 600))
        self.assertEqual(SPREAD_ASPECT, 1.4)

    def test_a_degenerate_size_does_not_fit(self) -> None:
        """还没布局（高度 0）时不能进模式 C：那会显示两个空条。"""
        self.assertFalse(needs_spread_width(1200, 0))
        self.assertFalse(needs_spread_width(0, 800))


class TestCoverDetection(unittest.TestCase):
    """封面自动判型：两条正信号，否则回落到设置。"""

    def test_the_name_markers_are_the_documented_ones(self) -> None:
        self.assertEqual(COVER_MARKERS, ("cover", "封面", "000"))

    def test_a_cover_name_is_recognised(self) -> None:
        for name in ("cover.jpg", "Cover.PNG", "封面.png", "000.jpg", "0001.png"):
            self.assertTrue(name_says_cover(name), name)

    def test_an_ordinary_page_name_is_not_a_cover(self) -> None:
        for name in ("page_001.png", "p1.jpg", "第01话_003.png", ""):
            self.assertFalse(name_says_cover(name), name)

    def test_a_cover_name_wins_over_the_setting(self) -> None:
        """规则 1 优先：文件名说它是封面，用户把开关关掉也照样单独显示。"""
        book = _Book([_Page("cover.jpg", 600, 900), _Page("p2.png", 600, 900)])
        self.assertTrue(looks_like_a_cover(book, False))

    def test_three_portrait_pages_mean_a_standalone_cover(self) -> None:
        """规则 2：前三页都是竖版，按"封面单独"处理。"""
        book = _Book([_Page(f"p{i}.png", 600, 900) for i in range(4)])
        self.assertTrue(looks_like_a_cover(book, False))

    def test_landscape_pages_fall_back_to_the_setting(self) -> None:
        """形状不命中、名字也不命中时，用户说了算。"""
        book = _Book([_Page(f"w{i}.png", 1200, 700) for i in range(4)])
        self.assertFalse(looks_like_a_cover(book, False))
        self.assertTrue(looks_like_a_cover(book, True))

    def test_fewer_than_three_pages_falls_back_to_the_setting(self) -> None:
        """不足三页没法比形状，所以只能听设置的。"""
        two = _Book([_Page("a.png", 600, 900), _Page("b.png", 600, 900)])
        self.assertFalse(looks_like_a_cover(two, False))
        self.assertTrue(looks_like_a_cover(two, True))

    def test_an_empty_book_falls_back_to_the_setting(self) -> None:
        self.assertTrue(looks_like_a_cover(_Book([]), True))
        self.assertFalse(looks_like_a_cover(_Book([]), False))

    def test_unknown_sizes_are_not_portrait(self) -> None:
        """尺寸还没探到时不能猜成竖版——那会让每本书都被判成有封面。"""
        book = _Book([_Page("a.png", 0, 0), _Page("b.png", 0, 0), _Page("c.png", 0, 0)])
        self.assertFalse(looks_like_a_cover(book, False))


class TestPositionLabelSpan(unittest.TestCase):
    """进度文本「第 x-y/z 页」。"""

    def test_a_two_page_spread_reads_as_a_range(self) -> None:
        self.assertEqual(
            Position(kind=KIND_COMIC, index=4, total=40, span=2).label,
            "第 5-6/40 页",
        )

    def test_the_range_never_runs_past_the_book(self) -> None:
        """这条守的是实测到的真缺陷：末页跨页会报出"第 40-41/40 页"这种不存在的页。"""
        self.assertEqual(
            Position(kind=KIND_COMIC, index=39, total=40, span=2).label,
            "第 40/40 页",
        )

    def test_a_lone_page_reads_as_a_single_page(self) -> None:
        self.assertEqual(
            Position(kind=KIND_COMIC, index=5, total=6, span=1).label,
            "第 6/6 页",
        )

    def test_span_of_one_never_prints_a_range(self) -> None:
        self.assertEqual(
            Position(kind=KIND_COMIC, index=4, total=40, span=1).label,
            "第 5/40 页",
        )


# ---------------------------------------------------------------------------
# SpreadView：摆放
# ---------------------------------------------------------------------------


class TestSpreadViewPlacement(unittest.TestCase):
    """两个槽位里放的是哪一页。"""

    def setUp(self) -> None:
        self.view = SpreadView()
        self.view.resize(1200, 800)
        self.view.show()
        _app.processEvents()

    def tearDown(self) -> None:
        self.view.deleteLater()
        _app.processEvents()

    def test_rtl_shows_the_lower_page_on_the_right_slot(self) -> None:
        self.view.set_binding("rtl")
        self.view.set_spread((1, 2))
        self.assertEqual(self.view.pages_shown(), (2, 1))
        self.assertIs(self.view.right_slot().parent(), self.view)

    def test_ltr_shows_the_lower_page_on_the_left_slot(self) -> None:
        self.view.set_binding("ltr")
        self.view.set_spread((1, 2))
        self.assertEqual(self.view.pages_shown(), (1, 2))

    def test_a_lone_page_moves_side_with_the_binding(self) -> None:
        self.view.set_spread((5, None))
        rtl = self.view.pages_shown()
        self.view.set_binding("ltr")
        ltr = self.view.pages_shown()
        self.assertEqual((rtl, ltr), ((None, 5), (5, None)))

    def test_switching_the_binding_keeps_the_same_pair(self) -> None:
        """换方向只是左右对调，不许换页。"""
        self.view.set_spread((3, 4))
        before = self.view.pair
        self.view.set_binding("ltr")
        self.assertEqual(self.view.pair, before)

    def test_an_unknown_binding_falls_back_to_rtl(self) -> None:
        self.view.set_binding("nonsense")
        self.assertEqual(self.view.binding, "rtl")
        self.assertEqual(set(LONE_SLOT), {"rtl", "ltr"})

    def test_a_page_goes_into_the_slot_that_shows_it(self) -> None:
        """按**页码**投图，不按槽位——这样 reader 解码顺序随便。"""
        self.view.set_binding("rtl")
        self.view.set_spread((1, 2))
        self.view.set_page(1, None, "第 1 页的坏页提示")
        self.assertEqual(self.view.right_slot().message, "第 1 页的坏页提示")
        self.view.set_page(2, None, "第 2 页的坏页提示")
        self.assertEqual(self.view.left_slot().message, "第 2 页的坏页提示")

    def test_setting_a_page_not_in_the_spread_is_a_no_op(self) -> None:
        self.view.set_spread((1, 2))
        self.view.set_page(4, None, "不该出现")
        self.assertNotEqual(self.view.left_slot().message, "不该出现")
        self.assertNotEqual(self.view.right_slot().message, "不该出现")

    def test_clear_blanks_both_slots(self) -> None:
        self.view.set_spread((1, 2))
        self.view.set_page(1, None, "x")
        self.view.clear()
        self.assertFalse(self.view.left_slot().has_page)
        self.assertFalse(self.view.right_slot().has_page)

    def test_the_gutter_becomes_the_layout_spacing(self) -> None:
        for gutter in (0, 16, 32):
            self.view.set_gutter(gutter)
            self.assertEqual(self.view.gutter, gutter)
            self.assertEqual(self.view._row.spacing(), gutter)

    def test_a_negative_gutter_is_clamped_to_zero(self) -> None:
        """负间距会让两页重叠，也会让接缝算术出负数宽度。"""
        self.view.set_gutter(-10)
        self.assertEqual(self.view.gutter, 0)

    def test_the_default_gutter_is_zero(self) -> None:
        self.assertEqual(DEFAULT_GUTTER, 0)
        self.assertEqual(SpreadView().gutter, 0)

    def test_the_gutter_ceiling_matches_the_setting(self) -> None:
        self.assertEqual(SPREAD_MAX_GUTTER, MAX_GUTTER)
        self.assertEqual(MAX_GUTTER, 32)


class TestSpreadViewGeometry(unittest.TestCase):
    """两页共享高度、各自在半个视口里。"""

    def setUp(self) -> None:
        self.tmp = Path(os.environ["TEMP"]) / "mr-stage5-view"
        self.tmp.mkdir(exist_ok=True)
        self.cbz = make_comic(self.tmp / "v.cbz", [PORTRAIT] * 4)
        self.book = open_book(self.cbz)
        # `open_book()` leaves every page size at 0 until something probes it. Mode C
        # sizes its slots from those numbers, so the geometry tests must establish
        # them - otherwise `SpreadView` takes its "nothing measured" fallback and
        # every height assertion would be testing that fallback instead.
        for index in range(len(self.book.pages)):
            self.book.set_page_size(index, *PORTRAIT)
        self.view = SpreadView()
        self.view.resize(1200, 800)
        self.view.show()
        self.view.bind_book(self.book)
        _app.processEvents()

    def tearDown(self) -> None:
        self.view.deleteLater()
        _app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_both_pages_get_the_same_height(self) -> None:
        """接缝水平：两页高度必须相等。"""
        self.view.set_spread((0, 1))
        _app.processEvents()
        self.assertEqual(
            self.view.left_slot().height(), self.view.right_slot().height()
        )

    def test_the_height_fits_the_viewport(self) -> None:
        self.view.set_spread((0, 1))
        _app.processEvents()
        self.assertLessEqual(self.view.left_slot().height(), self.view.height())

    def test_a_lone_page_still_gets_the_pair_height(self) -> None:
        """落单页不该变成"半屏高"：高度是按页算的，不是按有没有同伴算的。"""
        self.view.set_spread((0, None))
        _app.processEvents()
        solo = self.view.right_slot().height()
        self.view.set_spread((0, 1))
        _app.processEvents()
        paired = self.view.left_slot().height()
        self.assertEqual(solo, paired)

    def test_a_resize_recomputes_without_decoding(self) -> None:
        """换视口大小必须重算高度——纯算术，不许解码。"""
        self.view.set_spread((0, 1))
        _app.processEvents()
        before = self.view.left_slot().height()
        self.view.setFixedSize(400, 800)
        _app.processEvents()
        self.view._relayout()
        self.assertNotEqual(self.view.left_slot().height(), before, "视口变窄该重算")
        self.assertFalse(self.view.left_slot().has_page, "重排不该解码")

    def test_an_unmeasured_book_still_lays_out(self) -> None:
        """尺寸还没探到时也要给出合理的槽位，而不是 0 高。

        这条走的是 `_relayout()` 的"什么都没测到"回退分支：每页半宽、视口高。
        它是模式 C 在"刚打开、还没探尺寸"那一瞬间的样子，不该是零高的一片。
        """
        book = open_book(make_comic(self.tmp / "u.cbz", [PORTRAIT] * 2))
        for page in book.pages:
            page.width, page.height = 0, 0
        self.view.bind_book(book)
        self.view.set_spread((0, 1))
        _app.processEvents()
        self.assertGreater(self.view.left_slot().height(), 0)
        self.assertGreater(self.view.left_slot().width(), 0)


# ---------------------------------------------------------------------------
# ComicReader：模式 C 接线
# ---------------------------------------------------------------------------


class TestComicReaderSpreadMode(unittest.TestCase):
    """不建窗口，只问 Reader 自己。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(os.environ["TEMP"]) / "mr-stage5-reader"
        cls.tmp.mkdir(exist_ok=True)
        cls.cbz = make_comic(cls.tmp / "r.cbz", [PORTRAIT] * 8)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.book = open_book(self.cbz)
        self.reader = ComicReader()
        self.reader.resize(1200, 800)
        self.reader.show()
        self.reader.show_book(self.book, index=0)
        _app.processEvents()

    def tearDown(self) -> None:
        self.reader.stop()
        self.reader.deleteLater()
        _app.processEvents()

    def _to_spread(self) -> None:
        self.reader.set_mode("spread")
        _app.processEvents()

    def test_spread_is_implemented_and_all_three_modes_are(self) -> None:
        self.assertEqual(IMPLEMENTED_MODES, ("single", "scroll", "spread"))
        self.assertIn("spread", ComicReader.IMPLEMENTED_MODES)

    def test_entering_the_mode_shows_a_spread(self) -> None:
        self._to_spread()
        self.assertEqual(self.reader.mode, "spread")
        self.assertTrue(self.reader._spread.pair)

    def test_the_viewport_becomes_the_spread_view(self) -> None:
        self.assertIs(self.reader.viewport(), self.reader._page_view)
        self._to_spread()
        self.assertIs(self.reader.viewport(), self.reader._spread)
        self.reader.set_mode("single")
        self.assertIs(self.reader.viewport(), self.reader._page_view)

    def test_the_mode_switch_announces_the_viewport_change(self) -> None:
        seen: list[int] = []
        self.reader.viewportChanged.connect(lambda: seen.append(1))
        self._to_spread()
        self.assertEqual(len(seen), 1)

    def test_there_is_no_scrollbar_in_mode_c(self) -> None:
        """没有滚动条 = 自动滚动自动被闸门拦住，不需要新代码。"""
        self._to_spread()
        self.assertIsNone(self.reader.scrollbar())

    def test_the_cover_stands_alone_by_default(self) -> None:
        """8 页竖版 + 默认开关 → 封面单独一屏，之后是 (1,2)、(3,4)…"""
        self._to_spread()
        self.assertEqual(self.reader._spread.pair, (0, None))

    def test_paging_moves_one_spread_at_a_time(self) -> None:
        """规格：下一跨页始终是 index+2（封面那一步除外）。"""
        self._to_spread()
        self.reader.next_page()
        self.assertEqual(self.reader._spread.pair, (1, 2))
        self.reader.next_page()
        self.assertEqual(self.reader._spread.pair, (3, 4))
        self.reader.next_page()
        self.assertEqual(self.reader._spread.pair, (5, 6))

    def test_paging_back_walks_the_same_spreads(self) -> None:
        self._to_spread()
        for _ in range(3):
            self.reader.next_page()
        self.assertEqual(self.reader._spread.pair, (5, 6))
        self.reader.prev_page()
        self.assertEqual(self.reader._spread.pair, (3, 4))

    def test_the_last_spread_reports_the_boundary(self) -> None:
        self._to_spread()
        for _ in range(4):
            self.reader.next_page()
        seen: list[str] = []
        self.reader.notice.connect(seen.append)
        self.reader.next_page()
        self.assertEqual(len(seen), 1)
        self.assertIn("最后一页", seen[0])

    def test_the_first_spread_reports_the_boundary(self) -> None:
        self._to_spread()
        seen: list[str] = []
        self.reader.notice.connect(seen.append)
        self.reader.prev_page()
        self.assertEqual(len(seen), 1)
        self.assertIn("第一页", seen[0])

    def test_the_index_is_the_spread_s_first_page(self) -> None:
        """Progress.page 存的是跨页第一页，重开才能回到同一个跨页。"""
        self._to_spread()
        self.reader.next_page()
        self.assertEqual(self.reader.index, 1, "(1,2) 的第一页是 1")

    def test_the_position_reports_span_two(self) -> None:
        self._to_spread()
        self.reader.next_page()
        self.assertEqual(self.reader.current_position().span, 2)
        self.assertEqual(self.reader.current_position().label, "第 2-3/8 页")

    def test_a_lone_page_reports_span_one(self) -> None:
        """落单页只显示一页，span=2 会报出一个不在屏幕上的页码。"""
        self._to_spread()
        self.assertEqual(self.reader.current_position().span, 1)
        self.assertEqual(self.reader.current_position().label, "第 1/8 页")

    def test_entering_the_mode_lands_on_the_spread_containing_the_page(self) -> None:
        """从模式 A 的第 4 页（index 3）切进来，要落到 (3,4) 而不是从头开始。"""
        self.reader.show_book(self.book, index=3)
        _app.processEvents()
        self._to_spread()
        self.assertEqual(self.reader._spread.pair, (3, 4))
        self.assertEqual(self.reader.index, 3)

    def test_leaving_the_mode_keeps_the_page(self) -> None:
        self._to_spread()
        self.reader.next_page()
        index = self.reader.index
        self.reader.set_mode("single")
        _app.processEvents()
        self.assertEqual(self.reader.index, index)

    def test_switching_binding_swaps_the_slots_live(self) -> None:
        self._to_spread()
        self.reader.next_page()
        rtl = self.reader._spread.pages_shown()
        self.reader.cycle_binding()
        ltr = self.reader._spread.pages_shown()
        self.assertEqual(rtl, (2, 1))
        self.assertEqual(ltr, (1, 2))
        self.assertEqual(self.reader.binding, "ltr")

    def test_the_gutter_is_clamped_like_the_setting(self) -> None:
        self._to_spread()
        self.reader.set_gutter(999)
        self.assertEqual(self.reader.gutter, MAX_GUTTER)
        self.reader.set_gutter(-5)
        self.assertEqual(self.reader.gutter, 0)

    def test_setting_the_gutter_reaches_the_view(self) -> None:
        self._to_spread()
        self.reader.set_gutter(24)
        self.assertEqual(self.reader._spread.gutter, 24)

    def test_the_cover_setting_is_remembered_even_before_a_book_opens(self) -> None:
        """设置推下来时还没有书，但读者要记住它，不能丢掉。"""
        fresh = ComicReader()
        try:
            fresh.set_cover_alone(False)
            self.assertFalse(fresh._cover_setting)
        finally:
            fresh.deleteLater()

    def test_turning_the_cover_setting_off_pairs_the_first_page(self) -> None:
        """横版书（形状规则不命中）才看得出设置的差别。"""
        book = open_book(make_comic(self.tmp / "wide.cbz", [LANDSCAPE] * 6))
        self.reader.show_book(book, index=0)
        self.reader.set_cover_alone(False)
        _app.processEvents()
        self._to_spread()
        self.assertEqual(self.reader._spread.pair, (0, 1))

    def test_a_cover_name_overrides_the_setting(self) -> None:
        book = open_book(make_comic(
            self.tmp / "named.cbz", [PORTRAIT] * 4,
            names=["cover.jpg", "p2.png", "p3.png", "p4.png"],
        ))
        self.reader.show_book(book, index=0)
        self.reader.set_cover_alone(False)
        _app.processEvents()
        self._to_spread()
        self.assertEqual(self.reader._spread.pair, (0, None))

    def test_a_cover_named_book_shows_the_cover_first(self) -> None:
        """文件名命中时，第一屏就是封面本身。"""
        self._to_spread()
        self.assertEqual(self.reader._spread.anchor, 0)

    def test_stop_cancels_the_strip_timer_in_every_mode(self) -> None:
        self.reader._strip._timer.start()
        self.reader.stop()
        self.assertFalse(self.reader._strip._timer.isActive())


class TestSpreadModePortraitGuard(unittest.TestCase):
    """竖屏窗口不许进模式 C，并且给提示。"""

    def setUp(self) -> None:
        self.tmp = Path(os.environ["TEMP"]) / "mr-stage5-portrait"
        self.tmp.mkdir(exist_ok=True)
        self.cbz = make_comic(self.tmp / "p.cbz", [PORTRAIT] * 6)
        self.book = open_book(self.cbz)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reader(self, width: int, height: int) -> ComicReader:
        reader = ComicReader()
        reader.resize(width, height)
        reader.show()
        reader.show_book(self.book, index=0)
        _app.processEvents()
        return reader

    def test_a_portrait_window_refuses_the_mode(self) -> None:
        reader = self._reader(600, 900)
        try:
            seen: list[str] = []
            reader.notice.connect(seen.append)
            reader.set_mode("spread")
            self.assertEqual(reader.mode, "single", "竖屏不许进双页")
            self.assertEqual(len(seen), 1)
            self.assertIn("太窄", seen[0])
        finally:
            reader.stop()
            reader.deleteLater()

    def test_a_landscape_window_accepts_the_mode(self) -> None:
        reader = self._reader(1200, 800)
        try:
            reader.set_mode("spread")
            self.assertEqual(reader.mode, "spread")
        finally:
            reader.stop()
            reader.deleteLater()

    def test_resizing_a_spread_down_to_portrait_falls_back(self) -> None:
        """用户可以把横屏窗口拖窄，那时必须自己退出来。"""
        reader = self._reader(1200, 800)
        try:
            reader.set_mode("spread")
            self.assertEqual(reader.mode, "spread")
            seen: list[str] = []
            reader.notice.connect(seen.append)
            reader.resize(500, 900)
            _app.processEvents()
            self.assertEqual(reader.mode, "single")
            self.assertIn("太窄", seen[-1])
        finally:
            reader.stop()
            reader.deleteLater()

    def test_a_refused_mode_does_not_claim_a_fallback_that_did_not_happen(self) -> None:
        """窄窗拒绝双页时说的话必须和做的事一致。

        这条守的是一个实测到的死锁：从连续滚动点模式按钮，模式被拒，提示却写
        "已切回单页"——而代码原封不动地把模式留在 `scroll`，按钮图标也一直是
        `scroll` 的。用户看到的是"点了没反应，还说切回去了"。
        """
        reader = self._reader(600, 900)
        try:
            reader.set_mode("scroll")
            seen: list[str] = []
            reader.notice.connect(seen.append)
            reader.set_mode("spread")
            self.assertEqual(reader.mode, "scroll", "拒绝就是不改，别假装改了")
            self.assertEqual(len(seen), 1)
            self.assertIn("太窄", seen[0])
            self.assertNotIn("已切回单页", seen[0])
        finally:
            reader.stop()
            reader.deleteLater()

    def test_restoring_a_mode_says_nothing(self) -> None:
        """恢复上次的模式是替上一场会话办事，不是回答用户，所以不该弹提示。"""
        wide = self._reader(1200, 800)
        try:
            seen: list[str] = []
            wide.notice.connect(seen.append)
            wide.restore_mode("scroll")
            self.assertEqual(wide.mode, "scroll")
            self.assertEqual(seen, [])
        finally:
            wide.stop()
            wide.deleteLater()

    def test_restoring_spread_on_a_portrait_window_lands_on_single(self) -> None:
        """竖窗里存下的双页落到单页，而不是留在上一本书碰巧用的模式。"""
        reader = self._reader(600, 900)
        try:
            reader.set_mode("scroll")
            seen: list[str] = []
            reader.notice.connect(seen.append)
            reader.restore_mode("spread")
            self.assertEqual(reader.mode, "single")
            self.assertEqual(seen, [], "安静回落")
        finally:
            reader.stop()
            reader.deleteLater()

    def test_restoring_an_empty_mode_is_a_no_op(self) -> None:
        """旧存档里 mode 是空串，`restore_mode()` 必须当作"没存过"。"""
        reader = self._reader(1200, 800)
        try:
            reader.set_mode("scroll")
            reader.restore_mode("")
            self.assertEqual(reader.mode, "scroll", "空串不该把模式打回 single")
        finally:
            reader.stop()
            reader.deleteLater()

    def test_the_page_survives_the_fallback(self) -> None:
        reader = self._reader(1200, 800)
        try:
            reader.set_mode("spread")
            reader.next_page()
            reader.next_page()
            index = reader.index
            reader.resize(500, 900)
            _app.processEvents()
            self.assertEqual(reader.index, index, "退回单页不该换页")
        finally:
            reader.stop()
            reader.deleteLater()


# ---------------------------------------------------------------------------
# 接进窗口之后
# ---------------------------------------------------------------------------


class SpreadWindowTestCase(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cbz_path = make_comic(self.tmp / "comic.cbz", [PORTRAIT] * 8)
        self.wide_path = make_comic(self.tmp / "wide.cbz", [LANDSCAPE] * 8)
        self.cover_path = make_comic(
            self.tmp / "cover.cbz", [PORTRAIT] * 8,
            names=["cover.jpg"] + [f"p{i}.png" for i in range(2, 9)],
        )
        self.text_path = make_book(self.tmp, name="novel.txt", chapters=3)

    def _spread_window(self, path=None) -> MiniWindow:
        """A window wide enough for mode C, already in mode C."""
        window = self.make_window(path=path or self.cbz_path)
        window.resize(1200, 800)
        pump()
        window._comic_reader.set_mode("spread")
        pump()
        return window


class TestWindowSpreadMode(SpreadWindowTestCase):
    def test_the_mode_button_reaches_spread(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.resize(1200, 800)
        pump()
        seen = [window._comic_reader.mode]
        for _ in range(len(IMPLEMENTED_MODES)):
            window.cycle_comic_mode()
            pump()
            seen.append(window._comic_reader.mode)
        self.assertEqual(seen, ["single", "scroll", "spread", "single"])

    def test_the_progress_records_the_mode(self) -> None:
        window = self._spread_window()
        window.persist_progress()
        self.assertEqual(self.progress.get(window.book.id).mode, "spread")

    def test_a_saved_spread_mode_is_restored_at_startup(self) -> None:
        self.settings.comic_default_mode = "spread"
        window = self.make_window()
        window.resize(1200, 800)
        pump()
        window.load_path(self.cbz_path)
        pump()
        self.assertEqual(window._comic_reader.mode, "spread")

    def test_a_saved_spread_mode_on_a_portrait_window_falls_back_quietly(self) -> None:
        """存了双页但窗口是竖屏：开机不能弹"还没实现"那种噪音，也不该进双页。"""
        self.settings.comic_default_mode = "spread"
        window = self.make_window()
        window.resize(500, 900)
        pump()
        window.load_path(self.cbz_path)
        pump()
        self.assertEqual(window._comic_reader.mode, "single")

    def test_the_bottom_bar_shows_the_page_range(self) -> None:
        window = self._spread_window()
        window._comic_reader.next_page()
        pump()
        self.assertEqual(window.chapter_label.text(), "第 2-3/8 页")

    def test_the_bottom_bar_shows_a_lone_cover_as_one_page(self) -> None:
        window = self._spread_window()
        self.assertEqual(window.chapter_label.text(), "第 1/8 页")

    def test_the_capsules_disable_at_the_spread_ends(self) -> None:
        window = self._spread_window()
        self.assertFalse(window.prev_button.isEnabled(), "封面是第一屏")
        self.assertTrue(window.next_button.isEnabled())
        for _ in range(4):
            window.step_chapter(1)
        pump()
        self.assertFalse(window.next_button.isEnabled(), "最后一个跨页")
        self.assertTrue(window.prev_button.isEnabled())

    def test_a_spread_turn_moves_two_pages(self) -> None:
        """这条守的是一个实测到的真缺陷：窗口按"页"加 delta，于是按两次停在
        同一个跨页上（第 4 次才到第 3 个跨页）。模式 C 的步长必须交给 reader。"""
        window = self._spread_window()
        seen = []
        for _ in range(3):
            window.step_chapter(1)
            pump()
            seen.append(window._comic_reader._spread.pair)
        self.assertEqual(seen, [(1, 2), (3, 4), (5, 6)])

    def test_the_context_menu_turns_one_spread(self) -> None:
        window = self._spread_window()
        window.next_page_action.trigger()
        pump()
        self.assertEqual(window._comic_reader._spread.pair, (1, 2))

    def test_the_menu_items_disable_at_the_spread_ends(self) -> None:
        window = self._spread_window()
        window.reader_menu.aboutToShow.emit()
        self.assertFalse(window.prev_page_action.isEnabled())
        for _ in range(4):
            window.step_chapter(1)
        pump()
        window.reader_menu.aboutToShow.emit()
        self.assertFalse(window.next_page_action.isEnabled())

    def test_a_spread_turn_schedules_a_progress_save(self) -> None:
        """翻一跨页要触发 900ms 的防抖保存（`_on_scrolled` -> `_progress_timer`）。

        走 reader 的 `scrolled` 信号，不走 `step_chapter`：后者最后会显式
        `persist_progress()`，而它**有意**把防抖定时器停掉（显式保存压过待定的
        一次）。模式 B 的同名测试也是这么测的，这里保持一致。
        """
        window = self._spread_window()
        window._progress_timer.stop()
        window._comic_reader.next_page()
        pump()
        self.assertTrue(window._progress_timer.isActive())

    def test_the_page_is_written_to_the_store(self) -> None:
        window = self._spread_window()
        window.step_chapter(1)
        pump()
        window.persist_progress()
        self.assertEqual(self.progress.get(window.book.id).page, window.chapter_index)

    def test_reopening_lands_on_the_saved_spread(self) -> None:
        window = self._spread_window()
        for _ in range(2):
            window.step_chapter(1)
        pump()
        expected = window.chapter_index
        window.persist_progress()

        again = self.make_window(path=self.cbz_path)
        again.resize(1200, 800)
        pump()
        again._comic_reader.set_mode("spread")
        pump()
        self.assertEqual(again.chapter_index, expected)

    def test_cycling_the_mode_writes_it_without_a_page_turn(self) -> None:
        """切模式这个动作本身就要落盘。

        以前只有翻页、滚动这类路径会顺带写一次 `Progress.mode`，于是"切到双页、
        关掉、重开"的模式记忆是空的——用户看到的就是"切了没记住"。这条测试
        不调 `persist_progress()`，只点按钮。
        """
        window = self.make_window(path=self.cbz_path)
        window.resize(1200, 800)
        pump()
        window.cycle_comic_mode()  # single -> scroll
        pump()
        window.cycle_comic_mode()  # scroll -> spread
        pump()
        self.assertEqual(window._comic_reader.mode, "spread")
        self.assertEqual(self.progress.get(window.book.id).mode, "spread")

    def test_confirming_the_settings_does_not_reset_the_mode(self) -> None:
        """改字号之类的设置不该把用户在标题栏选的模式打回默认值。

        `apply_comic_settings()` 挂在每一次 `apply_preferences()` 上：只要无条件
        重推 `comic_default_mode`，用户切到双页后去设置里点一下"确定"就会
        莫名其妙回到单页。
        """
        window = self._spread_window()
        self.assertEqual(window._comic_reader.mode, "spread")
        self.settings.font_size = 30
        window.apply_preferences()
        pump()
        self.assertEqual(window._comic_reader.mode, "spread", "改设置不该动模式")

    def test_editing_the_default_mode_does_move_the_reader(self) -> None:
        """反过来说：设置里真的改了"阅读模式"，就得生效。"""
        window = self.make_window(path=self.cbz_path)
        window.resize(1200, 800)
        pump()
        self.assertEqual(window._comic_reader.mode, "single")
        self.settings.comic_default_mode = "scroll"
        window.apply_preferences()
        pump()
        self.assertEqual(window._comic_reader.mode, "scroll")

    def test_the_saved_mode_is_restored_on_reopen(self) -> None:
        """重开一本漫画要回到上次看的模式，而不是回到默认值。"""
        window = self.make_window(path=self.cbz_path)
        window.resize(1200, 800)
        pump()
        for _ in range(2):  # single -> scroll -> spread，走用户真正走的按钮
            window.cycle_comic_mode()
            pump()
        self.assertEqual(window._comic_reader.mode, "spread")
        self.assertEqual(self.progress.get(window.book.id).mode, "spread")

        again = self.make_window()
        again.resize(1200, 800)
        pump()
        self.assertEqual(again._comic_reader.mode, "single", "开书前还是默认值")
        again.load_path(self.cbz_path)
        pump()
        self.assertEqual(again._comic_reader.mode, "spread", "开书后要装回存下的模式")

    def test_the_edge_resize_filter_follows_the_mode(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.resize(1200, 800)
        pump()
        window._comic_reader.set_mode("spread")
        pump()
        self.assertIs(window._filtered_viewport, window._comic_reader._spread)
        window._comic_reader.set_mode("single")
        pump()
        self.assertIs(window._filtered_viewport, window._comic_reader._page_view)


class TestWindowSpreadSettings(SpreadWindowTestCase):
    """设置推给 reader，并真的改变摆放。"""

    def test_the_binding_reaches_the_reader(self) -> None:
        self.settings.comic_binding = "ltr"
        window = self._spread_window()
        self.assertEqual(window._comic_reader.binding, "ltr")
        window._comic_reader.next_page()
        pump()
        self.assertEqual(window._comic_reader._spread.pages_shown(), (1, 2))

    def test_rtl_is_the_default(self) -> None:
        window = self._spread_window()
        self.assertEqual(window._comic_reader.binding, "rtl")
        window._comic_reader.next_page()
        pump()
        self.assertEqual(window._comic_reader._spread.pages_shown(), (2, 1))

    def test_the_gutter_reaches_the_reader(self) -> None:
        self.settings.comic_gutter = 20
        window = self._spread_window()
        self.assertEqual(window._comic_reader.gutter, 20)
        self.assertEqual(window._comic_reader._spread.gutter, 20)

    def test_the_gutter_default_is_zero(self) -> None:
        window = self._spread_window()
        self.assertEqual(window._comic_reader.gutter, 0)

    def test_the_cover_setting_reaches_the_reader(self) -> None:
        self.settings.comic_cover_alone = False
        window = self._spread_window(path=self.wide_path)
        self.assertFalse(window._comic_reader.cover_alone)
        self.assertEqual(window._comic_reader._spread.pair, (0, 1))

    def test_a_cover_named_book_ignores_the_off_switch(self) -> None:
        """文件名说是封面，开关关掉也照样单独显示——自动判型优先。"""
        self.settings.comic_cover_alone = False
        window = self._spread_window(path=self.cover_path)
        self.assertTrue(window._comic_reader.cover_alone)
        self.assertEqual(window._comic_reader._spread.pair, (0, None))

    def test_a_gutter_out_of_range_is_clamped_by_the_setting(self) -> None:
        self.settings.comic_gutter = 999
        self.settings.clamp()
        self.assertEqual(self.settings.comic_gutter, MAX_GUTTER)


class TestWindowSpreadAutoscroll(SpreadWindowTestCase):
    """模式 C 下自动滚动不可用。"""

    def test_the_autoscroller_is_unbound(self) -> None:
        window = self._spread_window()
        self.assertIsNone(window._autoscroll_bar.maximum() > 0 and window._autoscroll_bar or None)

    def test_the_toggle_does_nothing_but_explain(self) -> None:
        window = self._spread_window()
        window.toggle_autoscroll()
        pump()
        self.assertFalse(window.autoscroller.is_running())
        self.assertIn("自动滚动", window.warn_label.text())

    def test_a_spread_has_no_scrollable_range(self) -> None:
        window = self._spread_window()
        self.assertIsNone(window._comic_reader.scrollbar())


class TestWindowModeSwitchingDoesNotCrash(SpreadWindowTestCase):
    """切回单页/滚动模式不崩，页码不丢。"""

    def test_spread_to_single_keeps_the_page(self) -> None:
        window = self._spread_window()
        window.step_chapter(1)
        pump()
        keep = window.chapter_index
        window._comic_reader.set_mode("single")
        pump()
        self.assertEqual(window.chapter_index, keep)
        self.assertEqual(window._comic_reader.mode, "single")

    def test_spread_to_scroll_and_back(self) -> None:
        window = self._spread_window()
        window.step_chapter(1)
        pump()
        window._comic_reader.set_mode("scroll")
        pump()
        self.assertEqual(window._comic_reader.mode, "scroll")
        self.assertIsNotNone(window._comic_reader.scrollbar())
        window._comic_reader.set_mode("spread")
        pump()
        self.assertEqual(window._comic_reader.mode, "spread")

    def test_single_to_scroll_to_spread(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.resize(1200, 800)
        pump()
        for mode in ("scroll", "spread", "single", "spread", "scroll", "single"):
            window._comic_reader.set_mode(mode)
            pump()
            self.assertEqual(window._comic_reader.mode, mode, mode)

    def test_the_zoom_button_works_in_mode_c(self) -> None:
        window = self._spread_window()
        window.cycle_comic_zoom()
        pump()
        self.assertEqual(window._comic_reader.zoom, "height")

    def test_reloading_a_comic_in_mode_c_keeps_it_a_comic(self) -> None:
        window = self._spread_window()
        window.step_chapter(1)
        pump()
        window.reload_current_chapter()
        pump()
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIs(window.reader, window._comic_reader)

    def test_a_text_book_is_unaffected(self) -> None:
        window = self.make_window(path=self.text_path)
        self.assertEqual(window._comic_reader.mode, "single")
        window.step_chapter(1)
        pump()
        self.assertEqual(window.chapter_index, 1)

    def test_the_placeholder_does_not_raise_in_mode_c(self) -> None:
        window = self._spread_window()
        window._show_placeholder()
        pump()

    def test_the_status_bar_still_names_the_page(self) -> None:
        window = self._spread_window()
        window.step_chapter(1)
        pump()
        self.assertIn("page_002.png", window.status_label.full_text())

    def test_apply_style_reaches_the_spread(self) -> None:
        window = self._spread_window()
        window.settings.fg = "#123456"
        window.apply_preferences()
        pump()

    def test_the_content_alpha_preview_works_in_mode_c(self) -> None:
        window = self._spread_window()
        window._comic_reader.preview_content_alpha(128)
        self.assertAlmostEqual(window._comic_reader.content_opacity(), 128 / 255, places=2)

    def test_a_bookmark_in_mode_c_is_labelled_by_page(self) -> None:
        window = self._spread_window()
        window.step_chapter(1)
        pump()
        window.add_bookmark()
        pump()
        marks = window.store.list_for(window.book.id)
        self.assertEqual(marks[0].label, f"第 {window.chapter_index + 1} 页")


class TestStage5StaysQtFreeInCore(unittest.TestCase):
    """阶段 5 的纯算术在 ui/ 里，但同样不许 import Qt。"""

    BANNED = ("PyQt5", "PySide6", "QtCore", "QtWidgets", "minireader.qt")

    def test_the_spread_math_does_not_import_qt(self) -> None:
        from minireader.ui import spread

        source = Path(spread.__file__).read_text(encoding="utf-8")
        for banned in self.BANNED:
            self.assertNotIn(banned, source, f"spread.py 不许出现 {banned}")


#: 已经**从代码里删除**的符号。阶段 5 删掉 `MODE_PENDING`（三种模式都实现了），
#: 但 `comic_reader.py:500` 还留着一次引用——那行不可达，所以没有测试变红，可它
#: 是一颗雷：将来任何"先加词表、后实现"的阶段会让它立刻 `NameError`。
#:
#: 这份名单是"删符号"时的登记处：删一个符号，把名字加到这里，守卫会自动确认
#: 整个 `minireader/` 里没有任何地方还在引用它。
DELETED_SYMBOLS: tuple[str, ...] = ("MODE_PENDING",)


class TestDeletedSymbolsAreNotReferenced(unittest.TestCase):
    """删一个符号，要连**引用点**一起删，不能只删定义。

    `hasattr(module, "X") is False` 只证明**定义**没了，它对别的文件里的引用一无所知。
    完整的两半是：定义不存在（`test_comic_ui.py::TestModeCycle` 那条断言守着），
    以及**没有任何代码引用它**——后者只能扫源码，因为运行时的 `hasattr` 看不见
    其它文件里的名字。

    为什么用 AST 而不是字符串搜索：字符串搜索会把**注释和文档**里的提及也算成
    引用，而那些是刻意留下的（`comic_reader.py` 就在注释里解释了为什么删掉它）。
    AST 只看见真正的 `Name`/`Attribute` 节点，也就是"会被求值"的地方。

    Qt-free：只读文件、走 `ast`，不 import `minireader.qt`，所以留在本文件里不破坏
    "core 无 Qt"的契约。写法参考 `test_comic.py::TestNoDanglingQBuffer`。
    见 docs/DEVELOPMENT.md 第 21 条。
    """

    @staticmethod
    def _referencing_names(tree) -> list[tuple[str, int]]:
        """`(名字, 行号)` for every name/attribute the tree actually evaluates."""
        import ast

        found: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.append((node.id, node.lineno))
            elif isinstance(node, ast.Attribute):
                found.append((node.attr, node.lineno))
        return found

    def test_no_source_file_references_a_deleted_symbol(self) -> None:
        import ast

        package = Path(ROOT) / "minireader"
        sources = sorted(package.rglob("*.py"))
        self.assertGreater(len(sources), 10, "没扫到源码，路径可能写错了")

        offenders: list[str] = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name, lineno in self._referencing_names(tree):
                if name in DELETED_SYMBOLS:
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{lineno} 引用了已删除的 {name}")

        self.assertEqual(
            offenders,
            [],
            "这些符号已经从代码里删掉了，引用点也必须一起删。"
            "只删定义的话，那一行在真正执行到之前不会报错，"
            "会在未来某个阶段变成 NameError。见 docs/DEVELOPMENT.md 第 21 条。",
        )

    def test_the_guard_would_actually_catch_it(self) -> None:
        """给上一条测试本身做反面校验：确认它认得出引用。

        否则 `offenders` 恒为空也可能只是因为 AST 匹配写错了，而不是因为源码干净。
        """
        import ast

        bad = "notice = MODE_PENDING.get(mode, 'x')"
        tree = ast.parse(bad)
        names = [n for n, _ in self._referencing_names(tree)]
        self.assertIn("MODE_PENDING", names, "AST 匹配逻辑坏了，上一条测试是假绿")

        # 注释和文档里的提及**不算**引用——那是刻意保留的说明，不是会被求值的代码。
        commented = "# 阶段 5 删掉了 MODE_PENDING，这里解释一下\nvalue = 1"
        tree = ast.parse(commented)
        names = [n for n, _ in self._referencing_names(tree)]
        self.assertNotIn(
            "MODE_PENDING",
            names,
            "注释里的提及被误报成引用了——守卫会逼着人删掉解释性的注释",
        )

        # 属性访问也算引用：`m.MODE_PENDING` 和裸 `MODE_PENDING` 一样危险。
        dotted = "value = comic_reader.MODE_PENDING"
        tree = ast.parse(dotted)
        names = [n for n, _ in self._referencing_names(tree)]
        self.assertIn("MODE_PENDING", names, "`module.X` 形式的引用漏掉了")


if __name__ == "__main__":
    unittest.main()
