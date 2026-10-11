"""EPUB 解析的单元测试。

样本全部在 tempfile.mkdtemp() 里用 zipfile 现搭，不放二进制 fixture、不联网：
EPUB 就是一个 zip，用代码写出来的样本能一眼看出"是哪一个字节导致了这个行为"，
而二进制 fixture 几年后没人知道它为什么长那样。

每个测试都对着一个真实会出问题的地方，docstring 里写清楚它防的是什么。

    py -3.12 -m unittest tests.test_epub -v
"""

from __future__ import annotations

import codecs
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import epub as E  # noqa: E402

MIMETYPE = "application/epub+zip"

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

ENCRYPTION_XML = """<?xml version="1.0" encoding="utf-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">
    <EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
  </EncryptedData>
</encryption>
"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:test"/></head>
  <docTitle><text>测试书</text></docTitle>
  <navMap>
    <navPoint id="n1" playOrder="1">
      <navLabel><text>第一章 起点</text></navLabel>
      <content src="text/ch1.xhtml"/>
    </navPoint>
    <navPoint id="n2" playOrder="2">
      <navLabel><text>第二章 终点</text></navLabel>
      <content src="text/ch2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

CH1 = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>'
    "<body><h1>第一章 起点</h1><p>第一段。</p><p>第二段。</p></body></html>"
)
CH2 = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第二章</title></head>'
    "<body><h1>第二章 终点</h1><p>第三段。</p><p>第四段。</p></body></html>"
)

# 用 opf: 前缀写的那一套。真实 EPUB 里 default namespace 更常见，但两种都存在。
PREFIXED_OPF = """<?xml version="1.0" encoding="utf-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf"
             xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <opf:metadata>
    <dc:title>前缀书名</dc:title>
    <dc:creator>前缀作者</dc:creator>
  </opf:metadata>
  <opf:manifest>
    <opf:item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
  </opf:manifest>
  <opf:spine>
    <opf:itemref idref="ch1"/>
  </opf:spine>
</opf:package>
"""


# ---------------------------------------------------------------------------
# 搭样本用的工具
# ---------------------------------------------------------------------------


def as_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def write_zip(path: str, files: dict[str, str | bytes]) -> str:
    """把 {zip 内路径: 文本或字节} 写成一个 zip，返回路径。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if "mimetype" in files:
            # mimetype 必须是第一个成员而且不压缩——这是 EPUB 的硬性规定。解析器
            # 并不依赖它，但样本要像真书，否则测的就不是真实文件。
            zf.writestr(zipfile.ZipInfo("mimetype"), as_bytes(files["mimetype"]))
        for name, value in files.items():
            if name == "mimetype":
                continue
            zf.writestr(name, as_bytes(value))
    return path


def body_lines(text: str) -> list[str]:
    """正文里的非空行。段落有没有被拆开，看这个列表最清楚。"""
    return [line for line in text.split("\n") if line.strip()]


def item_xml(
    item_id: str, href: str, media_type: str = "application/xhtml+xml", properties: str = ""
) -> str:
    extra = f' properties="{properties}"' if properties else ""
    return f'    <item id="{item_id}" href="{href}" media-type="{media_type}"{extra}/>'


def ref_xml(idref: str, linear: str = "") -> str:
    extra = f' linear="{linear}"' if linear else ""
    return f'    <itemref idref="{idref}"{extra}/>'


def make_opf(
    title: str = "测试书",
    author: str = "测试作者",
    items: tuple[str, ...] | list[str] = (),
    spine: tuple[str, ...] | list[str] = (),
    version: str = "3.0",
    toc: str = "",
) -> str:
    """生成一个带命名空间的 OPF——真实文件全都长这样。"""
    toc_attr = f' toc="{toc}"' if toc else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf"'
        f' version="{version}" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{title}</dc:title>\n"
        f"    <dc:creator>{author}</dc:creator>\n"
        '    <dc:identifier id="bookid">urn:uuid:test</dc:identifier>\n'
        "    <dc:language>zh-CN</dc:language>\n"
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(items) + "\n  </manifest>\n"
        f"  <spine{toc_attr}>\n" + "\n".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )


