"""FB2 阅读：把 FictionBook 的 XML 读成章节。

FB2 是俄罗斯的电子书格式，一个文件就是一个 XML 文档（图片以 base64 内嵌在
`<binary>` 里）。它自带结构——`<body>` 里的 `<section>` 可以任意嵌套，每个
`<section>` 有可选的 `<title>`——所以和 EPUB 一样，章节边界是书给的，不该再跑
TXT 那套正则（约束 24：源格式的边界标记 ≠ 读者的章节边界）。

四件事值得写下来：

1. **只认第一个没有 name 的 `<body>`。** FB2 允许一个文件里放多个 body，注释和
   尾注放在 `<body name="notes">` 里。把注释当正文读，读者会在第 3 章中间读到
   一整页译者注。

2. **每个顶层 `<section>` 是一章，嵌套的 `<section>` 不另起一章。** 嵌套的标题
   会作为普通的一行留在正文里。理由：FB2 的嵌套常常是"章 → 节"，把每一层都摊平
   成章节会让一本 12 章的书在目录里出现 60 项；而丢掉嵌套标题又会让读者看不出
   节的边界。折中是"章是章节、节是文字"。

3. **空白规则与 EPUB 共用**（`_squash` / `_clean_text`），不是抄一份。同一段文字
   在 EPUB 与 FB2 里必须断行一致，两份正则迟早会漂移。

4. **XML 由解析器按文件自己的声明解码。** FB2 常见 `windows-1251`，所以这里把
   **字节**直接喂给 `ET.fromstring`，让它读 `<?xml ... encoding="..."?>`，而不是
   先猜编码再解析——先猜会把一本俄文书变成问号。

**已知边界**：`xml.etree` 会展开内部实体，理论上一个精心构造的 FB2 可以让内存
爆掉。EPUB 的 OPF/XHTML 从阶段 3 起走的就是同一个解析器，所以这不是新引入的
风险；要堵它得引入 `defusedxml`，而第二批的约束是零新依赖。
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .epub import Segment, _clean_text, _squash

__all__ = ["Fb2Content", "Fb2Error", "parse_fb2"]


class Fb2Error(Exception):
    """FB2 读不了。消息是中文，直接给用户看。"""


@dataclass(frozen=True)
class Fb2Content:
    """一本 FB2 读出来的东西，形状与 `EpubContent` 一致。"""

    title: str
    author: str
    segments: list[Segment]
    notes: list[str] = field(default_factory=list)


# 正文里当成"一行文字"的元素。`p` 是段落，`subtitle` 是节内小标题，`v` 是诗的
# 一行，`text-author` 是引文的署名，`date` 是日期。刻意不含 `cite`/`poem`/
# `epigraph` 这类容器——它们的文字在里面的 `p`/`v` 上，列进来会重复一遍。
_TEXT_TAGS = frozenset({"p", "subtitle", "v", "text-author", "date"})


def _local(tag: object) -> str:
    """去掉命名空间的小写标签名。注释/PI 的 tag 不是 str，返回空串。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _all_text(el: ET.Element | None) -> str:
    """一个元素下所有文字，压成一行。"""
    if el is None:
        return ""
    return _squash("".join(el.itertext()))


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in el if _local(child.tag) == name]


def _lines(el: ET.Element, skip: ET.Element | None = None) -> list[str]:
    """`el` 下面所有可读的行，按文档顺序。

    `skip` 是"这个元素的文字已经单独用掉了"的出口：章节自己的 `<title>` 会变成
    段标题，再在正文里出现一次就成了重复的第一行。
    """
    out: list[str] = []
    for child in el:
        if child is skip:
            continue
        tag = _local(child.tag)
        if tag in _TEXT_TAGS:
            text = _all_text(child)
            if text:
                out.append(text)
        elif tag == "empty-line":
            out.append("")
        elif tag == "title":
            # 嵌套 section 的标题：留成一行，读者才看得出节的边界。
            text = _all_text(child)
            if text:
                out.append(text)
        elif tag == "binary":
            # base64 图片，不是文字。
            continue
        else:
            out.extend(_lines(child))
    return out


def _text_of(el: ET.Element, skip: ET.Element | None = None) -> str:
    return _clean_text("\n".join(_lines(el, skip)))


def _title_of(el: ET.Element) -> str:
    return _all_text(_child(el, "title"))


def _author_of(el: ET.Element) -> str:
    """`<author>` 里的名字拼成一行。FB2 把姓名拆成好几个元素。"""
    parts = []
    for name in ("first-name", "middle-name", "last-name", "nickname"):
        text = _all_text(_child(el, name))
        if text:
            parts.append(text)
    return " ".join(parts)


def _description(root: ET.Element) -> tuple[str, str]:
    """(书名, 作者) 从 `<description><title-info>` 取；取不到给空串。"""
    desc = _child(root, "description")
    if desc is None:
        return "", ""
    info = _child(desc, "title-info")
    if info is None:
        return "", ""
    title = _all_text(_child(info, "book-title"))
    authors = _children(info, "author")
    author = _author_of(authors[0]) if authors else ""
    return title, author


def _main_body(root: ET.Element) -> ET.Element | None:
    """没有 name 的那个 `<body>`（就是正文）。

    有 name 的 body 是注释、尾注之类；一个都没有 name 时退回第一个。
    """
    bodies = _children(root, "body")
    for body in bodies:
        if not body.get("name", "").strip():
            return body
    return bodies[0] if bodies else None


def _section_segment(section: ET.Element) -> Segment:
    title_el = _child(section, "title")
    return Segment(title=_all_text(title_el), text=_text_of(section, skip=title_el))


def _segments_of(body: ET.Element) -> list[Segment]:
    sections = _children(body, "section")
    if not sections:
        # 规范允许 body 直接放 <p>。很少见，但读不出正文就报错比读成空书好。
        title_el = _child(body, "title")
        return [Segment(title=_all_text(title_el), text=_text_of(body, skip=title_el))]
    out: list[Segment] = []
    for section in sections:
        segment = _section_segment(section)
        if segment.text or segment.title:
            out.append(segment)
    return out


def parse_fb2(path: str | os.PathLike[str]) -> Fb2Content:
    """把一个 FB2 读成章节。任何失败都以 `Fb2Error` 抛出，消息是中文。"""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise Fb2Error(f"读不到这个文件：{exc.strerror or exc}") from exc
    if not data.strip():
        raise Fb2Error("这本 FB2 里没有可读的正文")

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise Fb2Error(f"这本 FB2 读不了：{exc}") from exc

    body = _main_body(root)
    if body is None:
        raise Fb2Error("这本 FB2 里没有正文（找不到 <body>）")

    segments = _segments_of(body)
    if not any(segment.text for segment in segments):
        raise Fb2Error("这本 FB2 里没有可读的正文")

    title, author = _description(root)
    if not title:
        title = _title_of(body)
    notes: list[str] = []
    if len(_children(root, "body")) > 1:
        notes.append("这个文件里还有注释/尾注，没有当正文读")
    return Fb2Content(title=title, author=author, segments=segments, notes=notes)
