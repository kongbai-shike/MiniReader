"""阶段 8：`rendition:spread` 的解析与消费。

阶段 7 把 `rendition:spread` 读进了 `FixedContent.spread`，但没有任何 UI 读它
（阶段 7 报告 §10 把它记成待定项）。本阶段接上，所以这组测试的重点是**一个
布尔闸门**：书说 `none` 的时候，双页模式退化成"一次一页"。

四值的语义容易记错，值得在这里钉死（EPUB 3.3 §8.2.2.3 "Synthetic spreads"）：

* `none`      —— 不要合成跨页，每页单独显示。
* `landscape` —— 仅当**阅读设备**处于横屏时才合成跨页。
* `both`      —— 不管设备朝向都合成。
* `auto`      —— 交给阅读系统判断（我们判断"是"，也就是阶段 5 的原有行为）。

**这四个值没有一个在说"哪些页适合配对"**。它们回答的是"能不能配对"，与页的
形状无关。所以实现是一个布尔闸门 `pairs_pages(policy, landscape)`，而不是按页
形状分叉的锚点算法：`pairs=False` 时每页各自成一个 spread，阅读顺序不变，模块
其余部分一行不用改。这条判断如果做错，表现是横版页被单独显示、竖版页被配对，
也就是图画书被切得更碎——所以下面有一条测试专门盯"策略不影响页形"。

`landscape` 的取值来源是视口而不是自己量：`ComicReader._resolve_pairs()` 把
`_spread_fits()` 的答案传进去，因为"太窄放不下两页的视口"在任何有用的定义下都
不是横屏。这也让纯函数层能三行驱动，不需要窗口。

    py -3.12 -m unittest tests.test_epub_spread -v
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core.comic import open_epub_fixed  # noqa: E402
from minireader.core.epub import SPREAD_VALUES  # noqa: E402
from minireader.qt import QApplication, install_message_filter  # noqa: E402
from minireader.ui.comic_reader import ComicReader  # noqa: E402
from minireader.ui.spread import (  # noqa: E402
    SPREAD_POLICIES,
    anchor_of,
    is_first_spread,
    is_last_spread,
    next_anchor,
    pairs_pages,
    spread_at,
    spread_starts,
)

from .test_comic_ui import make_png  # noqa: E402
from .test_epub_fixed import FixedTestCase, fixed_files  # noqa: E402

_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

_app = QApplication.instance() or QApplication([])

LAYOUT_META = '<meta property="rendition:layout">pre-paginated</meta>'


def fixed_meta(spread: str | None) -> str:
    """固定版式的 metadata，`spread=None` 表示这本书没写 `rendition:spread`。"""
    meta = LAYOUT_META
    if spread is not None:
        meta += f'<meta property="rendition:spread">{spread}</meta>'
    return meta


def three_pages() -> tuple[tuple[str, ...], ...]:
    """三页单图页。三页足够看出"配对"和"不配对"的差别。"""
    return (("img/p0.png",), ("img/p1.png",), ("img/p2.png",))


def real_images(files: dict[str, str | bytes], size=(600, 900)) -> dict[str, str | bytes]:
    """把假图片字节换成真 PNG。

    `fixed_files()` 的图片是 `b"\\x89PNG..."` 加填充——核心层从不解码，所以那
    对解析测试够用。但这一组测试要把书交给 `ComicReader`，它会真的解码；假字节
    会让 Qt 往 stderr 刷一串 `libpng error: Read Error`，淹没真正的失败。
    """
    pixel = make_png(*size)
    for name in list(files):
        if name.endswith(".png"):
            files[name] = pixel
    return files


# ---------------------------------------------------------------------------
# 1. 策略解析：四值 + 空串 + 非法值
# ---------------------------------------------------------------------------


class TestPairsPolicy(unittest.TestCase):
    """`pairs_pages()` 是这一阶段唯一的策略判断，四个值逐个钉。"""

    def test_none_forbids_pairing(self) -> None:
        self.assertFalse(pairs_pages("none"))

    def test_none_forbids_pairing_in_a_landscape_viewport_too(self) -> None:
        """`none` 与视口无关——这正是它与 `landscape` 的区别。"""
        self.assertFalse(pairs_pages("none", landscape=True))
        self.assertFalse(pairs_pages("none", landscape=False))

    def test_auto_lets_the_reader_decide_and_we_say_yes(self) -> None:
        self.assertTrue(pairs_pages("auto"))

    def test_an_empty_policy_means_no_opinion_which_pairs(self) -> None:
        """空串来自非 EPUB 的来源，读法与 `auto` 相同。"""
        self.assertTrue(pairs_pages(""))

    def test_landscape_follows_the_viewport(self) -> None:
        self.assertTrue(pairs_pages("landscape", landscape=True))
        self.assertFalse(pairs_pages("landscape", landscape=False))

    def test_both_ignores_the_viewport(self) -> None:
        self.assertTrue(pairs_pages("both", landscape=True))
        self.assertTrue(pairs_pages("both", landscape=False))

    def test_case_and_whitespace_do_not_matter(self) -> None:
        """值是从 XML 里取出来的，大小写和空格不该改变含义。"""
        self.assertFalse(pairs_pages("  NONE  "))

    def test_an_unknown_value_falls_back_to_pairing(self) -> None:
        """规范说非法值回落 auto，而 auto 对我们就是"配对"。"""
        self.assertTrue(pairs_pages("sideways"))

    def test_the_policy_list_agrees_with_the_parser(self) -> None:
        """两个模块刻意各写一份常量（一个 Qt-free 一个 core-free），不能漂。"""
        self.assertEqual(SPREAD_POLICIES, SPREAD_VALUES)


# ---------------------------------------------------------------------------
# 2. 不配对时六个锚点函数全部退化
# ---------------------------------------------------------------------------


class TestPairingOffDegrades(unittest.TestCase):
    """`pairs=False` 只该有一个后果：每个 spread 恰好一页。

    这组测试之所以重要，是因为退化必须**干净**：如果 `spread_at` 还按两步走，
    不配对的书会每隔一页才显示一次，中间那些页永远看不到。
    """

    def test_every_page_is_its_own_spread(self) -> None:
        self.assertEqual(spread_starts(5, True, pairs=False), [0, 1, 2, 3, 4])

    def test_cover_alone_makes_no_difference_when_pairing_is_off(self) -> None:
        """封面单独显示是"配对"的一个特例，不配对时它自然失去意义。"""
        self.assertEqual(spread_starts(5, False, pairs=False), [0, 1, 2, 3, 4])

    def test_an_empty_book_has_no_spreads(self) -> None:
        self.assertEqual(spread_starts(0, True, pairs=False), [])

    def test_spread_at_returns_one_page(self) -> None:
        for index in range(5):
            self.assertEqual(spread_at(index, 5, True, pairs=False), (index, None))

    def test_spread_at_never_returns_a_second_page_even_on_the_cover(self) -> None:
        """封面判断在不配对时不能插一脚，否则第 0 页会变成 (0, None) 而第 1 页
        变成 (1, 2)——那正是"每隔一页显示一次"的 bug。"""
        self.assertEqual(spread_at(1, 5, True, pairs=False), (1, None))

    def test_spread_at_clamps_like_the_paired_version(self) -> None:
        self.assertEqual(spread_at(99, 5, True, pairs=False), (4, None))
        self.assertEqual(spread_at(-3, 5, True, pairs=False), (0, None))

    def test_anchor_of_agrees_with_spread_at(self) -> None:
        for index in range(5):
            self.assertEqual(
                anchor_of(index, 5, True, pairs=False),
                spread_at(index, 5, True, pairs=False)[0],
            )

    def test_next_anchor_moves_one_page(self) -> None:
        self.assertEqual(next_anchor(0, 5, True, 1, pairs=False), 1)
        self.assertEqual(next_anchor(3, 5, True, 1, pairs=False), 4)
        self.assertEqual(next_anchor(3, 5, True, -1, pairs=False), 2)

    def test_next_anchor_stops_at_the_ends(self) -> None:
        self.assertEqual(next_anchor(4, 5, True, 1, pairs=False), 4)
        self.assertEqual(next_anchor(0, 5, True, -1, pairs=False), 0)

    def test_first_and_last_follow_the_one_page_spreads(self) -> None:
        self.assertTrue(is_first_spread(0, 5, True, pairs=False))
        self.assertFalse(is_first_spread(1, 5, True, pairs=False))
        self.assertTrue(is_last_spread(4, 5, True, pairs=False))
        self.assertFalse(is_last_spread(3, 5, True, pairs=False))

    def test_every_page_still_lands_in_exactly_one_spread(self) -> None:
        """完整性证明。配对与否都不能让任何一页消失。

        这条是阶段 5 `test_every_page_lands_in_exactly_one_spread` 的不配对版：
        同一个不变量，两种策略。
        """
        for total in range(0, 25):
            for cover in (True, False):
                seen: list[int] = []
                for start in spread_starts(total, cover, pairs=False):
                    first, second = spread_at(start, total, cover, pairs=False)
                    seen.append(first)
                    if second is not None:
                        seen.append(second)
                self.assertEqual(
                    sorted(seen),
                    list(range(total)),
                    f"total={total} cover={cover}",
                )


# ---------------------------------------------------------------------------
# 3. 书把值带上来
# ---------------------------------------------------------------------------


class TestTheBookCarriesThePolicy(FixedTestCase):
    """`ComicBook.spread` 是容器层唯一新增的东西。"""

    def test_a_none_book_says_none(self) -> None:
        book = open_epub_fixed(self.fake(meta=fixed_meta("none")))
        self.assertEqual(book.spread, "none")

    def test_a_landscape_book_says_landscape(self) -> None:
        book = open_epub_fixed(self.fake(meta=fixed_meta("landscape")))
        self.assertEqual(book.spread, "landscape")

    def test_a_book_without_the_meta_says_auto(self) -> None:
        """没写 = auto，而 auto 对我们就是"配对"，所以与阶段 5 行为一致。"""
        book = open_epub_fixed(self.fake(meta=fixed_meta(None)))
        self.assertEqual(book.spread, "auto")

    def test_an_illegal_value_falls_back_to_auto_in_the_parser(self) -> None:
        """解析层已经做了回落，容器层不必再判一次。"""
        book = open_epub_fixed(self.fake(meta=fixed_meta("sideways")))
        self.assertEqual(book.spread, "auto")

    def test_the_policy_reaches_the_pure_function(self) -> None:
        """端到端：书里的值喂给 `pairs_pages()` 得到"不配对"。"""
        book = open_epub_fixed(self.fake(meta=fixed_meta("none"), pages=three_pages()))
        self.assertFalse(pairs_pages(book.spread))
        self.assertEqual(spread_starts(len(book.pages), True, pairs=False), [0, 1, 2])

    def test_the_other_sources_have_no_opinion(self) -> None:
        """CBZ 没有 `rendition:spread` 可言，字段默认空串，读作"配对"。"""
        from minireader.core.comic import ComicBook

        self.assertEqual(ComicBook.__dataclass_fields__["spread"].default, "")
        self.assertTrue(pairs_pages(ComicBook.__dataclass_fields__["spread"].default))


# ---------------------------------------------------------------------------
# 4. 读者消费：一次一页，并说明一次
# ---------------------------------------------------------------------------


class TestReaderConsumesThePolicy(FixedTestCase):
    """`ComicReader` 把书的意见变成一个闸门，且只播一次提示。"""

    def fixed_book(self, *, meta: str, name: str = "book.epub") -> str:
        """一本图片能真解码的固定版式书。

        用 `real_images()` 换掉假字节：读者会解码，而假 PNG 只会往 stderr 刷
        `libpng error`，把真正的失败淹掉。
        """
        files = fixed_files(three_pages(), meta=meta)
        return self.write(real_images(files), name)

    def reader(self, **kwargs) -> ComicReader:
        book = open_epub_fixed(self.fixed_book(**kwargs))
        reader = ComicReader()
        self.addCleanup(reader.deleteLater)
        # 窗口够宽，否则模式 C 会以"窗口太窄"为由拒绝进入，测不到策略。
        reader.resize(1200, 800)
        reader.show()
        reader.set_cover_alone(False)
        reader.show_book(book, index=0)
        _app.processEvents()
        return reader

    def to_spread(self, reader: ComicReader) -> list[str]:
        notices: list[str] = []
        reader.notice.connect(notices.append)
        reader.set_mode("spread")
        _app.processEvents()
        return notices

    def test_a_none_book_shows_one_page_per_spread(self) -> None:
        reader = self.reader(meta=fixed_meta("none"))
        self.to_spread(reader)
        self.assertEqual(reader._spread.pair, (0, None))
        reader.next_page()
        _app.processEvents()
        self.assertEqual(reader._spread.pair, (1, None))

    def test_a_none_book_pages_through_every_page(self) -> None:
        """不配对不等于"只显示奇数页"——每一页都要能翻到。"""
        reader = self.reader(meta=fixed_meta("none"))
        self.to_spread(reader)
        seen = [reader._spread.pair[0]]
        for _ in range(2):
            reader.next_page()
            _app.processEvents()
            seen.append(reader._spread.pair[0])
        self.assertEqual(seen, [0, 1, 2])

    def test_a_none_book_reports_span_one(self) -> None:
        reader = self.reader(meta=fixed_meta("none"))
        self.to_spread(reader)
        self.assertEqual(reader.current_position().span, 1)

    def test_an_auto_book_still_pairs(self) -> None:
        """阶段 5 的行为不能被这次改动带走。"""
        reader = self.reader(meta=fixed_meta(None))
        self.to_spread(reader)
        self.assertEqual(reader._spread.pair, (0, 1))

    def test_the_reason_is_told_once(self) -> None:
        """不说明的话，双页按钮看起来像坏了：页一张一张地出，没有解释。"""
        reader = self.reader(meta=fixed_meta("none"))
        notices = self.to_spread(reader)
        self.assertEqual(len([n for n in notices if "单页" in n]), 1)

    def test_the_reason_is_not_repeated_on_every_render(self) -> None:
        reader = self.reader(meta=fixed_meta("none"))
        notices = self.to_spread(reader)
        for _ in range(3):
            reader.next_page()
            _app.processEvents()
        self.assertEqual(len([n for n in notices if "单页" in n]), 1)

    def test_a_paired_book_says_nothing(self) -> None:
        reader = self.reader(meta=fixed_meta(None))
        notices = self.to_spread(reader)
        self.assertEqual([n for n in notices if "单页" in n], [])

    def test_switching_books_forgets_what_was_told(self) -> None:
        """上一本说过的不能压制这一本：提示是按书计的。

        换书时还停在双页模式，所以新书的第一屏就是一次新的 render——它必须
        再说一次，否则用户换到另一本同样禁止配对的书时会以为按钮坏了。
        """
        reader = self.reader(meta=fixed_meta("none"))
        self.to_spread(reader)
        self.assertIsNotNone(reader._pairs_told)
        other = open_epub_fixed(self.fixed_book(meta=fixed_meta("none"), name="other.epub"))
        notices: list[str] = []
        reader.notice.connect(notices.append)
        reader.show_book(other, index=0)
        _app.processEvents()
        self.assertEqual(len([n for n in notices if "单页" in n]), 1)

    def test_a_landscape_book_in_a_wide_window_pairs(self) -> None:
        reader = self.reader(meta=fixed_meta("landscape"))
        self.to_spread(reader)
        self.assertEqual(reader._spread.pair, (0, 1))

    def test_a_narrow_window_turns_a_landscape_book_into_single_pages(self) -> None:
        """`landscape` 问的是视口，所以窗口窄下来它就变假。

        这里不经过 `set_mode()`（窄窗口会被"窗口太窄"挡在门外），直接验证
        `_resolve_pairs()` 的回答，因为那是 `_spread_fits()` 的唯一消费者。
        """
        reader = self.reader(meta=fixed_meta("landscape"))
        reader.resize(300, 800)
        _app.processEvents()
        reader._resolve_pairs()
        self.assertFalse(reader._pairs)

    def test_both_ignores_a_narrow_window(self) -> None:
        reader = self.reader(meta=fixed_meta("both"))
        reader.resize(300, 800)
        _app.processEvents()
        reader._resolve_pairs()
        self.assertTrue(reader._pairs)


if __name__ == "__main__":
    unittest.main()