def nav_xhtml(entries: list[tuple[str, str]], marker: str = 'epub:type="toc"') -> str:
    """EPUB3 的 nav 文档。marker 决定用 epub:type 还是 role 来标记目录。"""
    links = "\n".join(
        f'        <li><a href="{href}">{title}</a></li>' for href, title in entries
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"'
        ' xmlns:epub="http://www.idpf.org/2007/ops">\n'
        "<head><title>目录</title></head>\n"
        "<body>\n"
        f"  <nav {marker}>\n      <ol>\n{links}\n      </ol>\n  </nav>\n"
        "</body></html>\n"
    )


def single_doc_opf(doc_href: str = "text/ch1.xhtml", version: str = "3.0") -> str:
    return make_opf(
        items=[item_xml("ch1", doc_href)],
        spine=[ref_xml("ch1")],
        version=version,
    )


class EpubTestCase(unittest.TestCase):
    """所有 EPUB 测试的公共部分：一个临时目录 + 几个现搭样本的工厂方法。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mr-epub-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, files: dict[str, str | bytes], name: str = "book.epub") -> str:
        return write_zip(os.path.join(self.tmp, name), files)

    def epub2_files(self) -> dict[str, str | bytes]:
        """最小可用的 EPUB2：container + OPF + ncx + 两节正文。"""
        return {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("ncx", "toc.ncx", "application/x-dtbncx+xml"),
                    item_xml("ch1", "text/ch1.xhtml"),
                    item_xml("ch2", "text/ch2.xhtml"),
                ],
                spine=[ref_xml("ch1"), ref_xml("ch2")],
                version="2.0",
                toc="ncx",
            ),
            "OEBPS/toc.ncx": NCX,
            "OEBPS/text/ch1.xhtml": CH1,
            "OEBPS/text/ch2.xhtml": CH2,
        }

    def one_doc_files(self, body: str, *, head: str = "", other: dict[str, str | bytes] | None = None) -> dict:
        """一份正文的整本书，正文内容由调用方给。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": single_doc_opf(),
            "OEBPS/text/ch1.xhtml": f"<html><head>{head}</head><body>{body}</body></html>",
        }
        if other:
            files.update(other)
        return files

    def parse_one_body(self, body: str, *, head: str = "") -> str:
        """只关心一节正文的测试用这个：返回该节的纯文本。"""
        content = E.parse_epub(self.write(self.one_doc_files(body, head=head)))
        self.assertEqual(len(content.segments), 1, "这一份样本应该正好产出一节")
        return content.segments[0].text


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


