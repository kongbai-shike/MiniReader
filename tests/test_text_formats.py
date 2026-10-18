"""第二批核心层：FB2 / DOCX / HTML 三条文字格式路径。

这三个模块（`minireader.core.fb2` / `docx` / `htmldoc`）有一个共同点和一个不同点。
共同点：它们都是**文字书**，最后都走 `Book.from_segments()`，也就是「段边界由格式
自己给，章节偏移由 `from_segments()` 边追加边算」。不同点：三种格式的边界可信度
完全不同，这正是这个文件要分开守的东西。

1. **FB2 的边界是可信的。** `<section>` 嵌套是书的真实结构，所以每个**顶层**
   `<section>` 是一章、嵌套的不另起一章。这里断言的是：嵌套标题留在正文里而不是
   变成第二个章节、`<body name="notes">` 不被当正文、俄文书按 XML 声明里的
   `windows-1251` 解码（不猜）。

2. **DOCX 没有章节，只有段落。** 按每 40 段切一节是刻意的粗糙——不切就是一章
   300 页。这里断言切法、断言 `w:delText`（修订删除的字）和 `w:instrText`（域代码）
   不进正文、断言表格里的文字**要**进来。

3. **HTML 的边界要自己找。** 复用了 `epub.py` 的 `_TextExtractor`（它本来就是通用
   HTML 解析器），抽出纯文本后跑 `split_chapters_auto()`——和 TXT 一样。这里断言
   `<meta charset>` 优先于统计判断（国标网页）、断言 `<script>`/`<style>` 不进正文、
   断言章节标题同时留在正文第一行（`Book.chapter_body()` 的约定是"首行等于标题就
   剥掉"，模块自己删会让标题在正文里丢一次）。

样本全部手搓：FB2 是字符串，DOCX 是 `zipfile` 写两个成员，HTML 是字符串。没有一个
需要外部文件或网络，所以这 100 多条在任意机器上都能跑。

Qt-free，和 `minireader.core.*` 的其它模块一样。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import docx as docx_module  # noqa: E402
from minireader.core import fb2 as fb2_module  # noqa: E402
from minireader.core import formats  # noqa: E402
from minireader.core import htmldoc as htmldoc_module  # noqa: E402
from minireader.core.book_base import KIND_TEXT  # noqa: E402
from minireader.core.docx import DocxError, looks_like_docx, parse_docx  # noqa: E402
from minireader.core.fb2 import Fb2Error, parse_fb2  # noqa: E402
from minireader.core.htmldoc import HtmlError, looks_like_html, parse_html  # noqa: E402
from minireader.core.model import Book, BookError  # noqa: E402
from minireader.core.opener import open_book, open_book_kind  # noqa: E402


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

#: 一份最小的 FB2。刻意包含：书名/作者（`<description>`）、正文 body 的 `<title>`、
#: 两个顶层 section（第二个带一个嵌套 section）、以及一个 `name="notes"` 的 body。
FB2_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description>
    <title-info>
      <book-title>测试书</book-title>
      <author><first-name>列夫</first-name><middle-name>尼古拉耶维奇</middle-name>
        <last-name>托尔斯泰</last-name></author>
    </title-info>
  </description>
  <body>
    <title><p>正文</p></title>
    <section><title><p>第一章</p></title><p>正文一。</p></section>
    <section><title><p>第二章</p></title><p>正文二。</p>
      <section><title><p>节一</p></title><p>嵌套正文。</p></section>
    </section>
  </body>
  <body name="notes">
    <section><p>这是一整页译者注，不该被当正文读。</p></section>
  </body>
</FictionBook>
"""


def make_docx_xml(*paragraphs: str) -> str:
    """一份最小的 `word/document.xml`，每段一个 `<w:p>`。"""
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )


DOCX_CORE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<cp:coreProperties'
    ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>文档标题</dc:title><dc:creator>某作者</dc:creator>"
    "</cp:coreProperties>"
)


def make_html(body: str, *, title: str = "网页标题", charset: str = "utf-8") -> str:
    return (
        "<!doctype html>\n<html><head>"
        f'<meta charset="{charset}"><title>{title}</title>'
        f"</head><body>{body}</body></html>"
    )


