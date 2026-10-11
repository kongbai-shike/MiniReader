"""The reading model: one Book, its chapters, and where the reader is in it.

Ported behaviour from client.js:400-418 (import) and 621-630 (chapter text), so a
chapter body is sliced and de-titled exactly as the plugin did.

Deliberately Qt-free. `minireader.core.*` must stay importable without a display,
which is what lets the whole kernel be tested headlessly.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .chapters import Chapter, SplitResult, split_chapters_auto
from .encoding import DecodeError, Decoded, decode_txt

# A 200 MB "novel" is a mis-click, and reading it into one str would cost ~400 MB.
MAX_BYTES = 200 * 1024 * 1024


class BookError(Exception):
    """Something the user needs to be told about, in their own language."""


def book_id(path: str | os.PathLike[str]) -> str:
    """Stable id for a book, derived from its normalised absolute path.

    Bookmarked progress is keyed by this, so moving a file loses its progress -
    honest and cheap, versus copying megabytes of text into an app-data folder.
    """
    norm = os.path.normcase(os.path.abspath(str(path)))
    return "mr-" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# A zip archive starts with "PK\x03\x04" (or PK\x05\x06 / PK\x07\x08 for an empty or
# spanned archive). Checking the magic is four free byte comparisons, which is what
# lets from_path() avoid sniffing every .txt file with zipfile.is_zipfile() - open()
# plus a seek - when virtually no text file is a zip.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _is_epub(p: Path) -> bool:
    """Is this path an EPUB?

    The extension alone is not enough: a renamed .epub (or a book downloaded with no
    extension) would otherwise be run through the TXT decoder, which would "succeed"
    and show the reader a screenful of binary rubbish. So the extension decides the
    fast yes, and the zip magic decides whether it is worth asking the EPUB parser.
    """
    if p.suffix.lower() == ".epub":
        return True
    try:
        with open(p, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    if head not in _ZIP_MAGIC:
        return False
    from .epub import looks_like_epub

    return looks_like_epub(p)


@dataclass
class Book:
    id: str
    title: str
    path: str
    size: int
    mtime: float
    encoding: str
    encoding_label: str
    confident: bool
    text: str
    chapters: list[Chapter]
    rule_id: str | None
    added_at: float = field(default_factory=time.time)
    notes: list[str] = field(default_factory=list)
    # "txt" or "epub". The reader does not branch on it; it is for the UI and for
    # to_record(), so the library can say which kind of file this is.
    source: str = "txt"
    # Only EPUBs carry one. Empty for TXT, which has no metadata block.
    author: str = ""

    # ---- construction ----------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes, path: str, force_encoding: str | None = None) -> "Book":
        decoded: Decoded = decode_txt(data, force=force_encoding)
        return cls.from_text(
            decoded.text,
            path=path,
            encoding=decoded.encoding,
            encoding_label=decoded.label,
            confident=decoded.confident,
            notes=list(decoded.notes),
            size=len(data),
            mtime=_mtime_of(path),
        )

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        path: str,
        encoding: str = "utf-8",
        encoding_label: str = "UTF-8",
        confident: bool = True,
        notes: list[str] | None = None,
        size: int | None = None,
        mtime: float | None = None,
        pattern: str | None = None,
    ) -> "Book":
        # The plugin normalised CRLF AFTER decoding (client.js:404). decode_txt does
        # it too, but doing it again here keeps from_text honest when called directly.
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        split: SplitResult = split_chapters_auto(normalized, pattern)
        name = os.path.basename(str(path)) or "novel.txt"
        title = name[:-4] if name.lower().endswith(".txt") else name
        return cls(
            id=book_id(path),
            title=title or "未命名",
            path=os.path.abspath(str(path)),
            size=int(size if size is not None else len(normalized.encode("utf-8"))),
            mtime=float(mtime if mtime is not None else 0.0),
            encoding=encoding,
            encoding_label=encoding_label,
            confident=confident,
            text=normalized,
            chapters=split.chapters,
            rule_id=split.rule_id,
            notes=list(notes or []),
        )

    @classmethod
    def from_path(cls, path: str | os.PathLike[str], force_encoding: str | None = None) -> "Book":
        p = Path(path)
        try:
            stat = p.stat()
        except OSError as exc:
            raise BookError(f"读不到这个文件：{exc.strerror or exc}") from exc
        if not p.is_file():
            raise BookError("这不是一个文件")
        if stat.st_size == 0:
            raise BookError("文件是空的")
        if stat.st_size > MAX_BYTES:
            raise BookError(f"文件太大（{human_size(stat.st_size)}），超过 {human_size(MAX_BYTES)} 上限")
        if _is_epub(p):
            return cls._from_epub(p, stat)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise BookError(f"读取失败：{exc.strerror or exc}") from exc
        try:
            return cls.from_bytes(data, str(p), force_encoding=force_encoding)
        except DecodeError as exc:
            raise BookError(str(exc)) from exc

    @classmethod
    def _from_epub(cls, p: Path, stat: os.stat_result) -> "Book":
        """Open an EPUB through the spine, never through the TXT chapter regex."""
        from .epub import EpubError, parse_epub

        try:
            content = parse_epub(p)
        except EpubError as exc:
            raise BookError(str(exc)) from exc
        return cls.from_segments(
            content.segments,
            path=str(p),
            title=content.title,
            author=content.author,
            notes=content.notes,
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

    @classmethod
    def from_segments(
        cls,
        segments,
        *,
        path: str,
        title: str | None = None,
        author: str = "",
        notes: list[str] | None = None,
        size: int | None = None,
        mtime: float | None = None,
    ) -> "Book":
        """Build a book from chapters that are ALREADY split - i.e. an EPUB spine.

        An EPUB states its own chapter boundaries, so running the TXT chapter regex
        over its text would be wrong twice over: it would ignore the real spine, and
        Chinese novels are full of in-text "第一章" mentions that the regex would
        happily turn into phantom chapters. So this path joins the segments and
        derives the offsets arithmetically, never guessing.

        Segments are joined with a blank line so two chapters can never fuse into one
        line, and the offsets are computed as the parts are appended - no second pass
        over the text, which matters when the text is hundreds of megabytes.

        The separator belongs to the END of the previous chapter, which keeps the two
        invariants the TXT splitter also guarantees: adjacent chapters are contiguous
        (`chapters[i].end == chapters[i + 1].start`) and `chapters[-1].end == len(text)`.
        Leaving the separator owned by nobody would work visually but put two
        characters of the book out of reach of any future whole-book scan.

        The segment titles are deliberately NOT written into the text: `chapter_body()`
        strips a duplicated heading line, and an EPUB body does not contain one.
        """
        from dataclasses import replace

        from .chapters import Chapter, TXT_CHAPTER

        parts: list[str] = []
        chapters: list[Chapter] = []
        pos = 0
        skipped = 0
        for i, seg in enumerate(segments):
            # strip("\n") only: leading indentation inside a paragraph is content.
            body = seg.text.strip("\n")
            if not body.strip():
                skipped += 1
                continue
            if chapters:
                parts.append("\n\n")
                pos += 2
                chapters[-1] = replace(chapters[-1], end=pos)
            start = pos
            parts.append(body)
            pos += len(body)
            chapters.append(Chapter(seg.title or f"{TXT_CHAPTER} {i + 1}", start, pos))

        text = "".join(parts)
        if not chapters:
            raise BookError("这本 EPUB 里没有可读的正文")

        all_notes = list(notes or [])
        if skipped:
            all_notes.append(f"跳过了 {skipped} 个空章节")

        name = os.path.basename(str(path)) or "book.epub"
        fallback = name[:-5] if name.lower().endswith(".epub") else name
        return cls(
            id=book_id(path),
            title=title or fallback or "未命名",
            path=os.path.abspath(str(path)),
            size=int(size if size is not None else len(text.encode("utf-8"))),
            mtime=float(mtime if mtime is not None else 0.0),
            encoding="epub",
            encoding_label="EPUB",
            confident=True,
            text=text,
            chapters=chapters,
            rule_id=None,
            notes=all_notes,
            source="epub",
            author=author,
        )

    # ---- reading ---------------------------------------------------------

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    def chapter(self, index: int) -> Chapter | None:
        if 0 <= index < len(self.chapters):
            return self.chapters[index]
        return None

    def chapter_body(self, index: int) -> str:
        """Chapter text with the heading line removed when it is duplicated.

        Port of currentChapterText() (client.js:621-630): the heading line is also
        the first line of the chapter slice, so it would otherwise be printed twice.
        """
        ch = self.chapter(index)
        if ch is None:
            return ""
        raw = self.text[ch.start : ch.end]
        lines = raw.split("\n")
        first = (lines[0] if lines else "").strip()
        body = "\n".join(lines[1:]) if (ch.title and first == ch.title) else raw
        return body.lstrip("\n")

    def clamp(self, index: int) -> int:
        if not self.chapters:
            return 0
        return max(0, min(index, len(self.chapters) - 1))

    def split_summary(self) -> str:
        """One line describing how this book was split - shown in the UI."""
        from .chapters import RULE_LABELS

        if self.rule_id:
            return f"自动分章：{self.chapter_count} 章（规则：{RULE_LABELS.get(self.rule_id, self.rule_id)}）"
        return f"分章：{self.chapter_count} 章"

    def describe(self) -> str:
        """One line for the status bar. The author is only worth showing if there is one."""
        head = f"{self.encoding_label} · {human_size(self.size)} · {len(self.text)} 字 · {self.chapter_count} 章"
        if self.author:
            return f"{self.author} · {head}"
        return head

    def to_record(self) -> dict:
        """The library-index entry. Text is never persisted - see book_id()."""
        record = {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "encoding": self.encoding_label,
            "chapters": self.chapter_count,
            "rule": self.rule_id or "default",
            "added_at": self.added_at,
            "source": self.source,
        }
        if self.author:
            record["author"] = self.author
        return record


def _mtime_of(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


@dataclass
class Progress:
    """Where the reader is. `offset` is a scrollbar pixel value, and the scrollbar
    of a QTextEdit is genuinely pixel-based (verified: 0..33835 for a 500-block
    document whose singleStep is one line height)."""

    chapter: int = 0
    offset: int = 0

    def to_dict(self) -> dict:
        return {"chapter": int(self.chapter), "offset": int(self.offset)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Progress":
        if not isinstance(data, dict):
            return cls()
        try:
            return cls(chapter=max(0, int(data.get("chapter", 0))), offset=max(0, int(data.get("offset", 0))))
        except (TypeError, ValueError):
            return cls()
