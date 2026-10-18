"""阶段 7：EPUB 固定版式（Fixed Layout）的检测、取页与分派。

固定版式和流式共用 `.epub` 这一个扩展名，区别只在 OPF 的 metadata 里。所以
这组测试的重点不是"能不能打开"，而是**同一个扩展名被分成两条路**：一条抽文字
（阶段 1 起就有，本阶段一行没改），一条取图片（本阶段新增）。

三个设计点值得先说明，因为它们决定了下面为什么这么测：

1. **判定必须在 `parse_epub()` 之前**。流式那条路对"整本都是图片"的书会报
   「这本书里没有能读的正文（可能整本都是图片）」——那对固定版式是误判，不是
   错误。所以 `opener.open_book()` 先问 `epub_layout()`（只读 OPF 清单，几 KB），
   再决定调哪条路。这里有一条测试专门盯着"固定版式的书不会走到流式那条路上"。

2. **判定失败一律按流式**。`epub_layout()` 绝不抛异常：坏文件应该由真正的打开
   路径用它的中文文案报出来，而不是在"判断能不能开"这一步变成另一句话。同一个
   坏文件，两条路必须给出同一句错话——这是阶段 6 就立下的规矩。

3. **图片字节核心层从不解码**。固定版式的一页就是一张（或几张）图片的 zip 成员
   名，`ComicBook.read_page()` 原样搬字节。所以下面的假图片全是 `b"\x89PNG..."`,
   反正没人会去解它——尺寸由 UI 的 `QImageReader` 探，和 `.cbz` 完全同路。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

from minireader.core.comic import ComicBook, open_epub_fixed
from minireader.core.epub import (
    LAYOUT_PRE_PAGINATED,
    LAYOUT_REFLOWABLE,
    EpubError,
    epub_layout,
    parse_epub,
    parse_fixed,
)
from minireader.core.formats import KIND_COMIC, KIND_TEXT
from minireader.core.model import BookError
from minireader.core.opener import open_book, open_book_kind

MIMETYPE = "application/epub+zip"

#: 一个成员名的内容无关紧要：核心层从不解码。用固定字节是为了让"这一页读到的
#: 是不是那张图"可以直接用等号断言，而不是去比对长度。
PIXEL_A = b"\x89PNG\r\n\x1a\n" + b"A" * 24
PIXEL_B = b"\x89PNG\r\n\x1a\n" + b"B" * 24
PIXEL_C = b"\x89PNG\r\n\x1a\n" + b"C" * 24

CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0"'
    ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="{opf}"'
    ' media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)


def as_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def write_zip(path: str, files: dict[str, str | bytes]) -> str:
    """把 {zip 内路径: 文本或字节} 写成 zip，返回路径。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if "mimetype" in files:
            zf.writestr(zipfile.ZipInfo("mimetype"), as_bytes(files["mimetype"]))
        for name, value in files.items():
            if name == "mimetype":
                continue
            zf.writestr(name, as_bytes(value))
    return path


def item_xml(item_id: str, href: str, media_type: str = "application/xhtml+xml") -> str:
    return f'    <item id="{item_id}" href="{href}" media-type="{media_type}"/>'


def ref_xml(idref: str) -> str:
    return f'    <itemref idref="{idref}"/>'


def make_opf(
    *,
    title: str = "固定版式测试书",
    author: str = "测试作者",
    meta: str = "",
    items: tuple[str, ...] | list[str] = (),
    spine: tuple[str, ...] | list[str] = (),
    progression: str = "",
    version: str = "3.0",
) -> str:
    """一个带命名空间的 OPF，`meta` 直接插进 metadata 里。

    `progression` 写在 spine 上而不是 metadata 里，因为规范就是那么定义的
    （`page-progression-direction`），这也正是它要单独取一次的原因。
    """
    direction = f' page-progression-direction="{progression}"' if progression else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf"'
        f' version="{version}" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{title}</dc:title>\n"
        f"    <dc:creator>{author}</dc:creator>\n"
        '    <dc:identifier id="bookid">urn:uuid:test</dc:identifier>\n'
        "    <dc:language>ja</dc:language>\n" + meta + "\n"
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(items) + "\n  </manifest>\n"
        f"  <spine{direction}>\n" + "\n".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )


def page_xhtml(*hrefs: str) -> str:
    """一页的 XHTML。一个 href 用 `<img>`，多个用 SVG 里的 `<image>`。

    这样两种写法都出现在测试样本里：单图页是最常见的情况，拼版页在真实书里
    通常就是 SVG 包着几张 image。
    """
    if len(hrefs) == 1:
        body = f'<img src="{hrefs[0]}"/>'
    else:
        inner = "".join(
            f'<image xlink:href="{href}" width="600" height="900"/>' for href in hrefs
        )
        body = (
            '<svg xmlns="http://www.w3.org/2000/svg"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink"'
            ' width="600" height="1800">' + inner + "</svg>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + "</body></html>\n"
    )