class EpubHappyPathTest(EpubTestCase):
    def test_minimal_epub2_parses_title_author_and_two_segments(self) -> None:
        """最小 EPUB2：书名、作者、目录标题、正文都对，而且段落没有被粘成一行。

        段落粘连是全文抽取里最容易犯又最难发现的错：文本"看着有"，但整章糊成
        一团。所以这里断言的是行列表，不是"包含某个子串"。
        """
        content = E.parse_epub(self.write(self.epub2_files()))
        self.assertEqual(content.title, "测试书")
        self.assertEqual(content.author, "测试作者")
        self.assertEqual(
            [segment.title for segment in content.segments], ["第一章 起点", "第二章 终点"]
        )
        self.assertEqual(
            body_lines(content.segments[0].text), ["第一章 起点", "第一段。", "第二段。"]
        )
        self.assertNotIn("第一段。第二段。", content.segments[0].text)
        self.assertEqual(
            body_lines(content.segments[1].text), ["第二章 终点", "第三段。", "第四段。"]
        )

    def test_epub3_nav_titles_are_used(self) -> None:
        """EPUB3 的 nav 目录决定章节标题，正文里的 h1 不能顶掉它。"""
        nav = nav_xhtml(
            [("text/ch1.xhtml", "目录标题甲"), ("text/ch2.xhtml", "目录标题乙")]
        )
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("nav", "nav.xhtml", properties="nav"),
                    item_xml("ch1", "text/ch1.xhtml"),
                    item_xml("ch2", "text/ch2.xhtml"),
                ],
                spine=[ref_xml("ch1"), ref_xml("ch2")],
            ),
            "OEBPS/nav.xhtml": nav,
            "OEBPS/text/ch1.xhtml": "<html><head><title>书名</title></head>"
            "<body><h1>文档里的标题甲</h1><p>正文甲。</p></body></html>",
            "OEBPS/text/ch2.xhtml": "<html><head><title>书名</title></head>"
            "<body><h1>文档里的标题乙</h1><p>正文乙。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(
            [segment.title for segment in content.segments], ["目录标题甲", "目录标题乙"]
        )
        self.assertIn("正文甲。", content.segments[0].text)

    def test_nav_with_role_doc_toc_is_accepted(self) -> None:
        """有些工具不写 epub:type，改用 role="doc-toc"，一样要认。

        landmarks / page-list 那类 `<nav>` 里的 `<a>` 不是章节目录，所以只认被明确
        标成 toc 的那一个。
        """
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[item_xml("nav", "nav.xhtml", properties="nav"), item_xml("c1", "text/ch1.xhtml")],
                spine=[ref_xml("c1")],
            ),
            "OEBPS/nav.xhtml": nav_xhtml(
                [("text/ch1.xhtml", "用 role 标的目录")], 'role="doc-toc"'
            ),
            "OEBPS/text/ch1.xhtml": "<html><body><p>正文。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual([segment.title for segment in content.segments], ["用 role 标的目录"])

    def test_namespaced_opf_still_parses(self) -> None:
        """带命名空间的 OPF 必须能解析（现实里所有 OPF 都带）。

        这个测试专门防"顺手优化"：把按本地名查找换成 root.findall("manifest")，
        在真实文件上会得到空列表，然后整本书被判定成没有正文——而且不报错，
        只表现为"打开这本书什么都没有"。
        """
        files = self.epub2_files()
        opf = str(files["OEBPS/content.opf"])
        self.assertIn('xmlns="http://www.idpf.org/2007/opf"', opf)
        self.assertIn("<dc:title>", opf)
        content = E.parse_epub(self.write(files))
        self.assertEqual(content.title, "测试书")
        self.assertEqual(len(content.segments), 2)

    def test_prefixed_opf_namespace_still_parses(self) -> None:
        """元素名带 opf: 前缀的 OPF（default namespace 的另一种写法）也要能读。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": PREFIXED_OPF,
            "OEBPS/text/ch1.xhtml": "<html><body><p>前缀正文。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(content.title, "前缀书名")
        self.assertEqual(content.author, "前缀作者")
        self.assertEqual(len(content.segments), 1)
        self.assertIn("前缀正文。", content.segments[0].text)


# ---------------------------------------------------------------------------
# 正文抽取
# ---------------------------------------------------------------------------


class EpubTextExtractionTest(EpubTestCase):
    def test_malformed_xhtml_still_yields_text(self) -> None:
        """没闭合标签的 XHTML 也要能读。

        老转换器产出的正文里 `<p>` 不闭合、`<div>` 交叉嵌套是常态。这里要是换成
        ElementTree，整本书直接 ParseError 打不开；HTMLParser 最坏只丢一个换行。
        """
        text = self.parse_one_body("<div><p>第一段<p>第二段<br>第三段</div>")
        self.assertEqual(body_lines(text), ["第一段", "第二段", "第三段"])

    def test_br_and_nested_blocks_produce_separate_lines(self) -> None:
        """`<br>` 和嵌套块级标签都要断行，否则正文会连成一行。"""
        text = self.parse_one_body(
            "<div>第一行<br/>第二行<p>第三行<blockquote>第四行</blockquote></p></div>"
        )
        self.assertEqual(body_lines(text), ["第一行", "第二行", "第三行", "第四行"])

    def test_entities_are_decoded(self) -> None:
        """`&amp;` `&#65;` `&nbsp;` 都要有合理结果。

        `&nbsp;` 统一变成普通空格：中文 EPUB 常用它做段首缩进，留着 U+00A0 会让
        每行尾部空白清理失效，阅读器的自动换行也会变得古怪。
        """
        text = self.parse_one_body("<p>甲&amp;乙&#65;&nbsp;丙</p><p>尖括号 &lt;标签&gt;</p>")
        self.assertIn("甲&乙A 丙", text)
        self.assertIn("<标签>", text)
        self.assertNotIn("\xa0", text)

    def test_script_style_and_title_content_is_dropped(self) -> None:
        """`<script>`/`<style>`/`<head>`/`<title>` 里的内容一律不进正文。"""
        text = self.parse_one_body(
            "<p>正文保留。</p><script>alert('脚本内容')</script><style>p{color:red}</style>",
            head="<title>书名不该出现在正文里</title>"
            "<style>body{font-size:12px}</style>"
            "<script>var 隐藏脚本 = 1;</script>",
        )
        self.assertIn("正文保留。", text)
        self.assertNotIn("脚本内容", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("font-size", text)
        self.assertNotIn("隐藏脚本", text)
        self.assertNotIn("书名不该出现在正文里", text)

    def test_missing_toc_falls_back_to_heading_then_numbered_section(self) -> None:
        """没有目录时的兜底顺序：正文里的小标题 > `<title>` > "第 N 节"。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("c1", "text/c1.xhtml"),
                    item_xml("c2", "text/c2.xhtml"),
                    item_xml("c3", "text/c3.xhtml"),
                ],
                spine=[ref_xml("c1"), ref_xml("c2"), ref_xml("c3")],
            ),
            "OEBPS/text/c1.xhtml": "<html><body><h1>只有小标题</h1><p>正文一。</p></body></html>",
            "OEBPS/text/c2.xhtml": "<html><head><title>文档标题</title></head>"
            "<body><p>正文二。</p></body></html>",
            "OEBPS/text/c3.xhtml": "<html><body><p>正文三。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(
            [segment.title for segment in content.segments],
            ["只有小标题", "文档标题", "第 3 节"],
        )


