"""EPUB 解析（只用标准库）。

EPUB 本质上就是一个 zip，里面装着 XML 清单、XHTML 正文和一个可选的目录文件。
标准库里的 zipfile / ElementTree / HTMLParser 足够把它拆成一节一节的纯文本，
所以这里没有任何第三方依赖——这一点和 minireader/core 的其它模块一样，是硬约定。

下面每一条都是被真实的书坑过之后才写下的，改之前请先看完：

* OPF 和 container.xml 几乎总是带命名空间的（`<package xmlns="...opf">`）。
  `root.findall("manifest")` 在真实文件上返回空列表，于是整本书被误判成"没有
  正文"。所以这里查找结构时一律比对标签的**本地名**（剥掉 `{命名空间}` 前缀），
  而不是拿带前缀的标签名去查。
* 正文用 HTMLParser，不用 ElementTree。真实 EPUB 里存在没闭合的 `<p>`、大小写
  混乱的标签、裸写的 `&`，ElementTree 遇到这些直接抛 ParseError，结果是
  "这本书能读"变成"这本书打不开"。HTMLParser 容错，最坏也只是少一个换行。
  目录文件仍然用 ElementTree：它们是机器生成的、格式规整，而且我们需要精确的
  父子层级关系。
* **块级标签必须变成换行**。只把标签当空气丢掉，`<p>第一段</p><p>第二段</p>`
  会变成"第一段第二段"，整章正文糊成一团。这是全文抽取里唯一真正要紧的细节。
* `<title>` 的内容要从正文里丢掉（否则每节开头都多一行书名），但它本身是目录
  缺失时最好的标题来源，所以一边丢、一边偷偷留下来。
* 目录项里的 `href` 带 `#锚点` 是常态。锚点指向文档**内部**的位置，不能当成
  文件名的一部分去找文件——这是目录项定位失败的头号原因，也是这里把它单独
  拆出来的原因。

对外只有两个函数：`looks_like_epub()` 用来快速识别，`parse_epub()` 负责真正解析。
所有失败都以 `EpubError` 抛出，消息是给用户看的中文；BadZipFile / KeyError /
ParseError 这些内部异常一律不许漏到界面上。
"""

from __future__ import annotations

import codecs
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

__all__ = ["EpubContent", "EpubError", "Segment", "looks_like_epub", "parse_epub"]

CONTAINER_PATH = "META-INF/container.xml"
ENCRYPTION_PATH = "META-INF/encryption.xml"
EPUB_MIMETYPE = b"application/epub+zip"

# 用户看到的原文。测试逐字断言，改这句话等于改产品的提示语。
DRM_MESSAGE = "这本 EPUB 有 DRM 加密，暂时打不开"

# 连续空行的上限。正文里的空行是有意义的（分段），但畸形 XHTML 动不动吐出几十个
# 换行，全留着会让阅读器里出现翻不到头的空白页。
MAX_BLANK_LINES = 2

# 这些标签的内容不是正文，整段丢掉。
_DROP_TAGS = frozenset({"script", "style", "head", "title"})

# 只有开始标签、没有结束标签的元素。HTMLParser 对 `<br/>` 会先调 handle_starttag
# 再调 handle_endtag；不在这里挡一下，一个 `<br>` 会变成两个换行，段落之间凭空
# 多出一个空行。
_VOID_TAGS = frozenset(
    {
        "br", "hr", "img", "image", "meta", "link", "input", "source",
        "area", "base", "col", "embed", "param", "track", "wbr",
    }
)
_BREAK_VOID_TAGS = frozenset({"br", "hr"})
_IMAGE_TAGS = frozenset({"img", "image"})

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# 块级标签：前后都要断开，否则段落会连成一行。前 13 个是必须的，后面一组是同一类
# 东西（表格、列表、语义化区块），漏掉它们会让表格和列表挤成一行。
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr",
        "blockquote", "section", "article",
        "table", "thead", "tbody", "tfoot", "td", "th", "ul", "ol", "dl",
        "dt", "dd", "pre", "figure", "figcaption", "header", "footer", "nav",
        "aside", "main", "form", "fieldset", "address", "center", "details",
        "summary", "caption", "body",
    }
)

