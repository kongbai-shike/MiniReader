"""HTML 阅读：单个 `.html`/`.htm` 文件读成书。

和 EPUB 的正文抽取共用同一个解析器（`epub._TextExtractor`），因为 EPUB 里的
每一章本来就是 XHTML——两套解析器迟早会漂移，然后同一段 HTML 在两条路上断行
不一样。这里只补 EPUB 路径不需要的三件事：

1. **编码要自己猜。** EPUB 的 XHTML 有 XML 声明兜底，单个 HTML 文件常常只有
   `<meta charset=gbk>`，甚至什么都没有（老的中文网页）。所以先看 `<meta>`
   声明，再用 `encoding.decode_txt()` 那套常用字密度打分。

2. **章节边界要自己找。** EPUB 用 spine + 目录，HTML 没有。这里把整份文档抽成
   纯文本后跑 `split_chapters_auto()`——和 TXT 完全一样（约束 24 的反面：HTML
   也没有可信的边界标记，所以退回到 TXT 的启发式是诚实的做法）。

3. **`<title>` 是书名。** `_TextExtractor.doc_title` 已经把 `<title>` 收好了。

**已知边界**：`<frameset>` 里的框架、`<script>` 生成的正文、CSS 隐藏的正文都不读；
一个 HTML 文件里的相对链接不追（那是"抓站"，不是"读书"）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .encoding import DecodeError, decode_txt
from .epub import Segment, _TextExtractor

__all__ = ["HtmlContent", "HtmlError", "parse_html"]

# `<meta charset="gbk">` 或 `<meta http-equiv="Content-Type" content="text/html;
# charset=gbk">`，两种写法都要认。属性顺序不固定，所以不写成一个正则。
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:+-]+)""", re.IGNORECASE
)


class HtmlError(Exception):
    """HTML 读不了。消息是中文，直接给用户看。"""


@dataclass(frozen=True)
class HtmlContent:
    """一个 HTML 文件读出来的东西，形状与 `EpubContent` 一致。"""

    title: str
    author: str
    segments: list[Segment]
    notes: list[str] = field(default_factory=list)


def _declared_charset(data: bytes) -> str:
    """`<meta>` 里声明的编码，没有则空串。只看文件前 4 KB。"""
    match = _META_CHARSET_RE.search(data[:4096])
    if not match:
        return ""
    return match.group(1).decode("ascii", errors="ignore").strip()


def _decode(data: bytes) -> tuple[str, str, list[str]]:
    """把字节解成文本，返回 (文本, 编码标签, 提示)。

    顺序：`<meta>` 声明 → `decode_txt()` 的 BOM/严格 UTF-8/常用字密度。
    声明优先是因为它是作者的话，比统计猜测可信；但声明写错的页面也真实存在
    （整站复制粘贴同一个模板），所以声明失败时**静默回退**到统计，不报错。
    """
    notes: list[str] = []
    declared = _declared_charset(data)
    if declared:
        try:
            text = data.decode(declared, errors="replace")
        except LookupError:
            notes.append(f"页面声明的编码 {declared} 不认识，改用自动判断")
        else:
            return text, declared.upper(), notes
    try:
        decoded = decode_txt(data)
    except DecodeError as exc:  # decode_txt 的兜底是 gb18030，实际很难走到
        raise HtmlError(str(exc)) from exc
    return decoded.text, decoded.label, notes + list(decoded.notes)


def looks_like_html(path: str | os.PathLike[str]) -> bool:
    """这个文件看起来是不是 HTML？只看开头几个字节，绝不抛异常。

    给拖放用：一个 `.txt` 里也可能写着 `<html>`，反过来一个 `.html` 也可能是纯
    文本。扩展名表已经能回答大部分情况，这个函数是给"后缀不认识但内容像网页"
    的兜底。
    """
    try:
        target = Path(path)
        if not target.is_file():
            return False
        with target.open("rb") as fh:
            head = fh.read(1024).lstrip().lower()
    except (OSError, TypeError, ValueError):
        return False
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:512]


def parse_html(path: str | os.PathLike[str]) -> HtmlContent:
    """把一个 HTML 文件读成书。任何失败都以 `HtmlError` 抛出，消息是中文。"""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise HtmlError(f"读不到这个文件：{exc.strerror or exc}") from exc
    if not data.strip():
        raise HtmlError("这个网页里没有可读的正文")

    text, label, notes = _decode(data)
    extractor = _TextExtractor()
    try:
        extractor.feed(text)
        extractor.close()
    except Exception:  # noqa: BLE001 - 与 `_extract()` 同一条理由：畸形 HTML 也照用已抽到的
        pass
    body = extractor.text
    if not body.strip():
        raise HtmlError("这个网页里没有可读的正文")

    segments = _segments_of(body)
    title = extractor.doc_title or extractor.first_heading
    if not title:
        title = os.path.splitext(os.path.basename(str(target)))[0]
    if label:
        notes.append(f"编码按 {label} 判断")
    return HtmlContent(title=title, author="", segments=segments, notes=notes)


def _segments_of(text: str) -> list[Segment]:
    """按 TXT 的启发式切章。

    章节标题同时留在正文第一行——`Book.chapter_body()` 的约定是"首行等于标题就
    剥掉"，所以这里不需要（也不该）自己动手删，否则标题会在正文里丢一次、
    在目录里出现一次，读者翻到正文第一行时会发现它没了。
    """
    from .chapters import split_chapters_auto

    result = split_chapters_auto(text)
    return [
        Segment(title=chapter.title, text=text[chapter.start : chapter.end])
        for chapter in result.chapters
    ]