# ---------------------------------------------------------------------------
# 目录映射
# ---------------------------------------------------------------------------


class EpubTocTest(EpubTestCase):
    def test_toc_href_with_fragment_parent_dir_and_escape_resolves(self) -> None:
        """href 里的 `#锚点` 要拆掉，`../` 要按 nav 所在目录解析，`%20` 要还原。

        锚点混进文件名是目录项定位失败的头号原因：文件明明在包里，却一个目录项
        也匹配不上，最后整本书都变成"第 N 节"。
        """
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("nav", "nav/nav.xhtml", properties="nav"),
                    item_xml("c1", "text/ch1.xhtml"),
                    item_xml("c2", "text/ch2.xhtml"),
                    item_xml("c3", "text/ch 3.xhtml"),
                ],
                spine=[ref_xml("c1"), ref_xml("c2"), ref_xml("c3")],
            ),
            "OEBPS/nav/nav.xhtml": nav_xhtml(
                [
                    ("../text/ch1.xhtml#c1", "第一章 甲"),
                    ("../text/ch2.xhtml#第二节", "第二章 乙"),
                    ("../text/ch%203.xhtml", "第三章 丙"),
                ]
            ),
            "OEBPS/text/ch1.xhtml": "<html><body><p>正文一。</p></body></html>",
            "OEBPS/text/ch2.xhtml": "<html><body><p>正文二。</p></body></html>",
            "OEBPS/text/ch 3.xhtml": "<html><body><p>正文三。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(
            [segment.title for segment in content.segments],
            ["第一章 甲", "第二章 乙", "第三章 丙"],
        )

    def test_toc_entry_outside_spine_is_skipped(self) -> None:
        """目录项指向书脊之外的文档时跳过它并记提示，绝不猜位置。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("nav", "nav.xhtml", properties="nav"),
                    item_xml("c1", "text/ch1.xhtml"),
                    item_xml("cover", "cover.xhtml"),
                ],
                spine=[ref_xml("c1")],
            ),
            "OEBPS/nav.xhtml": nav_xhtml(
                [("cover.xhtml", "封面"), ("text/ch1.xhtml", "第一章 正文")]
            ),
            "OEBPS/text/ch1.xhtml": "<html><body><p>正文。</p></body></html>",
            "OEBPS/cover.xhtml": "<html><body><h1>封面</h1></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual([segment.title for segment in content.segments], ["第一章 正文"])
        self.assertTrue(
            any("定位不到" in note for note in content.notes),
            f"应该记一条无法定位的提示，实际是 {content.notes}",
        )

    def test_nested_nav_points_keep_document_order_and_indent(self) -> None:
        """NCX 里嵌套的 navPoint 要按文档顺序收进来，层级用全角空格缩进标出来。"""
        ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n1">
      <navLabel><text>第一章 甲</text></navLabel>
      <content src="text/c1.xhtml"/>
      <navPoint id="n1a">
        <navLabel><text>第一节 小标题</text></navLabel>
        <content src="text/c1a.xhtml"/>
      </navPoint>
    </navPoint>
    <navPoint id="n2">
      <navLabel><text>第二章 乙</text></navLabel>
      <content src="text/c2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("ncx", "toc.ncx", "application/x-dtbncx+xml"),
                    item_xml("c1", "text/c1.xhtml"),
                    item_xml("c1a", "text/c1a.xhtml"),
                    item_xml("c2", "text/c2.xhtml"),
                ],
                spine=[ref_xml("c1"), ref_xml("c1a"), ref_xml("c2")],
                version="2.0",
                toc="ncx",
            ),
            "OEBPS/toc.ncx": ncx,
            "OEBPS/text/c1.xhtml": "<html><body><p>正文一。</p></body></html>",
            "OEBPS/text/c1a.xhtml": "<html><body><p>正文一甲。</p></body></html>",
            "OEBPS/text/c2.xhtml": "<html><body><p>正文二。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(
            [segment.title for segment in content.segments],
            ["第一章 甲", "\u3000第一节 小标题", "第二章 乙"],
        )

    def test_image_only_page_is_skipped_and_noted(self) -> None:
        """整页只有图片的章节要跳过，并在提示里写明白（用户会以为少了内容）。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[
                    item_xml("c1", "text/ch1.xhtml"),
                    item_xml("pic", "text/pic.xhtml"),
                    item_xml("c2", "text/ch2.xhtml"),
                ],
                spine=[ref_xml("c1"), ref_xml("pic"), ref_xml("c2")],
            ),
            "OEBPS/text/ch1.xhtml": "<html><body><p>正文一。</p></body></html>",
            "OEBPS/text/pic.xhtml": "<html><body><div>"
            '<img src="../img/pic.png" alt="插图"/></div></body></html>',
            "OEBPS/text/ch2.xhtml": "<html><body><p>正文二。</p></body></html>",
            "OEBPS/img/pic.png": b"\x89PNG\r\n\x1a\n",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(len(content.segments), 2)
        self.assertIn("已忽略 1 张图片", content.notes)
        self.assertTrue(any("没有文字的页面" in note for note in content.notes))

    def test_all_image_book_raises(self) -> None:
        """一本书连一段文字都抽不出来时，报错而不是返回一个空书。"""
        files = self.one_doc_files('<div><img src="pic.png" alt="插图"/></div>')
        with self.assertRaises(E.EpubError) as ctx:
            E.parse_epub(self.write(files))
        self.assertIn("正文", str(ctx.exception))

    def test_linear_no_is_included_with_note(self) -> None:
        """linear="no" 的页面照收（少了它用户会以为内容缺了），只记一条提示。"""
        files = self.epub2_files()
        files["OEBPS/content.opf"] = make_opf(
            items=[
                item_xml("ncx", "toc.ncx", "application/x-dtbncx+xml"),
                item_xml("ch1", "text/ch1.xhtml"),
                item_xml("ch2", "text/ch2.xhtml"),
            ],
            spine=[ref_xml("ch1"), ref_xml("ch2", linear="no")],
            version="2.0",
            toc="ncx",
        )
        content = E.parse_epub(self.write(files))
        self.assertEqual(len(content.segments), 2)
        self.assertTrue(any("linear" in note for note in content.notes))


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


