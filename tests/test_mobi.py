"""阶段 6 核心层：MOBI/AZW3 路径（`minireader.core.mobidoc`）。

这个文件守的是 Kindle 容器的**判定与文案**，以及「解不开」和「有 DRM」这两件事
必须被分开。它同样是 Qt-free 的。

三条设计要点决定了这里的测法：

1. **假 MOBI 是手搓的，不是下载的。** `build_mobi()` 用 `struct.pack` 拼出真的
   PalmDB 头 + 记录表 + MOBI 头 + 未压缩正文，一共几十行。于是绝大多数断言
   （嗅探、DRM、损坏、相位、派发）在**没装 `mobi` 库**的机器上照样能跑——
   而"没装 mobi"正是默认安装的样子，也是最该被测到的样子。真正需要 `mobi`
   的只有"正文可读"那几条，它们单独 skip（理由见 `HAVE_MOBI`）。

2. **DRM 的判定在 `mobi` 之前。** `open_mobi()` 先嗅探加密字段，再 import
   `mobi`。所以这里有一条专门的断言：**没装 mobi 时打开带 DRM 的书，得到的
   仍然是 DRM 文案**，而不是「请安装 mobi」。这不是巧合，是顺序的设计。

3. **文案是用户可见的**。每条 `BookError` 都断言了原文，因为它们会原样出现在
   底栏的警告位。`DRM_NOTE` 尤其重要：它必须说清「请自己用 Calibre 脱壳」，
   因为 MiniReader 里没有任何解密代码。

另外，"损坏"和"有 DRM"是两种不同的坏法，`_failure_note()` 专门区分它们，
所以 `TestCorruptIsNotDrm` 会同时喂进两种文件、断言两条不同的文案。
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import formats as formats_module  # noqa: E402
from minireader.core import mobidoc  # noqa: E402
from minireader.core.book_base import KIND_COMIC, KIND_TEXT  # noqa: E402
from minireader.core.formats import (  # noqa: E402
    CURRENT_PHASE,
    KINDLE_EXTS,
    Format,
    format_for,
    is_supported,
)
from minireader.core.model import BookError  # noqa: E402
from minireader.core.mobidoc import (  # noqa: E402
    CORRUPT_NOTE,
    DRM_NOTE,
    MOBI_NOTE,
    PRINT_REPLICA_NOTE,
    is_available,
    is_drm,
    looks_like_mobi,
    open_mobi,
    sniff,
)
from minireader.core.opener import open_book, open_book_kind  # noqa: E402

#: `mobi` 是可选的（GPL-3.0，`pip install mobi`）。缺了它，"正文可读"那几条无从
#: 谈起，所以 skip 而不是 fail——只读 TXT/EPUB/PDF 的安装是完全合理的，而且那正是
#: 默认安装。**注意 skip 的只有这一小撮**：判定类断言全部用手搓的假 MOBI，
#: 不需要这个库。
HAVE_MOBI = is_available()
MOBI_SKIP = "没有安装 mobi 库，解包相关的断言无法运行"


# ---------------------------------------------------------------------------
# 手搓一个最小但**结构正确**的 MOBI
# ---------------------------------------------------------------------------

#: PalmDB 固定头长度。
PALM_HEADER = 78

#: MOBI 头里正文数据的偏移（相对记录 0 开头）。
RECORD0_PREFIX = 16


def build_mobi(
    body: bytes,
    *,
    crypto: int = 0,
    title: str = "测试书",
    compression: int = 1,
    ident: bytes = b"BOOKMOBI",
    records: int = 1,
    header_len: int = 232,
) -> bytes:
    """拼一个真的能过 `mobi.extract()` 的未压缩 MOBI。

    结构按 PalmDB 的实际布局来（对照 `mobi/mobi_sectioner.py` 与
    `mobi/mobi_header.py` 读的偏移）：

        [0, 78)            PalmDB 头，0x3C 处是 "BOOKMOBI"，76 处是记录数
        [78, 78+8n)        记录表，每项 8 字节（4 字节偏移 + 4 字节属性），大端
        记录 0             2 字节填充 + MOBI 头（PalmDOC 字段在前，MOBI 头从 16 开始）
        记录 1             正文

    记录 0 的**偏移必须正好指向记录表之后**，因为 `MobiHeader` 把记录 0 当作
    "从记录起点开始"的缓冲区来解，而 `mobi` 又要求至少有两个记录
    （`MobiHeader.__init__` 会去读 `start + 1`）。这两点是最容易手搓错的地方。

    `crypto` 就是 DRM 字段（MOBI 头 0x0C，大端 uint16）；0 表示没加密。
    """
    header = bytearray(header_len)
    struct.pack_into(">H", header, 0x00, compression)
    struct.pack_into(">L", header, 0x04, len(body))  # text_length
    struct.pack_into(">H", header, 0x08, records)  # 正文记录数
    struct.pack_into(">H", header, 0x0A, 4096)  # max_section_size
    struct.pack_into(">H", header, 0x0C, crypto)  # <-- 加密字段
    header[0x10:0x14] = b"MOBI"
    struct.pack_into(">L", header, 0x14, 200)  # header_length
    struct.pack_into(">L", header, 0x18, 2)  # type: book
    struct.pack_into(">L", header, 0x1C, 65001)  # codepage: utf-8
    struct.pack_into(">L", header, 0x20, 1)  # unique id
    struct.pack_into(">L", header, 0x24, 6)  # version
    struct.pack_into(">L", header, 0x28, 0xFFFFFFFF)  # metaOrthIndex
    struct.pack_into(">L", header, 0x2C, 0xFFFFFFFF)  # metaInflIndex
    struct.pack_into(">L", header, 0x50, 0xFFFFFFFF)  # firstnontext
    raw_title = title.encode("utf-8")
    struct.pack_into(">L", header, 0x54, 136)  # title offset
    struct.pack_into(">L", header, 0x58, len(raw_title))  # title length
    struct.pack_into(">L", header, 0x68, 6)  # mobi version
    struct.pack_into(">L", header, 0x6C, 0xFFFFFFFF)  # firstresource
    header[136 : 136 + len(raw_title)] = raw_title

    # 记录表 8 字节 * 2 条，之后 2 字节填充，记录 0 从那里开始。
    rec0 = PALM_HEADER + 2 * 8 + 2
    rec1 = rec0 + len(header)

    palm = bytearray(PALM_HEADER)
    palm[0:32] = b"TestBook".ljust(32, b"\x00")
    palm[0x3C:0x44] = ident
    struct.pack_into(">H", palm, 76, 2)

    table = struct.pack(">LLLL", rec0, 0, rec1, 0)
    return bytes(palm) + table + b"\x00\x00" + bytes(header) + body


def html_doc(*, pagebreak: bool = True) -> bytes:
    """两章的 HTML 正文。`pagebreak=False` 时不给分页标记。"""
    sep = "<mbp:pagebreak/>" if pagebreak else ""
    return (
        '<html><head><title>手搓书</title></head><body>'
        "<h1>第一章 起点</h1><p>第一段。</p>"
        f"{sep}"
        "<h1>第二章 终点</h1><p>第二段。</p>"
        "</body></html>"
    ).encode("utf-8")


def write_epub(path, *, title: str = "解包得到的书") -> str:
    """一个最小可解析的 EPUB，用来喂 `_read_extracted()` 的 KF8 分支。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">'
            f"<metadata><dc:title>{title}</dc:title><dc:creator>作者甲</dc:creator></metadata>"
            '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest><spine><itemref idref=\"c1\"/></spine></package>",
        )
        zf.writestr(
            "ch1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>'
            "<body><h1>第一章 起点</h1><p>正文一段。</p></body></html>",
        )
    return str(path)