class Batch2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mr-b2-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 造文件 -------------------------------------------------------------

    def write(self, name: str, data: str | bytes, encoding: str = "utf-8") -> Path:
        target = self.tmp / name
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding=encoding)
        return target

    def write_docx(self, name: str = "a.docx", *, paragraphs=("第一段", "第二段"),
                   core: str | None = DOCX_CORE, document: str | None = None) -> Path:
        target = self.tmp / name
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("word/document.xml", document or make_docx_xml(*paragraphs))
            if core is not None:
                zf.writestr("docProps/core.xml", core)
        return target


# ---------------------------------------------------------------------------
# FB2
# ---------------------------------------------------------------------------


class TestFb2(Batch2TestCase):
    def test_the_title_and_author_come_from_the_description(self) -> None:
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        self.assertEqual(content.title, "测试书")
        self.assertEqual(content.author, "列夫 尼古拉耶维奇 托尔斯泰")

    def test_each_top_level_section_is_one_chapter(self) -> None:
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        self.assertEqual([s.title for s in content.segments], ["第一章", "第二章"])

    def test_a_nested_section_does_not_start_a_new_chapter(self) -> None:
        """摊平会让 12 章的书在目录里出现 60 项；丢掉又看不出节的边界。"""
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        self.assertEqual(len(content.segments), 2)
        self.assertIn("嵌套正文。", content.segments[1].text)

    def test_a_nested_title_stays_in_the_body_as_a_plain_line(self) -> None:
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        self.assertIn("节一", content.segments[1].text.split("\n"))

    def test_the_chapter_title_is_not_repeated_in_the_body(self) -> None:
        """`Book.chapter_body()` 靠「首行等于标题」剥标题，重复两次就剥不干净。"""
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        self.assertEqual(content.segments[0].text, "正文一。")

    def test_a_named_body_is_not_read_as_the_main_text(self) -> None:
        """注释/尾注在 `<body name="notes">`，当正文读会让读者在第 3 章中间读到译者注。"""
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        joined = "\n".join(s.text for s in content.segments)
        self.assertNotIn("译者注", joined)

    def test_a_second_body_is_reported_as_a_note(self) -> None:
        content = parse_fb2(self.write("a.fb2", FB2_TEXT))
        self.assertIn("这个文件里还有注释/尾注，没有当正文读", content.notes)

    def test_a_single_body_has_no_such_note(self) -> None:
        text = FB2_TEXT.replace(
            '<body name="notes">\n    <section><p>这是一整页译者注，不该被当正文读。</p></section>\n  </body>',
            "",
        )
        content = parse_fb2(self.write("b.fb2", text))
        self.assertNotIn("这个文件里还有注释/尾注，没有当正文读", content.notes)

    def test_the_xml_declaration_decides_the_encoding(self) -> None:
        """先猜编码会把俄文书变成问号——所以字节直接喂解析器，让它自己读声明。"""
        text = (
            '<?xml version="1.0" encoding="windows-1251"?>'
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            "<body><section><title><p>Глава</p></title><p>Привет мир</p></section></body>"
            "</FictionBook>"
        )
        content = parse_fb2(self.write("ru.fb2", text.encode("cp1251")))
        self.assertEqual(content.segments[0].title, "Глава")
        self.assertEqual(content.segments[0].text, "Привет мир")

    def test_a_body_without_sections_is_one_chapter(self) -> None:
        text = (
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            "<body><p>只有一段正文。</p></body></FictionBook>"
        )
        content = parse_fb2(self.write("c.fb2", text))
        self.assertEqual(len(content.segments), 1)
        self.assertIn("只有一段正文。", content.segments[0].text)

    def test_the_body_title_is_the_fallback_book_title(self) -> None:
        text = (
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            "<body><title><p>备用书名</p></title><section><p>正文。</p></section></body>"
            "</FictionBook>"
        )
        content = parse_fb2(self.write("d.fb2", text))
        self.assertEqual(content.title, "备用书名")

    def test_broken_xml_says_so_in_chinese(self) -> None:
        with self.assertRaises(Fb2Error) as caught:
            parse_fb2(self.write("bad.fb2", "<FictionBook><body>"))
        self.assertIn("这本 FB2 读不了", str(caught.exception))

    def test_an_empty_file_says_so(self) -> None:
        with self.assertRaises(Fb2Error) as caught:
            parse_fb2(self.write("empty.fb2", ""))
        self.assertEqual(str(caught.exception), "这本 FB2 里没有可读的正文")

    def test_a_file_without_a_body_says_so(self) -> None:
        with self.assertRaises(Fb2Error) as caught:
            parse_fb2(self.write("nobody.fb2", '<FictionBook xmlns="x"><description/></FictionBook>'))
        self.assertIn("找不到 <body>", str(caught.exception))

    def test_a_missing_file_says_so(self) -> None:
        with self.assertRaises(Fb2Error) as caught:
            parse_fb2(self.tmp / "nope.fb2")
        self.assertIn("读不到这个文件", str(caught.exception))

    def test_an_empty_line_element_becomes_a_blank_line(self) -> None:
        text = (
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            "<body><section><p>上</p><empty-line/><p>下</p></section></body></FictionBook>"
        )
        content = parse_fb2(self.write("e.fb2", text))
        self.assertIn("\n", content.segments[0].text)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