_WS_RE = re.compile(r"\s+")
# XML 声明里的 encoding，只看文件开头。
_XML_DECL_RE = re.compile(
    rb"\A\s*<\?xml[^>]*?encoding\s*=\s*['\"]([A-Za-z0-9_.:+-]+)['\"]", re.IGNORECASE
)
_HEX_RE = re.compile(r"[0-9A-Fa-f]{2}")

# NCX 是递归结构，但真实目录不会很深；给个上限，畸形文件不至于把递归栈撑爆。
_MAX_NCX_DEPTH = 8


class EpubError(Exception):
    """用户能看懂的错误。"""


@dataclass(frozen=True)
class Segment:
    title: str          # 章节标题（来自目录，没有则回退到 "第 N 节"）
    text: str           # 该节的纯文本正文


@dataclass
class EpubContent:
    title: str
    author: str
    segments: list[Segment]
    notes: list[str] = field(default_factory=list)  # 给用户看的提示，例如 "已忽略 12 张图片"


# ---------------------------------------------------------------------------
# 小工具：zip 内路径、XML 本地名、字节解码
# ---------------------------------------------------------------------------


def _dirname(path: str) -> str:
    """zip 内路径的目录部分；没有目录时返回空串。"""
    idx = path.rfind("/")
    return path[:idx] if idx >= 0 else ""


def _percent_decode(text: str) -> str:
    """解开 href 里的 %XX。

    先拼字节再整体按 UTF-8 解码，而不是逐个 %XX 转成字符：中文文件名在 href 里
    是逐字节转义的（`%E7%AC%AC...`），逐字节转字符只会得到一串乱码。
    """
    if "%" not in text:
        return text
    out = bytearray()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "%" and i + 3 <= len(text) and _HEX_RE.fullmatch(text[i + 1 : i + 3]):
            out.append(int(text[i + 1 : i + 3], 16))
            i += 3
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _resolve(base_dir: str, href: str) -> tuple[str, str]:
    """把 href 解析成 (zip 内的绝对路径, 锚点片段)。

    href 里会出现反斜杠、`./`、`../`、百分号转义和 `#锚点`。片段必须单独返回：
    它指向文档内部的位置，混进文件名里就永远找不到文件。
    """
    raw = (href or "").strip().replace("\\", "/")
    path_part, _, fragment = raw.partition("#")
    path_part = _percent_decode(path_part)
    if not path_part:
        return "", fragment
    stack: list[str] = []
    if not path_part.startswith("/") and base_dir:
        stack = [part for part in base_dir.split("/") if part]
    for part in path_part.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    return "/".join(stack), fragment


def _local(tag: object) -> str:
    """标签的本地名：`{http://...opf}manifest` -> `manifest`。"""
    if not isinstance(tag, str):
        return ""  # 注释和 PI 的 tag 不是字符串
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _strip_ns(root: ET.Element) -> ET.Element:
    """把整棵树上每个标签的命名空间前缀就地剥掉。

    真实 OPF 一定带命名空间，而带前缀的标签名让 `findall("manifest")` 找不到任何
    东西（返回空列表，不报错），于是整本书变成"没有正文"。
    """
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.startswith("{"):
            element.tag = element.tag.split("}", 1)[1]
    return root


def _first_local(root: ET.Element, name: str) -> ET.Element | None:
    """按本地名找第一个后代元素（含自身）。

    用本地名而不是带前缀的完整标签名，即使调用方忘了先 _strip_ns() 也能找到。
    """
    for element in root.iter():
        if _local(element.tag) == name:
            return element
    return None


def _case_name(names: list[str], wanted: str) -> str:
    """在 zip 名单里按不区分大小写找一个名字，找不到就原样返回。"""
    low = wanted.lower()
    for name in names:
        if name.lower() == low:
            return name
    return wanted


