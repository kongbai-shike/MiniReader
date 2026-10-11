"""EPUB 通过 Book.from_path 的端到端接入测试。

`core/epub.py` 的单元测试覆盖解析本身，`from_segments` 属于 model 层——两边各自
都绿，接缝却可能断（例如按扩展名路由漏了、EpubError 没翻译成 BookError、或者
EPUB 的章节数被 TXT 正则重新切了一遍）。这里造真 EPUB 走完整条链路。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minireader.core import Book, BookError  # noqa: E402
from minireader.core.model import _is_epub  # noqa: E402

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
    <dc:identifier id="uid">urn:uuid:test</dc:identifier><dc:language>zh</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="c3.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/><itemref idref="c3"/></spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>目录</title></head><body>
<nav epub:type="toc"><ol>
<li><a href="c1.xhtml">第一章 起点</a></li>
<li><a href="c2.xhtml">第二章 发展</a></li>
<li><a href="c3.xhtml">第三章 终点</a></li>
</ol></nav></body></html>
"""


def chapter_xhtml(title: str, paras: list[str]) -> str:
    body = "".join(f"<p>{p}</p>" for p in paras)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>'
        f"{title}</title></head><body><h1>{title}</h1>{body}</body></html>"
    )


def make_epub(path: str, *, title: str = "测试书", author: str = "某作者") -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("OEBPS/content.opf", OPF.format(title=title, author=author))
        zf.writestr("OEBPS/nav.xhtml", NAV)
        # 正文里故意塞进"第四章""第 9 章"这种行内提及：TXT 正则会把它们切成幻影章节，
        # EPUB 必须按 spine 走，章节数只能是 3。
        zf.writestr("OEBPS/c1.xhtml", chapter_xhtml("第一章 起点", ["甲段正文。", "第四章 这是行内提及，不是章节。"]))
        zf.writestr("OEBPS/c2.xhtml", chapter_xhtml("第二章 发展", ["乙段正文。"]))
        zf.writestr("OEBPS/c3.xhtml", chapter_xhtml("第三章 终点", ["丙段正文。"]))
        zf.writestr("OEBPS/extra.xhtml", chapter_xhtml("孤儿文档", ["不在 spine 里，不该出现。"]))
        zf.writestr(
            "META-INF/encryption.xml",
            '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>',
        ) if False else None


class EpubBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mr-epub-")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name: str = "book.epub") -> str:
        return os.path.join(self.tmp, name)