class TestDocx(Batch2TestCase):
    def test_paragraphs_become_one_segment_when_short(self) -> None:
        content = parse_docx(self.write_docx(paragraphs=("第一段", "第二段")))
        self.assertEqual(len(content.segments), 1)
        self.assertEqual(content.segments[0].text, "第一段\n第二段")

    def test_a_long_document_is_cut_every_forty_paragraphs(self) -> None:
        """不切就是一章 300 页，目录点不动。"""
        paragraphs = [f"第 {i} 段" for i in range(1, 41 + 1)]
        content = parse_docx(self.write_docx(paragraphs=paragraphs))
        self.assertEqual(len(content.segments), 2)
        self.assertIn("第 1 段", content.segments[0].text)
        self.assertIn("第 41 段", content.segments[1].text)

    def test_the_cut_size_is_the_documented_constant(self) -> None:
        self.assertEqual(docx_module.PARAGRAPHS_PER_CHAPTER, 40)

    def test_a_deleted_revision_does_not_leak_into_the_text(self) -> None:
        """`itertext()` 会把 `<w:delText>` 也带出来，那是修订里被删掉的字。"""
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>留下的</w:t></w:r>"
            "<w:del><w:r><w:delText>删掉的</w:delText></w:r></w:del></w:p></w:body></w:document>"
        )
        content = parse_docx(self.write_docx(document=document))
        self.assertIn("留下的", content.segments[0].text)
        self.assertNotIn("删掉的", content.segments[0].text)

    def test_field_code_does_not_leak_into_the_text(self) -> None:
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>正文</w:t></w:r>"
            "<w:r><w:instrText>PAGE</w:instrText></w:r></w:p></w:body></w:document>"
        )
        content = parse_docx(self.write_docx(document=document))
        self.assertNotIn("PAGE", content.segments[0].text)

    def test_text_inside_a_table_is_read(self) -> None:
        """表格里还是 `<w:p>`，用 `root.iter()` 找而不是只看 body 的直接子元素。"""
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>表格外</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格里</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>"
        )
        content = parse_docx(self.write_docx(document=document))
        self.assertIn("表格里", content.segments[0].text)

    def test_a_tab_becomes_a_space(self) -> None:
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>前</w:t><w:tab/><w:t>后</w:t></w:r></w:p></w:body></w:document>"
        )
        content = parse_docx(self.write_docx(document=document))
        self.assertEqual(content.segments[0].text, "前 后")

    def test_the_title_and_author_come_from_core_properties(self) -> None:
        content = parse_docx(self.write_docx())
        self.assertEqual(content.title, "文档标题")
        self.assertEqual(content.author, "某作者")

    def test_a_document_without_core_properties_falls_back_to_the_filename(self) -> None:
        """`docProps/core.xml` 是可选的；没有它不该打不开，只是书名退回文件名。"""
        content = parse_docx(self.write_docx(name="报告.docx", core=None))
        self.assertEqual(content.title, "报告")
        self.assertEqual(content.author, "")

    def test_a_zip_without_the_document_part_says_so(self) -> None:
        target = self.tmp / "notdocx.docx"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with self.assertRaises(DocxError) as caught:
            parse_docx(target)
        self.assertIn("没有 word/document.xml", str(caught.exception))

    def test_a_file_that_is_not_a_zip_says_so(self) -> None:
        with self.assertRaises(DocxError) as caught:
            parse_docx(self.write("a.docx", "这不是 zip"))
        self.assertIn("不是 DOCX", str(caught.exception))

    def test_a_document_without_paragraphs_says_so(self) -> None:
        with self.assertRaises(DocxError) as caught:
            parse_docx(self.write_docx(paragraphs=()))
        self.assertEqual(str(caught.exception), "这个 DOCX 里没有可读的正文")

    def test_a_missing_file_says_so(self) -> None:
        with self.assertRaises(DocxError) as caught:
            parse_docx(self.tmp / "nope.docx")
        self.assertIn("读不到这个文件", str(caught.exception))

    def test_the_huge_document_guard_is_the_documented_constant(self) -> None:
        self.assertEqual(docx_module.MAX_DOCUMENT_BYTES, 64 * 1024 * 1024)

    def test_a_giant_document_part_is_refused_before_parsing(self) -> None:
        """上限是 `getinfo()` 报的**解压后**大小，所以在 `read()` 之前就能拒。

        真造一个 64 MB 的成员会让这个测试文件变慢几百倍，所以把上限打桩成 10 字节
        ——测的是「先看大小再读」这个顺序，不是那个具体数字（数字有它自己的断言）。
        """
        target = self.write_docx()
        with mock.patch.object(docx_module, "MAX_DOCUMENT_BYTES", 10):
            with self.assertRaises(DocxError) as caught:
                parse_docx(target)
        self.assertIn("正文太大", str(caught.exception))

    def test_looks_like_docx_accepts_a_real_one(self) -> None:
        self.assertTrue(looks_like_docx(self.write_docx()))

    def test_looks_like_docx_rejects_a_plain_zip(self) -> None:
        target = self.tmp / "plain.zip"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("a.txt", "x")
        self.assertFalse(looks_like_docx(target))

    def test_looks_like_docx_never_raises(self) -> None:
        self.assertFalse(looks_like_docx(self.tmp / "nope.docx"))
        self.assertFalse(looks_like_docx(self.write("a.docx", "不是 zip")))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