def fixed_files(
    pages: tuple[tuple[str, ...], ...] = (("img/p0.png",),),
    *,
    meta: str = '<meta property="rendition:layout">pre-paginated</meta>',
    progression: str = "",
) -> dict[str, str | bytes]:
    """一本固定版式 EPUB 的全部成员。

    `pages` 是"每一页由哪几张图组成"，所以拼版页写成 `(("a.png", "b.png"),)`。
    """
    files: dict[str, str | bytes] = {
        "mimetype": MIMETYPE,
        "META-INF/container.xml": CONTAINER.format(opf="OEBPS/content.opf"),
    }
    items = []
    spine = []
    for index, hrefs in enumerate(pages):
        doc = f"page{index}.xhtml"
        files["OEBPS/" + doc] = page_xhtml(*hrefs)
        items.append(item_xml(f"p{index}", doc))
        spine.append(ref_xml(f"p{index}"))
    #: 每张图给一段可分辨的字节，这样"读到的是不是那张图"能用等号断言。
    #: 按名字里的字母发，而不是按结尾：拼版页的两张图名字通常都不以数字结尾。
    pixels = {
        "a": PIXEL_A,
        "b": PIXEL_B,
        "c": PIXEL_C,
        "p0": PIXEL_A,
        "p1": PIXEL_B,
        "p2": PIXEL_C,
    }
    for hrefs in pages:
        for href in hrefs:
            stem = os.path.splitext(os.path.basename(href))[0]
            files["OEBPS/" + href] = pixels.get(stem, PIXEL_C)
    # 每张图都要在清单里登记成 image/*，否则"这个 href 是不是图片"就没有依据。
    seen = []
    for hrefs in pages:
        for href in hrefs:
            if href not in seen:
                seen.append(href)
                items.append(item_xml(href.replace("/", "-"), href, "image/png"))
    files["OEBPS/content.opf"] = make_opf(
        meta=meta, items=items, spine=spine, progression=progression
    )
    return files


def text_files() -> dict[str, str | bytes]:
    """一本普通流式 EPUB，用来证明阶段 7 没有改变它的行为。"""
    return {
        "mimetype": MIMETYPE,
        "META-INF/container.xml": CONTAINER.format(opf="OEBPS/content.opf"),
        "OEBPS/content.opf": make_opf(
            items=[item_xml("ch1", "ch1.xhtml")],
            spine=[ref_xml("ch1")],
        ),
        "OEBPS/ch1.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>第一章</h1><p>这里是可以读的正文。</p></body></html>"
        ),
    }


class FixedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mr-epub-fxl-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, files: dict[str, str | bytes], name: str = "book.epub") -> str:
        return write_zip(os.path.join(self.tmp, name), files)

    def fake(self, pages=((("img/p0.png",),)), name="book.epub", **kwargs) -> str:
        return self.write(fixed_files(pages, **kwargs), name)


# ---------------------------------------------------------------------------
# 1. 检测：五种信号 + 流式默认
# ---------------------------------------------------------------------------


