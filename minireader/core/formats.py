"""Which file extensions MiniReader knows, and what each one turns into.

This table is the single source of truth. Everything that used to hard-code
`.txt` - the drag-and-drop filter, the open dialog's file-type list, the reader
selection - reads it from here instead, so adding a format later means editing
one tuple rather than hunting for string literals.

Two things worth knowing:

1. `.pdf` and `.epub` appear once each but are DUAL-IDENTITY: a PDF can be a
   text document or a scanned comic, and an EPUB can be reflowable text or
   fixed-layout pages. `kind` here is only the *default* guess; `open_book()`
   sniffs the content and may hand back the other kind.

2. `phase` records which delivery phase introduced the format. It is not used
   for dispatch - it exists so the settings dialog and the file dialog can say
   "还没有支持" for a format that is listed but not yet implemented, instead of
   silently offering the user a filter that opens nothing.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .book_base import KIND_COMIC, KIND_TEXT


@dataclass(frozen=True)
class Format:
    """One row of the table: a set of extensions and what they open as."""

    kind: str
    exts: tuple[str, ...]
    label: str
    phase: int

    @property
    def ready(self) -> bool:
        """True once this format's phase has actually shipped."""
        return self.phase <= CURRENT_PHASE


#: The highest phase implemented in this build. Bump it when a phase lands.
#:
#: 阶段 6 把相位从 3 推到 6（不是 4）：阶段 4 和 5 是阅读模式的推进，没有新增格式，
#: 所以格式表里没有它们对应的行。跳到 6 是因为 `.mobi`/`.azw3` 这一行写的 phase 就是
#: 6，相位是「格式解锁进度」而不是「阶段计数器」——中间那几个数字只表示没有新格式。
#:
#: 阶段 8 把它从 6 推到 8，同理：`.cbr`/`.rar` 那一行写的 phase 是 8，阶段 7 加的是
#: `.epub` 的第二种形态而不是新扩展名，所以 7 从来没有对应的行可解锁。
CURRENT_PHASE = 8

#: Extensions whose kind is only a *default*. A PDF can be a novel or a scan, so
#: `format_for()` answers "comic" (the heavier reader, which is the safer guess)
#: while `open_book()` sniffs the content and may return the other kind. Drag and
#: drop must not use the table's answer for these - it would accept the file and
#: then open something else.
DUAL_IDENTITY_EXTS: tuple[str, ...] = (".pdf",)

#: Extensions that go through `mobidoc.py` rather than the plain text reader.
#: Kept here next to `DUAL_IDENTITY_EXTS` for the same reason: the extension list
#: belongs to the format table, not to the dispatcher, so `opener.py` never spells
#: an extension out. `.azw3` is in the same entry because it is the same container
#: with a newer markup version inside - the reader decides which by sniffing.
KINDLE_EXTS: tuple[str, ...] = (".mobi", ".azw3")

#: Extensions whose kind is decided by the container's own metadata rather than by
#: the table. An EPUB is a text book by default (`Format` says `KIND_TEXT`) but a
#: fixed-layout one is a stack of pages, so `opener.open_book()` reads the OPF's
#: `rendition:layout` before choosing. The table's answer stays the default, which
#: is what drag-and-drop uses - accepting a fixed-layout EPUB as a text book is
#: still the right answer for "can this build open it at all".
#:
#: 阶段 7 加的是 `.epub` 的第二种形态，不是新扩展名，所以相位没有因此变化：
#: 相位是「格式解锁进度」，而 `.epub` 这一行从阶段 0 起就是 ready。把它推到 7 不会
#: 解锁任何东西，却会让 `test_mobi.py` 里「相位 = N」那条断言失去意义。
EPUB_EXTS: tuple[str, ...] = (".epub",)

#: 第二批加的三种文字格式。它们的相位写的是 6 而不是新的 9，理由见下面 `FORMATS`
#: 上方那段：相位是「这类能力属于哪个里程碑」，第二批补的是 MOBI 那一档的文字书，
#: 不是新的里程碑。写在单独的常量里是为了 `opener.py` 永远不用拼扩展名字面量。
FB2_EXTS: tuple[str, ...] = (".fb2",)
DOCX_EXTS: tuple[str, ...] = (".docx",)
HTML_EXTS: tuple[str, ...] = (".html", ".htm")

#: 第二批加的 7z 漫画包。相位 8 与 `.cbr`/`.rar` 同档：两者都是「要靠外部解压程序
#: 才能读的漫画容器」，只是用的程序不同（7-Zip 对 7z，WinRAR 或 7-Zip 对 RAR）。
CB7_EXTS: tuple[str, ...] = (".cb7",)

