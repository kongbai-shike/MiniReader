"""阶段 2 界面层：漫画在真实窗口里的行为。

核心层（格式表 / 压缩包 / ComicBook）由 `tests/test_comic.py` 守，那个文件是 Qt-free
的。这里反过来：只测"接进 MiniWindow 之后"的部分——两种 Reader 怎么共存、漫画按钮
什么时候可见、翻页手势、页码怎么持久化。造页面需要真的能解码的图片，所以这里的
压缩包必须装**真 PNG**（`test_comic.py` 里塞的是垃圾字节，因为它从不解码）。

五个类断言值得单独说明：

1. **两个 Reader 全程存活，切换只换索引**。`self.reader` 必须始终指向"当前那个"，
   而 `_text_reader` / `_comic_reader` 都留在 `_reader_stack` 里。文本模式下
   `self.reader is self._text_reader` 是阶段 1 的验收条件之一，阶段 2 加漫画时
   最容易弄坏的就是它——所以两边都断言。

2. **漫画按钮只在漫画模式可见**。`setVisible(True)` 而父窗口没显示时 `isVisible()`
   恒为 False，所以用 `isVisibleTo(window)`：它问的是"如果窗口显示出来，这个控件
   会显示吗"，正是我们要的语义。

3. **滚轮去抖是功能不是优化**。触控板一次轻扫连发 10+ 个 wheel 事件，不去抖就会
   一下翻 10 页。`test_a_trackpad_flick_turns_one_page` 连发 11 个事件断言只翻一页，
   这是阶段 2 计划里明确点名的一条。

4. **页码持久化走 `Progress.page`**。文字书存像素 offset，漫画存页码，两者刻意
   不统一（见 `core/model.py` 的 `Progress` docstring）。旧 progress.json 没有
   `page` 键时按 0 处理，所以"第一次打开漫画从第 1 页开始"是兼容性的一部分。

5. **共享路径必须对漫画优雅降级**。window.py 是给文字书写的，`reload_current_chapter`
   这类路径会碰到 Book-only 的属性。漫画上要么正确地什么都不做，要么按页码工作，
   绝不能抛 AttributeError——`test_reload_current_chapter` 守的就是这个（这条曾经
   真的崩过：`Book.from_path` 把漫画读成文字书，喂给 ComicReader 后在
   `book.pages` 上炸了）。

    py -3.12 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

# 必须在 QApplication 存在之前设好（与 test_ui_offscreen.py 同样的理由）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import formats as formats_module  # noqa: E402
from minireader.core.book_base import KIND_COMIC, KIND_TEXT  # noqa: E402
from minireader.core.comic import ComicBook  # noqa: E402
from minireader.core.formats import CURRENT_PHASE, Format  # noqa: E402
from minireader.core.model import Book, Progress  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.qt import (  # noqa: E402
    QApplication,
    QContextMenuEvent,
    QEvent,
    QKeyEvent,
    QMimeData,
    QMouseEvent,
    QPoint,
    QPointF,
    QUrl,
    QWheelEvent,
    Qt,
    install_message_filter,
)
from minireader.ui.comic_cache import (  # noqa: E402
    DEFAULT_CACHE_MB,
    PageCache,
    decode,
    fit_size,
    probe_size,
)
from minireader.ui.comic_reader import (  # noqa: E402
    IMPLEMENTED_MODES,
    MODE_LABELS,
    MODE_TIPS,
    PRELOAD_RADIUS,
    READING_MODES,
    WHEEL_DEBOUNCE_MS,
    ComicReader,
    boundary_notice,
    next_mode,
)
from minireader.ui.page_view import ZOOM_LABELS, ZOOM_MODES, ZOOM_TIPS, PageView, next_zoom  # noqa: E402
from minireader.ui.reader import ReaderView  # noqa: E402
from minireader.ui.window import TITLE_BTN_MIN, MiniWindow  # noqa: E402

from .test_reading_features import _settle, pump  # noqa: E402
from .test_ui_offscreen import WindowTestCase, make_book  # noqa: E402

_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

_app = QApplication.instance() or QApplication([])


# ---- 造真图片 -------------------------------------------------------------


def make_png(width: int, height: int, colour: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """一张纯色 PNG。

    手写而不是用 Pillow：项目不依赖 Pillow（`minireader/core/images.py` 用 Qt 自己的
    图片插件），测试也不该为了造一张图去装它。PNG 的最小结构是 签名 + IHDR + IDAT
    + IEND，每块前面 4 字节长度、后面 4 字节 CRC32，都是 `struct` + `zlib` 能做的。
    """
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


#: 每页尺寸互不相同，这样"翻页真的换了图"可以被尺寸证明（而不是只看索引）。
PAGE_SIZES = ((40, 60), (60, 40), (50, 50), (30, 90))


def make_cbz(path: Path, pages: int = 3, names: list[str] | None = None) -> Path:
    """装真 PNG 的 CBZ。`names` 为空时用 `page_1.png` 这种自然序名字。"""
    if names is None:
        names = [f"page_{i + 1}.png" for i in range(pages)]
    with zipfile.ZipFile(path, "w") as zf:
        for i, name in enumerate(names):
            width, height = PAGE_SIZES[i % len(PAGE_SIZES)]
            zf.writestr(name, make_png(width, height))
    return path


def make_comic_folder(path: Path, pages: int = 3) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        width, height = PAGE_SIZES[i % len(PAGE_SIZES)]
        (path / f"page_{i + 1}.png").write_bytes(make_png(width, height))
    return path


def wheel_at(widget, down: int) -> None:
    """给 `widget` 发一个真 QWheelEvent。

    与 test_ui_offscreen.py 的同名辅助函数方向约定一致：`down` 为正表示"向书的末尾
    翻"，Qt 把它报成负的 angleDelta。
    """
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        pos,
        pos,
        QPoint(0, 0),
        QPoint(0, -120 * down),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    widget.wheelEvent(event)


# ---------------------------------------------------------------------------
# 缓存与缩放：不需要窗口，纯算术 + 解码
# ---------------------------------------------------------------------------


class TestProbeSize(unittest.TestCase):
    """`probe_size()` 不解码就报尺寸——连续滚动模式排版占位块全靠它。"""

    def setUp(self) -> None:
        self.tmp = Path(__file__).parent / "_comic_ui_tmp"
        self.tmp.mkdir(exist_ok=True)
        self.cbz = make_cbz(self.tmp / "probe.cbz", pages=2)

    def tearDown(self) -> None:
        for item in self.tmp.glob("*"):
            item.unlink()
        self.tmp.rmdir()

    def test_it_reports_the_real_size_without_decoding(self) -> None:
        book = open_book(self.cbz)
        self.assertEqual(probe_size(book, 0), PAGE_SIZES[0])
        self.assertEqual(probe_size(book, 1), PAGE_SIZES[1])

    def test_a_missing_page_reports_unknown_rather_than_raising(self) -> None:
        """越界页返回 (0,0)：一张坏页不该把整本书带下去。"""
        book = open_book(self.cbz)
        self.assertEqual(probe_size(book, 99), (0, 0))

    def test_garbage_bytes_report_unknown_rather_than_raising(self) -> None:
        path = self.tmp / "junk.cbz"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.png", b"not an image at all")
        book = open_book(path)
        self.assertEqual(probe_size(book, 0), (0, 0))

    def test_it_is_stable_when_called_in_a_loop(self) -> None:
        """这条守的是一个真实的段错误。

        `QBuffer(QByteArray(head))` 里的临时 QByteArray 在语句结束就被回收，而
        QBuffer 的 C++ 构造函数收的是**借用**指针，于是 buffer 指向已释放内存——
        单次调用 8 次里崩 1 次，放进循环 5/5 全崩（exit code 0xC0000005）。
        修法是让 QByteArray 活成一个具名局部变量（见 comic_cache.py 的注释）。
        400 轮足够让回归现形。
        """
        book = open_book(self.cbz)
        for _ in range(400):
            self.assertEqual(probe_size(book, 0), PAGE_SIZES[0])


class TestDecode(unittest.TestCase):
    def test_a_real_png_decodes(self) -> None:
        pixmap, message = decode(make_png(12, 7))
        self.assertEqual(message, "")
        self.assertIsNotNone(pixmap)
        self.assertEqual((pixmap.width(), pixmap.height()), (12, 7))

    def test_empty_bytes_say_so(self) -> None:
        pixmap, message = decode(b"")
        self.assertIsNone(pixmap)
        self.assertEqual(message, "这一页是空的")

    def test_garbage_bytes_say_so(self) -> None:
        pixmap, message = decode(b"definitely not a png")
        self.assertIsNone(pixmap)
        self.assertEqual(message, "这一页不是有效的图片")


class TestPageCache(unittest.TestCase):
    def setUp(self) -> None:
        self.cbz = make_cbz(Path(os.environ["TEMP"]) / "mr-cache-test.cbz", pages=2)
        self.book = open_book(self.cbz)

    def tearDown(self) -> None:
        self.cbz.unlink()

    def test_it_remembers_a_page_under_the_book_id(self) -> None:
        cache = PageCache()
        pixmap, message = decode(make_png(10, 10))
        self.assertEqual(message, "")
        cache.put(self.book.id, 0, pixmap)
        self.assertIn((self.book.id, 0), cache)
        self.assertIs(cache.get(self.book.id, 0), pixmap)

    def test_load_decodes_on_a_miss_and_hits_afterwards(self) -> None:
        cache = PageCache()
        first, message = cache.load(self.book, 0)
        self.assertEqual(message, "")
        self.assertEqual((first.width(), first.height()), PAGE_SIZES[0])
        self.assertIn((self.book.id, 0), cache, "解码后必须进缓存")
        second, _ = cache.load(self.book, 0)
        self.assertIs(second, first, "第二次必须是同一个对象，不是重新解码")

    def test_the_budget_is_clamped_to_the_documented_range(self) -> None:
        """64-1024：手改 settings.json 塞进 100000 不许把内存吃光。"""
        cache = PageCache()
        cache.set_budget_mb(1)
        self.assertEqual(cache.budget_mb, 64)
        cache.set_budget_mb(99999)
        self.assertEqual(cache.budget_mb, 1024)
        cache.set_budget_mb(512)
        self.assertEqual(cache.budget_mb, 512)

    def test_the_default_budget_is_256_mb(self) -> None:
        self.assertEqual(DEFAULT_CACHE_MB, 256)
        self.assertEqual(PageCache().budget_mb, 256)

    def test_eviction_keeps_the_most_recent_page(self) -> None:
        """按**内存**淘汰而不是按页数：2500x2500 的图一张 25 MB。

        预算 64 MB（= 64*1024*1024 = 67108864 字节）因此刚好装得下两张、装不下三张。
        """
        cache = PageCache()
        cache.set_budget_mb(64)
        big, _ = decode(make_png(2500, 2500))  # 2500*2500*4 = 25 MB
        self.assertIsNotNone(big)
        for index in range(3):
            cache.put(self.book.id, index, big)
        # 25*3 = 75 MB > 64 MB，所以最早那页必须被淘汰。
        self.assertNotIn((self.book.id, 0), cache)
        self.assertIn((self.book.id, 2), cache)

    def test_get_refreshes_recency(self) -> None:
        cache = PageCache()
        cache.set_budget_mb(64)
        big, _ = decode(make_png(2500, 2500))
        cache.put(self.book.id, 0, big)
        cache.put(self.book.id, 1, big)
        self.assertIn((self.book.id, 0), cache, "两张 25 MB 装得下 64 MB")
        cache.get(self.book.id, 0)  # 0 变成最新
        cache.put(self.book.id, 2, big)  # 挤掉的是 1，不是 0
        self.assertIn((self.book.id, 0), cache)
        self.assertNotIn((self.book.id, 1), cache)

    def test_forget_book_only_drops_that_book(self) -> None:
        cache = PageCache()
        pixmap, _ = decode(make_png(10, 10))
        cache.put("book-a", 0, pixmap)
        cache.put("book-b", 0, pixmap)
        self.assertEqual(cache.forget_book("book-a"), 1)
        self.assertNotIn(("book-a", 0), cache)
        self.assertIn(("book-b", 0), cache)

    def test_clear_empties_everything(self) -> None:
        cache = PageCache()
        pixmap, _ = decode(make_png(10, 10))
        cache.put("a", 0, pixmap)
        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.bytes_used, 0)

    def test_stats_reads_like_a_sentence(self) -> None:
        cache = PageCache()
        self.assertEqual(cache.stats(), "缓存 0 页 / 0.0 MB")

    def test_a_broken_page_is_reported_and_not_cached(self) -> None:
        path = Path(os.environ["TEMP"]) / "mr-cache-broken.cbz"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.png", b"garbage")
        try:
            book = open_book(path)
            cache = PageCache()
            pixmap, message = cache.load(book, 0)
            self.assertIsNone(pixmap)
            self.assertEqual(message, "这一页不是有效的图片")
            self.assertNotIn((book.id, 0), cache, "坏页不进缓存，免得反复命中垃圾")
        finally:
            path.unlink()


class TestFitSize(unittest.TestCase):
    """缩放三档的算术部分。纯函数，不需要 QApplication。"""

    SOURCE = (1000, 2000)
    VIEW = (500, 400)

    def test_width_fits_the_width(self) -> None:
        self.assertEqual(fit_size(self.SOURCE, self.VIEW, "width"), (500, 1000))

    def test_height_fits_the_height(self) -> None:
        self.assertEqual(fit_size(self.SOURCE, self.VIEW, "height"), (200, 400))

    def test_original_keeps_the_source_size(self) -> None:
        self.assertEqual(fit_size(self.SOURCE, self.VIEW, "original"), (1000, 2000))

    def test_a_small_page_is_never_enlarged(self) -> None:
        """放大一张小图只会让它变糊，所以 scale 上限是 1.0。"""
        self.assertEqual(fit_size((100, 50), self.VIEW, "width"), (100, 50))
        self.assertEqual(fit_size((100, 50), self.VIEW, "height"), (100, 50))

    def test_an_unknown_source_size_falls_back_to_the_viewport(self) -> None:
        self.assertEqual(fit_size((0, 0), self.VIEW, "width"), self.VIEW)

    def test_a_collapsed_viewport_falls_back_to_the_source(self) -> None:
        self.assertEqual(fit_size(self.SOURCE, (0, 0), "width"), self.SOURCE)

    def test_it_never_returns_a_zero_dimension(self) -> None:
        width, height = fit_size((1, 10000), (500, 400), "width")
        self.assertGreaterEqual(width, 1)
        self.assertGreaterEqual(height, 1)


class TestZoomCycle(unittest.TestCase):
    def test_the_cycle_order_is_width_height_original(self) -> None:
        self.assertEqual(ZOOM_MODES, ("width", "height", "original"))
        self.assertEqual(next_zoom("width"), "height")
        self.assertEqual(next_zoom("height"), "original")
        self.assertEqual(next_zoom("original"), "width", "转一圈回到起点")

    def test_every_zoom_has_a_label_and_a_tip(self) -> None:
        for zoom in ZOOM_MODES:
            self.assertIn(zoom, ZOOM_LABELS)
            self.assertIn(zoom, ZOOM_TIPS)

    def test_an_unknown_zoom_falls_back_to_width(self) -> None:
        self.assertEqual(next_zoom("nonsense"), ZOOM_MODES[0])


class TestModeCycle(unittest.TestCase):
    def test_the_cycle_covers_all_three_modes(self) -> None:
        self.assertEqual(READING_MODES, ("single", "scroll", "spread"))
        self.assertEqual(next_mode("single"), "scroll")
        self.assertEqual(next_mode("scroll"), "spread")
        self.assertEqual(next_mode("spread"), "single")

    def test_stage_5_implements_all_three_modes(self) -> None:
        """阶段 4 加入连续滚动，阶段 5 加入双页；三种模式都实现了。

        这条断言的是"这一版能画哪几种"，所以每落地一个模式它就该跟着变一次——
        和 `CURRENT_PHASE` 那类相位断言同理（见 docs/DEVELOPMENT.md 第 8 条）：
        它守的不是"永远只有单页"这个真值，而是"没实现的模式不许被切过去"。
        现在三种都有实现，所以 `MODE_PENDING` 已经不存在了。
        """
        self.assertEqual(IMPLEMENTED_MODES, ("single", "scroll", "spread"))
        self.assertEqual(set(IMPLEMENTED_MODES), set(READING_MODES))
        from minireader.ui import comic_reader

        self.assertFalse(
            hasattr(comic_reader, "MODE_PENDING"),
            "全部实现之后不该再留着「还没做」的词表",
        )

    def test_every_mode_has_a_label_and_a_tip(self) -> None:
        for mode in READING_MODES:
            self.assertIn(mode, MODE_LABELS)
            self.assertIn(mode, MODE_TIPS)


class TestPageView(unittest.TestCase):
    def test_it_starts_empty(self) -> None:
        view = PageView()
        self.assertFalse(view.has_page)

    def test_set_page_stores_the_pixmap(self) -> None:
        view = PageView()
        pixmap, _ = decode(make_png(20, 10))
        view.set_page(pixmap, "")
        self.assertTrue(view.has_page)
        self.assertEqual(view.message, "")

    def test_a_message_replaces_the_page(self) -> None:
        view = PageView()
        view.set_page(None, "这一页是空的")
        self.assertFalse(view.has_page)
        self.assertEqual(view.message, "这一页是空的")

    def test_clear_forgets_both(self) -> None:
        view = PageView()
        pixmap, _ = decode(make_png(20, 10))
        view.set_page(pixmap, "")
        view.clear()
        self.assertFalse(view.has_page)
        self.assertEqual(view.message, "")

    def test_an_unknown_zoom_falls_back_to_the_first_mode(self) -> None:
        view = PageView()
        view.set_zoom("nonsense")
        self.assertEqual(view.zoom, ZOOM_MODES[0])


# ---------------------------------------------------------------------------
# ComicReader 本身：不经过窗口
# ---------------------------------------------------------------------------


class TestComicReaderAlone(unittest.TestCase):
    """`ComicReader` 的契约：paging、手势、文本形状的降级答案。

    不建窗口，因为这里要问的是 Reader 自己——窗口那一层的行为在下面的
    `ComicWindowTestCase` 里测。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(os.environ["TEMP"]) / "mr-comic-reader-tests"
        cls.tmp.mkdir(exist_ok=True)
        cls.cbz = make_cbz(cls.tmp / "reader.cbz", pages=4)
        cls.book = open_book(cls.cbz)

    @classmethod
    def tearDownClass(cls) -> None:
        for item in cls.tmp.glob("*"):
            item.unlink()
        cls.tmp.rmdir()

    def setUp(self) -> None:
        self.reader = ComicReader()
        self.reader.resize(300, 200)
        self.reader.show_book(self.book, index=0)

    def tearDown(self) -> None:
        self.reader.stop()

    def test_it_reports_its_kind(self) -> None:
        self.assertEqual(self.reader.kind, KIND_COMIC)

    def test_it_puts_the_reader_before_qt_in_the_mro(self) -> None:
        """阶段 1 立下的规矩：Reader 必须排在最前，否则它会被 Qt 的同名方法盖掉。"""
        names = [cls.__name__ for cls in ComicReader.__mro__]
        self.assertEqual(names[:3], ["ComicReader", "Reader", "QWidget"])

    def test_show_book_sets_the_page(self) -> None:
        self.assertEqual(self.reader.index, 0)
        self.assertEqual(self.reader.current_position().label, "第 1/4 页")

    def test_it_ignores_the_text_position_vocabulary(self) -> None:
        """`show_chapter()` 会把 offset/at_end/char_offset 全传过来。

        漫画没有字符偏移这回事，所以这三个参数是**被忽略**而不是被拒绝——拒绝的话
        window 那条共享路径每次都会炸。
        """
        self.reader.show_book(self.book, index=2, offset=999, at_end=True, char_offset=12345)
        self.assertEqual(self.reader.index, 2, "只有 index 有意义")

    def test_a_text_book_is_refused_rather_than_trusted(self) -> None:
        """这条守的是一个真实的 AttributeError。

        `window.reload_current_chapter()` 曾经用 `Book.from_path()` 重读书，漫画因此
        变成一个文字 Book 被喂进来，然后在 `book.pages` 上炸掉。ComicReader 现在自己
        把非漫画的书当成"没有东西可显示"。
        """
        text_book = Book.from_text(
            "第一章 起\n正文。\n", path=str(self.tmp / "novel.txt")
        )
        self.reader.show_book(text_book, index=0)
        self.assertIsNone(self.reader.book, "非漫画的书不许留下来")
        self.assertEqual(self.reader.index, 0)

    def test_next_and_prev_page(self) -> None:
        self.reader.next_page()
        self.assertEqual(self.reader.index, 1)
        self.reader.next_page()
        self.assertEqual(self.reader.index, 2)
        self.reader.prev_page()
        self.assertEqual(self.reader.index, 1)

    def test_it_clamps_at_both_ends(self) -> None:
        self.reader.prev_page()
        self.assertEqual(self.reader.index, 0, "第一页再往前还是第一页")
        for _ in range(10):
            self.reader.next_page()
        self.assertEqual(self.reader.index, 3, "最后一页再往后还是最后一页")

    def test_goto_a_position_from_current_position(self) -> None:
        self.reader.goto(self.reader.current_position())
        self.assertEqual(self.reader.index, 0, "原地跳转不该动")

    def test_goto_emits_scrolled_so_progress_gets_saved(self) -> None:
        seen: list[int] = []
        self.reader.scrolled.connect(lambda: seen.append(1))
        self.reader.next_page()
        self.assertEqual(len(seen), 1)
        self.reader.next_page()
        self.assertEqual(len(seen), 2)

    def test_goto_to_the_same_page_does_not_emit(self) -> None:
        """停在原地不该触发保存——否则每点一下没动的按钮就写一次盘。"""
        seen: list[int] = []
        self.reader.scrolled.connect(lambda: seen.append(1))
        self.reader.goto(self.reader.current_position())
        self.assertEqual(seen, [])

    def test_progress_percent_counts_the_current_page_as_reached(self) -> None:
        self.assertEqual(self.reader.progress_percent(), 25.0)
        self.reader.next_page()
        self.assertEqual(self.reader.progress_percent(), 50.0)

    def test_total_positions_is_the_page_count(self) -> None:
        self.assertEqual(self.reader.total_positions(), 4)

    def test_the_keyboard_turns_pages(self) -> None:
        for key, expected in (
            (Qt.Key_Right, 1),
            (Qt.Key_Down, 2),
            (Qt.Key_PageDown, 3),
        ):
            self.reader.keyPressEvent(_key_event(key))
            self.assertEqual(self.reader.index, expected, f"{key} 应翻到 {expected}")
        for key, expected in (
            (Qt.Key_Left, 2),
            (Qt.Key_Up, 1),
            (Qt.Key_PageUp, 0),
        ):
            self.reader.keyPressEvent(_key_event(key))
            self.assertEqual(self.reader.index, expected, f"{key} 应退到 {expected}")

    def test_home_and_end_jump_to_the_ends(self) -> None:
        self.reader.keyPressEvent(_key_event(Qt.Key_End))
        self.assertEqual(self.reader.index, 3)
        self.reader.keyPressEvent(_key_event(Qt.Key_Home))
        self.assertEqual(self.reader.index, 0)

    def test_a_trackpad_flick_turns_one_page(self) -> None:
        """去抖的核心断言：连发 11 个 wheel 事件只许翻一页。

        触控板一次轻扫会连发 10+ 个事件，不去抖就一下跳 10 页。去抖窗口是
        WHEEL_DEBOUNCE_MS（120ms），测试里不等它过期，所以后面的都被吞掉。
        """
        self.assertTrue(WHEEL_DEBOUNCE_MS > 0)
        for _ in range(11):
            wheel_at(self.reader, 1)
        self.assertEqual(self.reader.index, 1, "一次轻扫 = 一页")

    def test_a_second_flick_after_the_debounce_turns_another_page(self) -> None:
        wheel_at(self.reader, 1)
        self.assertEqual(self.reader.index, 1)
        self.reader._wheel_timer.stop()  # 等价于等过了去抖窗口
        wheel_at(self.reader, 1)
        self.assertEqual(self.reader.index, 2)

    def test_a_wheel_event_with_no_delta_is_ignored(self) -> None:
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, 0),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        self.reader.wheelEvent(event)
        self.assertEqual(self.reader.index, 0, "delta 为 0 不该翻页")

    def test_clicking_the_left_third_goes_back(self) -> None:
        self.reader.next_page()
        self.assertEqual(self.reader.index, 1)
        self.reader.mousePressEvent(_mouse_event(QPoint(30, 100)))  # 300/3 = 100
        self.assertEqual(self.reader.index, 0)

    def test_clicking_the_rest_goes_forward(self) -> None:
        self.reader.mousePressEvent(_mouse_event(QPoint(250, 100)))
        self.assertEqual(self.reader.index, 1)

    def test_it_reports_text_shaped_answers_so_shared_paths_are_no_ops(self) -> None:
        """window.py 里那些文字书的路径靠 `char_count() > 0` 之类的守卫自我禁用。

        漫画给中性答案，它们就正确地什么都不做，而不是抛 AttributeError。
        """
        self.assertEqual(self.reader.char_count(), 0)
        self.assertEqual(self.reader.first_visible_char(), 0)
        self.assertEqual(self.reader.current_offset(), 0)
        self.assertEqual(self.reader.max_offset(), 0)
        self.reader.scroll_to_char(500)  # 不该抛

    def test_it_has_no_scrollbar_in_single_page_mode(self) -> None:
        """`scrollbar()` 是"有没有可滚动条"的中性访问器，单页返回 None。

        `verticalScrollBar()` 故意**不**声明——它是 Qt 自己的方法，window.py 用它建
        整个会话唯一的 AutoScroller，盖掉就毁掉文字滚动。
        """
        self.assertIsNone(self.reader.scrollbar())

    def test_it_does_not_shadow_qts_own_methods(self) -> None:
        """`verticalScrollBar` 是这条规则的**真正**理由。

        window.py:559 用 `self.reader.verticalScrollBar()` 建整个会话唯一的 AutoScroller，
        ComicReader 一盖掉它，文字书的自动滚动就没了。`show` / `hide` / `close` /
        `update` 同理：它们是 Qt 的窗口语义，不是 Reader 的。

        `viewport` 不在此列——ComicReader **必须**声明它（返回 `_page_view`），否则
        window.py 把事件过滤器装在 ComicReader 上而不是真正画页面的控件上。基类
        `Reader` 里没有它，由 test_book_base.py 守。
        """
        for name in ("verticalScrollBar", "show", "hide", "close", "update"):
            self.assertNotIn(
                name,
                ComicReader.__dict__,
                f"ComicReader 不许自己声明 {name}——它会被 window.py 当成 Qt 的用法",
            )

    def test_the_viewport_is_the_page_view_not_the_reader(self) -> None:
        """window.py 对 `reader.viewport()` 装事件过滤器做边缘缩放。

        返回 self 的话，事件过滤器会装在 ComicReader 上而不是真正画页面的控件上。
        """
        self.assertIsNot(self.reader.viewport(), self.reader)

    def test_the_mode_cycles_through_what_this_build_renders(self) -> None:
        """`cycle_mode()` 转遍 `IMPLEMENTED_MODES`。

        阶段 5 起三种模式都能画，所以转一圈是 single -> scroll -> spread -> 回到
        single。这条守的规则是"按钮不许停在一个画不出来的模式上"。
        """
        self.assertEqual(self.reader.mode, "single")
        seen = ["single"]
        for _ in range(len(IMPLEMENTED_MODES)):
            self.reader.cycle_mode()
            seen.append(self.reader.mode)
        self.assertEqual(seen, ["single", "scroll", "spread", "single"])
        self.assertEqual(
            set(IMPLEMENTED_MODES),
            set(READING_MODES),
            "转一圈不该漏掉任何一种能画的模式",
        )

    def test_asking_for_an_unimplemented_mode_says_so(self) -> None:
        """哨兵：用户主动切一个还没实现的模式，必须得到解释而不是静默失败。

        **阶段 5 起这条测试跳过**——三种模式（single/scroll/spread）都实现了，
        `pending` 恒为空。skip 的原因是**规则已经没有反例**，不是测试坏了、
        更不是可以删。

        加新模式时怎么用：给 `READING_MODES` 加一项、先别进 `IMPLEMENTED_MODES`，
        这条会自动不再 skip 并重新开始断言。要改回断言的是**它下面的那段代码**，
        不是这个 skip 分支——`pending` 非空时本来就会往下走。
        """
        pending = [m for m in READING_MODES if m not in IMPLEMENTED_MODES]
        if not pending:
            self.skipTest("所有模式都实现了，这条不再适用")
        # Reaching here means a future phase left a mode unimplemented again; the
        # wording is asserted as "some non-empty explanation", because the exact
        # sentence belongs to the phase that ships the mode.
        seen: list[str] = []
        self.reader.notice.connect(seen.append)
        self.reader.set_mode(pending[0])
        self.assertEqual(self.reader.mode, "single", "未实现的模式不许切过去")
        self.assertEqual(len(seen), 1, "但必须告诉用户为什么")
        self.assertTrue(seen[0])

    def test_an_unknown_mode_falls_back_to_single(self) -> None:
        self.reader.set_mode("nonsense")
        self.assertEqual(self.reader.mode, "single")

    def test_the_zoom_cycles_through_all_three(self) -> None:
        seen = [self.reader.zoom]
        for _ in range(3):
            self.reader.cycle_zoom()
            seen.append(self.reader.zoom)
        self.assertEqual(seen, ["width", "height", "original", "width"])

    def test_an_unknown_zoom_falls_back_to_width(self) -> None:
        self.reader.set_zoom("nonsense")
        self.assertEqual(self.reader.zoom, "width")

    def test_the_labels_and_tips_track_the_state(self) -> None:
        self.assertEqual(self.reader.mode_label(), MODE_LABELS["single"])
        self.assertEqual(self.reader.mode_tip(), f"阅读模式：{MODE_TIPS['single']}")
        self.assertEqual(self.reader.zoom_label(), ZOOM_LABELS["width"])
        self.assertEqual(self.reader.zoom_tip(), f"缩放：{ZOOM_TIPS['width']}")

    def test_an_empty_book_clears_the_page_instead_of_raising(self) -> None:
        self.reader.show_book(None)
        self.assertIsNone(self.reader.book)
        self.assertFalse(self.reader._page_view.has_page)

    def test_apply_style_does_not_raise_on_a_comic(self) -> None:
        """底栏的"这一页坏了"提示要跟着设置里的前景色走，页面本身不吃样式。"""
        from minireader.storage import Settings

        self.reader.apply_style(Settings())

    def test_content_opacity_defaults_to_fully_opaque(self) -> None:
        self.assertEqual(self.reader.content_opacity(), 1.0)

    def test_stop_releases_the_cache(self) -> None:
        self.assertGreater(len(self.reader.cache), 0)
        self.reader.stop()
        self.assertEqual(len(self.reader.cache), 0)

    def test_preloading_decodes_the_neighbours(self) -> None:
        """±1 页预加载：翻页时下一张已经在内存里，翻起来才不顿。"""
        self.assertEqual(PRELOAD_RADIUS, 1)
        self.reader._preload_neighbours()
        self.assertIn((self.book.id, 1), self.reader.cache)
        self.assertNotIn((self.book.id, 3), self.reader.cache, "只预取一格，不是整本")

    def test_switching_books_forgets_the_old_pages(self) -> None:
        other = open_book(make_cbz(self.tmp / "other.cbz", pages=2))
        old_id = self.book.id
        self.assertIn((old_id, 0), self.reader.cache)
        self.reader.show_book(other, index=0)
        self.assertNotIn((old_id, 0), self.reader.cache, "旧书的内存归新书用")