class TestRenditionDetection(FixedTestCase):
    """`epub_layout()` 只读 OPF 的 metadata，回答 (layout, progression, spread)。"""

    def test_the_standard_property_is_read(self) -> None:
        """`<meta property="rendition:layout">pre-paginated</meta>` 是 EPUB3 标准写法。"""
        path = self.fake()
        layout, progression, spread = epub_layout(path)
        self.assertEqual(layout, LAYOUT_PRE_PAGINATED)
        self.assertEqual(progression, "ltr")
        self.assertEqual(spread, "auto")

    def test_an_explicit_reflowable_is_reflowable(self) -> None:
        """写明 reflowable 的书不能被当成固定版式。"""
        path = self.write(
            text_files() | {
                "OEBPS/content.opf": make_opf(
                    meta='<meta property="rendition:layout">reflowable</meta>',
                    items=[item_xml("ch1", "ch1.xhtml")],
                    spine=[ref_xml("ch1")],
                )
            }
        )
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)

    def test_the_old_apple_and_kobo_spelling_is_read(self) -> None:
        """`<meta name="fixed-layout" content="true"/>` 是 Apple/Kobo 的老约定。

        固定版式的书里有相当一部分是这个年代出的，只认 EPUB3 标准写法会漏掉
        它们，而漏掉的后果不是报错，是**整本书被当成"没有正文"打不开**。
        """
        path = self.fake(meta='<meta name="fixed-layout" content="true"/>')
        self.assertEqual(epub_layout(path)[0], LAYOUT_PRE_PAGINATED)

    def test_the_old_spelling_accepts_yes_and_one(self) -> None:
        """老约定的取值不止 true：yes 和 1 都见过。"""
        for value in ("true", "TRUE", "yes", "1"):
            with self.subTest(value=value):
                path = self.fake(
                    meta=f'<meta name="fixed-layout" content="{value}"/>',
                    name=f"book-{value}.epub",
                )
                self.assertEqual(epub_layout(path)[0], LAYOUT_PRE_PAGINATED)

    def test_a_false_fixed_layout_is_not_fixed(self) -> None:
        """`fixed-layout=false` 是在说"我不是固定版式"，不能反向理解。"""
        path = self.fake(meta='<meta name="fixed-layout" content="false"/>')
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)

    def test_book_type_comic_is_fixed(self) -> None:
        """`<meta name="book-type" content="comic"/>` 是漫画 EPUB 的旧约定。"""
        path = self.fake(meta='<meta name="book-type" content="comic"/>')
        self.assertEqual(epub_layout(path)[0], LAYOUT_PRE_PAGINATED)

    def test_book_type_novel_is_not_fixed(self) -> None:
        path = self.fake(meta='<meta name="book-type" content="novel"/>')
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)

    def test_the_standard_property_wins_over_the_old_spelling(self) -> None:
        """两种写法同时出现时，标准写法优先——它是新规范，旧约定只是兼容。"""
        meta = (
            '<meta property="rendition:layout">reflowable</meta>'
            '<meta name="fixed-layout" content="true"/>'
        )
        path = self.fake(meta=meta)
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)

    def test_page_progression_comes_from_the_spine(self) -> None:
        """`page-progression-direction` 在 spine 上，不在 metadata 里。"""
        path = self.fake(progression="rtl")
        self.assertEqual(epub_layout(path)[1], "rtl")

    def test_a_missing_progression_means_ltr(self) -> None:
        """没写就是从左往右，这是规范的默认值。"""
        path = self.fake()
        self.assertEqual(epub_layout(path)[1], "ltr")

    def test_an_unknown_progression_is_ignored(self) -> None:
        """规范只定义了 ltr 和 rtl；别的值不猜，按默认处理。"""
        path = self.fake(progression="sideways")
        self.assertEqual(epub_layout(path)[1], "ltr")

    def test_rendition_spread_is_read(self) -> None:
        """`rendition:spread` 决定双页策略，四个合法值都要原样带出来。"""
        for value in ("auto", "none", "landscape", "both"):
            with self.subTest(value=value):
                path = self.fake(
                    meta=(
                        '<meta property="rendition:layout">pre-paginated</meta>'
                        f'<meta property="rendition:spread">{value}</meta>'
                    ),
                    name=f"book-{value}.epub",
                )
                self.assertEqual(epub_layout(path)[2], value)

    def test_an_unknown_spread_falls_back_to_auto(self) -> None:
        """规范里没有的值按 auto 处理，不当作错误——多一个值不该让书打不开。"""
        path = self.fake(
            meta=(
                '<meta property="rendition:layout">pre-paginated</meta>'
                '<meta property="rendition:spread">sometimes</meta>'
            )
        )
        self.assertEqual(epub_layout(path)[2], "auto")

    def test_the_first_effective_layout_wins(self) -> None:
        """同一份 OPF 里重复声明时，第一次的有效值赢。

        真实书里重复声明的值是一样的，所以这条规则不会改变任何真实结果；它存在
        只是为了让"重复声明"有一个确定答案，而不是看字典顺序。
        """
        meta = (
            '<meta property="rendition:layout">pre-paginated</meta>'
            '<meta property="rendition:layout">reflowable</meta>'
        )
        path = self.fake(meta=meta)
        self.assertEqual(epub_layout(path)[0], LAYOUT_PRE_PAGINATED)