class TestHtml(Batch2TestCase):
    def test_the_title_comes_from_the_title_tag(self) -> None:
        content = parse_html(self.write("a.html", make_html("<p>正文。</p>")))
        self.assertEqual(content.title, "网页标题")

    def test_the_first_heading_is_the_fallback_title(self) -> None:
        page = make_html("<h1>第一章</h1><p>正文。</p>", title="")
        content = parse_html(self.write("b.html", page))
        self.assertEqual(content.title, "第一章")

    def test_chapters_come_from_the_auto_rule(self) -> None:
        page = make_html("<h1>第一章 开端</h1><p>一。</p><h1>第二章 继续</h1><p>二。</p>")
        content = parse_html(self.write("c.html", page))
        self.assertEqual([s.title for s in content.segments], ["第一章 开端", "第二章 继续"])

    def test_the_chapter_title_stays_on_the_first_line_of_the_body(self) -> None:
        """`Book.chapter_body()` 的约定是「首行等于标题就剥掉」。模块自己删会让
        标题在正文里丢一次，所以它必须留着。"""
        page = make_html("<h1>第一章 开端</h1><p>一。</p><h1>第二章 继续</h1><p>二。</p>")
        content = parse_html(self.write("d.html", page))
        self.assertEqual(content.segments[0].text.split("\n")[0], "第一章 开端")

    def test_script_and_style_do_not_reach_the_text(self) -> None:
        page = make_html(
            "<script>var x = 1;</script><style>.a{color:red}</style><p>正文。</p>"
        )
        content = parse_html(self.write("e.html", page))
        joined = "\n".join(s.text for s in content.segments)
        self.assertNotIn("var x", joined)
        self.assertNotIn("color:red", joined)
        self.assertIn("正文。", joined)

    def test_the_meta_charset_beats_the_statistical_guess(self) -> None:
        """国标网页的 `<meta charset="gbk">` 是作者的话，比常用字密度可信。"""
        page = make_html("<h1>第一章 开始</h1><p>这是国标编码的中文正文，用来测试解码。</p>",
                         title="国标网页", charset="gbk")
        content = parse_html(self.write("gbk.html", page.encode("gbk")))
        self.assertEqual(content.title, "国标网页")
        self.assertIn("这是国标编码的中文正文", content.segments[0].text)
        self.assertIn("编码按 GBK 判断", " ".join(content.notes))

    def test_a_page_without_a_charset_falls_back_to_the_sniffer(self) -> None:
        page = "<html><head><title>无声明</title></head><body><p>中文正文。</p></body></html>"
        content = parse_html(self.write("f.html", page, encoding="utf-8"))
        self.assertIn("中文正文。", content.segments[0].text)

    def test_an_unknown_declared_charset_falls_back_instead_of_raising(self) -> None:
        page = make_html("<p>正文。</p>", charset="not-a-real-charset")
        content = parse_html(self.write("g.html", page))
        self.assertIn("正文。", content.segments[0].text)
        self.assertIn("不认识", " ".join(content.notes))

    def test_an_empty_page_says_so(self) -> None:
        with self.assertRaises(HtmlError) as caught:
            parse_html(self.write("empty.html", ""))
        self.assertEqual(str(caught.exception), "这个网页里没有可读的正文")

    def test_a_page_with_only_markup_says_so(self) -> None:
        with self.assertRaises(HtmlError) as caught:
            parse_html(self.write("bare.html", "<html><body><br/><hr/></body></html>"))
        self.assertEqual(str(caught.exception), "这个网页里没有可读的正文")

    def test_a_missing_file_says_so(self) -> None:
        with self.assertRaises(HtmlError) as caught:
            parse_html(self.tmp / "nope.html")
        self.assertIn("读不到这个文件", str(caught.exception))

    def test_broken_markup_still_yields_what_was_read(self) -> None:
        """HTMLParser 在畸形输入上可能抛；已经抽到的正文照用，不该整本打不开。"""
        page = "<html><body><p>能读到的字</p><div><span></body>"
        content = parse_html(self.write("h.html", page))
        self.assertIn("能读到的字", content.segments[0].text)

    def test_looks_like_html_accepts_a_real_page(self) -> None:
        self.assertTrue(looks_like_html(self.write("a.html", make_html("<p>x</p>"))))

    def test_looks_like_html_accepts_a_bare_fragment(self) -> None:
        self.assertTrue(looks_like_html(self.write("b.html", "<html><body>x</body></html>")))

    def test_looks_like_html_rejects_plain_text(self) -> None:
        self.assertFalse(looks_like_html(self.write("c.html", "就是一段文字\n没有标签")))

    def test_looks_like_html_never_raises(self) -> None:
        self.assertFalse(looks_like_html(self.tmp / "nope.html"))