class EpubEncodingTest(EpubTestCase):
    def test_xml_declared_encoding_and_bom_are_honoured(self) -> None:
        """XML 声明里的 encoding（GBK 老书）和 BOM（多一个 \\ufeff）都要处理对。"""
        gbk_doc = (
            '<?xml version="1.0" encoding="GBK"?>\n'
            "<html><head><title>简体</title></head><body><p>简体中文可以读。</p></body></html>"
        )
        bom_doc = (
            codecs.BOM_UTF8
            + "<html><head><title>带 BOM</title></head><body><p>带 BOM 的一节。</p></body></html>".encode(
                "utf-8"
            )
        )
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
            "OEBPS/content.opf": make_opf(
                items=[item_xml("c1", "text/c1.xhtml"), item_xml("c2", "text/c2.xhtml")],
                spine=[ref_xml("c1"), ref_xml("c2")],
            ),
            "OEBPS/text/c1.xhtml": gbk_doc.encode("gbk"),
            "OEBPS/text/c2.xhtml": bom_doc,
        }
        content = E.parse_epub(self.write(files))
        self.assertIn("简体中文可以读。", content.segments[0].text)
        self.assertNotIn("\ufffd", content.segments[0].text)
        self.assertIn("带 BOM 的一节。", content.segments[1].text)
        self.assertNotIn("\ufeff", content.segments[1].text)


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