class TestDetectionNeverRaises(FixedTestCase):
    """`epub_layout()` 是分派层的前置判断，它绝不能成为新的错误来源。"""

    def test_a_missing_file_answers_reflowable(self) -> None:
        layout, progression, spread = epub_layout(os.path.join(self.tmp, "nope.epub"))
        self.assertEqual((layout, progression, spread), (LAYOUT_REFLOWABLE, "ltr", "auto"))

    def test_a_directory_answers_reflowable(self) -> None:
        self.assertEqual(epub_layout(self.tmp)[0], LAYOUT_REFLOWABLE)

    def test_a_non_zip_answers_reflowable(self) -> None:
        path = os.path.join(self.tmp, "fake.epub")
        with open(path, "wb") as fh:
            fh.write(b"this is not a zip at all")
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)

    def test_a_broken_opf_answers_reflowable(self) -> None:
        """OPF 不是合法 XML 时，判断这一步不报错——留给真正的打开路径去报。"""
        files = text_files()
        files["OEBPS/content.opf"] = "<package><metadata>"
        path = self.write(files, "broken.epub")
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)

    def test_a_drm_epub_answers_reflowable(self) -> None:
        """有 DRM 时不猜形态：让打开路径去报 DRM，那是用户能照着做的话。"""
        files = text_files()
        files["META-INF/encryption.xml"] = "<encryption/>"
        path = self.write(files, "drm.epub")
        self.assertEqual(epub_layout(path)[0], LAYOUT_REFLOWABLE)


# ---------------------------------------------------------------------------
# 2. 取页：parse_fixed() 的页序列
# ---------------------------------------------------------------------------


class TestFixedPages(FixedTestCase):
    def test_one_image_per_page(self) -> None:
        path = self.fake((("img/p0.png",), ("img/p1.png",)))
        content = parse_fixed(path)
        self.assertEqual(len(content.pages), 2)
        self.assertEqual([page.images for page in content.pages], [("OEBPS/img/p0.png",), ("OEBPS/img/p1.png",)])

    def test_the_image_path_is_resolved_against_the_page(self) -> None:
        """`href` 是相对那一页文档的，不是相对 OPF 的。

        这一页在 `OEBPS/page0.xhtml`，图片写的是 `img/p0.png`，所以最终路径是
        `OEBPS/img/p0.png`。用 OPF 的目录去解析也能碰巧对，但那是巧合：书脊项
        在子目录里时就会错。
        """
        path = self.fake()
        content = parse_fixed(path)
        self.assertEqual(content.pages[0].images[0], "OEBPS/img/p0.png")

    def test_a_parent_directory_href_is_resolved(self) -> None:
        """`../images/x.png` 这种写法必须按栈式解析，不能只拼字符串。"""
        files = fixed_files()
        files["OEBPS/page0.xhtml"] = page_xhtml("../images/p0.png")
        files["images/p0.png"] = PIXEL_C
        files["OEBPS/content.opf"] = make_opf(
            meta='<meta property="rendition:layout">pre-paginated</meta>',
            items=[
                item_xml("p0", "page0.xhtml"),
                item_xml("i0", "../images/p0.png", "image/png"),
            ],
            spine=[ref_xml("p0")],
        )
        path = self.write(files)
        self.assertEqual(parse_fixed(path).pages[0].images, ("images/p0.png",))

    def test_a_percent_encoded_href_is_decoded(self) -> None:
        """中文文件名在 href 里是逐字节转义的，不解码就找不到成员。"""
        files = fixed_files()
        files["OEBPS/page0.xhtml"] = page_xhtml("img/%E7%AC%AC1%E9%A1%B5.png")
        files["OEBPS/img/第1页.png"] = PIXEL_A
        path = self.write(files)
        self.assertEqual(parse_fixed(path).pages[0].images, ("OEBPS/img/第1页.png",))

    def test_the_page_name_comes_from_the_spine_document(self) -> None:
        """页的显示名取书脊项的文件名，因为它才是"第几页"的依据。"""
        path = self.fake((("img/p0.png",),))
        self.assertEqual(parse_fixed(path).pages[0].name, "page0.xhtml")

    def test_a_multi_image_page_keeps_every_image_in_order(self) -> None:
        """拼版页（一张跨页被切成两半）按文档顺序全部收下。

        顺序不能乱：纵向拼接时上半张必须在上。收下而不是丢掉，是因为丢掉一半
        比多显示一张更糟——用户会以为那页只有半张图。
        """
        path = self.fake((("img/a.png", "img/b.png"),))
        content = parse_fixed(path)
        self.assertEqual(len(content.pages), 1)
        self.assertEqual(content.pages[0].images, ("OEBPS/img/a.png", "OEBPS/img/b.png"))

    def test_a_page_without_images_is_skipped(self) -> None:
        """没有图片的书脊项（空白版权页）跳过，并记一条提示。

        收进来只会让用户翻到一片空白，而且翻页计数会把那一页算进去。
        """
        files = fixed_files((("img/p0.png",),))
        files["OEBPS/blank.xhtml"] = "<html><body></body></html>"
        files["OEBPS/content.opf"] = make_opf(
            meta='<meta property="rendition:layout">pre-paginated</meta>',
            items=[
                item_xml("p0", "page0.xhtml"),
                item_xml("b", "blank.xhtml"),
                item_xml("i0", "img/p0.png", "image/png"),
            ],
            spine=[ref_xml("p0"), ref_xml("b")],
        )
        path = self.write(files)
        content = parse_fixed(path)
        self.assertEqual(len(content.pages), 1)
        self.assertTrue(any("没有图片" in note for note in content.notes))

    def test_a_book_with_no_images_at_all_says_so(self) -> None:
        """一页图片都没有的固定版式书，错误文案要说清是"没有页面图片"。"""
        files = text_files()
        files["OEBPS/content.opf"] = make_opf(
            meta='<meta property="rendition:layout">pre-paginated</meta>',
            items=[item_xml("ch1", "ch1.xhtml")],
            spine=[ref_xml("ch1")],
        )
        path = self.write(files)
        with self.assertRaises(EpubError) as caught:
            parse_fixed(path)
        self.assertIn("没有找到任何页面图片", str(caught.exception))

    def test_the_progression_and_spread_survive_into_the_content(self) -> None:
        path = self.fake(
            meta=(
                '<meta property="rendition:layout">pre-paginated</meta>'
                '<meta property="rendition:spread">both</meta>'
            ),
            progression="rtl",
        )
        content = parse_fixed(path)
        self.assertEqual(content.progression, "rtl")
        self.assertEqual(content.spread, "both")

    def test_the_title_and_author_come_from_the_metadata(self) -> None:
        path = self.fake()
        content = parse_fixed(path)
        self.assertEqual(content.title, "固定版式测试书")
        self.assertEqual(content.author, "测试作者")

    def test_a_svg_image_without_the_xlink_prefix_is_read(self) -> None:
        """SVG2 允许直接写 `href`，新版 InDesign 就这么导。

        只认 `xlink:href` 会漏掉整页，而且漏得没有声音。
        """
        files = fixed_files()
        files["OEBPS/page0.xhtml"] = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<image href="img/p0.png" width="600" height="900"/></svg></body></html>'
        )
        path = self.write(files)
        self.assertEqual(parse_fixed(path).pages[0].images, ("OEBPS/img/p0.png",))

    def test_an_unclosed_image_tag_does_not_lose_the_page(self) -> None:
        """真实书里的 XHTML 未必良构；一页的容错不该毁掉整本书。"""
        files = fixed_files()
        files["OEBPS/page0.xhtml"] = (
            '<html><body><img src="img/p0.png"><p>unclosed'
        )
        path = self.write(files)
        self.assertEqual(parse_fixed(path).pages[0].images, ("OEBPS/img/p0.png",))

    def test_a_missing_image_member_still_yields_the_page(self) -> None:
        """图片登记在 XHTML 里但 zip 里没有：页还在，读字节时才失败。

        这样列页、算页数、显示"这一页坏了"都能正常工作，而不是整本书打不开。
        """
        files = fixed_files()
        files.pop("OEBPS/img/p0.png")
        path = self.write(files)
        content = parse_fixed(path)
        self.assertEqual(len(content.pages), 1)
        book = open_epub_fixed(path)
        with self.assertRaises(Exception):
            book.read_page(0)


