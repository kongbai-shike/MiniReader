"""DOCX 阅读：从 `word/document.xml` 里把段落抠出来。

DOCX 是一个 zip（OPC 容器），正文在 `word/document.xml`。和 EPUB 不同，它**没有
章节概念**——Word 的"标题 1"样式是排版信息，不是结构信息，而且绝大多数 DOCX
根本不带它。所以这里的章节边界按段落数切（`_PARAGRAPHS_PER_CHAPTER`），并且
明确写进 notes 告诉读者这是猜的。

四件事：

1. **只读 `word/document.xml`，不碰 `word/styles.xml`。** 判断"这一段是不是标题"
   需要解析样式继承链（`w:pStyle` → `styles.xml` 的 `w:basedOn`），几百行代码
   换一个"也许更准"的目录。DOCX 的定位是"能读"，不是"读得和 Word 一样"。

2. **段落边界 = `</w:p>`。** 用 `itertext()` 拿一个段落里的全部文字，它会自动
   跳过 `<w:t>` 之外的东西（`<w:instrText>` 域代码、`<w:delText>` 修订删除）。
   但 `itertext()` **也**会把域代码和删除的文字带出来，所以这里先按标签过滤。

3. **表格里的文字也要。** 表格在 `w:tbl > w:tr > w:tc`，里面还是 `w:p`。用
   `iter()` 找所有 `w:p` 而不是只找 `w:body` 的直接子元素，表格自然就被包含。

4. **段落数切章是刻意的粗糙。** 一本 300 页的 DOCX 切成 40 段一"章"，读者至少
   能在目录里跳；不切就是一章 300 页，滚动条全是刻度。notes 里说明这一点。

**已知边界**：`.doc`（97-2003 二进制格式）不支持，扩展名表里也不登记；页码、
页眉页脚、脚注都不读。
"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .epub import Segment, _squash

__all__ = ["DocxContent", "DocxError", "parse_docx"]

# 一个 DOCX 的正文有多大。压缩比很高（XML 冗余），所以 64 MB 的 XML 能装下
# 一本几十万字的书；超过就当坏文件，免得 `read()` 把内存吃光。
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024

# 段落数切章。40 段大约是一屏半到两屏的阅读量，目录长度也在可接受范围
# （10 万字的书 → 约 25 章）。
PARAGRAPHS_PER_CHAPTER = 40

_DOCUMENT_PATH = "word/document.xml"

# 修订删除的文字、域代码、批注引用，都不是正文。
_SKIP_TAGS = frozenset({"delText", "instrText", "delInstrText", "commentReference"})

# 这些标签的文字虽然不在 `w:p` 里，但确实是正文（脚注、尾注会以引用形式出现，
# 这里不追；`w:tab`/`w:br` 在 `w:p` 内部已经由 `itertext` 之外的逻辑处理）。
_TAB_TAG = "tab"
_BREAK_TAG = "br"


class DocxError(Exception):
    """DOCX 读不了。消息是中文，直接给用户看。"""


@dataclass(frozen=True)
class DocxContent:
    """一本 DOCX 读出来的东西，形状与 `EpubContent` 一致。"""

    title: str
    author: str
    segments: list[Segment]
    notes: list[str] = field(default_factory=list)


def _local(tag: object) -> str:
    """去掉命名空间的小写标签名。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _paragraph_text(paragraph: ET.Element) -> str:
    """一个 `w:p` 里的文字，制表符变空格、换行变空格。"""
    parts: list[str] = []
    for node in paragraph.iter():
        tag = _local(node.tag)
        if tag in _SKIP_TAGS:
            continue
        if tag == _TAB_TAG:
            parts.append(" ")
        elif tag == _BREAK_TAG:
            parts.append(" ")
        elif tag == "t" and node.text:
            parts.append(node.text)
    return _squash("".join(parts))


def _paragraphs(root: ET.Element) -> list[str]:
    """所有非空段落，按文档顺序。表格里的段落也在内。"""
    out: list[str] = []
    for node in root.iter():
        if _local(node.tag) != "p":
            continue
        text = _paragraph_text(node)
        if text:
            out.append(text)
    return out