class TestFromPathRoutesEpub(EpubBase):
    def test_an_epub_opens_through_the_spine_not_the_regex(self) -> None:
        p = self.path()
        make_epub(p)
        book = Book.from_path(p)
        # 3 段 spine，而不是 TXT 正则切出来的 4 个（第一章标题 + 行内"第四章"）。
        self.assertEqual(book.chapter_count, 3, [c.title for c in book.chapters])
        self.assertEqual([c.title for c in book.chapters], ["第一章 起点", "第二章 发展", "第三章 终点"])
        self.assertEqual(book.encoding, "epub")
        self.assertEqual(book.encoding_label, "EPUB")
        self.assertEqual(book.source, "epub")
        self.assertEqual(book.author, "某作者")
        self.assertEqual(book.rule_id, None)
        self.assertTrue(book.confident)
        self.assertNotIn("孤儿文档", book.text, "不在 spine 里的文档被读进来了")

    def test_the_metadata_reaches_title_and_author(self) -> None:
        p = self.path()
        make_epub(p, title="斗破苍穹", author="天蚕土豆")
        book = Book.from_path(p)
        self.assertEqual(book.title, "斗破苍穹")
        self.assertEqual(book.author, "天蚕土豆")
        self.assertIn("天蚕土豆", book.describe())

    def test_every_chapter_body_is_addressable_and_covers_the_text(self) -> None:
        p = self.path()
        make_epub(p)
        book = Book.from_path(p)
        self.assertEqual(book.chapters[0].start, 0)
        self.assertEqual(book.chapters[-1].end, len(book.text))
        for i in range(book.chapter_count):
            body = book.chapter_body(i)
            self.assertTrue(body.strip(), f"第 {i} 章正文是空的")
        # 相邻章连续：这是 TXT 分章器也保证的不变量，EPUB 不能破。
        for a, b in zip(book.chapters, book.chapters[1:]):
            self.assertEqual(a.end, b.start, "章节之间有空隙或重叠")

    def test_chapters_are_separated_by_a_blank_line(self) -> None:
        """两章之间必须有空行，否则章末和下一章开头会粘成一行。

        空行算在前一章的尾巴上（和 TXT 分章器一致），所以断言的是"后一章起点之前
        以空行结尾"，而不是"两章之间存在一段谁都不属于的文本"。
        """
        p = self.path()
        make_epub(p)
        book = Book.from_path(p)
        for b in book.chapters[1:]:
            self.assertTrue(
                book.text[: b.start].endswith("\n\n"),
                repr(book.text[max(0, b.start - 8) : b.start]),
            )
            self.assertFalse(book.text[b.start].isspace(), "后一章正文不该以空白开头")

    def test_a_renamed_epub_is_still_detected(self) -> None:
        """扩展名骗不了它：改名成 .txt 也应当按 EPUB 打开。"""
        p = self.path("伪装.txt")
        make_epub(p)
        self.assertTrue(_is_epub(__import__("pathlib").Path(p)))
        book = Book.from_path(p)
        self.assertEqual(book.source, "epub")
        self.assertEqual(book.chapter_count, 3)

    def test_a_plain_txt_is_not_mistaken_for_an_epub(self) -> None:
        p = self.path("普通.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("第一章 甲\n正文\n第二章 乙\n正文\n")
        self.assertFalse(_is_epub(__import__("pathlib").Path(p)))
        book = Book.from_path(p)
        self.assertEqual(book.source, "txt")
        self.assertEqual(book.chapter_count, 2)

    def test_the_epub_path_records_the_real_file_size_and_mtime(self) -> None:
        p = self.path()
        make_epub(p)
        book = Book.from_path(p)
        self.assertEqual(book.size, os.path.getsize(p))
        self.assertAlmostEqual(book.mtime, os.stat(p).st_mtime, places=3)

    def test_the_book_id_is_stable_across_txt_and_epub_paths(self) -> None:
        from minireader.core import book_id

        p = self.path()
        make_epub(p)
        self.assertEqual(Book.from_path(p).id, book_id(p))

    def test_the_record_carries_source_and_author(self) -> None:
        p = self.path()
        make_epub(p, author="作者甲")
        record = Book.from_path(p).to_record()
        self.assertEqual(record["source"], "epub")
        self.assertEqual(record["author"], "作者甲")
        self.assertEqual(record["chapters"], 3)
        self.assertNotIn("text", record, "正文绝不能进索引")

    def test_a_txt_record_has_no_author_key(self) -> None:
        p = self.path("无作者.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("第一章 甲\n正文\n")
        record = Book.from_path(p).to_record()
        self.assertEqual(record["source"], "txt")
        self.assertNotIn("author", record)


class TestEpubFailuresBecomeBookError(EpubBase):
    """解析层的 EpubError 必须翻译成 BookError，否则 UI 的 except BookError 漏掉它。"""

    def test_a_corrupt_zip_raises_book_error(self) -> None:
        p = self.path("坏的.epub")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 200)
        with self.assertRaises(BookError) as ctx:
            Book.from_path(p)
        self.assertTrue(str(ctx.exception))

    def test_an_empty_zip_raises_book_error(self) -> None:
        p = self.path("空.epub")
        with zipfile.ZipFile(p, "w"):
            pass
        with self.assertRaises(BookError):
            Book.from_path(p)

    def test_a_drm_epub_raises_book_error_with_a_chinese_reason(self) -> None:
        p = self.path("加密.epub")
        make_epub(p)
        with zipfile.ZipFile(p, "a") as zf:
            zf.writestr(
                "META-INF/encryption.xml",
                '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#"><EncryptionMethod/></EncryptedData>'
                "</encryption>",
            )
        with self.assertRaises(BookError) as ctx:
            Book.from_path(p)
        self.assertIn("DRM", str(ctx.exception))

    def test_an_epub_with_no_readable_text_raises_book_error(self) -> None:
        p = self.path("空白.epub")
        empty = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title> </title></head>'
            "<body>   </body></html>"
        )
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OEBPS/content.opf", OPF.format(title="空", author=""))
            zf.writestr("OEBPS/nav.xhtml", NAV)
            for name in ("c1", "c2", "c3"):
                zf.writestr(f"OEBPS/{name}.xhtml", empty)
        with self.assertRaises(BookError):
            Book.from_path(p)

    def test_the_size_limit_still_applies_to_epubs(self) -> None:
        from minireader.core.model import MAX_BYTES

        p = self.path("超大.epub")
        make_epub(p)
        real = os.path.getsize(p)
        self.assertLess(real, MAX_BYTES)
        # 用稀疏文件做不到，所以直接证明判断顺序：oversize 在解析之前就被拦下。
        import minireader.core.model as M

        original = M.MAX_BYTES
        try:
            M.MAX_BYTES = real - 1
            with self.assertRaises(BookError) as ctx:
                Book.from_path(p)
            self.assertIn("太大", str(ctx.exception))
        finally:
            M.MAX_BYTES = original