# ---------------------------------------------------------------------------
# 3. 打开：ComicBook 的形状
# ---------------------------------------------------------------------------


class TestOpenFixed(FixedTestCase):
    def test_open_epub_fixed_returns_a_comic(self) -> None:
        book = open_epub_fixed(self.fake())
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.kind, KIND_COMIC)

    def test_the_source_is_epub(self) -> None:
        """`source` 决定 `read_page()` 从哪读字节，也决定界面上的那行说明。"""
        book = open_epub_fixed(self.fake())
        self.assertEqual(book.source, "epub")
        self.assertEqual(book.source_label, "固定版式 EPUB")

    def test_the_page_count_matches_the_spine(self) -> None:
        book = open_epub_fixed(self.fake((("img/p0.png",), ("img/p1.png",), ("img/p2.png",))))
        self.assertEqual(book.position_count, 3)
        self.assertEqual(book.chapter_count, 3)

    def test_reading_a_page_returns_the_image_bytes(self) -> None:
        """核心层原样搬字节，不解码——所以这里能用等号断言。"""
        book = open_epub_fixed(self.fake())
        self.assertEqual(book.read_page(0), PIXEL_A)

    def test_reading_a_page_head_returns_a_prefix(self) -> None:
        book = open_epub_fixed(self.fake())
        self.assertEqual(book.read_page_head(0, limit=8), PIXEL_A[:8])

    def test_reading_several_heads_keeps_them_aligned(self) -> None:
        """批量取页头必须走 zip 那条路，而不是把成员名当文件路径读。

        `ComicBook.read_page_heads()` 原先只对 `cbz` 走 `archives.read_member_heads()`，
        其余来源一律当普通文件。EPUB 也是 zip，于是每一页的"文件"都不存在，批量
        探测安静地返回一串空头——不报错，只让尺寸全部变成"未知"，而这正好是这条
        10x 路径存在的原因（避免每页开一次压缩包）。所以这条测试断的是字节内容，
        不是长度：`[b"", b""]` 和 `[PIXEL_A, PIXEL_B]` 长度不同才怪。
        """
        book = open_epub_fixed(self.fake((("img/p0.png",), ("img/p1.png",))))
        heads = book.read_page_heads([0, 1], limit=8)
        self.assertEqual(heads, [PIXEL_A[:8], PIXEL_B[:8]])

    def test_an_out_of_range_page_is_clamped(self) -> None:
        book = open_epub_fixed(self.fake())
        self.assertEqual(book.clamp(99), 0)

    def test_the_binding_comes_from_the_page_progression(self) -> None:
        """固定版式的装订方向是书自己说的，不是用户设置的。"""
        book = open_epub_fixed(self.fake(progression="rtl"))
        self.assertEqual(book.binding, "rtl")

    def test_a_left_bound_book_says_ltr(self) -> None:
        book = open_epub_fixed(self.fake(progression="ltr"))
        self.assertEqual(book.binding, "ltr")

    def test_a_book_without_a_progression_has_no_opinion(self) -> None:
        """没写方向的容器不猜——空串表示"按设置走"，而不是"ltr"。

        这一条和"书里写了 ltr"必须分开测：如果两者都回答 ltr，那么每一本没写
        方向的日漫 EPUB 都会把用户的右綴じ设置悄悄改掉，而用户会以为是设置没保存。
        """
        book = open_epub_fixed(self.fake())
        self.assertEqual(book.binding, "")

    def test_a_declared_ltr_is_an_opinion(self) -> None:
        """写明了 ltr 就是书的主张，和"什么都没说"不是一回事。"""
        book = open_epub_fixed(self.fake(progression="ltr"))
        self.assertEqual(book.binding, "ltr")

    def test_the_first_page_has_no_size_until_the_ui_probes_it(self) -> None:
        """核心层不许碰 Qt，所以尺寸只能由 UI 用 QImageReader 探。

        这和 `.cbz` 完全一致，也是为什么 `ComicPage.width` 从 0 开始。
        """
        book = open_epub_fixed(self.fake())
        self.assertFalse(book.pages[0].size_known)
        self.assertFalse(book.sizes_known())

    def test_a_multi_image_page_reports_its_count(self) -> None:
        book = open_epub_fixed(self.fake((("img/a.png", "img/b.png"),)))
        self.assertEqual(book.page_image_count(0), 2)
        self.assertEqual(book.page_image_refs(0), ("OEBPS/img/a.png", "OEBPS/img/b.png"))

    def test_a_single_image_page_reports_one(self) -> None:
        book = open_epub_fixed(self.fake())
        self.assertEqual(book.page_image_count(0), 1)

    def test_reading_one_image_of_a_multi_image_page(self) -> None:
        book = open_epub_fixed(self.fake((("img/a.png", "img/b.png"),)))
        self.assertEqual(book.read_page_image(0, 0), PIXEL_A)
        self.assertEqual(book.read_page_image(0, 1), PIXEL_B)

    def test_the_first_image_is_what_read_page_returns(self) -> None:
        """多图页的 `read_page()` 给第一张——"这一页代表什么"有一个答案。"""
        book = open_epub_fixed(self.fake((("img/a.png", "img/b.png"),)))
        self.assertEqual(book.read_page(0), PIXEL_A)

    def test_a_book_with_no_pages_is_refused(self) -> None:
        """空书脊要报错，而不是给一个 0 页的阅读器。"""
        files = text_files()
        files["OEBPS/content.opf"] = make_opf(
            meta='<meta property="rendition:layout">pre-paginated</meta>',
        )
        path = self.write(files)
        with self.assertRaises(BookError):
            open_epub_fixed(path)

    def test_a_missing_file_is_a_book_error(self) -> None:
        with self.assertRaises(BookError):
            open_epub_fixed(os.path.join(self.tmp, "nope.epub"))

    def test_the_epub_error_becomes_a_book_error(self) -> None:
        """`EpubError` 是核心层的内部类型；窗口只认 `BookError`。

        不转的话中文提示里会夹一个 Python 类名。
        """
        files = text_files()
        files["META-INF/encryption.xml"] = "<encryption/>"
        path = self.write(files, "drm.epub")
        with self.assertRaises(BookError) as caught:
            open_epub_fixed(path)
        self.assertIn("DRM", str(caught.exception))