def _chapters_of(paragraphs: list[str]) -> list[Segment]:
    """按段落数把段落切成段。

    段标题刻意留空——DOCX 没有可信的标题，编一个"第 N 章"会让目录看起来像
    书的目录而其实是行号。`Book.from_segments()` 会给空标题补"章节 N"。
    """
    if not paragraphs:
        return []
    if len(paragraphs) <= PARAGRAPHS_PER_CHAPTER:
        return [Segment(title="", text="\n".join(paragraphs))]
    segments: list[Segment] = []
    for start in range(0, len(paragraphs), PARAGRAPHS_PER_CHAPTER):
        chunk = paragraphs[start : start + PARAGRAPHS_PER_CHAPTER]
        segments.append(Segment(title="", text="\n".join(chunk)))
    return segments


_CORE_RE = re.compile(r"<dc:(?P<tag>title|creator)[^>]*>(?P<text>.*?)</dc:(?P=tag)>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _core_meta(zf: zipfile.ZipFile) -> tuple[str, str]:
    """从 `docProps/core.xml` 取 (标题, 作者)。取不到给空串。

    `core.xml` 是 Dublin Core 的简单 XML，这里用正则而不是 ET：它可能带任意
    命名空间前缀（`dc:` / `dcterms:` / 无前缀），为两个字段写一棵命名空间树
    不值得；而且它是元数据，读错了最多是标题不好看。
    """
    try:
        raw = zf.read("docProps/core.xml")
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
        return "", ""
    text = raw.decode("utf-8", errors="replace")
    found: dict[str, str] = {}
    for match in _CORE_RE.finditer(text):
        key = match.group("tag")
        if key in found:
            continue
        value = _squash(_TAG_RE.sub("", match.group("text")))
        if value:
            found[key] = value
    return found.get("title", ""), found.get("creator", "")


def looks_like_docx(path: str | os.PathLike[str]) -> bool:
    """这个文件看起来是不是 DOCX？只看容器，不解析内容，绝不抛异常。

    判据是"zip 里有 `word/document.xml`"。这比查扩展名可靠：`.docx` 后缀也可能是
    改名的 zip 或坏文件，而一个 `.zip` 里正好装着 DOCX 的情况也真实存在。
    """
    try:
        target = Path(path)
        if not target.is_file() or not zipfile.is_zipfile(target):
            return False
        with zipfile.ZipFile(target) as zf:
            lowered = {name.lower() for name in zf.namelist()}
            return _DOCUMENT_PATH in lowered
    except (OSError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def parse_docx(path: str | os.PathLike[str]) -> DocxContent:
    """把一个 DOCX 读成章节。任何失败都以 `DocxError` 抛出，消息是中文。"""
    target = Path(path)
    try:
        with zipfile.ZipFile(target) as zf:
            try:
                info = zf.getinfo(_DOCUMENT_PATH)
            except KeyError as exc:
                raise DocxError("这个文件里没有 word/document.xml，可能不是 DOCX") from exc
            if info.file_size > MAX_DOCUMENT_BYTES:
                raise DocxError(
                    f"这个 DOCX 的正文太大（{info.file_size // (1024 * 1024)} MB），读不了"
                )
            try:
                raw = zf.read(info)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise DocxError(f"这个 DOCX 已损坏：{exc}") from exc
            title, author = _core_meta(zf)
    except DocxError:
        raise
    except OSError as exc:
        raise DocxError(f"读不到这个文件：{exc.strerror or exc}") from exc
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise DocxError(f"这个文件不是 DOCX：{exc}") from exc

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DocxError(f"这个 DOCX 的正文读不了：{exc}") from exc

    paragraphs = _paragraphs(root)
    if not paragraphs:
        raise DocxError("这个 DOCX 里没有可读的正文")

    segments = _chapters_of(paragraphs)
    notes: list[str] = []
    if len(segments) > 1:
        notes.append(f"DOCX 没有章节，按每 {PARAGRAPHS_PER_CHAPTER} 段切成一节")
    if not title:
        title = os.path.splitext(os.path.basename(str(target)))[0]
    return DocxContent(title=title, author=author, segments=segments, notes=notes)