class TestFromSegmentsDirectly(unittest.TestCase):
    """from_segments 的边界，走 OpenAI 接口而不经过 zip。"""

    def seg(self, title: str, text: str):
        from minireader.core import Segment

        return Segment(title, text)

    def test_empty_segments_raise(self) -> None:
        with self.assertRaises(BookError):
            Book.from_segments([], path="x.epub")

    def test_all_blank_segments_raise(self) -> None:
        with self.assertRaises(BookError):
            Book.from_segments([self.seg("一", "  \n "), self.seg("二", "\n")], path="x.epub")

    def test_blank_segments_are_skipped_and_noted(self) -> None:
        book = Book.from_segments(
            [self.seg("一", "甲"), self.seg("空", "  "), self.seg("二", "乙")],
            path="x.epub",
            title="书",
        )
        self.assertEqual(book.chapter_count, 2)
        self.assertTrue(any("跳过" in n for n in book.notes), book.notes)

    def test_missing_titles_get_a_numbered_fallback(self) -> None:
        """TXT_CHAPTER 是"章节"（TXT 文本里出现的字眼），复用它是为了让两种来源的
        兜底标题长得一样。"""
        from minireader.core.chapters import TXT_CHAPTER

        book = Book.from_segments([self.seg("", "甲"), self.seg("", "乙")], path="x.epub")
        self.assertEqual([c.title for c in book.chapters], [f"{TXT_CHAPTER} 1", f"{TXT_CHAPTER} 2"])

    def test_the_title_falls_back_to_the_file_name(self) -> None:
        book = Book.from_segments([self.seg("一", "甲")], path=os.path.join("d", "我的书.epub"))
        self.assertEqual(book.title, "我的书")

    def test_leading_indentation_is_preserved_but_outer_newlines_are_trimmed(self) -> None:
        book = Book.from_segments([self.seg("一", "\n\n　　缩进正文\n\n")], path="x.epub")
        self.assertEqual(book.text, "　　缩进正文")

    def test_chapter_body_does_not_strip_a_legitimate_first_line(self) -> None:
        """chapter_body() 会删掉与标题重复的首行；EPUB 正文里没有标题行，所以正文必须原样。"""
        book = Book.from_segments([self.seg("一", "第一章 一\n正文内容")], path="x.epub")
        self.assertIn("第一章 一", book.chapter_body(0))

    def test_segments_larger_than_needed_still_end_at_the_text_length(self) -> None:
        book = Book.from_segments([self.seg("一", "甲" * 5000), self.seg("二", "乙" * 3000)], path="x.epub")
        self.assertEqual(book.chapters[-1].end, len(book.text))
        self.assertEqual(len(book.text), 5000 + 2 + 3000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