# ---------------------------------------------------------------------------
# 4. 分派：同一个 .epub 扩展名，两条路
# ---------------------------------------------------------------------------


class TestDispatch(FixedTestCase):
    def test_a_fixed_layout_epub_opens_as_a_comic(self) -> None:
        book = open_book(self.fake())
        self.assertEqual(book.kind, KIND_COMIC)
        self.assertEqual(book.source, "epub")

    def test_a_reflowable_epub_still_opens_as_a_text_book(self) -> None:
        """阶段 7 最重要的回归：普通 EPUB 一个字都不许变。"""
        book = open_book(self.write(text_files()))
        self.assertEqual(book.kind, KIND_TEXT)
        self.assertEqual(book.title, "固定版式测试书")

    def test_the_text_path_is_not_touched(self) -> None:
        """固定版式的书**不会**走到 `parse_epub()` 那条路上。

        如果走了，它会得到「这本书里没有能读的正文」——那对它是误判。这条测试
        盯的就是分派顺序：先判形态，再选路径。
        """
        path = self.fake()
        with self.assertRaises(EpubError) as caught:
            parse_epub(path)
        self.assertIn("没有能读的正文", str(caught.exception))
        # 而 open_book 走的是另一条路，所以它成功。
        self.assertEqual(open_book(path).kind, KIND_COMIC)

    def test_open_book_kind_still_says_text_for_epub(self) -> None:
        """拖拽用的便宜判断不解析内容，所以它对所有 `.epub` 都报"文字书"。

        这是刻意的：`open_book_kind()` 要为拖进来的每个文件跑一遍，读 OPF 不在
        它的预算里。真正打开时 `open_book()` 会重新判断。两句话都对，只是回答
        的问题不同——一条测试盯着这个差异，免得以后有人"顺手统一"了它们。
        """
        self.assertEqual(open_book_kind(self.fake()), KIND_TEXT)

    def test_a_fixed_layout_epub_is_supported(self) -> None:
        """`.epub` 在格式表里是阶段 0，所以固定版式不需要动相位。"""
        from minireader.core.formats import is_supported

        self.assertTrue(is_supported(self.fake()))

    def test_the_extension_list_does_not_mention_fixed_layout(self) -> None:
        """固定版式不是新扩展名，所以格式表里不该出现第二个 `.epub` 行。"""
        from minireader.core.formats import FORMATS

        epub_rows = [fmt for fmt in FORMATS if ".epub" in fmt.exts]
        self.assertEqual(len(epub_rows), 1)