# ---------------------------------------------------------------------------
# 格式表与派发
# ---------------------------------------------------------------------------


class TestFormatTable(unittest.TestCase):
    def test_every_new_extension_is_in_the_table(self) -> None:
        for ext in (".fb2", ".docx", ".html", ".htm", ".cb7"):
            with self.subTest(ext=ext):
                self.assertIsNotNone(formats.format_for("a" + ext))

    def test_the_three_text_formats_are_text(self) -> None:
        for ext in (".fb2", ".docx", ".html", ".htm"):
            with self.subTest(ext=ext):
                self.assertEqual(formats.kind_for("a" + ext), KIND_TEXT)

    def test_cb7_is_a_comic(self) -> None:
        self.assertEqual(formats.kind_for("a.cb7"), "comic")

    def test_they_are_ready_at_this_phase(self) -> None:
        """第二批的相位写在表里；CURRENT_PHASE 必须已经够高，否则过滤器会漏掉它们。"""
        for ext in (".fb2", ".docx", ".html", ".htm", ".cb7"):
            with self.subTest(ext=ext):
                self.assertTrue(formats.is_supported("a" + ext))

    def test_the_extension_constants_match_the_table(self) -> None:
        self.assertEqual(formats.FB2_EXTS, (".fb2",))
        self.assertEqual(formats.DOCX_EXTS, (".docx",))
        self.assertEqual(formats.HTML_EXTS, (".html", ".htm"))
        self.assertEqual(formats.CB7_EXTS, (".cb7",))

    def test_htm_and_html_share_one_row(self) -> None:
        """.htm 是同一份东西的短名字，拆成两行会让过滤器里出现两个一样的标签。"""
        row = formats.format_for("a.htm")
        self.assertIs(row, formats.format_for("a.html"))