class EpubFailureTest(EpubTestCase):
    def test_missing_container_xml_falls_back_to_first_opf(self) -> None:
        """没有 container.xml 时退回压缩包里第一个 *.opf，并在提示里说明。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "OEBPS/book.opf": single_doc_opf(),
            "OEBPS/text/ch1.xhtml": "<html><body><p>没有 container 的书。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(content.title, "测试书")
        self.assertEqual(len(content.segments), 1)
        self.assertTrue(
            any("container.xml" in note for note in content.notes),
            f"应该说明是退而求其次找到的 OPF，实际是 {content.notes}",
        )

    def test_no_opf_at_all_raises(self) -> None:
        """压缩包里连 OPF 都没有时抛 EpubError。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "OEBPS/text/ch1.xhtml": "<html><body><p>正文。</p></body></html>",
        }
        with self.assertRaises(E.EpubError) as ctx:
            E.parse_epub(self.write(files))
        self.assertIn("OPF", str(ctx.exception))

    def test_non_zip_file_raises_epub_error(self) -> None:
        """把 txt 改名成 .epub 是最常见的误操作，必须给中文错误而不是 BadZipFile。"""
        path = os.path.join(self.tmp, "fake.epub")
        with open(path, "wb") as fh:
            fh.write("第一章 这不是 EPUB\n正文。\n".encode("utf-8"))
        with self.assertRaises(E.EpubError):
            E.parse_epub(path)
        self.assertFalse(E.looks_like_epub(path))

    def test_truncated_zip_raises_epub_error(self) -> None:
        """下载到一半的 EPUB：zip 中央目录在文件末尾，砍掉就再也读不出来。"""
        path = self.write(self.epub2_files())
        with open(path, "rb") as fh:
            data = fh.read()
        broken = os.path.join(self.tmp, "broken.epub")
        with open(broken, "wb") as fh:
            fh.write(data[: len(data) // 2])
        with self.assertRaises(E.EpubError):
            E.parse_epub(broken)

    def test_broken_archives_only_ever_raise_epub_error(self) -> None:
        """畸形输入只许抛 EpubError。

        界面层只会 catch 这一个异常；漏出 BadZipFile / KeyError / ParseError 就是
        一个崩溃弹窗。所以这里把各种"半坏不坏"的压缩包都喂一遍。
        """
        cases: list[tuple[str, dict[str, str | bytes]]] = [
            ("只有 mimetype", {"mimetype": MIMETYPE}),
            (
                "container 指向不存在的 OPF 且没有别的 OPF",
                {
                    "mimetype": MIMETYPE,
                    "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/没有这个.opf"),
                },
            ),
            (
                "container.xml 本身是垃圾",
                {"mimetype": MIMETYPE, "META-INF/container.xml": "<<<不是 XML"},
            ),
            (
                "OPF 是垃圾字节",
                {
                    "mimetype": MIMETYPE,
                    "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
                    "OEBPS/content.opf": "\x00\x01不是 XML",
                },
            ),
            (
                "OPF 合法但没有书脊",
                {
                    "mimetype": MIMETYPE,
                    "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
                    "OEBPS/content.opf": '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"/>',
                },
            ),
            (
                "书脊里的文件不在压缩包里",
                {
                    "mimetype": MIMETYPE,
                    "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/content.opf"),
                    "OEBPS/content.opf": single_doc_opf(),
                },
            ),
        ]
        for label, files in cases:
            with self.subTest(case=label):
                path = self.write(files, name="broken.epub")
                with self.assertRaises(E.EpubError):
                    E.parse_epub(path)

    def test_drm_encryption_is_refused_with_exact_message(self) -> None:
        """有 encryption.xml 就是加密书，直接拒绝，而且提示语要逐字对得上。"""
        files = self.epub2_files()
        files["META-INF/encryption.xml"] = ENCRYPTION_XML
        with self.assertRaises(E.EpubError) as ctx:
            E.parse_epub(self.write(files))
        self.assertEqual(str(ctx.exception), "这本 EPUB 有 DRM 加密，暂时打不开")

    def test_missing_title_falls_back_to_file_name(self) -> None:
        """书里没写书名时用 OPF 文件名顶上，并且如实告诉用户。"""
        files: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "META-INF/container.xml": CONTAINER_XML.format(opf="OEBPS/book.opf"),
            "OEBPS/book.opf": make_opf(
                title="", author="", items=[item_xml("ch1", "text/ch1.xhtml")], spine=[ref_xml("ch1")]
            ),
            "OEBPS/text/ch1.xhtml": "<html><body><p>没有书名的书。</p></body></html>",
        }
        content = E.parse_epub(self.write(files))
        self.assertEqual(content.title, "book")
        self.assertTrue(any("书名" in note for note in content.notes))


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------


class EpubDetectTest(EpubTestCase):
    def test_looks_like_epub_distinguishes_real_epub_and_txt(self) -> None:
        """识别函数：真 EPUB 为真，txt 为假，而且对不存在的路径也不抛异常。"""
        self.assertTrue(E.looks_like_epub(self.write(self.epub2_files())))

        no_container: dict[str, str | bytes] = {
            "mimetype": MIMETYPE,
            "OEBPS/book.opf": single_doc_opf(),
            "OEBPS/text/ch1.xhtml": "<html><body><p>正文。</p></body></html>",
        }
        self.assertTrue(E.looks_like_epub(self.write(no_container, name="nocon.epub")))

        txt = os.path.join(self.tmp, "novel.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("第一章 起点\n正文。\n")
        self.assertFalse(E.looks_like_epub(txt))
        self.assertFalse(E.looks_like_epub(os.path.join(self.tmp, "不存在.epub")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