#: Every extension MiniReader recognises.
#:
#: Order matters for the file dialog: it is the order the filters appear in,
#: and the first entry is the default filter.
FORMATS: tuple[Format, ...] = (
    Format(KIND_TEXT, (".txt",), "文本文件", 0),
    Format(KIND_TEXT, (".epub",), "EPUB 电子书", 0),
    Format(KIND_COMIC, (".cbz", ".zip"), "漫画压缩包", 2),
    Format(KIND_COMIC, DUAL_IDENTITY_EXTS, "PDF 文档", 3),
    Format(KIND_TEXT, KINDLE_EXTS, "Kindle 电子书", 6),
    Format(KIND_COMIC, (".cbr", ".rar"), "RAR 漫画", 8),
    Format(KIND_TEXT, FB2_EXTS, "FB2 电子书", 6),
    Format(KIND_TEXT, DOCX_EXTS, "DOCX 文档", 6),
    Format(KIND_TEXT, HTML_EXTS, "网页", 6),
    Format(KIND_COMIC, CB7_EXTS, "7z 漫画", 8),
)

#: Extensions that are a folder rather than a file. Kept separate because the
#: open dialog cannot select them and drag-and-drop has to stat them.
IMAGE_EXTS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
)

#: Extension -> Format, built once.
_BY_EXT: dict[str, Format] = {
    ext: fmt for fmt in FORMATS for ext in fmt.exts
}


def _ext_of(path) -> str:
    """Lowercased extension including the dot, or "" when there is none."""
    name = str(path)
    dot = name.rfind(".")
    slash = max(name.rfind("/"), name.rfind("\\"))
    if dot <= slash or dot == len(name) - 1:
        return ""
    return name[dot:].lower()


def format_for(path) -> Format | None:
    """The table row for `path`, or None when the extension is unknown.

    Only looks at the name - it never touches the disk, because drag-and-drop
    calls this on every file in the payload before deciding to accept.
    """
    return _BY_EXT.get(_ext_of(path))


def kind_for(path) -> str | None:
    """The default book kind for `path`, or None when unsupported."""
    fmt = format_for(path)
    return fmt.kind if fmt is not None else None


def is_image(path) -> bool:
    """True when `path` looks like a single image file."""
    return _ext_of(path) in IMAGE_EXTS


def is_supported(path) -> bool:
    """True when `path` is a file we can open *today*.

    A format listed in the table but scheduled for a later phase counts as
    unsupported, so the user gets "这个格式要等阶段 3" rather than a confusing
    parse error from a parser that does not exist yet.
    """
    fmt = format_for(path)
    if fmt is not None:
        return fmt.ready
    return is_image(path)


def support_note(path) -> str:
    """Why `path` is not supported, in the user's language.

    Returns "" when it *is* supported, so callers can do
    `note = support_note(p); if note: warn(note)`.
    """
    fmt = format_for(path)
    if fmt is None:
        if is_image(path):
            return ""
        ext = _ext_of(path)
        return f"不支持的文件类型：{ext or '无扩展名'}"
    if not fmt.ready:
        return f"{fmt.label}要等阶段 {fmt.phase}，现在还不能打开"
    return ""


def _filters(label: str, fmts: list[Format]) -> str:
    """Qt file-dialog filter for a set of formats, e.g. "文本文件 (*.txt *.epub)"."""
    exts: list[str] = []
    for fmt in fmts:
        exts.extend(fmt.exts)
    return f"{label} ({' '.join('*' + e for e in exts)})"


def dialog_filters() -> str:
    """The whole `QFileDialog` filter string, built from the table.

    Three groups plus an everything-else fallback. A group is named after its
    formats when they all agree (so "漫画压缩包" rather than a generic label),
    and falls back to a neutral name when a group mixes formats - which happens
    because `.pdf` and `.epub` are genuinely both a text book and a comic, and
    the user should not have to guess which group we filed them under.
    """
    ready = [f for f in FORMATS if f.ready]
    comics = [f for f in ready if f.kind == KIND_COMIC]
    texts = [f for f in ready if f.kind == KIND_TEXT]

    groups: list[str] = []
    if texts:
        groups.append(_filters(_group_label(texts, "支持的电子书"), texts))
    if comics:
        groups.append(_filters(_group_label(comics, "支持的漫画"), comics))

    every: list[str] = []
    for fmt in ready:
        every.extend(fmt.exts)
    every.extend(IMAGE_EXTS)
    groups.append(f"所有支持的文件 ({' '.join('*' + e for e in every)})")
    groups.append("所有文件 (*.*)")
    return ";;".join(groups)


def _group_label(fmts: list[Format], fallback: str) -> str:
    """One label for a whole group: the shared format name, else `fallback`."""
    labels = {fmt.label for fmt in fmts}
    if len(labels) == 1:
        return labels.pop()
    return fallback