# ---------------------------------------------------------------------------
# 5. 与阶段 4/5 的接缝：装订方向、封面、双页
# ---------------------------------------------------------------------------


class TestStageFourAndFiveIntegration(FixedTestCase):
    def test_the_binding_reaches_the_spread_math(self) -> None:
        """`ComicBook.binding` 喂给 `visual_order()` 时，rtl 会把左右交换。"""
        from minireader.ui.spread import visual_order

        book = open_epub_fixed(self.fake(progression="rtl"))
        self.assertEqual(visual_order((1, 2), book.binding), (2, 1))

    def test_an_ltr_book_keeps_the_reading_order(self) -> None:
        from minireader.ui.spread import visual_order

        book = open_epub_fixed(self.fake(progression="ltr"))
        self.assertEqual(visual_order((1, 2), book.binding), (1, 2))

    def test_cover_alone_still_works_on_a_fixed_layout_book(self) -> None:
        """阶段 5 的封面判定读的是页名和页形，固定版式的书一样适用。"""
        from minireader.ui.spread import looks_like_a_cover

        book = open_epub_fixed(self.fake())
        # 名字里没有 cover，页形还不知道（核心层不探尺寸），所以回落到设置。
        self.assertTrue(looks_like_a_cover(book, True))
        self.assertFalse(looks_like_a_cover(book, False))

    def test_spread_starts_is_unaffected_by_the_source(self) -> None:
        from minireader.ui.spread import spread_starts

        book = open_epub_fixed(self.fake((("img/p0.png",), ("img/p1.png",), ("img/p2.png",))))
        self.assertEqual(spread_starts(len(book.pages), True), [0, 1])
        self.assertEqual(spread_starts(len(book.pages), False), [0, 2])


# ---------------------------------------------------------------------------
# 5b. 装订方向的三层来源（书 / 本书覆盖 / 设置）
# ---------------------------------------------------------------------------