class TestDispatch(Batch2TestCase):
    def test_fb2_opens_as_a_text_book(self) -> None:
        book = open_book(self.write("a.fb2", FB2_TEXT))
        self.assertIsInstance(book, Book)
        self.assertEqual(book.source, "fb2")
        self.assertEqual(book.chapter_count, 2)

    def test_docx_opens_as_a_text_book(self) -> None:
        book = open_book(self.write_docx())
        self.assertIsInstance(book, Book)
        self.assertEqual(book.source, "docx")

    def test_html_opens_as_a_text_book(self) -> None:
        page = make_html("<h1>第一章</h1><p>正文。</p><h1>第二章</h1><p>再一段。</p>")
        book = open_book(self.write("a.html", page))
        self.assertIsInstance(book, Book)
        self.assertEqual(book.source, "html")
        self.assertEqual(book.chapter_count, 2)

    def test_htm_opens_the_same_way(self) -> None:
        page = make_html("<h1>第一章</h1><p>正文。</p><h1>第二章</h1><p>再一段。</p>")
        book = open_book(self.write("a.htm", page))
        self.assertEqual(book.source, "html")

    def test_the_kind_is_text_for_all_three(self) -> None:
        for name in ("a.fb2", "a.docx", "a.html"):
            with self.subTest(name=name):
                self.assertEqual(open_book_kind(self.tmp / name), KIND_TEXT)

    def test_a_broken_fb2_reaches_the_user_as_a_book_error(self) -> None:
        """窗口只 catch `BookError`；裸 `Fb2Error` 会显示成「打不开：Fb2Error: …」。"""
        with self.assertRaises(BookError) as caught:
            open_book(self.write("bad.fb2", "<FictionBook>"))
        self.assertIn("这本 FB2 读不了", str(caught.exception))

    def test_a_broken_docx_reaches_the_user_as_a_book_error(self) -> None:
        with self.assertRaises(BookError) as caught:
            open_book(self.write("bad.docx", "不是 zip"))
        self.assertIn("不是 DOCX", str(caught.exception))

    def test_a_page_without_any_text_reaches_the_user_as_a_book_error(self) -> None:
        """注意不能拿空文件测：`Book.from_path()` 更早就用「文件是空的」拦下了。
        这里要的是「有内容、但没有一个可读的字」。"""
        with self.assertRaises(BookError) as caught:
            open_book(self.write("bare.html", "<html><body><br/><hr/></body></html>"))
        self.assertEqual(str(caught.exception), "这个网页里没有可读的正文")

    def test_the_book_carries_the_right_labels(self) -> None:
        book = open_book(self.write("a.fb2", FB2_TEXT))
        self.assertEqual(book.encoding_label, "FB2")
        self.assertEqual(book.encoding, "fb2")

    def test_describe_mentions_the_format(self) -> None:
        book = open_book(self.write_docx())
        self.assertIn("DOCX", book.describe())

    def test_the_record_keeps_the_library_schema(self) -> None:
        book = open_book(self.write("a.fb2", FB2_TEXT))
        record = book.to_record()
        for key in ("id", "title", "path", "size", "mtime", "encoding", "chapters", "rule"):
            with self.subTest(key=key):
                self.assertIn(key, record)


# ---------------------------------------------------------------------------
# 与 epub 共享的那部分
# ---------------------------------------------------------------------------


class TestSharedWithEpub(Batch2TestCase):
    """三个新模块都从 `epub.py` 借了东西，借的是同一份而不是抄一份。"""

    def test_fb2_uses_the_epub_whitespace_rules(self) -> None:
        from minireader.core import epub

        self.assertIs(fb2_module._clean_text, epub._clean_text)
        self.assertIs(fb2_module.Segment, epub.Segment)

    def test_htmldoc_uses_the_epub_extractor(self) -> None:
        from minireader.core import epub

        self.assertIs(htmldoc_module._TextExtractor, epub._TextExtractor)

    def test_htmldoc_splits_with_the_txt_rule(self) -> None:
        """HTML 没有可信边界标记，和 TXT 一样靠正则找（约束 24 的反面）。

        行为证据：正文里**没有** `<h1>`，只有纯文本的「第 N 章」行——EPUB 那条路
        不会切（它只看 spine），TXT 那条会切。这里必须切。
        """
        page = make_html(
            "<p>第一章 开端</p><p>一。</p><p>第二章 继续</p><p>二。</p><p>第三章 收尾</p><p>三。</p>"
        )
        content = parse_html(self.write("plain.html", page))
        self.assertGreaterEqual(len(content.segments), 3)
        self.assertIn("开端", content.segments[0].title)


if __name__ == "__main__":
    unittest.main()