def _squash(text: str) -> str:
    """把一段短文本压成一行：所有空白折叠成单个空格，首尾去掉。"""
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _clean_text(raw: str) -> str:
    """正文收尾清理。

    换行统一 -> NBSP 转普通空格 -> 每行去尾部空白 -> 连续 3 行以上空行压到 2 行
    -> 整体去首尾空白。

    NBSP 转空格是刻意的：中文 EPUB 常用 `&nbsp;` 做段首缩进，留着 U+00A0 会让
    行尾空白清理失效，也会让阅读器的自动换行表现古怪。
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    out: list[str] = []
    blanks = 0
    for line in text.split("\n"):
        line = line.rstrip(" \t\u3000")
        if line:
            blanks = 0
        else:
            blanks += 1
            if blanks > MAX_BLANK_LINES:
                continue
        out.append(line)
    return "\n".join(out).strip()


def _decode_xhtml(data: bytes) -> str:
    """把一节正文的原始字节解成文本。

    XHTML 名义上是 UTF-8，但现实里有 GBK 的老书，XML 声明里会老实写着
    encoding="gbk"。优先级：BOM > XML 声明 > UTF-8，并且始终 errors="replace"——
    一个坏字节不该让整本书打不开。
    """
    if not data:
        return ""
    # UTF-32 的 BOM 以 UTF-16 的 BOM 开头，必须先判断（见 encoding.py 同样的坑）。
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(encoding, errors="replace").lstrip("\ufeff")
    match = _XML_DECL_RE.match(data)
    if match:
        declared = match.group(1).decode("ascii", "replace")
        try:
            return data.decode(declared, errors="replace").lstrip("\ufeff")
        except LookupError:
            pass  # 声明了一个本机没有的编码名，退回 UTF-8
    return data.decode("utf-8", errors="replace").lstrip("\ufeff")


# ---------------------------------------------------------------------------
# 正文抽取
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """把一节 XHTML 变成纯文本，顺手记下 <title>、各级小标题和图片数量。

    用 HTMLParser 而不是 ElementTree 是刻意的：真实 EPUB 的正文里大量存在没闭合的
    `<p>`、错配的标签、裸写的 `&`，ElementTree 会因为它们直接抛 ParseError，于是
    "这本书能读"变成"这本书打不开"。HTMLParser 容错，坏标签最坏只是少一个换行。

    `<title>` 的内容不进正文，但它常常是目录缺失时最像样的标题，所以单独留下。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._drop: list[str] = []
        self._title_buf: list[str] = []
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []
        self.headings: list[str] = []
        self.doc_title = ""
        self.images = 0

    # ---- HTMLParser 回调 -------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._drop.append(tag)
            return
        if self._drop:
            return
        if tag in _VOID_TAGS:
            if tag in _IMAGE_TAGS:
                self.images += 1
            elif tag in _BREAK_VOID_TAGS:
                self._parts.append("\n")
            return
        if tag in _HEADING_TAGS and self._heading_tag is None:
            self._heading_tag = tag
            self._heading_buf = []
            self._parts.append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_TAGS:
            if tag == "title" and not self.doc_title and self._title_buf:
                self.doc_title = _squash("".join(self._title_buf))
                self._title_buf = []
            # 畸形文件里会出现没开始就结束的标签，或者交叉嵌套，所以弹出时一路弹到它。
            if tag in self._drop:
                while self._drop and self._drop.pop() != tag:
                    pass
            return
        if tag in _VOID_TAGS:
            return
        if self._drop:
            return
        if self._heading_tag == tag:
            title = _squash("".join(self._heading_buf))
            if title:
                self.headings.append(title)
            self._heading_tag = None
            self._heading_buf = []
            self._parts.append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop:
            if self._drop[-1] == "title":
                self._title_buf.append(data)
            return
        if self._heading_tag is not None:
            self._heading_buf.append(data)
        self._parts.append(data)

    # ---- 结果 -----------------------------------------------------------

    @property
    def first_heading(self) -> str:
        return self.headings[0] if self.headings else ""

    @property
    def text(self) -> str:
        return _clean_text("".join(self._parts))


def _extract(data: bytes) -> _TextExtractor:
    """跑一遍抽取器，返回已经填好结果的实例。

    HTMLParser 在极端畸形的输入上仍可能抛异常（例如越界的字符引用），这里兜住：
    已经抽到的正文照用，最多丢一节里剩下的部分，绝不能因此让整本书打不开。
    """
    extractor = _TextExtractor()
    try:
        extractor.feed(_decode_xhtml(data))
        extractor.close()
    except Exception:  # noqa: BLE001 - 抽取器的健壮性是刻意的，见 docstring
        pass
    return extractor


# ---------------------------------------------------------------------------
# 目录：EPUB3 nav / EPUB2 ncx
# ---------------------------------------------------------------------------