class TestBindingLevels(unittest.TestCase):
    """一本书的方向有三个来源，优先级和"没写"的含义都必须固定下来。

    `ComicReader._binding_for()` 先问书、再问设置；`cycle_binding()` 记的是
    "本书覆盖"，不碰设置。三层的顺序错了，表现是"用户翻了方向、重开又变回去"，
    或者"翻一本日漫把所有书都翻了"——都很难从现象反推原因，所以在这里钉死。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, cls.ROOT)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from minireader.qt import QtWidgets

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def reader(self, binding: str = ""):
        from minireader.core.comic import ComicBook, ComicPage
        from minireader.ui.comic_reader import ComicReader

        book = ComicBook(
            id="x",
            title="t",
            path="t.epub",
            size=1,
            mtime=0.0,
            source="epub",
            pages=[ComicPage(index=i, ref=f"p{i}.png", name=f"第 {i + 1} 页") for i in range(4)],
            binding=binding,
        )
        reader = ComicReader()
        reader.set_binding("rtl")
        reader.show_book(book)
        return reader

    def test_the_book_beats_the_setting(self) -> None:
        reader = self.reader("ltr")
        self.assertEqual(reader.binding, "ltr")

    def test_a_book_with_no_opinion_falls_back_to_the_setting(self) -> None:
        reader = self.reader("")
        self.assertEqual(reader.binding, "rtl")

    def test_a_cycle_is_an_override_not_a_setting_change(self) -> None:
        reader = self.reader("rtl")
        reader.cycle_binding()
        self.assertEqual(reader.binding, "ltr")
        self.assertEqual(reader.binding_setting, "rtl", "设置不该被本书的点击改掉")

    def test_the_override_does_not_leak_to_the_next_book(self) -> None:
        """A direction picked for one book must not follow the user to the next."""
        from minireader.core.comic import ComicBook, ComicPage

        reader = self.reader("rtl")
        reader.cycle_binding()
        self.assertEqual(reader.binding, "ltr")
        other = ComicBook(
            id="y",
            title="u",
            path="u.epub",
            size=1,
            mtime=0.0,
            source="epub",
            pages=[ComicPage(index=0, ref="p.png", name="第 1 页")],
            binding="rtl",
        )
        reader.show_book(other)
        self.assertEqual(reader.binding, "rtl", "上一本的覆盖值跟过来了")

    def test_the_tip_says_where_the_direction_came_from(self) -> None:
        """三层来源必须能从提示里读出来，否则用户以为点击没生效。"""
        self.assertIn("书里声明", self.reader("rtl").binding_tip())
        self.assertIn("跟随设置", self.reader("").binding_tip())
        flipped = self.reader("rtl")
        flipped.cycle_binding()
        self.assertIn("本书手动指定", flipped.binding_tip())

    def test_the_button_label_follows_the_direction(self) -> None:
        reader = self.reader("rtl")
        self.assertEqual(reader.binding_label(), "右")
        reader.cycle_binding()
        self.assertEqual(reader.binding_label(), "左")


# ---------------------------------------------------------------------------
# 6. 核心层仍然 Qt-free
# ---------------------------------------------------------------------------


class TestCoreStaysQtFree(unittest.TestCase):
    """阶段 7 加的是核心层代码，所以它必须继续满足"核心层无 Qt"这条硬约束。"""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BANNED = ("PyQt5", "PySide6", "minireader.qt", "QtCore", "QtWidgets", "QtGui")

    def test_the_epub_module_does_not_import_qt(self) -> None:
        path = os.path.join(self.ROOT, "minireader", "core", "epub.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        for word in self.BANNED:
            self.assertNotIn(word, source)

    def test_the_comic_module_does_not_import_qt(self) -> None:
        path = os.path.join(self.ROOT, "minireader", "core", "comic.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        for word in self.BANNED:
            self.assertNotIn(word, source)

    def test_the_fixed_layout_path_never_decodes_an_image(self) -> None:
        """核心层不许解码：它连 Pillow 都不装，图片尺寸是 UI 的事。

        用 AST 扫标识符而不是找文本，理由和阶段 6 的"不解密"守卫一样——注释里
        提到 `QImage` 是在解释设计，不该被当成违规。
        """
        import ast

        banned = {"QImage", "QImageReader", "QPixmap", "Pillow", "PIL"}
        path = os.path.join(self.ROOT, "minireader", "core", "epub.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.alias):
                found.add((node.asname or node.name).split(".")[0])
        self.assertEqual(found & banned, set())


if __name__ == "__main__":
    unittest.main()
