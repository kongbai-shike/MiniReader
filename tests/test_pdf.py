"""阶段 3 专项：PDF 判型 / 双路径 / 手工覆盖 / ask-once。

这个文件把阶段 3 的规格逐条钉死。三条设计线索决定了断言的写法：

1. **判型看内容，不看扩展名**。`.pdf` 在格式表里的 `kind` 是「默认猜测（漫画）」，
   但真正开出来是文本还是漫画由 `pdfdoc.classify()` 测量决定。所以
   `kind_for("a.pdf") == KIND_COMIC` 和「文字型 PDF 开成 KIND_TEXT」可以同时为真，
   它们回答的是两个不同的问题。

2. **夹具是运行时造的，不进仓库**。pypdfium2 自己就能造 PDF：`PdfDocument.new()`
   + `new_page()` + `FPDFPageObj_NewTextObj`（文字型）/ `PdfImage`（扫描型）。
   往仓库塞二进制 PDF 会让「这个夹具到底是什么」变成不可读的谜，而且判型逻辑
   一旦调整，二进制的期望值就没法跟着改。三个造夹具的函数见 `make_text_pdf()` /
   `make_scan_pdf()` / `make_blank_pdf()`。

3. **`FPDFText_SetText` 要的是 UTF-16LE 宽字符，不是 str 也不是 bytes。**
   ctypes 的 `argtypes` 是 `LP_c_ushort`，直接传 `str` 抛
   `ArgumentError: expected LP_c_ushort instance instead of str`，传 `bytes`
   抛 `expected LP_c_ushort instance instead of bytes`。必须
   `ctypes.create_string_buffer(text.encode("utf-16-le") + b"\\x00\\x00")`
   再 `cast` 成 `POINTER(c_ushort)`。这是本文件唯一一处 ctypes 手艺，注释在
   `_set_text()` 里。

Qt 无关：判型、取字、渲染全是 `minireader.core.pdfdoc` 的事，这里不 import Qt。
需要 Qt 的那几条（ask-once 弹窗、右键覆盖）在 `PdfUiTestCase` 里，单独一个类。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 必须先设，再 import 任何 Qt 模块：离屏平台让 UI 测试在没有显示器的机器上也能跑。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import pdfdoc  # noqa: E402
from minireader.core.book_base import KIND_COMIC, KIND_TEXT  # noqa: E402
from minireader.core.comic import ComicBook  # noqa: E402
from minireader.core.formats import DUAL_IDENTITY_EXTS, kind_for  # noqa: E402
from minireader.core.model import Book, BookError, book_id  # noqa: E402
from minireader.core.opener import (  # noqa: E402
    PDF_AUTO,
    PDF_COMIC,
    PDF_TEXT,
    open_book,
    open_book_kind,
    pdf_choice,
)
from minireader.qt import QApplication, QEvent, install_message_filter  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui import window as window_module  # noqa: E402
from minireader.ui.comic_reader import ComicReader  # noqa: E402
from minireader.ui.reader import ReaderView  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

from .test_ui_offscreen import _dispose_window  # noqa: E402

_app = QApplication.instance() or QApplication([])

#: Qt 在离屏平台上会抱怨字体库和 raise()，两者都与被测行为无关。
_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

#: pypdfium2 是可选的（`pip install pypdfium2`）。缺了它整个文件的断言都无从谈起，
#: 所以 skip 而不是 fail——用户可能只读 TXT/CBZ，那种安装是完全合理的。
HAVE_PDFIUM = pdfdoc.is_available()
SKIP_REASON = "没有安装 pypdfium2，PDF 相关的断言无法运行"


def _pdfium():
    import pypdfium2

    return pypdfium2


def _raw():
    import pypdfium2.raw

    return pypdfium2.raw


def _set_text(obj, text: str) -> None:
    """Put `text` into a pdfium text object.

    pdfium's C API takes a UTF-16LE wide string. ctypes will not accept a `str`
    (`ArgumentError: expected LP_c_ushort instance instead of str`) nor `bytes`
    (same message, "instead of bytes"), so the buffer is built by hand and cast.
    The trailing double NUL is the terminator pdfium expects; without it the
    string is read past its end.
    """
    raw = _raw()
    buf = ctypes.create_string_buffer(text.encode("utf-16-le") + b"\x00\x00")
    raw.FPDFText_SetText(obj, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)))


def _add_text_page(doc, page, text: str, *, size: float = 12.0, x: float = 40.0, y: float = 700.0) -> None:
    """Draw one run of real, extractable text onto `page`."""
    raw = _raw()
    obj = raw.FPDFPageObj_NewTextObj(doc.raw, b"Helvetica", float(size))
    _set_text(obj, text)
    raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, float(x), float(y))
    raw.FPDFPage_InsertObject(page.raw, obj)
    raw.FPDFPage_GenerateContent(page.raw)


#: Enough text on a Letter page to clear `TEXT_DENSITY` (measured: ~5570 against a
#: threshold of 200, so the margin is wide and the fixture is not delicate).
TEXT_BODY = "The quick brown fox jumps over the lazy dog. " * 60


def make_text_pdf(path: Path, pages: int = 3) -> Path:
    """A PDF with real text on every page: `classify()` says text, no asking."""
    pdfium = _pdfium()
    doc = pdfium.PdfDocument.new()
    try:
        for _ in range(pages):
            page = doc.new_page(612, 792)
            try:
                _add_text_page(doc, page, TEXT_BODY)
            finally:
                page.close()
        doc.save(str(path))
    finally:
        doc.close()
    return path


def make_scan_pdf(path: Path, pages: int = 3) -> Path:
    """A PDF that is nothing but full-page pictures: `classify()` says comic.

    Built with `PdfImage` + a native `PdfBitmap`, because that is what produces a
    real image XObject - which is what `_page_stats()` walks when it measures
    coverage. A filled vector rectangle would cover the page too but is not an
    image object, and the classifier would (correctly) call it ambiguous.
    """
    pdfium = _pdfium()
    raw = _raw()
    width, height = 612, 792
    stride = width * 4
    # One flat colour: the classifier only looks at the image's bounds, so the
    # pixels themselves are irrelevant. Alpha 255 keeps it an opaque page.
    pixels = bytes((60, 120, 200, 255)) * (width * height)
    buf = ctypes.create_string_buffer(pixels)
    bitmap = pdfium.PdfBitmap.new_native(width, height, raw.FPDFBitmap_BGRA, False, buf, stride)
    try:
        doc = pdfium.PdfDocument.new()
        try:
            for _ in range(pages):
                page = doc.new_page(width, height)
                try:
                    image = pdfium.PdfImage.new(doc)
                    image.set_bitmap(bitmap)
                    image.set_matrix(pdfium.PdfMatrix().scale(width, height))
                    page.insert_obj(image)
                    raw.FPDFPage_GenerateContent(page.raw)
                finally:
                    page.close()
            doc.save(str(path))
        finally:
            doc.close()
    finally:
        bitmap.close()
    return path


def make_blank_pdf(path: Path, pages: int = 2) -> Path:
    """Pages with no content at all: no text, no images.

    The one shape that makes `classify()` say `needs_ask` while also being
    impossible to open as text - `Book.from_pdf()` refuses a document with no
    extractable text. That combination is what makes it the right fixture for
    "an answer must not be remembered when the open fails".
    """
    pdfium = _pdfium()
    doc = pdfium.PdfDocument.new()
    try:
        for _ in range(pages):
            page = doc.new_page(612, 792)
            page.close()
        doc.save(str(path))
    finally:
        doc.close()
    return path


def make_sparse_pdf(path: Path, pages: int = 2) -> Path:
    """A little text, no pictures: `needs_ask` is True, but text opens fine.

    Two short lines on a Letter page measure around density 20 - an order of
    magnitude under `TEXT_DENSITY` (200) - and there are no image objects, so
    coverage is 0. Ambiguous by measurement, yet `extract_text()` still returns
    real characters, which makes this the fixture for "asked, answered, opened,
    remembered".
    """
    pdfium = _pdfium()
    doc = pdfium.PdfDocument.new()
    try:
        for _ in range(pages):
            page = doc.new_page(612, 792)
            try:
                _add_text_page(doc, page, "Short line one.\nShort line two.")
            finally:
                page.close()
        doc.save(str(path))
    finally:
        doc.close()
    return path


def make_encrypted_pdf(path: Path) -> Path:
    """A password-protected PDF, so `_load()`'s error mapping can be asserted.

    Written by hand, because pypdfium2 cannot *add* security - the only save-time
    flag pdfium has is `FPDF_REMOVE_SECURITY`. So the trailer declares standard
    security (`/Encrypt`) directly and the O/U strings are placeholders: pdfium
    derives the key from the empty user password, compares it against U, fails,
    and reports "Incorrect password error". That refusal is this fixture's whole
    purpose - the app maps it to 「这个 PDF 有密码保护，打不开」and never tries to
    decrypt, which is the DRM constraint from the plan.
    """
    content = b"BT /F1 12 Tf 40 700 Td (secret) Tj ET"
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        # R=2/V=1 is 40-bit RC4, the oldest and simplest handler pdfium supports.
        6: b"<< /Filter /Standard /V 1 /R 2 /Length 40 "
           b"/O <" + b"ab" * 32 + b"> /U <" + b"cd" * 32 + b"> /P -1 >>",
    }

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref = len(out)
    count = max(objects) + 1
    out += f"xref\n0 {count}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, count):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R /Encrypt 6 0 R "
        f"/ID [<{'11' * 16}> <{'11' * 16}>] >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return path


def _bmp_size(data: bytes) -> tuple[int, int]:
    """Width and height out of a BMP header.

    Read straight from the 54-byte header rather than through Qt: the assertion
    is about what the *core layer* produced, so decoding it would be testing the
    decoder instead. Height is signed in a BMP (negative means top-down rows), so
    it is taken as an absolute value.
    """
    import struct

    width, height = struct.unpack_from("<ii", data, 18)
    return int(width), abs(int(height))


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfClassifyTestCase(unittest.TestCase):
    """`pdfdoc.classify()` - the measurement that decides which reader."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-"))

    def test_a_text_pdf_measures_as_text(self) -> None:
        info = pdfdoc.classify(make_text_pdf(self.tmp / "novel.pdf"))
        self.assertEqual(info.pages, 3)
        self.assertEqual(info.kind, KIND_TEXT)
        self.assertFalse(info.needs_ask, "文字量远超阈值，不该问用户")
        self.assertGreater(info.density, pdfdoc.TEXT_DENSITY)

    def test_a_scanned_pdf_measures_as_a_comic(self) -> None:
        info = pdfdoc.classify(make_scan_pdf(self.tmp / "scan.pdf"))
        self.assertEqual(info.pages, 3)
        self.assertEqual(info.kind, KIND_COMIC)
        self.assertFalse(info.needs_ask, "整页都是图，证据充分，不该问用户")
        self.assertGreaterEqual(info.coverage, pdfdoc.SCAN_COVERAGE)
        self.assertLess(info.density, pdfdoc.TEXT_DENSITY)

    def test_a_blank_pdf_is_ambiguous_and_says_so(self) -> None:
        """没文字也没图：测不出来就得说测不出来，而不是悄悄按文本开。"""
        info = pdfdoc.classify(make_blank_pdf(self.tmp / "blank.pdf"))
        self.assertEqual(info.kind, KIND_TEXT, "拿不准时默认文本，但要用 needs_ask 标出来")
        self.assertTrue(info.needs_ask)
        self.assertEqual(info.density, 0.0)
        self.assertEqual(info.coverage, 0.0)

    def test_a_sparse_pdf_is_ambiguous_too(self) -> None:
        """有一点字、没有图：密度低于阈值、覆盖率为 0，同样落在灰区。"""
        info = pdfdoc.classify(make_sparse_pdf(self.tmp / "sparse.pdf"))
        self.assertTrue(info.needs_ask)
        self.assertLess(info.density, pdfdoc.TEXT_DENSITY)
        self.assertLess(info.coverage, pdfdoc.SCAN_COVERAGE)

    def test_a_missing_file_says_so_in_chinese(self) -> None:
        with self.assertRaises(BookError) as caught:
            pdfdoc.classify(self.tmp / "nope.pdf")
        self.assertIn("找不到这个文件", str(caught.exception))

    def test_a_file_that_is_not_a_pdf_says_so(self) -> None:
        broken = self.tmp / "broken.pdf"
        broken.write_bytes(b"this is not a pdf at all, not even close")
        with self.assertRaises(BookError) as caught:
            pdfdoc.classify(broken)
        self.assertIn("不是有效的 PDF", str(caught.exception))

    def test_an_empty_file_is_reported_as_not_a_pdf(self) -> None:
        """0 字节文件 pdfium 也拒绝，但它不是「0 页」——没有文档可谈页数。"""
        empty = self.tmp / "empty.pdf"
        empty.write_bytes(b"")
        with self.assertRaises(BookError):
            pdfdoc.classify(empty)

    def test_a_zero_page_pdf_is_reported_as_zero_pages(self) -> None:
        """`PdfDocument.new()` 存盘就是 0 页文档。

        pdfium 拒绝加载它，而且用的是让人费解的 "Failed to load document
        (PDFium: Success)"——`_load()` 把它翻译成「这个 PDF 里没有页面」。
        """
        pdfium = _pdfium()
        zero = self.tmp / "zero.pdf"
        doc = pdfium.PdfDocument.new()
        try:
            doc.save(str(zero))
        finally:
            doc.close()
        with self.assertRaises(BookError) as caught:
            pdfdoc.classify(zero)
        message = str(caught.exception)
        self.assertIn("没有页面", message)
        self.assertNotIn("Success", message, "pdfium 的原文不能漏给用户")

    def test_an_encrypted_pdf_is_reported_as_password_protected(self) -> None:
        """加密 PDF 只提示，不解密——这是硬约束。"""
        secret = make_encrypted_pdf(self.tmp / "secret.pdf")
        with self.assertRaises(BookError) as caught:
            pdfdoc.classify(secret)
        self.assertIn("密码", str(caught.exception))

    def test_sampling_spreads_across_a_long_document(self) -> None:
        """长文档只抽样 5 页，且末页一定在样本里。"""
        indices = pdfdoc._sample_indices(100)
        self.assertEqual(len(indices), pdfdoc.SAMPLE_LIMIT)
        self.assertEqual(indices[-1], 99, "最后一页常常是唯一不同的一页（索引、版权页）")
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(len(set(indices)), len(indices), "抽样的页码不能重复")

    def test_short_documents_are_measured_whole(self) -> None:
        self.assertEqual(pdfdoc._sample_indices(3), [0, 1, 2])
        self.assertEqual(pdfdoc._sample_indices(5), [0, 1, 2, 3, 4])


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfTextPathTestCase(unittest.TestCase):
    """文字型 PDF 走 `Book`（TextReader）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-text-"))

    def test_a_text_pdf_opens_as_a_text_book(self) -> None:
        book = open_book(make_text_pdf(self.tmp / "novel.pdf"))
        self.assertIsInstance(book, Book)
        self.assertEqual(book.kind, KIND_TEXT)
        self.assertEqual(book.chapter_count, 3, "一页一章")

    def test_each_page_becomes_its_own_segment(self) -> None:
        """PDF 的页边界是真的边界，不能靠 TXT 的分章正则去猜。"""
        book = open_book(make_text_pdf(self.tmp / "novel.pdf"))
        titles = [book.chapter(i).title for i in range(book.chapter_count)]
        self.assertEqual(titles, ["第 1 页", "第 2 页", "第 3 页"])

    def test_the_extracted_text_is_actually_readable(self) -> None:
        book = open_book(make_text_pdf(self.tmp / "novel.pdf"))
        body = book.chapter_body(0)
        self.assertIn("quick brown fox", body)
        self.assertGreater(len(body), 1000)

    def test_pdfium_line_endings_are_cleaned_up(self) -> None:
        """pdfium 用 CRLF 分行、块尾多一个 CR——直接显示会在每段留下空行。"""
        segments = pdfdoc.extract_text(make_text_pdf(self.tmp / "novel.pdf"))
        for segment in segments:
            self.assertNotIn("\r", segment.text)
            self.assertFalse(segment.text.endswith("\n"))
            self.assertNotIn("\n\n\n", segment.text)

    def test_a_scan_opened_as_text_still_gives_a_book(self) -> None:
        """判错也要能读：扫描件按文本打开时，`from_segments()` 跳过无字的页。

        这是「拿不准时默认文本」这条默认值之所以安全的理由——不会开出一本空书，
        只会开出一本每页都是图、正文为空的怪书，而右键能改回来。
        """
        with self.assertRaises(BookError) as caught:
            open_book(make_scan_pdf(self.tmp / "scan.pdf"), pdf_kind=PDF_TEXT)
        self.assertIn("没有可提取的文字", str(caught.exception))


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfComicPathTestCase(unittest.TestCase):
    """扫描型 PDF 走 `ComicBook`（ComicReader）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-comic-"))

    def test_a_scanned_pdf_opens_as_a_comic_book(self) -> None:
        book = open_book(make_scan_pdf(self.tmp / "scan.pdf"))
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.kind, KIND_COMIC)
        self.assertEqual(book.position_count, 3)
        self.assertEqual(book.position_label, "页")

    def test_page_sizes_are_known_before_anything_is_drawn(self) -> None:
        """尺寸来自文档自己的页框，不是渲染出来的——打开时不解码第一页。"""
        book = open_book(make_scan_pdf(self.tmp / "scan.pdf"))
        self.assertTrue(book.sizes_known())
        self.assertEqual((book.pages[0].width, book.pages[0].height), (612, 792))

    def test_a_page_renders_to_bytes_qt_can_decode(self) -> None:
        """核心层只交字节，不交 QPixmap——BMP 是 Qt 免插件能解的格式。"""
        book = open_book(make_scan_pdf(self.tmp / "scan.pdf"))
        data = book.read_page(0)
        self.assertTrue(data.startswith(b"BM"), "渲染结果必须是 BMP")
        self.assertGreater(len(data), 1000)

    def test_the_rendered_page_is_the_expected_shape(self) -> None:
        """`RENDER_SCALE` 倍的页框，32 位色。"""
        raw = pdfdoc.render_page_bmp(make_scan_pdf(self.tmp / "scan.pdf"), 0)
        width, height = _bmp_size(raw)
        self.assertEqual(width, int(612 * pdfdoc.RENDER_SCALE))
        self.assertEqual(height, int(792 * pdfdoc.RENDER_SCALE))

    def test_a_page_out_of_range_is_refused(self) -> None:
        book = open_book(make_scan_pdf(self.tmp / "scan.pdf"))
        from minireader.core.archives import ArchiveError

        with self.assertRaises(ArchiveError):
            book.read_page(99)

    def test_a_pdf_comic_has_no_text_shaped_surprises(self) -> None:
        """漫画书要能回答文本形状的问题，否则 window 的共享路径会崩。"""
        book = open_book(make_scan_pdf(self.tmp / "scan.pdf"))
        self.assertEqual(book.chapter_count, 3)
        self.assertIsNone(book.chapter(0))
        self.assertEqual(book.chapter_body(0), "")
        self.assertEqual(book.text, "")
        self.assertEqual(book.split_summary(), "共 3 页")


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfChoiceTestCase(unittest.TestCase):
    """`pdf_choice()` - 谁的意见优先，以及什么时候该问。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-choice-"))

    def test_auto_measures_the_document(self) -> None:
        text = pdf_choice(make_text_pdf(self.tmp / "novel.pdf"), PDF_AUTO)
        self.assertEqual(text.kind, KIND_TEXT)
        self.assertFalse(text.needs_ask)
        scan = pdf_choice(make_scan_pdf(self.tmp / "scan.pdf"), PDF_AUTO)
        self.assertEqual(scan.kind, KIND_COMIC)
        self.assertFalse(scan.needs_ask)

    def test_an_explicit_preference_wins_over_the_measurement(self) -> None:
        """用户说的算：一本扫描件被指定按文本打开，就不去测量了。"""
        novel = make_text_pdf(self.tmp / "novel.pdf")
        self.assertEqual(pdf_choice(novel, PDF_COMIC).kind, KIND_COMIC)
        scan = make_scan_pdf(self.tmp / "scan.pdf")
        self.assertEqual(pdf_choice(scan, PDF_TEXT).kind, KIND_TEXT)

    def test_an_explicit_preference_never_asks(self) -> None:
        blank = make_blank_pdf(self.tmp / "blank.pdf")
        for prefer in (PDF_TEXT, PDF_COMIC):
            self.assertFalse(pdf_choice(blank, prefer).needs_ask)

    def test_ambiguity_is_reported_rather_than_decided(self) -> None:
        choice = pdf_choice(make_sparse_pdf(self.tmp / "sparse.pdf"), PDF_AUTO)
        self.assertTrue(choice.needs_ask)
        self.assertEqual(choice.kind, KIND_TEXT, "灰区默认文本，但要标记出来让窗口去问")

    def test_no_preference_at_all_measures_too(self) -> None:
        self.assertEqual(pdf_choice(make_text_pdf(self.tmp / "novel.pdf")).kind, KIND_TEXT)

    def test_an_unknown_preference_falls_back_to_measuring(self) -> None:
        """settings.json 是用户可以手改的：写个乱七八糟的值不能让打开崩掉。"""
        choice = pdf_choice(make_text_pdf(self.tmp / "novel.pdf"), "banana")
        self.assertEqual(choice.kind, KIND_TEXT)
        self.assertFalse(choice.needs_ask)


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfDispatchTestCase(unittest.TestCase):
    """`open_book()` 把两种身份分别送去哪里。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-dispatch-"))

    def test_the_content_decides_not_the_extension(self) -> None:
        """同一个 `.pdf` 扩展名，两种内容，两个不同的类。"""
        text_book = open_book(make_text_pdf(self.tmp / "novel.pdf"))
        comic_book = open_book(make_scan_pdf(self.tmp / "scan.pdf"))
        self.assertIsInstance(text_book, Book)
        self.assertIsInstance(comic_book, ComicBook)
        self.assertNotIsInstance(text_book, ComicBook)
        self.assertNotIsInstance(comic_book, Book)

    def test_the_callers_preference_is_honoured(self) -> None:
        scan = make_scan_pdf(self.tmp / "scan.pdf")
        self.assertIsInstance(open_book(scan, pdf_kind=PDF_COMIC), ComicBook)
        novel = make_text_pdf(self.tmp / "novel.pdf")
        self.assertIsInstance(open_book(novel, pdf_kind=PDF_TEXT), Book)

    def test_the_format_table_still_guesses_comic(self) -> None:
        """表里那一格是「默认猜测」，`open_book_kind()` 用它，不测量。

        拖放要对每个文件问一次 kind，测量 PDF 的文字密度不是那种时候该做的事。
        """
        self.assertEqual(kind_for("a.pdf"), KIND_COMIC)
        self.assertEqual(open_book_kind(self.tmp / "whatever.pdf"), KIND_COMIC)

    def test_pdf_is_a_dual_identity_extension(self) -> None:
        self.assertIn(".pdf", DUAL_IDENTITY_EXTS)

    def test_a_broken_pdf_reports_through_book_error(self) -> None:
        broken = self.tmp / "broken.pdf"
        broken.write_bytes(b"nope")
        with self.assertRaises(BookError):
            open_book(broken)


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfSettingsTestCase(unittest.TestCase):
    """`pdf_default_kind` 的三个值：auto / text / comic。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-settings-"))

    def test_the_default_is_auto(self) -> None:
        from minireader.storage import Settings

        self.assertEqual(Settings().pdf_default_kind, PDF_AUTO)

    def test_auto_measures(self) -> None:
        novel = make_text_pdf(self.tmp / "novel.pdf")
        scan = make_scan_pdf(self.tmp / "scan.pdf")
        self.assertEqual(pdf_choice(novel, PDF_AUTO).kind, KIND_TEXT)
        self.assertEqual(pdf_choice(scan, PDF_AUTO).kind, KIND_COMIC)

    def test_text_forces_the_text_reader(self) -> None:
        scan = make_scan_pdf(self.tmp / "scan.pdf")
        self.assertEqual(pdf_choice(scan, PDF_TEXT).kind, KIND_TEXT)

    def test_comic_forces_the_comic_reader(self) -> None:
        novel = make_text_pdf(self.tmp / "novel.pdf")
        self.assertEqual(pdf_choice(novel, PDF_COMIC).kind, KIND_COMIC)

    def test_the_setting_survives_a_round_trip(self) -> None:
        from minireader.storage import Settings

        path = self.tmp / "settings.json"
        ok, message = Settings(pdf_default_kind=PDF_COMIC).save(path)
        self.assertTrue(ok, message)
        self.assertEqual(Settings.load(path).pdf_default_kind, PDF_COMIC)

    def test_an_unknown_stored_value_is_clamped_to_auto(self) -> None:
        """手改 settings.json 写成别的值：clamp() 要把它收回 auto。"""
        from minireader.storage import Settings

        settings = Settings(pdf_default_kind="banana")
        settings.clamp()
        self.assertEqual(settings.pdf_default_kind, PDF_AUTO)

    def test_the_comic_settings_all_have_defaults(self) -> None:
        from minireader.storage import Settings

        settings = Settings()
        self.assertEqual(settings.comic_default_mode, "single")
        self.assertEqual(settings.comic_default_zoom, "width")
        self.assertEqual(settings.comic_cache_mb, 256)
        self.assertEqual(settings.comic_binding, "rtl")
        self.assertTrue(settings.comic_cover_alone)

    def test_the_comic_settings_are_clamped(self) -> None:
        from minireader.storage import Settings

        settings = Settings(
            comic_default_mode="banana",
            comic_default_zoom="banana",
            comic_cache_mb=99999,
            comic_binding="banana",
        )
        settings.clamp()
        self.assertEqual(settings.comic_default_mode, "single")
        self.assertEqual(settings.comic_default_zoom, "width")
        self.assertLessEqual(settings.comic_cache_mb, 1024)
        self.assertEqual(settings.comic_binding, "rtl")


@unittest.skipUnless(HAVE_PDFIUM, SKIP_REASON)
class PdfUiTestCase(unittest.TestCase):
    """ask-once 弹窗与右键手工覆盖——需要 QApplication 的那部分。

    两件事都在 window 层，因为它们都关乎「用户看到了什么」：

    * **ask-once**：`classify()` 说 `needs_ask` 时，`load_path()` 必须弹一次模态
      让用户选。不弹就是把模糊 PDF 静默按文本开，用户失去纠正机会——而这正是
      规格里「其余 → 默认文本型，弹一次询问」的后半句。
    * **手工覆盖**：右键「按文本打开」「按漫画打开」把答案写进 `Progress.pdf_kind`，
      下次开这本书直接用记忆值。

    `exec_dialog` 在模块级被替换（`window_module.exec_dialog`），这是既有测试
    （`tests/test_reading_features.py:646-651`）已经在用的手法：它只需要点一个按钮
    再返回 True/False，不需要真的跑一个模态事件循环。
    """

    def setUp(self) -> None:
        self._appdata = os.environ.get("APPDATA")
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-pdf-ui-"))
        os.environ["APPDATA"] = str(self.tmp)

        self.settings = Settings(wheel_animation_ms=0)
        self.progress = ProgressStore.load()
        self.library = Library.load()
        self.windows: list = []
        self._real_exec = window_module.exec_dialog

    def tearDown(self) -> None:
        window_module.exec_dialog = self._real_exec
        for window in self.windows:
            _dispose_window(window)
        _app.processEvents()
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        _app.processEvents()
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata

    def make_window(self):
        window = MiniWindow(self.settings, self.progress, self.library)
        self.windows.append(window)
        window._quit_on_close = False
        window.resize(480, 360)
        window.show()
        _app.processEvents()
        return window

    def stub_dialog(self, action: str) -> dict:
        """Replace `exec_dialog`: record the call, click `action`, return accepted.

        `action` is "text" / "comic" / "cancel". The recorded dict is returned so
        the test can assert the dialog was reached and inspect its title.
        """
        seen: dict = {"calls": 0, "dialog": None}

        def fake_exec(dialog):
            seen["calls"] += 1
            seen["dialog"] = dialog
            if action == "text":
                dialog.text_button.click()
                return True
            if action == "comic":
                dialog.comic_button.click()
                return True
            dialog.cancel_button.click()
            return False

        window_module.exec_dialog = fake_exec
        return seen

    # --- ask-once ---------------------------------------------------------

    def test_an_ambiguous_pdf_asks_once(self) -> None:
        window = self.make_window()
        seen = self.stub_dialog("text")
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(seen["calls"], 1, "灰区必须弹一次")
        self.assertEqual(seen["dialog"].windowTitle(), "这本书按哪种方式打开")

    def test_choosing_text_writes_pdf_text(self) -> None:
        window = self.make_window()
        self.stub_dialog("text")
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(window.book.kind, KIND_TEXT)
        self.assertIsInstance(window.reader, ReaderView)
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, PDF_TEXT)

    def test_choosing_comic_writes_pdf_comic(self) -> None:
        window = self.make_window()
        self.stub_dialog("comic")
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIsInstance(window.reader, ComicReader)
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, PDF_COMIC)

    def test_cancelling_opens_as_text_and_writes_nothing(self) -> None:
        """取消 = 「没意见」，不是「按文本」——所以什么都不写，下次还会问。"""
        window = self.make_window()
        self.stub_dialog("cancel")
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(window.book.kind, KIND_TEXT, "取消后按默认的文本型打开")
        self.assertEqual(
            self.progress.get(window.book.id).pdf_kind, "",
            "取消不许写记忆，否则用户再没有第二次被问的机会",
        )

    def test_a_remembered_answer_is_not_asked_about_again(self) -> None:
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        first = self.make_window()
        self.stub_dialog("text")
        self.assertTrue(first.load_path(str(sparse)))
        _app.processEvents()

        window_module.exec_dialog = self._real_exec
        second = self.make_window()
        seen = self.stub_dialog("comic")
        self.assertTrue(second.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(seen["calls"], 0, "已经答过了就不该再问")
        self.assertEqual(second.book.kind, KIND_TEXT, "记忆的是文本")

    def test_an_unambiguous_pdf_is_never_asked_about(self) -> None:
        """有把握的 PDF 一律静默打开——问一次是例外，不是流程。"""
        window = self.make_window()
        seen = self.stub_dialog("comic")
        novel = make_text_pdf(self.tmp / "novel.pdf")
        self.assertTrue(window.load_path(str(novel)))
        _app.processEvents()
        self.assertEqual(seen["calls"], 0)
        self.assertEqual(window.book.kind, KIND_TEXT)

    def test_a_failed_open_does_not_poison_the_memory(self) -> None:
        """最重要的一条：答案只在**真的开出来之后**才写。

        一份没有可提取文字的 PDF（纯空白）选「按文本」会打不开。如果答案先写了，
        这个文件就永远打不开了——屏幕上没有书，也就没有右键菜单能改回来。
        """
        blank = make_blank_pdf(self.tmp / "blank.pdf")
        window = self.make_window()
        self.stub_dialog("text")
        self.assertFalse(window.load_path(str(blank)), "空白 PDF 按文本打不开")
        _app.processEvents()
        self.assertEqual(
            self.progress.get(book_id(blank)).pdf_kind, "",
            "打开失败就不能记下答案，否则这个文件再也没有第二次机会",
        )

        # 再开一次：仍然会问，而且这次选漫画就能开出来。
        window_module.exec_dialog = self._real_exec
        second = self.make_window()
        seen = self.stub_dialog("comic")
        self.assertTrue(second.load_path(str(blank)))
        _app.processEvents()
        self.assertEqual(seen["calls"], 1, "上次没记下答案，所以这次还会问")
        self.assertEqual(second.book.kind, KIND_COMIC)
        self.assertEqual(self.progress.get(book_id(blank)).pdf_kind, PDF_COMIC)

    # --- 手工覆盖 ---------------------------------------------------------

    def test_the_menu_offers_both_readers_for_a_pdf(self) -> None:
        window = self.make_window()
        self.stub_dialog("text")
        self.assertTrue(window.load_path(str(make_sparse_pdf(self.tmp / "sparse.pdf"))))
        _app.processEvents()
        # 可见性由 `aboutToShow` 刷新（见 `_sync_pdf_actions`），不调它就还是在构造
        # 时设的初始 False 上——既有测试也是这么触发的
        # （tests/test_stage3_context_menu.py:80）。
        window._on_reader_menu_about_to_show()
        self.assertTrue(window.pdf_text_action.isVisible())
        self.assertTrue(window.pdf_comic_action.isVisible())
        self.assertTrue(window.pdf_text_action.isChecked(), "当前是按文本开的")

    def test_the_menu_hides_those_items_for_a_non_pdf(self) -> None:
        novel = self.tmp / "novel.txt"
        novel.write_text("第一章 起\n正文。\n", encoding="utf-8")
        window = self.make_window()
        self.assertTrue(window.load_path(str(novel)))
        _app.processEvents()
        window._on_reader_menu_about_to_show()
        self.assertFalse(window.pdf_text_action.isVisible())
        self.assertFalse(window.pdf_comic_action.isVisible())
        self.assertFalse(window.pdf_separator.isVisible())

    def test_overriding_switches_the_reader_and_is_remembered(self) -> None:
        """右键「按漫画打开」：立刻换 reader，并且下次开这本书直接用漫画。"""
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        window = self.make_window()
        self.stub_dialog("text")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()
        self.assertIsInstance(window.reader, ReaderView)

        window.remember_pdf_kind(PDF_COMIC)
        _app.processEvents()
        self.assertEqual(window.book.kind, KIND_COMIC)
        self.assertIsInstance(window.reader, ComicReader)
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, PDF_COMIC)

        # 重开：不再问，直接用漫画。
        window_module.exec_dialog = self._real_exec
        second = self.make_window()
        seen = self.stub_dialog("text")
        self.assertTrue(second.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(seen["calls"], 0, "手工覆盖过的书不该再被问")
        self.assertEqual(second.book.kind, KIND_COMIC)

    def test_overriding_back_to_text_works_too(self) -> None:
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        window = self.make_window()
        self.stub_dialog("comic")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()
        self.assertIsInstance(window.reader, ComicReader)

        window.remember_pdf_kind(PDF_TEXT)
        _app.processEvents()
        self.assertIsInstance(window.reader, ReaderView)
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, PDF_TEXT)

    def test_overriding_a_non_pdf_does_nothing(self) -> None:
        """菜单项只在 PDF 上可见，但代码路径也要自己守住。"""
        novel = self.tmp / "novel.txt"
        novel.write_text("第一章 起\n正文。\n", encoding="utf-8")
        window = self.make_window()
        self.assertTrue(window.load_path(str(novel)))
        _app.processEvents()
        before = window.book.kind
        window.remember_pdf_kind(PDF_COMIC)
        _app.processEvents()
        self.assertEqual(window.book.kind, before)
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, "")

    def test_an_unknown_kind_is_ignored(self) -> None:
        window = self.make_window()
        self.stub_dialog("text")
        self.assertTrue(window.load_path(str(make_sparse_pdf(self.tmp / "sparse.pdf"))))
        _app.processEvents()
        window.remember_pdf_kind("banana")
        _app.processEvents()
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, PDF_TEXT)

    def test_the_pdf_default_kind_setting_skips_the_question(self) -> None:
        """设置里选了「一律按漫画」，灰区 PDF 也不再问。"""
        self.settings.pdf_default_kind = PDF_COMIC
        window = self.make_window()
        seen = self.stub_dialog("text")
        self.assertTrue(window.load_path(str(make_sparse_pdf(self.tmp / "sparse.pdf"))))
        _app.processEvents()
        self.assertEqual(seen["calls"], 0, "设置已经表态了，不用再问")
        self.assertEqual(window.book.kind, KIND_COMIC)

    def test_the_per_book_answer_beats_the_setting(self) -> None:
        """这本书自己记过的答案，优先于设置里的全局默认。"""
        sparse = make_sparse_pdf(self.tmp / "sparse.pdf")
        window = self.make_window()
        self.stub_dialog("text")
        self.assertTrue(window.load_path(str(sparse)))
        _app.processEvents()

        self.settings.pdf_default_kind = PDF_COMIC
        window_module.exec_dialog = self._real_exec
        second = self.make_window()
        self.assertTrue(second.load_path(str(sparse)))
        _app.processEvents()
        self.assertEqual(second.book.kind, KIND_TEXT, "本书的记忆优先于全局默认")

    # --- 手工覆盖不能把文件锁死 -------------------------------------------

    def test_an_override_that_cannot_open_does_not_get_written(self) -> None:
        """选「按文本」但这份 PDF 没有文字时，答案不能落盘。

        这是探针抓到的真缺陷：`remember_pdf_kind()` 原来先写 Progress 再重开。
        扫描件没有可提取文字，按文本开必然失败，于是磁盘上留下一个指向
        "text" 的答案。当时屏幕上还是那本漫画，用户右键还能改；但**关掉程序
        重开**时 `_pdf_preference()` 会读回 "text"，`Book.from_pdf()` 再抛一次，
        屏幕上就没有书了——而右键菜单的 PDF 项只在有书且是 PDF 时才可见，
        于是这个文件被永久锁死，没有任何入口能改回来。

        修法：`remember_pdf_kind()` 改走 `load_path(pdf_kind=...)`，而 `load_path()`
        只在书真的到手之后才写（这条纪律 ask-once 那条路一直是遵守的）。
        """
        scan = make_scan_pdf(self.tmp / "scan.pdf")
        window = self.make_window()
        self.stub_dialog("comic")
        self.assertTrue(window.load_path(str(scan)))
        _app.processEvents()
        self.assertIsInstance(window.reader, ComicReader)
        self.assertEqual(self.progress.get(window.book.id).pdf_kind, "")

        window.remember_pdf_kind(PDF_TEXT)
        _app.processEvents()
        self.assertEqual(
            self.progress.get(window.book.id).pdf_kind, "",
            "开不出来就不能记下答案，否则这个文件再也没有第二次机会",
        )
        self.assertIsInstance(window.reader, ComicReader, "屏幕上还是能看的那本")
        self.assertIn("已保持原来的打开方式", window.warn_label.text())

    def test_that_poisoned_file_still_opens_next_launch(self) -> None:
        """上一条的后果验证：换一个窗口（= 下次启动）打开，必须还能开。"""
        scan = make_scan_pdf(self.tmp / "scan.pdf")
        window = self.make_window()
        self.stub_dialog("comic")
        self.assertTrue(window.load_path(str(scan)))
        _app.processEvents()
        window.remember_pdf_kind(PDF_TEXT)
        _app.processEvents()

        # 模拟下次启动：新的窗口、新的 store，只共享磁盘上的 progress.json。
        self.progress.save()
        window_module.exec_dialog = self._real_exec
        fresh = ProgressStore.load()
        second = MiniWindow(self.settings, fresh, Library.load())
        self.windows.append(second)
        second._quit_on_close = False
        second.resize(480, 360)
        second.show()
        _app.processEvents()
        self.assertTrue(second.load_path(str(scan)), "文件不该因为一次失败的覆盖而打不开")
        _app.processEvents()
        self.assertIsNotNone(second.book)
        self.assertIsInstance(second.reader, ComicReader)

    # --- 页尺寸 ------------------------------------------------------------

    def test_a_pdf_reports_its_page_box_not_the_rendered_pixels(self) -> None:
        """PDF 的页尺寸来自文档本身，不该被渲染出来的像素数覆盖。

        探针抓到的第二个真缺陷：`ComicReader._render()` 原来无条件把解码出的
        pixmap 尺寸写回 `ComicPage`。对 CBZ 这是对的（不解码就无从得知），但对
        PDF 是错的——页框本来就知道（`open_pdf()` 用文档自己的页框填好了），
        而 pixmap 是页框乘 `RENDER_SCALE`（2.0）。于是**同一份文档的第 1 页**
        （被访问过）报 1224×1584，第 2、3 页（没被访问）报 612×792。

        修法：只在 `not page.size_known` 时才写回。
        """
        scan = make_scan_pdf(self.tmp / "scan.pdf", pages=3)
        window = self.make_window()
        self.stub_dialog("comic")
        self.assertTrue(window.load_path(str(scan)))
        _app.processEvents()

        box = pdfdoc.page_sizes(scan)
        self.assertEqual(box, [(612, 792)] * 3, "夹具是 Letter 页")

        sizes = []
        for index in range(3):
            window.chapter_index = index
            window.show_chapter()
            _app.processEvents()
            page = window.book.pages[index]
            sizes.append((page.width, page.height))

        self.assertEqual(
            sizes, box,
            "三页报的尺寸必须一致，而且等于文档的页框——不是 RENDER_SCALE 倍的像素数",
        )
        self.assertNotEqual(sizes[0][0], int(612 * pdfdoc.RENDER_SCALE), "别把渲染尺寸当成页尺寸")

    def test_a_cbz_still_learns_its_sizes_by_decoding(self) -> None:
        """对照：CBZ 没有页框可查，所以第一次显示时仍然要记下尺寸。"""
        import struct
        import zipfile
        import zlib

        def png(width: int, height: int) -> bytes:
            def chunk(tag: bytes, data: bytes) -> bytes:
                return (struct.pack(">I", len(data)) + tag + data
                        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

            raw = b"".join(b"\x00" + bytes((200, 30, 30)) * width for _ in range(height))
            return (b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 6))
                    + chunk(b"IEND", b""))

        cbz = self.tmp / "sizes.cbz"
        with zipfile.ZipFile(cbz, "w") as archive:
            archive.writestr("01.png", png(40, 60))
            archive.writestr("02.png", png(60, 40))

        window = self.make_window()
        self.assertTrue(window.load_path(str(cbz)))
        _app.processEvents()
        self.assertEqual(
            [(page.width, page.height) for page in window.book.pages], [(40, 60), (0, 0)],
            "第一页是打开时渲染的，第二页还没碰过",
        )

        window.chapter_index = 1
        window.show_chapter()
        _app.processEvents()
        self.assertEqual(
            [(page.width, page.height) for page in window.book.pages], [(40, 60), (60, 40)],
            "翻到第二页之后尺寸就该知道了",
        )
        self.assertTrue(window.book.sizes_known())


if __name__ == "__main__":
    unittest.main()
