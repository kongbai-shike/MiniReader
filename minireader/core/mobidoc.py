"""Read Kindle MOBI/AZW3 files, through the optional `mobi` package.

A MOBI is a Palm Database: a 78-byte header, a table of record offsets, then
records. Record 0 holds a PalmDOC header followed by the MOBI header, and the
book's markup lives in the records after it. Two things make this module's job
more interesting than "call a parser":

* **The container tells us whether we are allowed to try.** Record 0 carries an
  encryption field. When it is non-zero the book is DRM-protected, and the answer
  is a message telling the user to strip it themselves - never an attempt to
  decrypt. Sniffing that field is ~100 bytes of reading, so it happens *before*
  the `mobi` package is imported at all: a DRM'd book gets the right message even
  on a machine where `mobi` was never installed.

* **"Cannot unpack" has two very different causes.** A DRM'd book and a truncated
  download both make the unpacker fail. Re-sniffing the encryption field when
  unpacking fails is what tells them apart, which is the whole reason
  `_failure_note()` exists.

**The `mobi` package is an optional dependency, imported inside functions.**
It is GPL-3.0 and pulls in `loguru` and `standard-imghdr`; a user who never opens
a Kindle book must not have it installed and must not pay for importing it.
`is_available()` answers whether it is present without importing it. It is
deliberately NOT in `requirements.txt`'s mandatory lines.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass, field

from .model import Book, BookError

#: Shown when the `mobi` package is missing. Names the exact command, because
#: "install a MOBI library" is not actionable and the user has to leave the app.
MOBI_NOTE = "打开 MOBI/AZW3 需要安装 mobi：pip install mobi"

#: Shown for an encrypted book. The wording matters: MiniReader does not remove
#: DRM and there is no code here that could - it names the tool the user already
#: has instead of pretending the file is broken.
DRM_NOTE = "这本书有 DRM 保护，请先用 Calibre 脱壳后再打开"

#: Shown when unpacking fails on a file that is *not* encrypted - i.e. the bytes
#: themselves are wrong, not withheld.
CORRUPT_NOTE = "这个文件读不了，可能已损坏或下载不完整"

#: Print Replica is a Kindle textbook format: every page is a picture, the text
#: layer is an index, and the unpacker hands back a PDF. Out of scope, and saying
#: so beats opening it and showing nothing.
PRINT_REPLICA_NOTE = "这本 Kindle 书是 Print Replica（版式固定），MiniReader 打不开"

# ---- Palm Database layout -------------------------------------------------
#
# All offsets are into the file. The PalmDOC/MOBI header is record 0, so its
# fields are addressed from the start of that record, not from the file.

#: Fixed size of the Palm Database header.
PALM_HEADER = 78

#: Type/creator pair. "BOOKMOBI" is a Mobipocket book; "TEXtREAd" is a bare
#: PalmDOC with no MOBI header at all (no metadata, no encryption field).
IDENT_OFFSET = 0x3C
IDENT_LENGTH = 8
BOOK_IDENTS: tuple[bytes, ...] = (b"BOOKMOBI", b"TEXtREAd")

#: Number of records, as a big-endian uint16.
RECORD_COUNT_OFFSET = 76

#: The record offset table starts right after the 78-byte header; each entry is
#: 8 bytes (4-byte offset + 4 bytes of attributes/uid), so offsets are every
#: other long word.
RECORD_TABLE_OFFSET = 78

#: Fields inside record 0.
MOBI_MAGIC_OFFSET = 16
MOBI_MAGIC = b"MOBI"
COMPRESSION_OFFSET = 0x00
RECORDS_IN_HEADER_OFFSET = 0x08
CRYPTO_OFFSET = 0x0C
TITLE_FIELD_OFFSET = 0x54

#: A real MOBI header is a few hundred bytes. The cap only exists so that a file
#: claiming a gigantic first record cannot make us read it all into memory.
MAX_HEADER_BYTES = 1 << 20

#: Print Replica books start their first text record with this.
PRINT_REPLICA_MAGIC = b"%MOP"

#: Compression codes, kept for the report and for tests: 1 = stored, 2 = PalmDOC,
#: 0x4448 = HUFF/CDIC. We never decompress by hand - `mobi` does that.
COMPRESSION_NONE = 1
COMPRESSION_PALMDOC = 2
COMPRESSION_HUFF = 0x4448

#: Where KindleUnpack leaves the unpacked markup, and how a mobi7 book marks a
#: page break. `mobiml2xhtml` rewrites `<mbp:pagebreak/>` into the div form, so
#: both spellings turn up in the output and both are matched here.
_PAGEBREAK_RE = re.compile(
    rb"<mbp:pagebreak[^>]*>"
    rb"|<div[^>]*class\s*=\s*[\"']?mbp_pagebreak[^>]*>",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MobiInfo:
    """What `sniff()` learned from the container, without unpacking anything.

    `crypto_type` is the field that decides DRM; `print_replica` is the field that
    decides "this is a different format wearing a MOBI hat"; `title` comes from the
    MOBI header and is the fallback when the markup has no usable title.
    """

    crypto_type: int
    compression: int
    records: int
    title: str
    print_replica: bool

    @property
    def encrypted(self) -> bool:
        """True when the book is DRM-protected. The only place that decides it."""
        return self.crypto_type != 0


@dataclass
class _Content:
    """What unpacking produced, in the shape `Book.from_segments()` wants."""

    title: str
    author: str
    segments: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def is_available() -> bool:
    """Whether the `mobi` package can be imported, without importing it.

    `find_spec` walks `sys.path` only, so this stays cheap and safe to call on
    every open. It answers "installed", not "working" - a broken install is
    reported by `open_mobi()` with the same user-facing message.
    """
    from importlib.util import find_spec

    try:
        return find_spec("mobi") is not None
    except (ImportError, ValueError):
        # A module whose parent is missing raises rather than returning None.
        return False


def _mobi():
    """Import the `mobi` package or raise the one error the user can act on."""
    try:
        import mobi
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise BookError(MOBI_NOTE) from exc
    return mobi


def _sniff_stream(fh) -> MobiInfo | None:
    """`sniff()`'s body, on an already-open binary stream.

    Split out so the file is opened exactly once: the record table sits at the
    front, record 0 in the middle, and the Print Replica check needs record 1,
    which can be megabytes away.
    """
    try:
        size = os.fstat(fh.fileno()).st_size
        head = fh.read(PALM_HEADER)
    except OSError:
        return None
    if len(head) < PALM_HEADER:
        return None
    if head[IDENT_OFFSET : IDENT_OFFSET + IDENT_LENGTH] not in BOOK_IDENTS:
        return None

    (count,) = struct.unpack_from(">H", head, RECORD_COUNT_OFFSET)
    if count <= 0:
        return None

    # Three offsets is all we need: record 0's start and end, and record 1's
    # start (for the Print Replica magic). Reading the whole table would scale
    # with a number we do not otherwise care about.
    wanted = min(count, 3)
    try:
        table = fh.read(8 * wanted)
    except OSError:
        return None
    if len(table) < 8:
        return None
    offsets = [
        struct.unpack_from(">L", table, 8 * i)[0] for i in range(len(table) // 8)
    ]

    start = offsets[0]
    if start < PALM_HEADER or start >= size:
        return None
    end = offsets[1] if len(offsets) > 1 else size
    if end <= start or end > size:
        end = size
    end = min(end, start + MAX_HEADER_BYTES)

    try:
        fh.seek(start)
        record = fh.read(end - start)
    except OSError:
        return None
    if len(record) < 4:
        return None

    (compression,) = struct.unpack_from(">H", record, COMPRESSION_OFFSET)
    records = 0
    if len(record) >= RECORDS_IN_HEADER_OFFSET + 2:
        (records,) = struct.unpack_from(">H", record, RECORDS_IN_HEADER_OFFSET)

    crypto = 0
    title = ""
    if (
        len(record) >= MOBI_MAGIC_OFFSET + 4
        and record[MOBI_MAGIC_OFFSET : MOBI_MAGIC_OFFSET + 4] == MOBI_MAGIC
    ):
        if len(record) >= CRYPTO_OFFSET + 2:
            (crypto,) = struct.unpack_from(">H", record, CRYPTO_OFFSET)
        title = _title_from(record)

    print_replica = False
    if len(offsets) > 1 and offsets[1] + 4 <= size:
        try:
            fh.seek(offsets[1])
            print_replica = fh.read(4) == PRINT_REPLICA_MAGIC
        except OSError:
            print_replica = False

    return MobiInfo(crypto, compression, records, title, print_replica)


def sniff(path) -> MobiInfo | None:
    """Read the container's own description of itself, or None if it is not one.

    Returns None for anything that is not a Palm Database book at all - an empty
    file, a text file with a `.mobi` name, a truncated download missing even the
    header. That is deliberately a *different* answer from "a MOBI that is
    encrypted", because the two lead to different messages.
    """
    target = os.path.abspath(str(path))
    try:
        with open(target, "rb") as fh:
            return _sniff_stream(fh)
    except OSError:
        return None


def looks_like_mobi(path) -> bool:
    """Cheap "is this worth handing to the MOBI reader?" check.

    Used by `open_book()` before it commits, and by tests. Never raises: a wrong
    guess costs one error dialog, an exception would interrupt the window.
    """
    return sniff(path) is not None


def is_drm(path) -> bool:
    """Whether `path` declares itself encrypted. False when it is not a MOBI."""
    info = sniff(path)
    return bool(info is not None and info.encrypted)


def _title_from(record: bytes) -> str:
    """The MOBI header's title, which is a byte range inside record 0.

    Two codecs are tried because the field predates the codepage field being
    reliable: a CP1252 title decodes as UTF-8 only by accident, and a UTF-8 title
    read as CP1252 comes back as mojibake. Neither failure is worth an exception -
    an unreadable title just falls back to the file name.
    """
    if len(record) < TITLE_FIELD_OFFSET + 8:
        return ""
    offset, length = struct.unpack_from(">II", record, TITLE_FIELD_OFFSET)
    if length <= 0 or offset + length > len(record):
        return ""
    raw = record[offset : offset + length]
    for codec in ("utf-8", "cp1252"):
        try:
            return raw.decode(codec).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()


def _failure_note(path) -> str:
    """Why unpacking failed: the encryption field is the only thing that knows.

    Both a DRM'd book and a corrupt file make the unpacker raise, and the
    exception text is written for the unpacker's author, not the reader. So the
    header is read again here - it is the one piece of evidence that separates
    "we are not allowed" from "these bytes are wrong".
    """
    info = sniff(path)
    if info is not None and info.encrypted:
        return DRM_NOTE
    return CORRUPT_NOTE


def _segments_from_piece(data: bytes) -> list:
    """One pagebreak-delimited piece of mobi7 markup -> one or more segments.

    Page breaks are the format's own idea of structure, but they are not the
    *only* structure a book carries. A very common mobi7 layout is one break
    after the front matter and then the whole novel running on with no further
    breaks - and taking the breaks as the only boundary would hand back a
    "chapter" three chapters long: the table of contents shows two entries and
    one turn jumps across three chapters.

    So every piece is also offered to the same chapter detector a `.txt` file
    gets, and the detector's answer is used when it finds more than one chapter
    inside the piece. The common case - one break per chapter, so one heading per
    piece - comes back as exactly one segment, which is why well-formed books are
    unaffected by this.
    """
    from .chapters import split_chapters_auto
    from .epub import Segment, _extract

    found = _extract(data)
    text = found.text
    if not text.strip():
        return []

    split = split_chapters_auto(text)
    if len(split.chapters) > 1:
        return [
            Segment(chapter.title, text[chapter.start : chapter.end])
            for chapter in split.chapters
        ]
    return [Segment(found.first_heading or found.doc_title or "", text)]


def _html_segments(data: bytes) -> tuple[list, str]:
    """Turn unpacked mobi7 markup into segments and a title.

    A mobi7 book is one flat HTML file with no spine, so there is no authoritative
    chapter list to read. Page breaks are the only structure the markup itself
    carries, so they come first; each piece is then handed to the chapter detector
    as well (`_segments_from_piece`), because a piece is not always a chapter.

    A file with no page breaks at all is the same code path with a single piece,
    which is the honest answer for a format that states no boundaries of its own.
    """
    from .epub import _extract

    title = _extract(data).doc_title

    pieces = [piece for piece in _PAGEBREAK_RE.split(data) if piece.strip()]
    if len(pieces) <= 1:
        pieces = [data]

    segments: list = []
    for piece in pieces:
        segments.extend(_segments_from_piece(piece))
    return segments, title


def _read_extracted(extracted, info: MobiInfo) -> _Content:
    """Read whatever KindleUnpack produced: an EPUB, a mobi7 HTML, or a PDF.

    The three cases are genuinely different books. A KF8 (`.azw3`) unpacks to an
    EPUB with a real spine, so it goes through the EPUB parser and inherits its
    chapter boundaries, metadata and notes. An older mobi7 unpacks to one HTML
    file. Print Replica unpacks to a PDF, which is a different reading experience
    entirely and is refused by name rather than opened and shown empty.
    """
    from pathlib import Path

    path = Path(extracted)
    suffix = path.suffix.lower()

    if suffix == ".epub":
        from .epub import parse_epub

        content = parse_epub(path)
        return _Content(
            content.title or info.title,
            content.author,
            list(content.segments),
            list(content.notes),
        )

    if suffix in (".html", ".htm", ".xhtml"):
        segments, title = _html_segments(path.read_bytes())
        return _Content(title or info.title, "", segments, [])

    if suffix == ".pdf":
        raise BookError(PRINT_REPLICA_NOTE)

    raise BookError(CORRUPT_NOTE)


def _unpack(target: str, info: MobiInfo) -> _Content:
    """Unpack `target` into memory, then delete every byte we caused to be written.

    `mobi.extract()` unpacks into a fresh temp directory and documents that the
    caller owns it, so the obvious code is `tempdir, path = mobi.extract(...)`
    with a `finally: shutil.rmtree(tempdir)`. **That leaks.** The library creates
    the directory *first* and only then starts unpacking, so when unpacking
    raises - which is exactly the corrupt-file path - it never returns, the
    `tempdir` name is still `""`, and the half-unpacked book stays in `%TEMP%`
    forever. A user with a broken download would collect one leaked directory per
    attempt, each holding however much of the book got written.

    So we do not rely on the return value. We point `tempfile` at a scratch
    directory we own for the duration of the call, and delete *that* in the
    `finally`. `mobi.extract()` builds its directory via `tempfile.mkdtemp()`,
    which honours `tempfile.tempdir`, so its output lands inside our scratch no
    matter how far it gets. One `rmtree` covers success, partial failure, and
    failure before the first byte was written alike.

    `tempfile.tempdir` is process-global, so this is not thread-safe. MiniReader
    is single-threaded by design (no `QThread`, no executor anywhere in
    `minireader/`), and the alternative - diffing `%TEMP%` before and after - is
    both racier and more code.
    """
    mobi = _mobi()
    scratch = tempfile.mkdtemp(prefix="minireader_mobi_")
    previous = tempfile.tempdir
    tempfile.tempdir = scratch
    try:
        _tempdir, extracted = mobi.extract(target)
        return _read_extracted(extracted, info)
    finally:
        tempfile.tempdir = previous
        shutil.rmtree(scratch, ignore_errors=True)


def open_mobi(path) -> Book:
    """Open a `.mobi` / `.azw3` as a text book.

    The order of the checks is the design:

    1. Sniff the container. Nothing that follows can be trusted if this fails.
    2. Refuse DRM by name. Doing this before the `mobi` import means a DRM'd book
       gets the right message even where the library is not installed, and means
       no decryption code path could ever be reached.
    3. Refuse Print Replica by name, for the same reason: a clear message beats a
       book that opens and shows nothing.
    4. Only then require the optional dependency.

    Raises `BookError` with a user-facing message for every failure the reader can
    act on. Never raises anything about a module named `mobi`.
    """
    target = os.path.abspath(str(path))
    info = sniff(target)
    if info is None:
        raise BookError(CORRUPT_NOTE)
    if info.encrypted:
        raise BookError(DRM_NOTE)
    if info.print_replica:
        raise BookError(PRINT_REPLICA_NOTE)
    if not is_available():
        raise BookError(MOBI_NOTE)

    try:
        content = _unpack(target, info)
    except BookError:
        raise
    except Exception as exc:  # noqa: BLE001 - the unpacker's errors are not ours
        raise BookError(_failure_note(target)) from exc

    try:
        stat = os.stat(target)
        size: int | None = stat.st_size
        mtime: float | None = stat.st_mtime
    except OSError:
        size = None
        mtime = None

    return Book.from_segments(
        content.segments,
        path=target,
        title=content.title or None,
        author=content.author,
        notes=content.notes,
        size=size,
        mtime=mtime,
        encoding="mobi",
        encoding_label="MOBI",
        source="mobi",
        empty_message="这本 Kindle 电子书里没有可读的正文",
    )


__all__ = [
    "COMPRESSION_HUFF",
    "COMPRESSION_NONE",
    "COMPRESSION_PALMDOC",
    "CORRUPT_NOTE",
    "DRM_NOTE",
    "MOBI_NOTE",
    "PRINT_REPLICA_NOTE",
    "MobiInfo",
    "is_available",
    "is_drm",
    "looks_like_mobi",
    "open_mobi",
    "sniff",
]