def _is_toc_nav(attrs: dict[str, str]) -> bool:
    kind = attrs.get("epub:type", "").lower()
    if "toc" in kind.split():
        return True
    return "doc-toc" in attrs.get("role", "").lower().split()


class _NavParser(HTMLParser):
    """从 EPUB3 的 nav 文档里取出目录项 (href, 标题)，保持文档顺序。

    只认 `<nav epub:type="toc">` 或 `<nav role="doc-toc">`：landmarks、page-list
    之类的导航同样是 `<nav>`，里面的 `<a>` 不是章节目录，照单全收会把"封面"、
    "版权页"、"书签"混进目录。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self._nav_depth = 0
        self._in_anchor = False
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_map = {key.lower(): (value or "") for key, value in attrs}
        if tag == "nav":
            if self._nav_depth:
                self._nav_depth += 1
            elif _is_toc_nav(attrs_map):
                self._nav_depth = 1
            return
        if self._nav_depth and tag == "a":
            self._in_anchor = True
            self._href = attrs_map.get("href", "")
            self._buf = []
        elif self._in_anchor and tag == "br":
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "nav":
            if self._nav_depth:
                self._nav_depth -= 1
            return
        if tag == "a" and self._in_anchor:
            title = _squash("".join(self._buf))
            if self._href and title:
                self.entries.append((self._href, title))
            self._in_anchor = False
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            self._buf.append(data)


def _parse_nav(data: bytes, base_dir: str) -> list[tuple[str, str]]:
    """nav 文档 -> [(zip 内路径, 标题)]。href 相对 nav 文档自己所在目录解析。"""
    parser = _NavParser()
    try:
        parser.feed(_decode_xhtml(data))
        parser.close()
    except Exception:  # noqa: BLE001 - 目录是尽力而为，坏了就退回下一套方案
        return []
    entries: list[tuple[str, str]] = []
    for href, title in parser.entries:
        path, _fragment = _resolve(base_dir, href)
        if path:
            entries.append((path, title))
    return entries


def _collect_nav_points(
    parent: ET.Element, base_dir: str, depth: int, out: list[tuple[str, str]]
) -> None:
    """递归收集 navPoint，保持文档顺序。

    层级用全角空格缩进放在标题里（第一章下面缩进一格），比丢掉层级信息更接近纸质
    目录的样子；深到 _MAX_NCX_DEPTH 就停，畸形文件不至于把递归栈撑爆。
    """
    if depth > _MAX_NCX_DEPTH:
        return
    for child in parent:
        if _local(child.tag) != "navPoint":
            continue
        label = ""
        src = ""
        nested: list[ET.Element] = []
        for sub in child:
            name = _local(sub.tag)
            if name == "navLabel" and not label:
                text_el = _first_local(sub, "text")
                if text_el is not None:
                    label = "".join(text_el.itertext())
            elif name == "content" and not src:
                src = (sub.get("src") or "").strip()
            elif name == "navPoint":
                nested.append(sub)
        path, _fragment = _resolve(base_dir, src)
        title = _squash(label)
        if path and title:
            out.append((path, "\u3000" * depth + title))
        _collect_nav_points(child, base_dir, depth + 1, out)


def _parse_ncx(data: bytes, base_dir: str, notes: list[str]) -> list[tuple[str, str]]:
    """EPUB2 的 toc.ncx -> [(zip 内路径, 标题)]。失败只记提示，不致命。"""
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        notes.append("目录文件（NCX）不是合法 XML，已改用正文里的标题")
        return []
    _strip_ns(root)
    nav_map = _first_local(root, "navMap")
    if nav_map is None:
        return []
    entries: list[tuple[str, str]] = []
    _collect_nav_points(nav_map, base_dir, 0, entries)
    return entries


# ---------------------------------------------------------------------------
# OPF 清单
# ---------------------------------------------------------------------------


def _read_metadata(package: ET.Element) -> tuple[str, str]:
    """书名和作者。dc:title / dc:creator 剥掉命名空间后就是 title / creator。"""
    title = ""
    author = ""
    for element in package.iter():
        name = _local(element.tag)
        if name == "title" and not title:
            title = _squash("".join(element.itertext()))
        elif name == "creator" and not author:
            author = _squash("".join(element.itertext()))
    return title, author


def _read_manifest(package: ET.Element, opf_path: str) -> tuple[dict[str, tuple[str, str]], str]:
    """清单：id -> (zip 内路径, media-type)，外加 EPUB3 的 nav 文档路径。

    href 是相对 OPF 自己所在目录的，所以在这里就解析成 zip 内的绝对路径，后面
    所有比对都只认绝对路径。
    """
    base_dir = _dirname(opf_path)
    manifest: dict[str, tuple[str, str]] = {}
    nav_path = ""
    manifest_el = _first_local(package, "manifest")
    if manifest_el is None:
        return manifest, nav_path
    for item in manifest_el:
        if _local(item.tag) != "item":
            continue
        item_id = (item.get("id") or "").strip()
        href = (item.get("href") or "").strip()
        media_type = (item.get("media-type") or "").strip()
        if not item_id or not href:
            continue
        path, _fragment = _resolve(base_dir, href)
        if not path:
            continue
        if "nav" in (item.get("properties") or "").split() and not nav_path:
            nav_path = path
        manifest[item_id] = (path, media_type)
    return manifest, nav_path


def _read_spine(
    package: ET.Element, manifest: dict[str, tuple[str, str]]
) -> tuple[list[tuple[str, str]], int, int]:
    """书脊：[(zip 内路径, media-type)]，外加 linear="no" 的条数和丢失引用的条数。

    linear="no" 表示"辅助页面"（封面、版权页），正规阅读器默认不在正文顺序里显示，
    但这里仍然收进来，只记一条提示：宁可多给一节，也不要让用户觉得内容缺了。
    """
    spine_el = _first_local(package, "spine")
    docs: list[tuple[str, str]] = []
    linear_no = 0
    missing = 0
    if spine_el is None:
        return docs, linear_no, missing
    for ref in spine_el:
        if _local(ref.tag) != "itemref":
            continue
        entry = manifest.get((ref.get("idref") or "").strip())
        if entry is None:
            missing += 1
            continue
        if (ref.get("linear") or "").strip().lower() == "no":
            linear_no += 1
        docs.append(entry)
    return docs, linear_no, missing


def _is_text_media(media_type: str) -> bool:
    """这个 media-type 值不值得当正文去解析。"""
    kind = media_type.strip().lower()
    if not kind:
        return True  # 没写的一律试一下，宁可多解析一次
    if kind.startswith(("image/", "audio/", "video/")):
        return False
    return "html" in kind or "xml" in kind or kind == "text/plain"


# ---------------------------------------------------------------------------
# zip 读取
# ---------------------------------------------------------------------------


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    """读一个必须存在的成员，读不到就抛用户能看懂的错误。"""
    try:
        return zf.read(name)
    except KeyError as exc:
        raise EpubError(f"EPUB 内部缺少文件：{name}") from exc
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise EpubError(f"读取 {name} 失败：{exc}") from exc


def _read_soft(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """读一个可有可无的成员，读不到返回 None（目录这类东西坏了不该让书打不开）。"""
    try:
        return zf.read(name)
    except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
        return None


def _find_opf(
    zf: zipfile.ZipFile, names: list[str], name_map: dict[str, str]
) -> tuple[str, list[str]]:
    """找到 OPF 清单的路径，并带回要告诉用户的提示。

    正常路径是 META-INF/container.xml -> <rootfile full-path="...">。这份文件缺失、
    损坏或者指向一个不存在的路径时，退回"压缩包里第一个 *.opf"，并且如实说明——
    一声不吭地猜会让用户以为这本书本来就长这样。
    """
    notes: list[str] = []
    container_name = name_map.get(CONTAINER_PATH.lower(), "")
    declared = ""
    if container_name:
        data = _read_soft(zf, container_name)
        if data is None:
            notes.append("container.xml 读不出来，已直接在压缩包里找 OPF")
        else:
            try:
                root = ET.fromstring(data)
                _strip_ns(root)
            except (ET.ParseError, ValueError):
                notes.append("container.xml 不是合法 XML，已直接在压缩包里找 OPF")
            else:
                for element in root.iter():
                    if _local(element.tag) != "rootfile":
                        continue
                    path, _fragment = _resolve("", (element.get("full-path") or "").strip())
                    if path:
                        declared = path
                        break
    else:
        notes.append("缺少 META-INF/container.xml，已直接在压缩包里找 OPF")

    if declared:
        actual = name_map.get(declared.lower())
        if actual:
            return actual, notes
        notes.append(f"container.xml 指向的 {declared} 不在压缩包里，已改找其它 OPF")

    for name in names:
        if name.lower().endswith(".opf"):
            return name, notes
    raise EpubError("这个 EPUB 里找不到 OPF 清单文件，可能不是完整的 EPUB")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def looks_like_epub(path: str | os.PathLike[str]) -> bool:
    """这个文件看起来是不是 EPUB？只看压缩包结构，不解析内容。

    给"打开文件"对话框和拖拽入口用：必须便宜，而且绝不能抛异常——猜错最多是
    多弹一次错误提示，抛异常则会把整个界面打断。
    """
    try:
        target = Path(path)
        if not target.is_file() or not zipfile.is_zipfile(target):
            return False
        with zipfile.ZipFile(target) as zf:
            names = zf.namelist()
            lowered = {name.lower() for name in names}
            if CONTAINER_PATH.lower() in lowered:
                return True
            if "mimetype" in lowered:
                try:
                    if zf.read(_case_name(names, "mimetype")).strip() == EPUB_MIMETYPE:
                        return True
                except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
                    pass
            return any(name.lower().endswith(".opf") for name in names)
    except (OSError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def parse_epub(path: str | os.PathLike[str]) -> EpubContent:
    """把一个 EPUB 读成书。

    任何失败都以 EpubError 抛出，消息是中文；zipfile / ElementTree / 解码层面的
    内部异常一个都不许漏出去。
    """
    try:
        return _parse(Path(path))
    except EpubError:
        raise
    except (
        zipfile.BadZipFile,
        KeyError,
        ET.ParseError,
        UnicodeDecodeError,
        OSError,
        RuntimeError,
        ValueError,
        IndexError,
    ) as exc:
        raise EpubError(f"这本 EPUB 读不了：{exc}") from exc


def _parse(path: Path) -> EpubContent:
    try:
        exists = path.exists()
        is_file = path.is_file()
    except OSError as exc:
        raise EpubError(f"读不到这个文件：{exc.strerror or exc}") from exc
    if not exists:
        raise EpubError(f"读不到这个文件：{path.name or path}")
    if not is_file:
        raise EpubError("这不是一个文件")
    if not zipfile.is_zipfile(path):
        raise EpubError("这不是一个 EPUB 文件（不是有效的压缩包）")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise EpubError("这个 EPUB 压缩包已损坏或下载不完整") from exc
    except OSError as exc:
        raise EpubError(f"打不开这个文件：{exc.strerror or exc}") from exc
    with zf:
        return _read_epub(zf)


def _read_epub(zf: zipfile.ZipFile) -> EpubContent:
    notes: list[str] = []
    names = zf.namelist()
    # zip 成员名区分大小写，但 href 里的大小写不一定跟它一致。建一份小写索引，
    # 按小写找真实名字，避免"文件明明在包里却报缺少文件"。
    name_map: dict[str, str] = {}
    for name in names:
        name_map.setdefault(name.lower(), name)

    if ENCRYPTION_PATH.lower() in name_map:
        raise EpubError(DRM_MESSAGE)

    opf_path, container_notes = _find_opf(zf, names, name_map)
    notes.extend(container_notes)
    package_bytes = _read_member(zf, opf_path)
    try:
        package = ET.fromstring(package_bytes)
    except (ET.ParseError, ValueError) as exc:
        raise EpubError("OPF 清单不是合法的 XML，这本书可能已经损坏") from exc
    _strip_ns(package)

    title, author = _read_metadata(package)
    if not title:
        title = Path(opf_path).stem or "未命名"
        notes.append("书里没有写书名，已用文件名代替")

    manifest, nav_path = _read_manifest(package, opf_path)
    spine, linear_no, missing_refs = _read_spine(package, manifest)
    if missing_refs:
        notes.append(f"有 {missing_refs} 个书脊条目在清单里找不到，已跳过")
    if linear_no:
        notes.append(f'有 {linear_no} 节标记为 linear="no"（辅助页面），已一并纳入')
    if not spine:
        raise EpubError("这本书的书脊是空的，找不到任何正文")

    entries = _read_toc(zf, names, package, manifest, nav_path, notes, name_map)
    title_by_spine, toc_notes = _map_toc(entries, spine)
    notes.extend(toc_notes)

    segments: list[Segment] = []
    images = 0
    empty_pages = 0
    skipped_pages = 0
    for index, (doc_path, media_type) in enumerate(spine):
        if not _is_text_media(media_type):
            skipped_pages += 1
            continue
        data = _read_soft(zf, name_map.get(doc_path.lower(), doc_path))
        if data is None:
            notes.append(f"压缩包里缺少文件：{doc_path}")
            continue
        extractor = _extract(data)
        images += extractor.images
        body = extractor.text
        if not body:
            # 只有图片的一页：收进去也只会得到一个空白节。
            empty_pages += 1
            continue
        segment_title = title_by_spine.get(index, "")
        if not segment_title:
            # 没有目录时，正文里的小标题比 <title> 更像章节名：很多 EPUB 的
            # <title> 写的是书名，每个文档都一样。
            segment_title = extractor.first_heading or extractor.doc_title
        if not segment_title:
            segment_title = f"第 {len(segments) + 1} 节"
        segments.append(Segment(segment_title, body))

    if skipped_pages:
        notes.append(f"已跳过 {skipped_pages} 个非文字页面")
    if images:
        notes.append(f"已忽略 {images} 张图片")
    if empty_pages:
        notes.append(f"已跳过 {empty_pages} 个没有文字的页面")
    if not segments:
        raise EpubError("这本书里没有能读的正文（可能整本都是图片）")

    return EpubContent(title=title, author=author, segments=segments, notes=notes)


def _read_toc(
    zf: zipfile.ZipFile,
    names: list[str],
    package: ET.Element,
    manifest: dict[str, tuple[str, str]],
    nav_path: str,
    notes: list[str],
    name_map: dict[str, str],
) -> list[tuple[str, str]]:
    """按 EPUB3 nav -> EPUB2 ncx 的顺序尽力拿目录。

    两套都没有就返回空列表，由调用方退回"正文里的小标题"。
    """
    if nav_path:
        data = _read_soft(zf, name_map.get(nav_path.lower(), nav_path))
        if data is not None:
            entries = _parse_nav(data, _dirname(nav_path))
            if entries:
                return entries
    ncx_path = _find_ncx(names, package, manifest)
    if ncx_path:
        data = _read_soft(zf, name_map.get(ncx_path.lower(), ncx_path))
        if data is not None:
            return _parse_ncx(data, _dirname(ncx_path), notes)
    return []


def _find_ncx(
    names: list[str], package: ET.Element, manifest: dict[str, tuple[str, str]]
) -> str:
    """找 NCX：先看书脊的 toc 属性，再看 media-type，最后扫一遍 .ncx。"""
    spine_el = _first_local(package, "spine")
    if spine_el is not None:
        toc_id = (spine_el.get("toc") or "").strip()
        if toc_id in manifest:
            return manifest[toc_id][0]
    for path, media_type in manifest.values():
        if "dtbncx" in media_type.lower():
            return path
    for name in names:
        if name.lower().endswith(".ncx"):
            return name
    return ""


def _map_toc(
    entries: list[tuple[str, str]], spine: list[tuple[str, str]]
) -> tuple[dict[int, str], list[str]]:
    """把目录项映射到书脊下标，返回 (下标 -> 标题, 提示)。

    映射不上的目录项直接丢掉并记一条提示，绝不猜：目录项可能指向书脊之外的文档
    （封面、附录），也可能指向某份文档的中间。把"猜错的位置"当成章节边界，比
    少一个目录项难查得多。同一份文档有多个目录项时保留第一个——没有锚点信息就
    切不开文档，合并比乱序更诚实。
    """
    index_of: dict[str, int] = {}
    for index, (doc_path, _media) in enumerate(spine):
        index_of.setdefault(doc_path, index)
    title_by_spine: dict[int, str] = {}
    unmapped = 0
    duplicated = 0
    for doc_path, entry_title in entries:
        index = index_of.get(doc_path)
        if index is None:
            unmapped += 1
            continue
        if index in title_by_spine:
            duplicated += 1
            continue
        title_by_spine[index] = entry_title
    notes: list[str] = []
    if unmapped:
        notes.append(f"有 {unmapped} 个目录项定位不到正文，已忽略")
    if duplicated:
        notes.append(f"有 {duplicated} 个目录项指向同一节，已合并到该节标题")
    return title_by_spine, notes