def _key_event(key):
    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)


def _mouse_event(pos: QPoint):
    """左键按下。本地坐标和全局坐标都给，`event_local_pos()` 两者都可能读。"""
    return QMouseEvent(
        QEvent.MouseButtonPress,
        QPointF(pos.x(), pos.y()),
        QPointF(pos.x(), pos.y()),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )


# ---------------------------------------------------------------------------
# 接进窗口之后
# ---------------------------------------------------------------------------


class ComicWindowTestCase(WindowTestCase):
    """在 `WindowTestCase` 的 %APPDATA% 沙箱上加一个漫画书。"""

    def setUp(self) -> None:
        super().setUp()
        self.cbz_path = make_cbz(self.tmp / "comic.cbz", pages=4)
        self.text_path = make_book(self.tmp, name="novel.txt", chapters=3)


class TestReaderStack(ComicWindowTestCase):
    """两个 Reader 共存，`self.reader` 始终指向当前那个。"""

    def test_a_fresh_window_starts_on_the_text_reader(self) -> None:
        window = self.make_window()
        self.assertIs(window.reader, window._text_reader)
        self.assertIsInstance(window.reader, ReaderView)
        self.assertEqual(window._reader_stack.currentIndex(), 0)
        self.assertEqual(window._reader_stack.count(), 2, "两个 Reader 都要在里面")

    def test_opening_a_comic_switches_the_stack(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIsInstance(window.book, ComicBook)
        self.assertIs(window.reader, window._comic_reader)
        self.assertEqual(window._reader_stack.currentIndex(), 1)

    def test_opening_a_text_book_keeps_the_text_reader(self) -> None:
        window = self.make_window(path=self.text_path)
        self.assertEqual(window.book.kind, KIND_TEXT)
        self.assertIs(window.reader, window._text_reader)
        self.assertEqual(window._reader_stack.currentIndex(), 0)

    def test_switching_back_and_forth_keeps_both_alive(self) -> None:
        """这条是阶段 2 计划里点名的验收条件：切回文字书后 `self.reader` 必须还是
        原来的那个 ReaderView 对象，而且两个 Reader 全程没有被销毁重建。"""
        window = self.make_window(path=self.text_path)
        text_reader = window._text_reader
        comic_reader = window._comic_reader

        window.load_path(self.cbz_path)
        pump()
        self.assertIs(window.reader, comic_reader)

        window.load_path(self.text_path)
        pump()
        self.assertIs(window.reader, text_reader)
        self.assertIs(window._text_reader, text_reader, "不是新建的")
        self.assertIs(window._comic_reader, comic_reader, "也没被销毁")

    def test_a_text_book_does_not_touch_the_comic_cache(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.load_path(self.text_path)
        pump()
        self.assertEqual(len(window._comic_reader.cache), 0, "切走了就该释放内存")

    def test_the_placeholder_clears_the_comic_too(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.show_chapter()
        self.assertIsNotNone(window.book)


class TestComicDefaultModeAtStartup(ComicWindowTestCase):
    """`apply_comic_settings()` 把 `comic_default_mode` 推给 reader 的纪律。

    模式 B 落地之前，`comic_default_mode="spread"` 是一个**存得下、读得回、但这一版
    渲染不了**的值。`set_mode()` 对渲染不了的模式会 emit 一句「将在阶段 N 加入」并
    保持原样——用户点按钮时这是对的，开机时不是：一个很久以前设的、当时还无能为力的
    设置，不该每次启动都在底栏弹一句话。所以 `apply_comic_settings()` 先问
    `IMPLEMENTED_MODES` 再推。
    """

    def _warnings_during_startup(self, mode: str) -> list[str]:
        """用 `comic_default_mode=mode` 造一个窗口，收集它启动时收到的 notice。"""
        self.settings.comic_default_mode = mode
        seen: list[str] = []
        window = self.make_window()
        window._comic_reader.notice.connect(seen.append)
        window.apply_comic_settings()
        pump()
        return seen

    def test_an_unimplemented_default_mode_is_not_pushed_at_startup(self) -> None:
        for mode in ("scroll", "spread"):
            if mode in IMPLEMENTED_MODES:
                continue
            with self.subTest(mode=mode):
                seen = self._warnings_during_startup(mode)
                self.assertEqual(seen, [], f"{mode} 还没实现，开机不该有提示")
                # 底栏也不该留下话——notice 接到 set_warning，是用户看得见的位置。
                window = self.windows[-1]
                self.assertEqual(window.warn_label.text(), "")

    def test_an_implemented_default_mode_is_pushed_at_startup(self) -> None:
        seen = self._warnings_during_startup("single")
        self.assertEqual(seen, [], "已实现的模式当然不该有提示")
        self.assertEqual(self.windows[-1]._comic_reader.mode, "single")

    def test_a_pending_mode_still_announces_itself_when_asked_directly(self) -> None:
        """哨兵：阶段 4 的收尾 2 立的规矩——开机时不推，但用户自己问一定要回答。

        **阶段 5 起这条测试跳过**——三种模式都实现了，`pending` 恒为空。skip 的原因是
        **规则已经没有反例**，不是测试坏了、更不是可以删。

        加新模式时怎么用：给 `READING_MODES` 加一项、先别进 `IMPLEMENTED_MODES`，
        这条会自动活过来（`pending` 非空就不再 skip），并重新断言"直接问必须回答、
        而且答案要出现在底栏 `warn_label` 上"。
        """
        window = self.make_window()
        pending = [m for m in READING_MODES if m not in IMPLEMENTED_MODES]
        if not pending:
            self.skipTest("所有模式都实现了，这条不再适用")
        seen: list[str] = []
        window._comic_reader.notice.connect(seen.append)
        window._comic_reader.set_mode(pending[0])
        self.assertEqual(len(seen), 1, "问一个没实现的模式必须得到解释")
        self.assertTrue(seen[0])
        self.assertEqual(window.warn_label.text(), seen[0], "而且要说给用户看见")


class TestComicButtons(ComicWindowTestCase):
    """标题栏的两个漫画按钮只在漫画模式可见。"""
    def test_they_are_hidden_for_a_text_book(self) -> None:
        window = self.make_window(path=self.text_path)
        self.assertFalse(window.comic_mode_button.isVisibleTo(window))
        self.assertFalse(window.comic_zoom_button.isVisibleTo(window))

    def test_they_are_hidden_before_any_book_is_open(self) -> None:
        window = self.make_window()
        self.assertFalse(window.comic_mode_button.isVisibleTo(window))
        self.assertFalse(window.comic_zoom_button.isVisibleTo(window))

    def test_they_appear_for_a_comic(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertTrue(window.comic_mode_button.isVisibleTo(window))
        self.assertTrue(window.comic_zoom_button.isVisibleTo(window))

    def test_they_disappear_again_when_switching_back_to_text(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.load_path(self.text_path)
        pump()
        self.assertFalse(window.comic_mode_button.isVisibleTo(window))
        self.assertFalse(window.comic_zoom_button.isVisibleTo(window))

    def test_they_meet_the_title_bar_button_size(self) -> None:
        """42px 的标题栏里放 26px 的方钮，和旁边那些按钮一样高。"""
        window = self.make_window(path=self.cbz_path)
        for button in (window.comic_mode_button, window.comic_zoom_button):
            self.assertGreaterEqual(button.minimumWidth(), TITLE_BTN_MIN)
            self.assertGreaterEqual(button.minimumHeight(), TITLE_BTN_MIN)

    def test_every_button_carries_a_tooltip(self) -> None:
        """图标是 ▭/↔ 这种符号，没有 tooltip 用户不可能猜到含义。"""
        window = self.make_window(path=self.cbz_path)
        self.assertTrue(window.comic_mode_button.toolTip())
        self.assertTrue(window.comic_zoom_button.toolTip())

    def test_the_zoom_button_cycles_and_the_label_follows(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.comic_zoom_button.text(), ZOOM_LABELS["width"])
        seen = [window.comic_zoom_button.text()]
        for _ in range(3):
            window.cycle_comic_zoom()
            seen.append(window.comic_zoom_button.text())
        self.assertEqual(seen, [ZOOM_LABELS["width"], ZOOM_LABELS["height"],
                                ZOOM_LABELS["original"], ZOOM_LABELS["width"]])

    def test_the_zoom_button_tooltip_follows_the_state(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.comic_zoom_button.toolTip(), f"缩放：{ZOOM_TIPS['width']}")
        window.cycle_comic_zoom()
        self.assertEqual(window.comic_zoom_button.toolTip(), f"缩放：{ZOOM_TIPS['height']}")

    def test_the_mode_button_follows_the_mode_cycle(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.comic_mode_button.text(), MODE_LABELS["single"])
        seen = [window.comic_mode_button.text()]
        for _ in range(len(IMPLEMENTED_MODES)):
            window.cycle_comic_mode()
            seen.append(window.comic_mode_button.text())
        self.assertEqual(seen, [MODE_LABELS[m] for m in list(IMPLEMENTED_MODES) + ["single"]])
        # 切到双页时 tooltip 也要跟上，否则按钮变了、提示还是旧的。
        window.cycle_comic_mode()
        self.assertIn(MODE_TIPS["scroll"], window.comic_mode_button.toolTip())

    def test_the_zoom_change_reaches_the_bottom_bar(self) -> None:
        """按钮变了而底栏没变会看起来像没生效——底栏次要信息里带着 zoom_tip()。"""
        window = self.make_window(path=self.cbz_path)
        self.assertIn(ZOOM_TIPS["width"], window.status_label.full_text())
        window.cycle_comic_zoom()
        self.assertIn(ZOOM_TIPS["height"], window.status_label.full_text())


class TestComicBindingButton(ComicWindowTestCase):
    """装订方向按钮：只在双页模式出现，点了只改本书。

    阶段 5 就写好了 `cycle_binding()`，但一直没有界面入口——书里声明的
    `page-progression-direction` 因此是**不可推翻**的。这一组测试钉住三件事：
    按钮只在看得见效果的模式里出现、点击只记"本书覆盖"、提示说清方向来自哪一层。
    """

    def spread_window(self, path=None):
        window = self.make_window(path=path or self.cbz_path)
        window._comic_reader.set_mode("spread")
        window._sync_comic_buttons()
        return window

    def test_it_is_hidden_outside_two_page_mode(self) -> None:
        """单页和连续滚动里改方向不改变任何像素，放个按了没反应的按钮更糟。"""
        window = self.make_window(path=self.cbz_path)
        self.assertFalse(window.comic_binding_button.isVisibleTo(window))
        window.cycle_comic_mode()  # scroll
        self.assertFalse(window.comic_binding_button.isVisibleTo(window))

    def test_it_appears_in_two_page_mode(self) -> None:
        window = self.spread_window()
        self.assertTrue(window.comic_binding_button.isVisibleTo(window))

    def test_it_is_hidden_for_a_text_book(self) -> None:
        window = self.make_window(path=self.text_path)
        window._comic_reader.set_mode("spread")
        window._sync_comic_buttons()
        self.assertFalse(window.comic_binding_button.isVisibleTo(window))

    def test_it_meets_the_title_bar_button_size(self) -> None:
        window = self.spread_window()
        self.assertGreaterEqual(window.comic_binding_button.minimumWidth(), TITLE_BTN_MIN)
        self.assertGreaterEqual(window.comic_binding_button.minimumHeight(), TITLE_BTN_MIN)

    def test_it_cycles_the_direction(self) -> None:
        window = self.spread_window()
        before = window._comic_reader.binding
        window.cycle_comic_binding()
        self.assertNotEqual(window._comic_reader.binding, before)
        window.cycle_comic_binding()
        self.assertEqual(window._comic_reader.binding, before)

    def test_a_click_is_an_override_and_leaves_the_setting_alone(self) -> None:
        """翻一本日漫不该把所有书都翻过去。"""
        window = self.spread_window()
        setting = window._comic_reader.binding_setting
        window.cycle_comic_binding()
        self.assertEqual(window._comic_reader.binding_setting, setting)

    def test_the_label_and_tooltip_follow(self) -> None:
        window = self.spread_window()
        seen = {window.comic_binding_button.text()}
        tips = {window.comic_binding_button.toolTip()}
        window.cycle_comic_binding()
        seen.add(window.comic_binding_button.text())
        tips.add(window.comic_binding_button.toolTip())
        self.assertEqual(len(seen), 2, "标签必须跟着方向变")
        self.assertEqual(len(tips), 2, "提示必须跟着方向变")
        for tip in tips:
            self.assertIn("装订方向", tip)

    def test_the_click_reaches_the_bottom_bar(self) -> None:
        """点了没反馈，用户会以为按钮坏了。"""
        window = self.spread_window()
        window.cycle_comic_binding()
        self.assertIn("装订方向", window.warn_label.text())

    def test_a_window_too_narrow_for_two_pages_hides_it_again(self) -> None:
        """窗口拖窄会自动退回单页，按钮必须跟着消失。

        `ComicReader.resizeEvent()` 退出双页后发 `viewportChanged`，窗口在这条
        信号里重挂视图并重刷按钮。少了这一步，按钮会留在一个已经单页的窗口上，
        而它此时什么都不改——看起来像坏了。
        """
        window = self.spread_window()
        self.assertTrue(window.comic_binding_button.isVisibleTo(window))
        window.resize(300, 700)
        pump()
        self.assertEqual(window._comic_reader.mode, "single")
        self.assertFalse(window.comic_binding_button.isVisibleTo(window))


class TestComicStatusBar(ComicWindowTestCase):
    """底栏：文本"第 x/y 章"、漫画"第 x/y 页"，控件名不变。"""

    def test_a_comic_counts_pages(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.chapter_label.text(), "第 1/4 页")
        self.assertEqual(window.progress_label.text(), "25.00%")

    def test_a_text_book_still_counts_chapters(self) -> None:
        window = self.make_window(path=self.text_path)
        self.assertEqual(window.chapter_label.text(), "第 1/3 章")

    def test_turning_a_page_updates_the_label(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(2)
        pump()
        self.assertEqual(window.chapter_label.text(), "第 3/4 页")
        self.assertEqual(window.progress_label.text(), "75.00%")

    def test_the_status_line_names_the_source_and_the_size(self) -> None:
        window = self.make_window(path=self.cbz_path)
        text = window.status_label.full_text()
        self.assertIn("漫画压缩包", text)
        self.assertIn("40×60", text, "第一页的真实尺寸")

    def test_the_prev_button_disables_on_the_first_page(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertFalse(window.prev_button.isEnabled())
        self.assertTrue(window.next_button.isEnabled())

    def test_the_next_button_disables_on_the_last_page(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(3)
        pump()
        self.assertTrue(window.prev_button.isEnabled())
        self.assertFalse(window.next_button.isEnabled())

    def test_the_tooltip_lists_the_title_the_summary_and_the_notes(self) -> None:
        window = self.make_window(path=self.cbz_path)
        tip = window.status_label.toolTip()
        self.assertIn(window.book.title, tip)
        self.assertIn("4 页", tip)

    def test_a_single_image_folder_gets_a_note(self) -> None:
        """只有一张图的文件夹要在 tooltip 里说清楚，否则用户以为程序坏了。"""
        folder = make_comic_folder(self.tmp / "one", pages=1)
        window = self.make_window(path=folder)
        self.assertIn("这个文件夹里只有一张图片", window.status_label.toolTip())


class TestComicPaging(ComicWindowTestCase):
    def test_step_chapter_turns_pages(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.step_chapter(1)
        pump()
        self.assertEqual(window.chapter_index, 1)
        self.assertEqual(window.chapter_label.text(), "第 2/4 页")

    def test_it_stops_at_both_ends(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.step_chapter(-1)
        pump()
        self.assertEqual(window.chapter_index, 0)
        for _ in range(10):
            window.step_chapter(1)
        pump()
        self.assertEqual(window.chapter_index, 3)

    def test_the_page_really_changes(self) -> None:
        """尺寸互不相同，所以"换了页"是尺寸证明的，不只是索引变了。"""
        window = self.make_window(path=self.cbz_path)
        first = window._comic_reader._page_view.page_size()
        window.goto_chapter(1)
        pump()
        second = window._comic_reader._page_view.page_size()
        self.assertNotEqual((first.width(), first.height()), (second.width(), second.height()))

    def test_the_reader_and_the_window_agree_on_the_page(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(2)
        pump()
        self.assertEqual(window._comic_reader.index, window.chapter_index)

    def test_a_wheel_over_the_page_turns_it(self) -> None:
        window = self.make_window(path=self.cbz_path)
        wheel_at(window._comic_reader, 1)
        pump()
        self.assertEqual(window.chapter_index, 1)

    def test_scrolling_schedules_a_progress_save(self) -> None:
        """翻页要触发 900ms 的防抖保存（`_on_scrolled` -> `_progress_timer`）。"""
        window = self.make_window(path=self.cbz_path)
        window._progress_timer.stop()
        window._comic_reader.next_page()
        pump()
        self.assertTrue(window._progress_timer.isActive())


class TestComicPagingSyncsTheWindow(ComicWindowTestCase):
    """翻页手势在 ComicReader 里改 `_index`，窗口的 `chapter_index` 必须跟着走。

    这条**曾经是坏的**，而且坏得很安静：滚轮/键盘/点击翻页只动了 ComicReader 自己的
    `_index`，窗口那一层停在旧页码。症状是底栏还写"第 1/4 页"、首末页按钮的灰亮是旧
    的，900ms 后 `persist_progress()` 写进 progress.json 的还是旧页——**翻完页重开又
    回到第一页**。根因是 `ComicReader.scrolled` 只接了 `_on_scrolled`，而它原本只干
    "重启 900ms 保存定时器"一件事，不同步位置。

    修法在 `_on_scrolled` 里：漫画分支把 `chapter_index` 从 `_comic_reader.index` 同步
    过来再 `_update_status()`。文字书那条路一个字节都没动——文本的 `scrolled` 是**章内
    像素滚动**，章号没变，底栏那格本来就只在换章时刷新（阶段 1 验收过的行为）。
    """

    def test_a_wheel_turn_moves_the_window_position_too(self) -> None:
        window = self.make_window(path=self.cbz_path)
        wheel_at(window._comic_reader, 1)
        pump()
        self.assertEqual(window._comic_reader.index, 1)
        self.assertEqual(window.chapter_index, 1, "窗口这一层也要跟着走")
        self.assertEqual(window.chapter_label.text(), "第 2/4 页")

    def test_a_wheel_turn_updates_the_bottom_bar(self) -> None:
        window = self.make_window(path=self.cbz_path)
        wheel_at(window._comic_reader, 1)
        pump()
        self.assertEqual(window.progress_label.text(), "50.00%")
        self.assertIn("page_2.png", window.status_label.full_text())

    def test_a_wheel_turn_survives_a_reopen(self) -> None:
        """这条是用户真正会遇到的症状：滚轮翻页后重开，必须还在那一页。"""
        window = self.make_window(path=self.cbz_path)
        wheel_at(window._comic_reader, 1)
        pump()
        window.persist_progress()
        self.assertEqual(self.progress.get(window.book.id).page, 1)

        again = self.make_window(path=self.cbz_path)
        pump()
        self.assertEqual(again.chapter_index, 1, "重开停在第 2 页，不是第 1 页")
        self.assertEqual(again.chapter_label.text(), "第 2/4 页")

    def test_a_keyboard_turn_moves_the_window_position_too(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window._comic_reader.keyPressEvent(_key_event(Qt.Key_Right))
        pump()
        self.assertEqual(window.chapter_index, 1)

    def test_a_click_turn_moves_the_window_position_too(self) -> None:
        window = self.make_window(path=self.cbz_path)
        reader = window._comic_reader
        reader.resize(300, 200)
        reader.mousePressEvent(_mouse_event(QPoint(290, 100)))  # 右侧三分之二
        pump()
        self.assertEqual(window.chapter_index, 1)

    def test_the_buttons_follow_a_wheel_turn(self) -> None:
        """首末页按钮的灰亮原来停在旧页：第 1 页时"上一页"该是灰的。"""
        window = self.make_window(path=self.cbz_path)
        self.assertFalse(window.prev_button.isEnabled())
        wheel_at(window._comic_reader, 1)
        pump()
        self.assertTrue(window.prev_button.isEnabled(), "已经不在第 1 页了")
        for _ in range(5):
            wheel_at(window._comic_reader, 1)
            _settle(WHEEL_DEBOUNCE_MS + 40)
            pump()
        self.assertEqual(window.chapter_index, 3, "停在末页")
        self.assertFalse(window.next_button.isEnabled(), "末页时下一页按钮该是灰的")

    def test_a_text_book_does_not_move_its_chapter_on_scroll(self) -> None:
        """反向断言：文本的 `scrolled` 是章内像素滚动，章号不许跟着变。"""
        window = self.make_window(path=self.text_path)
        self.assertEqual(window.chapter_index, 0)
        window.reader.verticalScrollBar().setValue(200)
        pump()
        self.assertEqual(window.chapter_index, 0, "章内滚动不是换章")
        self.assertEqual(window.chapter_label.text(), "第 1/3 章")


class TestComicProgress(ComicWindowTestCase):
    """页码持久化：`Progress.page` 是漫画的位置，文字书继续用 offset。"""

    def test_current_progress_carries_the_page_and_the_mode(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(2)
        pump()
        progress = window.current_progress()
        self.assertEqual(progress, Progress(chapter=2, offset=0, page=2, mode="single"))

    def test_a_text_book_does_not_set_page(self) -> None:
        window = self.make_window(path=self.text_path)
        progress = window.current_progress()
        self.assertEqual(progress.page, 0, "文字书的页码恒为 0")
        self.assertEqual(progress.mode, "")

    def test_persist_writes_the_page_into_the_store(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(2)
        pump()
        window.persist_progress()
        saved = self.progress.get(window.book.id)
        self.assertEqual(saved.page, 2)
        self.assertEqual(saved.chapter, 2)

    def test_reopening_lands_on_the_saved_page(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(2)
        pump()
        window.persist_progress()

        second = self.make_window(path=self.cbz_path)
        self.assertEqual(second.chapter_index, 2, "重开同一本漫画要回到第 3 页")
        self.assertEqual(second.chapter_label.text(), "第 3/4 页")

    def test_a_book_with_no_saved_page_starts_at_the_first(self) -> None:
        """旧 progress.json 里没有 `page` 键，`from_dict` 补 0——兼容性的一部分。"""
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.chapter_index, 0)

    def test_the_saved_page_survives_a_store_round_trip(self) -> None:
        progress = Progress(chapter=5, offset=0, page=5, mode="scroll")
        again = Progress.from_dict(progress.to_dict())
        self.assertEqual(again.page, 5)
        self.assertEqual(again.mode, "scroll")

    def test_a_negative_page_is_clamped(self) -> None:
        self.assertEqual(Progress.from_dict({"page": -3}).page, 0)

    def test_the_comic_mode_is_written_into_progress(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.persist_progress()
        self.assertEqual(self.progress.get(window.book.id).mode, "single")


class TestComicBookmarks(ComicWindowTestCase):
    """书签表按 book.id 索引，两种书共用一张表但不能串味。"""

    def test_a_comic_bookmark_is_labelled_by_page(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(2)
        pump()
        window.add_bookmark()
        marks = window.store.list_for(window.book.id)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].label, "第 3 页", "漫画书签说页码，不是字符偏移")
        self.assertEqual(marks[0].offset, 0, "漫画没有章内字符偏移")
        self.assertEqual(marks[0].chapter, 2)
        self.assertEqual(marks[0].excerpt, "", "漫画没有可以摘的句子")

    def test_it_reports_success(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.add_bookmark()
        pump()
        self.assertEqual(window.warn_label.text(), "已加书签")

    def test_a_text_bookmark_still_uses_characters(self) -> None:
        window = self.make_window(path=self.text_path)
        window.add_bookmark()
        pump()
        marks = window.store.list_for(window.book.id)
        self.assertEqual(len(marks), 1)
        self.assertNotEqual(marks[0].excerpt, "", "文字书书签有摘录")


class TestComicContextMenu(ComicWindowTestCase):
    def test_the_comic_view_asks_for_the_menu(self) -> None:
        window = self.make_window(path=self.cbz_path)
        seen: list[int] = []
        window._comic_reader.contextMenuRequested.connect(lambda _pos: seen.append(1))
        window._comic_reader.contextMenuEvent(
            QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(10, 10), QPoint(100, 100))
        )
        pump()
        self.assertEqual(len(seen), 1, "ComicReader 只发信号，菜单由窗口弹")

    def test_the_menu_shows_the_page_items(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.prev_page_action.text(), "上一页")
        self.assertEqual(window.next_page_action.text(), "下一页")

    def test_the_page_items_disable_at_the_ends(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.reader_menu.aboutToShow.emit()
        self.assertFalse(window.prev_page_action.isEnabled(), "第一页没有上一页")
        self.assertTrue(window.next_page_action.isEnabled())
        window.goto_chapter(3)
        pump()
        window.reader_menu.aboutToShow.emit()
        self.assertTrue(window.prev_page_action.isEnabled())
        self.assertFalse(window.next_page_action.isEnabled(), "最后一页没有下一页")

    def test_reloading_a_comic_keeps_it_a_comic(self) -> None:
        """这条守的是一个真实的崩溃。

        `reload_current_chapter()` 曾经用 `Book.from_path()` 重读，漫画因此变成文字
        Book 被喂进 ComicReader，在 `book.pages` 上抛 AttributeError。重开一次同一个
        文件必须还是同一类书。
        """
        window = self.make_window(path=self.cbz_path)
        window.goto_chapter(1)
        pump()
        window.reload_current_chapter()
        pump()
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIsInstance(window.book, ComicBook)
        self.assertIs(window.reader, window._comic_reader)
        self.assertEqual(window.chapter_index, 1, "留在同一页")

    def test_reloading_a_comic_reports_no_warning(self) -> None:
        """漫画没有"编码可能不准"这回事。"""
        window = self.make_window(path=self.cbz_path)
        window.reload_current_chapter()
        pump()
        self.assertEqual(window.warn_label.text(), "")

    def test_reloading_a_missing_comic_keeps_the_old_book(self) -> None:
        window = self.make_window(path=self.cbz_path)
        old_book = window.book
        self.cbz_path.unlink()
        window.reload_current_chapter()
        pump()
        self.assertIs(window.book, old_book)
        self.assertIn("重新加载失败", window.warn_label.text())


class TestComicSharedPaths(ComicWindowTestCase):
    """window.py 是给文字书写的；这些共享路径在漫画上必须优雅降级。"""

    def test_the_autoscroll_button_is_a_no_op(self) -> None:
        """单页漫画没有可滚动的条，`scrollbar()` 返回 None，自动滚动因此停下。

        `toggle_autoscroll()` 里那道闸门是必须的：AutoScroller 构造时吃死的是**文本**
        滚动条（见 window.py:559），而文本滚动条在漫画模式下依然有 maximum，所以不拦
        的话按钮会亮成"暂停"、屏幕上一动不动，等它悄悄滚到隐藏正文的底部还会
        `_on_autoscroll_end` 把漫画翻页——用户没碰键盘，页码自己变了。
        """
        window = self.make_window(path=self.cbz_path)
        window.toggle_autoscroll()
        pump()
        self.assertFalse(window.autoscroller.is_running(), "漫画上没有东西可以自动滚动")
        self.assertEqual(window.warn_label.text(), "漫画是整页显示的，用不着自动滚动")

    def test_resuming_autoscroll_after_a_chapter_change_says_the_same_thing(self) -> None:
        """`_resume_autoscroll()` 走的是同一道闸门，文案不该退回"这一章"。"""
        window = self.make_window(path=self.cbz_path)
        window._resume_autoscroll()
        pump()
        self.assertFalse(window.autoscroller.is_running())
        self.assertEqual(window.warn_label.text(), "漫画是整页显示的，用不着自动滚动")

    def test_the_toc_falls_back_to_an_empty_list(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window._refresh_toc()
        pump()
        self.assertEqual(window.book.chapters, [], "漫画没有章节")

    def test_opening_the_toc_does_not_raise(self) -> None:
        window = self.make_window(path=self.cbz_path)
        window.open_toc()
        pump()
        window.close_toc()

    def test_a_chapter_body_is_empty_rather_than_missing(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.book.chapter_body(0), "")
        self.assertIsNone(window.book.chapter(0))

    def test_the_summary_reads_as_a_page_count(self) -> None:
        window = self.make_window(path=self.cbz_path)
        self.assertEqual(window.book.split_summary(), "共 4 页")

    def test_it_is_confident_and_has_no_encoding(self) -> None:
        """`confident=True` 让"编码可能不准"那条警告不会误报在漫画上。"""
        window = self.make_window(path=self.cbz_path)
        self.assertTrue(window.book.confident)
        self.assertEqual(window.book.encoding_label, "漫画")


class TestComicDrops(ComicWindowTestCase):
    """拖放：格式表是唯一裁决处，漫画格式和图片文件夹都要能收。

    `_first_supported` / `_first_drop_note` 都是 **classmethod**（阶段 1 起就是），
    所以断言比较的是路径字符串。`QUrl.toLocalFile()` 在 Windows 上给的是正斜杠
    （`C:/Users/...`），用 `Path` 比较而不是字符串——同 `test_ui_offscreen.same_path`。
    """

    def _mime(self, *paths: Path):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        return mime

    def _drop(self, window, *paths: Path) -> None:
        """走真实的 `dropEvent`，不是绕过它直接调 load_path。"""
        mime = self._mime(*paths)

        class _FakeEvent:
            def __init__(self) -> None:
                self.accepted = False
                self.ignored = False

            def mimeData(self):  # noqa: N802 - Qt 的命名
                return mime

            def acceptProposedAction(self) -> None:  # noqa: N802
                self.accepted = True

            def ignore(self) -> None:  # noqa: N802
                self.ignored = True

        event = _FakeEvent()
        window.dropEvent(event)
        return event

    def test_a_cbz_is_accepted(self) -> None:
        window = self.make_window()
        got = window._first_supported(self._mime(self.cbz_path))
        self.assertIsNotNone(got)
        self.assertEqual(Path(got), self.cbz_path)

    def test_a_comic_folder_is_accepted(self) -> None:
        folder = make_comic_folder(self.tmp / "pages", pages=2)
        window = self.make_window()
        got = window._first_supported(self._mime(folder))
        self.assertIsNotNone(got)
        self.assertEqual(Path(got), folder)

    def test_an_empty_folder_is_refused_with_a_reason(self) -> None:
        """空文件夹要在**接受之前**拒绝，否则用户看到"接受拖放 + 报错弹窗"。"""
        empty = self.tmp / "empty"
        empty.mkdir()
        window = self.make_window()
        self.assertIsNone(window._first_supported(self._mime(empty)))
        self.assertEqual(window._first_drop_note(self._mime(empty)), "这个文件夹里没有能看的图片")

    def test_a_locked_format_is_still_refused(self) -> None:
        """相位闸门：表里认得、相位没到的格式，拖放要拒绝并说明原因。

        阶段 3 之前这里用的是 `.pdf`（当时 phase 3 还没落地），阶段 6 之前是
        `.mobi`，阶段 8 之前是 `.cbr`；每解锁一个格式就换成下一个仍锁着的。
        **阶段 8 之后格式表里没有锁着的行了**，所以样本改成打桩的一行格式——
        守的机制完全一样：`_first_supported()` 按 `support_note()` 判，而
        `support_note()` 报的是相位而不是"不是有效文件"。
        """
        locked = Format(KIND_COMIC, (".xyz",), "未来漫画", CURRENT_PHASE + 1)
        book = self.tmp / "book.xyz"
        book.write_bytes(b"x")
        window = self.make_window()
        with mock.patch.object(formats_module, "format_for", return_value=locked):
            self.assertIsNone(window._first_supported(self._mime(book)))
            self.assertIn("阶段 9", window._first_drop_note(self._mime(book)))

    def test_a_cbr_is_accepted_now_and_reports_the_missing_tool_later(self) -> None:
        """阶段 8 起 `.cbr` 是能开的格式，所以拖放会接受它。

        "接受"只表示扩展名受支持；这台机器上没有 WinRAR/7-Zip 时，真正的报错
        发生在 `open_book()` 里（"需要 WinRAR 或 7-Zip"）。两件事分开是对的：
        拖放阶段去探测解压工具会让每次悬停都跑子进程。
        """
        cbr = self.tmp / "book.cbr"
        cbr.write_bytes(b"Rar!")
        window = self.make_window()
        self.assertEqual(Path(window._first_supported(self._mime(cbr))), cbr)

    def test_a_text_book_wins_over_a_comic_in_the_same_drop(self) -> None:
        """`_first_supported` 按载荷顺序取第一个能开的，与书的类型无关。"""
        window = self.make_window()
        mime = self._mime(self.cbz_path, self.text_path)
        self.assertEqual(Path(window._first_supported(mime)), self.cbz_path, "取第一个能开的")

    def test_a_mixed_drop_skips_the_unopenable_ones(self) -> None:
        exe = self.tmp / "thing.exe"
        exe.write_bytes(b"MZ")
        window = self.make_window()
        mime = self._mime(exe, self.cbz_path)
        self.assertEqual(Path(window._first_supported(mime)), self.cbz_path)

    def test_a_drop_with_nothing_openable_says_so(self) -> None:
        """`.exe` 的 `support_note()` 是"不支持的文件类型：.exe"，比兜底话更具体。"""
        exe = self.tmp / "thing.exe"
        exe.write_bytes(b"MZ")
        window = self.make_window()
        self.assertEqual(window._first_drop_note(self._mime(exe)), "不支持的文件类型：.exe")

    def test_the_old_private_name_is_still_an_alias(self) -> None:
        """阶段 1 的测试直接调 `_first_txt`，改名不许弄坏它。

        两者都是 classmethod，所以比的是**底层函数**：`__func__` 相同才是同一个方法。
        直接比 `window._first_txt is window._first_supported` 永远为 False——每次属性
        访问都会新建一个 bound method 对象。
        """
        window = self.make_window()
        self.assertIs(type(window)._first_txt.__func__, type(window)._first_supported.__func__)
        self.assertEqual(
            window._first_txt(self._mime(self.text_path)),
            window._first_supported(self._mime(self.text_path)),
        )

    def test_dropping_a_comic_opens_it(self) -> None:
        window = self.make_window()
        event = self._drop(window, self.cbz_path)
        pump()
        self.assertTrue(event.accepted)
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIs(window.reader, window._comic_reader)

    def test_dropping_a_comic_folder_opens_it(self) -> None:
        folder = make_comic_folder(self.tmp / "pages", pages=2)
        window = self.make_window()
        event = self._drop(window, folder)
        pump()
        self.assertTrue(event.accepted)
        self.assertEqual(window.book.source, "folder")
        self.assertEqual(window.chapter_label.text(), "第 1/2 页")

    def test_dropping_an_unopenable_file_explains_itself(self) -> None:
        """`dragEnterEvent` 已经 ignore 过，光标本来就是"禁止"；补的是**原因**。"""
        exe = self.tmp / "thing.exe"
        exe.write_bytes(b"MZ")
        window = self.make_window()
        event = self._drop(window, exe)
        pump()
        self.assertFalse(event.accepted)
        self.assertEqual(window.warn_label.text(), "不支持的文件类型：.exe")


class TestComicErrorsAreFriendly(ComicWindowTestCase):
    """打不开的漫画要给人话，而不是 Python 类名。"""

    def test_a_broken_archive_reports_a_sentence(self) -> None:
        broken = self.tmp / "broken.cbz"
        broken.write_bytes(b"this is not a zip file at all")
        window = self.make_window()
        self.assertFalse(window.load_path(broken))
        self.assertIn("打不开", window.warn_label.text())
        self.assertNotIn("ArchiveError", window.warn_label.text())
        self.assertNotIn("BookError", window.warn_label.text())

    def test_an_empty_archive_reports_a_sentence(self) -> None:
        empty = self.tmp / "empty.cbz"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("readme.txt", b"nothing to see")
        window = self.make_window()
        self.assertFalse(window.load_path(empty))
        self.assertIn("没有能看的图片", window.warn_label.text())
        self.assertNotIn("ArchiveError", window.warn_label.text())

    def test_a_missing_file_reports_a_sentence(self) -> None:
        window = self.make_window()
        self.assertFalse(window.load_path(self.tmp / "nope.cbz"))
        self.assertIn("打不开", window.warn_label.text())
        self.assertNotIn("BookError", window.warn_label.text())

    def test_a_broken_page_shows_a_message_not_a_crash(self) -> None:
        """一本 400 页的漫画里有 3 张坏图，其余 397 页还得能看。"""
        mixed = self.tmp / "mixed.cbz"
        with zipfile.ZipFile(mixed, "w") as zf:
            zf.writestr("page_1.png", make_png(40, 60))
            zf.writestr("page_2.png", b"not an image")
            zf.writestr("page_3.png", make_png(60, 40))
        window = self.make_window(path=mixed)
        self.assertEqual(window.chapter_index, 0)
        window.goto_chapter(1)
        pump()
        self.assertEqual(window._comic_reader.message, "这一页不是有效的图片")
        window.goto_chapter(2)
        pump()
        self.assertEqual(window._comic_reader.message, "")
        self.assertTrue(window._comic_reader._page_view.has_page, "下一张好图照常显示")


class TestSingleImageAndFolder(ComicWindowTestCase):
    def test_a_single_image_opens_as_a_one_page_comic(self) -> None:
        image = self.tmp / "solo.png"
        image.write_bytes(make_png(70, 30))
        window = self.make_window(path=image)
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertEqual(window.book.position_count, 1)
        self.assertEqual(window.chapter_label.text(), "第 1/1 页")
        self.assertIn("这是一张单独的图片", window.status_label.toolTip())

    def test_a_folder_opens_as_a_comic(self) -> None:
        folder = make_comic_folder(self.tmp / "pages", pages=3)
        window = self.make_window(path=folder)
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertEqual(window.book.position_count, 3)
        self.assertEqual(window.book.source, "folder")

    def test_a_folder_pages_in_natural_order(self) -> None:
        """`page_10` 必须排在 `page_2` 后面——漫画最常见的翻页顺序错误。"""
        folder = self.tmp / "many"
        folder.mkdir()
        for number in (1, 2, 10):
            (folder / f"page_{number}.png").write_bytes(make_png(10 + number, 20))
        window = self.make_window(path=folder)
        self.assertEqual([page.name for page in window.book.pages],
                         ["page_1.png", "page_2.png", "page_10.png"])

    def test_a_folder_with_only_subdirectories_still_works(self) -> None:
        """`list_folder_pages()` 顶层没图时往子目录里走一层。"""
        folder = self.tmp / "nested"
        inner = folder / "chapter1"
        inner.mkdir(parents=True)
        (inner / "page_1.png").write_bytes(make_png(40, 60))
        window = self.make_window(path=folder)
        self.assertEqual(window.book.position_count, 1)


class TestComicWindowStaysQtFree(unittest.TestCase):
    """核心层不许碰 Qt——阶段 1 立下的规矩，阶段 2 新增的模块一并纳入。"""

    BANNED = ("PyQt5", "PySide6", "minireader.qt", "QtCore", "QtWidgets")

    def test_the_stage_2_core_modules_do_not_import_qt(self) -> None:
        from minireader.core import archives, comic, formats, images, opener

        for module in (archives, comic, formats, images, opener):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for banned in self.BANNED:
                self.assertNotIn(
                    banned, source, f"{module.__name__} 不许出现 {banned}"
                )

    def test_the_comic_cache_is_allowed_to_use_qt(self) -> None:
        """它是 UI 层：解码、缩放、画页面本来就得用 Qt。"""
        from minireader.ui import comic_cache

        source = Path(comic_cache.__file__).read_text(encoding="utf-8")
        self.assertIn("minireader.qt", source.replace("..qt", "minireader.qt"))


if __name__ == "__main__":
    unittest.main()