class MobiTestCase(unittest.TestCase):
    """给每条用例一个临时目录，收尾时删掉。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="minireader_mobi_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fake(self, name: str = "book.mobi", body: bytes | None = None, **kw) -> Path:
        target = self.tmp / name
        target.write_bytes(build_mobi(body if body is not None else html_doc(), **kw))
        return target


# ---------------------------------------------------------------------------
# 嗅探：容器自己怎么说
# ---------------------------------------------------------------------------


class TestSniff(MobiTestCase):
    def test_a_hand_built_mobi_is_recognised(self) -> None:
        info = sniff(self.fake())
        self.assertIsNotNone(info)
        self.assertEqual(info.crypto_type, 0)
        self.assertEqual(info.compression, mobidoc.COMPRESSION_NONE)
        self.assertEqual(info.records, 1)
        self.assertFalse(info.encrypted)
        self.assertFalse(info.print_replica)

    def test_the_title_comes_out_of_the_header(self) -> None:
        self.assertEqual(sniff(self.fake(title="书名在这里")).title, "书名在这里")

    def test_looks_like_mobi_agrees_with_sniff(self) -> None:
        self.assertTrue(looks_like_mobi(self.fake()))
        self.assertFalse(looks_like_mobi(self.tmp / "nope.mobi"))

    def test_a_text_file_with_a_mobi_name_is_not_a_mobi(self) -> None:
        target = self.tmp / "fake.mobi"
        target.write_bytes(b"this is plainly not a palm database, just text")
        self.assertIsNone(sniff(target))
        self.assertFalse(looks_like_mobi(target))

    def test_an_empty_file_is_not_a_mobi(self) -> None:
        target = self.tmp / "empty.mobi"
        target.write_bytes(b"")
        self.assertIsNone(sniff(target))

    def test_a_header_only_file_is_not_a_mobi(self) -> None:
        """短到连 78 字节的 PalmDB 头都不够——必须返回 None 而不是抛异常。"""
        target = self.tmp / "short.mobi"
        target.write_bytes(b"BOOKMOBI")
        self.assertIsNone(sniff(target))

    def test_a_palmdoc_without_a_mobi_header_is_still_a_container(self) -> None:
        """`TEXtREAd` 是没有 MOBI 头的裸 PalmDOC：认得出来，但没有加密字段。"""
        info = sniff(self.fake(ident=b"TEXtREAd"))
        self.assertIsNotNone(info, "TEXtREAd 也是合法的 PalmDB 电子书")
        self.assertEqual(info.crypto_type, 0, "没有 MOBI 头就没有加密字段，按 0 处理")

    def test_a_missing_file_does_not_raise(self) -> None:
        self.assertIsNone(sniff(self.tmp / "does_not_exist.mobi"))

    def test_a_bad_record_table_is_survived(self) -> None:
        """记录表指向文件外时返回 None，而不是让窗口看到一个 traceback。"""
        data = bytearray(build_mobi(html_doc()))
        struct.pack_into(">L", data, PALM_HEADER, 10_000_000)  # 记录 0 的偏移
        target = self.tmp / "bad.mobi"
        target.write_bytes(bytes(data))
        self.assertIsNone(sniff(target))


class TestDrmField(MobiTestCase):
    def test_a_zero_crypto_field_is_not_drm(self) -> None:
        self.assertFalse(is_drm(self.fake()))

    def test_a_non_zero_crypto_field_is_drm(self) -> None:
        for value in (1, 2, 0xFFFF):
            with self.subTest(crypto=value):
                self.assertTrue(is_drm(self.fake(body=html_doc(), crypto=value)))

    def test_drm_is_not_reported_for_something_that_is_not_a_mobi(self) -> None:
        """不是 MOBI 就没有"有没有加密"可言——返回 False，让损坏文案去说。"""
        target = self.tmp / "junk.mobi"
        target.write_bytes(b"not a container at all")
        self.assertFalse(is_drm(target))


# ---------------------------------------------------------------------------
# is_available：不 import 也能回答
# ---------------------------------------------------------------------------


class TestIsAvailable(unittest.TestCase):
    def test_it_agrees_with_find_spec(self) -> None:
        from importlib.util import find_spec

        self.assertEqual(is_available(), find_spec("mobi") is not None)

    def test_it_does_not_import_the_library(self) -> None:
        """`is_available()` 的全部意义就是"不付 import 的钱"。

        先把这个库从 `sys.modules` 里摘掉：别的用例（真的解包那几条）会把它
        import 进来，那样这条断言就变成在看别人的脸色。摘掉之后再看
        `is_available()` 有没有把它加回来——加的回来就说明它其实 import 了。
        """
        for name in [n for n in sys.modules if n == "mobi" or n.startswith("mobi.")]:
            del sys.modules[name]
        self.assertNotIn("mobi", sys.modules)
        is_available()
        self.assertNotIn("mobi", sys.modules, "is_available() 不许真的 import")

    def test_a_missing_parent_module_is_reported_as_absent(self) -> None:
        """父模块不存在时 find_spec 会抛而不是返回 None，必须被吃掉。"""
        with mock.patch("importlib.util.find_spec", side_effect=ImportError("no parent")):
            self.assertFalse(is_available())
        with mock.patch("importlib.util.find_spec", side_effect=ValueError("no parent")):
            self.assertFalse(is_available())


# ---------------------------------------------------------------------------
# 打开：三种坏法给出三条不同的文案
# ---------------------------------------------------------------------------


class TestOpenErrors(MobiTestCase):
    def test_an_encrypted_book_says_to_strip_the_drm(self) -> None:
        with self.assertRaises(BookError) as caught:
            open_mobi(self.fake(crypto=2))
        self.assertEqual(str(caught.exception), DRM_NOTE)
        self.assertIn("Calibre", str(caught.exception))

    def test_drm_is_reported_even_without_the_mobi_library(self) -> None:
        """**顺序即设计**：嗅探在 import mobi 之前。

        所以没装 mobi 的用户打开一本带 DRM 的书，看到的是"请用 Calibre 脱壳"，
        而不是"请安装 mobi"——后者会让他白装一个库，然后仍然打不开。
        """
        target = self.fake(crypto=2)
        with mock.patch.object(mobidoc, "is_available", return_value=False):
            with self.assertRaises(BookError) as caught:
                open_mobi(target)
        self.assertEqual(str(caught.exception), DRM_NOTE)

    def test_a_missing_library_says_which_command_to_run(self) -> None:
        target = self.fake()
        with mock.patch.object(mobidoc, "is_available", return_value=False):
            with self.assertRaises(BookError) as caught:
                open_mobi(target)
        self.assertEqual(str(caught.exception), MOBI_NOTE)
        self.assertIn("pip install mobi", str(caught.exception))

    def test_a_corrupt_file_says_so(self) -> None:
        """结构合法但正文被截断：`mobi` 解包会失败，且加密字段是 0。"""
        data = build_mobi(html_doc())[:140]
        target = self.tmp / "truncated.mobi"
        target.write_bytes(data)
        if not HAVE_MOBI:
            self.skipTest(MOBI_SKIP)
        with self.assertRaises(BookError) as caught:
            open_mobi(target)
        self.assertEqual(str(caught.exception), CORRUPT_NOTE)

    def test_something_that_is_not_a_container_at_all_is_corrupt(self) -> None:
        target = self.tmp / "junk.mobi"
        target.write_bytes(b"definitely not a palm database")
        with self.assertRaises(BookError) as caught:
            open_mobi(target)
        self.assertEqual(str(caught.exception), CORRUPT_NOTE)

    def test_a_print_replica_book_is_refused_by_name(self) -> None:
        """`%MOP` 开头的是版式固定的教材：每页一张图，解出来是个 PDF。"""
        target = self.fake(body=b"%MOP" + b"\x00" * 64)
        self.assertTrue(sniff(target).print_replica)
        with self.assertRaises(BookError) as caught:
            open_mobi(target)
        self.assertEqual(str(caught.exception), PRINT_REPLICA_NOTE)

    def test_print_replica_is_reported_before_the_library_check(self) -> None:
        target = self.fake(body=b"%MOP" + b"\x00" * 64)
        with mock.patch.object(mobidoc, "is_available", return_value=False):
            with self.assertRaises(BookError) as caught:
                open_mobi(target)
        self.assertEqual(str(caught.exception), PRINT_REPLICA_NOTE)


class TestCorruptIsNotDrm(MobiTestCase):
    """同一个"解不开"，两种原因，两条文案——这是 `_failure_note()` 的全部职责。"""

    def test_the_two_notes_are_different(self) -> None:
        self.assertNotEqual(DRM_NOTE, CORRUPT_NOTE)

    def test_the_note_follows_the_encryption_field(self) -> None:
        # 两个文件都截到 140 字节：PalmDB 头 + 记录表还在（所以嗅探得出来），
        # 但正文不完整（所以解包会失败）。区别只有加密字段。
        cut = self.fake().read_bytes()[:140]
        target = self.tmp / "cut.mobi"
        target.write_bytes(cut)
        self.assertEqual(mobidoc._failure_note(target), CORRUPT_NOTE)

        locked = self.tmp / "cut_drm.mobi"
        locked.write_bytes(self.fake(crypto=2).read_bytes()[:140])
        self.assertEqual(mobidoc._failure_note(locked), DRM_NOTE)

    def test_the_note_for_a_non_container_is_corrupt(self) -> None:
        target = self.tmp / "junk.mobi"
        target.write_bytes(b"junk")
        self.assertEqual(mobidoc._failure_note(target), CORRUPT_NOTE)


# ---------------------------------------------------------------------------
# 真的解包：正文可读
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_MOBI, MOBI_SKIP)
class TestOpenUncompressed(MobiTestCase):
    def test_an_uncompressed_book_opens_with_readable_text(self) -> None:
        book = open_mobi(self.fake())
        self.assertEqual(book.kind, KIND_TEXT)
        self.assertIn("第一段。", book.text)
        self.assertIn("第二段。", book.text)

    def test_the_book_reports_itself_as_mobi(self) -> None:
        """`from_segments()` 的四个参数必须被覆盖，否则它会自称 EPUB。"""
        book = open_mobi(self.fake())
        self.assertEqual(book.encoding, "mobi")
        self.assertEqual(book.encoding_label, "MOBI")
        self.assertEqual(book.source, "mobi")
        self.assertIn("MOBI", book.describe())

    def test_the_header_title_is_used_when_the_markup_has_none(self) -> None:
        body = b"<html><body><p>no title element here at all</p></body></html>"
        book = open_mobi(self.fake(title="来自头部的书名", body=body))
        self.assertEqual(book.title, "来自头部的书名")

    def test_the_markup_title_wins_over_the_header(self) -> None:
        book = open_mobi(self.fake(title="头部书名"))
        self.assertEqual(book.title, "手搓书", "<title> 比头部字段更贴近正文")

    def test_pagebreaks_become_chapters(self) -> None:
        book = open_mobi(self.fake())
        self.assertGreaterEqual(book.chapter_count, 2)
        titles = [c.title for c in book.chapters]
        self.assertTrue(any("第一章" in t for t in titles), titles)
        self.assertTrue(any("第二章" in t for t in titles), titles)

    def test_without_pagebreaks_it_falls_back_to_the_chapter_detector(self) -> None:
        """mobi7 没有 spine，只能靠正文里的章节标题——复用 TXT 的那套检测。"""
        body = (
            "<html><body><p>"
            + "开头一段。"
            + "</p><p>第1章 起</p><p>正文甲。</p><p>第2章 承</p><p>正文乙。</p>"
            + "</body></html>"
        ).encode("utf-8")
        book = open_mobi(self.fake(body=body))
        self.assertIn("正文甲。", book.text)
        titles = [c.title for c in book.chapters]
        self.assertTrue(any("第1章" in t for t in titles), titles)
        self.assertTrue(any("第2章" in t for t in titles), titles)

    def test_an_azw3_takes_the_same_path(self) -> None:
        book = open_mobi(self.fake(name="book.azw3"))
        self.assertIn("第一段。", book.text)
        self.assertEqual(book.encoding_label, "MOBI")

    def test_the_size_and_mtime_come_from_the_file(self) -> None:
        target = self.fake()
        book = open_mobi(target)
        self.assertEqual(book.size, target.stat().st_size)
        self.assertAlmostEqual(book.mtime, target.stat().st_mtime, places=3)

    def test_the_temporary_directory_is_removed(self) -> None:
        """`mobi.extract()` 把整本书解到一个临时目录，调用方负责删。

        成功路径要同时比对**两个**目录：库自己开的 `mobiex*`，和我们为了兜住
        失败路径而自建的 `minireader_mobi_*` scratch（见下一条）。只查前者的话，
        将来谁把 scratch 的 `rmtree` 删了、或 `tempfile.tempdir` 还原写错，
        这条测试仍然是绿的——成功路径一样会漏。
        """
        before = set(Path(tempfile.gettempdir()).glob("mobiex*"))
        scratch_before = set(Path(tempfile.gettempdir()).glob("minireader_mobi_*"))
        open_mobi(self.fake())
        self.assertEqual(
            set(Path(tempfile.gettempdir()).glob("mobiex*")),
            before,
            "解包目录必须被清掉，否则每开一次书就漏一份",
        )
        self.assertEqual(
            set(Path(tempfile.gettempdir()).glob("minireader_mobi_*")) - scratch_before,
            set(),
            "成功路径也要删掉我们自己开的 scratch 目录",
        )

    def test_a_failed_unpack_does_not_leak_its_directory(self) -> None:
        """解包**失败**时也不能漏。

        这条是补的，因为上面那条只在成功路径上比对，一直是绿的：`mobi.extract()`
        先 `mkdtemp()` 再开始解包，所以解包中途抛异常时它**不会返回**，调用方拿到的
        返回值根本不存在，`finally` 里那个名字还是空的——半本书就那么留在 `%TEMP%`。
        实测：修之前跑一次截断文件，`%TEMP%` 里就多一个 `mobiex*`。

        夹具用截断的文件：`sniff()` 仍然认得出容器（头部还在），但正文记录不完整，
        于是走到解包失败那条路——正是漏目录的那条。
        """
        target = self.tmp / "broken.mobi"
        target.write_bytes(build_mobi(html_doc())[:140])
        before = set(Path(tempfile.gettempdir()).glob("mobiex*"))
        scratch_before = set(Path(tempfile.gettempdir()).glob("minireader_mobi_*"))
        with self.assertRaises(BookError):
            open_mobi(target)
        self.assertEqual(
            set(Path(tempfile.gettempdir()).glob("mobiex*")),
            before,
            "解包失败也不能留下半本书",
        )
        self.assertEqual(
            set(Path(tempfile.gettempdir()).glob("minireader_mobi_*")) - scratch_before,
            set(),
            "我们自己开的 scratch 目录也要删掉",
        )

    def test_the_scratch_root_is_put_back_even_when_unpacking_blows_up(self) -> None:
        """异常路径也要把 `tempfile.tempdir` 还原，否则会污染后面的调用。"""
        original = tempfile.tempdir
        target = self.tmp / "broken.mobi"
        target.write_bytes(build_mobi(html_doc())[:140])
        with self.assertRaises(BookError):
            open_mobi(target)
        self.assertEqual(tempfile.tempdir, original)

    def test_opening_twice_is_fine(self) -> None:
        target = self.fake()
        first = open_mobi(target)
        second = open_mobi(target)
        self.assertEqual(first.text, second.text)

    def test_an_empty_book_says_so_in_mobi_words(self) -> None:
        """正文全空时要报 MOBI 的文案，而不是 EPUB 的默认文案。"""
        body = b"<html><body><p>   </p></body></html>"
        with self.assertRaises(BookError) as caught:
            open_mobi(self.fake(body=body))
        self.assertIn("Kindle", str(caught.exception))
        self.assertNotIn("EPUB", str(caught.exception))


# ---------------------------------------------------------------------------
# 解包产物：EPUB / HTML / PDF 三条分支
# ---------------------------------------------------------------------------


class TestExtractedShapes(MobiTestCase):
    """`_read_extracted()` 的三条分支，不经过真的解包器。"""

    def info(self) -> mobidoc.MobiInfo:
        return mobidoc.MobiInfo(0, mobidoc.COMPRESSION_NONE, 1, "头部书名", False)

    def test_an_epub_from_kf8_is_parsed_as_epub(self) -> None:
        epub = write_epub(self.tmp / "book.epub", title="KF8 的书名")
        content = mobidoc._read_extracted(epub, self.info())
        self.assertEqual(content.title, "KF8 的书名")
        self.assertEqual(content.author, "作者甲")
        self.assertIn("正文一段。", "".join(s.text for s in content.segments))

    def test_html_is_split_into_segments(self) -> None:
        target = self.tmp / "book.html"
        target.write_bytes(html_doc())
        content = mobidoc._read_extracted(target, self.info())
        self.assertEqual(len(content.segments), 2)
        self.assertIn("第一段。", content.segments[0].text)

    def test_a_piece_holding_several_chapters_is_split_further(self) -> None:
        """分页标记不是唯一的边界。

        mobi7 很常见的排版是「前言后一个分页标记，然后整本小说一路写下去」。
        只认分页标记的话，这一整段会被当成**一章**——目录里两个条目，一次翻页
        跨过三章。所以每一段还要再过一遍章节检测（`_segments_from_piece`）。
        """
        body = (
            '<html><head><title>T</title></head><body><p>开头。</p>'
            "<mbp:pagebreak/>"
            "<p>第1章 起</p><p>甲。</p><p>第2章 承</p><p>乙。</p>"
            "<p>第3章 转</p><p>丙。</p></body></html>"
        ).encode("utf-8")
        target = self.tmp / "book.html"
        target.write_bytes(body)
        content = mobidoc._read_extracted(target, self.info())
        titles = [s.title for s in content.segments]
        for wanted in ("第1章", "第2章", "第3章"):
            self.assertTrue(any(wanted in t for t in titles), titles)

    def test_a_well_formed_book_is_not_split_further(self) -> None:
        """一个分页标记一章时，章节检测必须**不**再动手，否则会把一章切碎。

        这是上一条的反面：`_segments_from_piece` 只在检测器真的找到多个章节时
        才用它的答案，找到 0 或 1 个就保持"一段 = 一章"。
        """
        target = self.tmp / "book.html"
        target.write_bytes(html_doc())
        content = mobidoc._read_extracted(target, self.info())
        self.assertEqual(len(content.segments), 2)
        self.assertIn("第一章", content.segments[0].title)
        self.assertIn("第二章", content.segments[1].title)

    def test_an_html_without_a_title_falls_back_to_the_header(self) -> None:
        target = self.tmp / "book.html"
        target.write_bytes(b"<html><body><p>only body text</p></body></html>")
        content = mobidoc._read_extracted(target, self.info())
        self.assertEqual(content.title, "头部书名")

    def test_a_pdf_means_print_replica(self) -> None:
        target = self.tmp / "book.pdf"
        target.write_bytes(b"%PDF-1.4 not really")
        with self.assertRaises(BookError) as caught:
            mobidoc._read_extracted(target, self.info())
        self.assertEqual(str(caught.exception), PRINT_REPLICA_NOTE)

    def test_an_unknown_artifact_is_corrupt(self) -> None:
        target = self.tmp / "book.xyz"
        target.write_bytes(b"whatever")
        with self.assertRaises(BookError) as caught:
            mobidoc._read_extracted(target, self.info())
        self.assertEqual(str(caught.exception), CORRUPT_NOTE)


# ---------------------------------------------------------------------------
# 派发与相位闸门
# ---------------------------------------------------------------------------


class TestPhaseGate(MobiTestCase):
    def test_the_phase_is_eight(self) -> None:
        # 阶段 6 写的这条断言是 `CURRENT_PHASE == 6`。阶段 8 把相位推到 8 之后，
        # 断言跟着相位走（"跟随相位"原则），意思不变：相位等于最后一个已完成的阶段。
        # 阶段 7 加的是 `.epub` 的第二种形态而不是新扩展名，所以没有 7 这个值。
        self.assertEqual(CURRENT_PHASE, 8)

    def test_mobi_and_azw3_are_now_supported(self) -> None:
        for path in ("a.mobi", "a.azw3", "a.MOBI", "a.AZW3"):
            with self.subTest(path=path):
                self.assertTrue(is_supported(path))

    def test_the_format_row_is_the_one_that_was_already_there(self) -> None:
        """阶段 6 只推了相位，没改表——`.mobi` 那一行从阶段 2 起就写着 phase 6。"""
        fmt = format_for("a.mobi")
        self.assertEqual(fmt.label, "Kindle 电子书")
        self.assertEqual(fmt.phase, 6)
        self.assertTrue(fmt.ready)
        self.assertEqual(fmt.exts, KINDLE_EXTS)

    def test_rar_comics_are_unlocked_now(self) -> None:
        # 阶段 6 时这条断言是 `assertFalse(is_supported("a.cbr"))`；阶段 8 把它
        # 翻了一面。断言的意思没变——格式表的 ready 完全由相位决定——只是相位到了。
        self.assertTrue(is_supported("a.cbr"), "阶段 8 已到")
        self.assertTrue(is_supported("a.rar"), "阶段 8 已到")
        fmt = format_for("a.cbr")
        self.assertEqual(fmt.label, "RAR 漫画")
        self.assertEqual(fmt.phase, 8)
        self.assertTrue(fmt.ready)


class TestDispatch(MobiTestCase):
    def test_open_book_sends_a_mobi_to_the_kindle_reader(self) -> None:
        """DRM 文案能出来就证明派发走到了 `mobidoc`，而不是通用文本路径。"""
        with self.assertRaises(BookError) as caught:
            open_book(self.fake(crypto=2))
        self.assertEqual(str(caught.exception), DRM_NOTE)

    def test_open_book_sends_an_azw3_to_the_same_place(self) -> None:
        with self.assertRaises(BookError) as caught:
            open_book(self.fake(name="book.azw3", crypto=2))
        self.assertEqual(str(caught.exception), DRM_NOTE)

    def test_open_book_kind_calls_a_mobi_a_text_book(self) -> None:
        self.assertEqual(open_book_kind(self.fake()), KIND_TEXT)

    def test_a_locked_format_never_reaches_the_kindle_branch(self) -> None:
        """相位闸门在派发之前：相位没到的格式报的是相位，不会进 mobidoc。

        阶段 8 之后 `.cbr` 已解锁，不再适合当"锁着的样本"，所以这里改用打桩的
        格式行——守的是同一条性质：`open_book()` 先过 `support_note()`，再分派。
        """
        locked = Format(KIND_COMIC, (".xyz",), "未来漫画", CURRENT_PHASE + 1)
        with unittest.mock.patch.object(formats_module, "format_for", return_value=locked):
            with self.assertRaises(BookError) as caught:
                open_book(self.tmp / "a.xyz")
        self.assertIn("阶段 9", str(caught.exception))

    @unittest.skipUnless(HAVE_MOBI, MOBI_SKIP)
    def test_open_book_returns_a_readable_book(self) -> None:
        book = open_book(self.fake())
        self.assertEqual(book.kind, KIND_TEXT)
        self.assertIn("第一段。", book.text)


class TestCoreStaysQtFree(unittest.TestCase):
    def test_the_mobi_module_does_not_import_qt(self) -> None:
        """核心层的规矩：`minireader.core.*` 里不许出现 Qt（阶段 1 立的）。"""
        source = (Path(ROOT) / "minireader" / "core" / "mobidoc.py").read_text("utf-8")
        for banned in ("PySide6", "PyQt5", "minireader.qt", "import qt"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_the_mobi_library_is_imported_lazily(self) -> None:
        """模块级不许 `import mobi`，否则没装的用户在启动时就炸。"""
        import ast

        tree = ast.parse((Path(ROOT) / "minireader" / "core" / "mobidoc.py").read_text("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "mobi", "必须延迟到函数体内 import")
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "mobi")

    def test_no_decryption_code_exists(self) -> None:
        """DRM 只提示不解密。这条守的是"以后别手滑加进去"。

        扫的是**标识符**而不是文本：模块 docstring 里写着「never an attempt to
        decrypt」这句话本身就该留着，用字符串搜索会把说明文字也算成违规，逼着
        后来的人删掉解释。AST 只看真正会被执行的名字——函数名、类名、变量名、
        属性名、import 别名——那才是"代码里有没有解密逻辑"的答案。
        """
        import ast

        source = (Path(ROOT) / "minireader" / "core" / "mobidoc.py").read_text("utf-8")
        # 注意别放太短的词：`des` 会误伤 `describe`，`key` 会误伤 `keyboard`。
        # 这几个词根够长，正常代码里不会无辜撞上。
        banned = ("decrypt", "dedrm", "unlock", "aes", "rc4", "cipher")
        found: list[str] = []

        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names.append(node.name)
                if node.asname:
                    names.append(node.asname)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            for name in names:
                if any(word in name.lower() for word in banned):
                    found.append(f"第 {node.lineno} 行：{name}")

        self.assertEqual(found, [], "MiniReader 不解密，只提示用户自己处理")


if __name__ == "__main__":
    unittest.main()
