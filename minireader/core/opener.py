"""Decide which kind of book a path is, then hand back the right object.

This is the one place that answers "what did the user just open?". Everything
else in the program branches on `book.kind`, never on the file extension, so
adding a format means adding a line here rather than an `if` in the window.

The dispatch order matters and is not arbitrary:

1. Unknown extension -> refuse immediately. This is the only branch that can
   produce a message without touching the disk, which is what drag-and-drop
   needs (it asks about every file in the payload before accepting the drop).
2. `.epub` -> the existing parser. An EPUB has two shapes and the file says
   which: `rendition:layout=pre-paginated` (or the older `fixed-layout` /
   `book-type=comic` spellings) means every spine item is a whole page of
   pictures, so it diverts to a `ComicBook`; anything else is a text book. The
   check reads the OPF's metadata only, so a reflowable book pays one small
   extra read and nothing more.
3. `.pdf` -> dual identity, so the content decides, not the extension. This is
   the only branch that opens the file twice (once to classify, once for real),
   which is affordable because opening a PDF document costs half a millisecond
   and nothing is parsed until a page is asked for.
4. Comic containers -> `ComicBook`.
5. Kindle containers (`.mobi`/`.azw3`) -> `mobidoc`, which needs the optional
   `mobi` package and reports its absence as a message rather than an
   ImportError. The branch exists so that "a MOBI is a text book" is stated in
   the dispatcher, where the next reader will look for it.
6. Everything else -> the text `Book`.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .book_base import KIND_COMIC, KIND_TEXT, BookBase
from .comic import ComicBook
from .formats import (
    DUAL_IDENTITY_EXTS,
    EPUB_EXTS,
    KINDLE_EXTS,
    _ext_of,
    format_for,
    is_image,
    is_supported,
    support_note,
)
from .model import Book, BookError

#: Which reader a dual-identity file should open in.
PDF_AUTO = "auto"
PDF_TEXT = "text"
PDF_COMIC = "comic"


@dataclass(frozen=True)
class PdfChoice:
    """Which reader a dual-identity file should use, and how sure we are.

    `needs_ask` is True only in the one case where nobody has an opinion: the
    caller did not say, and measuring the document could not settle it either
    (almost no text *and* almost no picture - a cover-only PDF, a blank template,
    a comic drawn as vectors). The window asks once in that case; every other
    open is silent.
    """

    kind: str
    needs_ask: bool = False


def pdf_choice(path, prefer: str | None = None) -> PdfChoice:
    """Which reader a PDF should use: `PDF_TEXT` or `PDF_COMIC`.

    `prefer` wins when it is an explicit answer, and it comes from two places
    that are both the user talking: the file's own remembered choice, and the
    `pdf_default_kind` setting. When neither has an opinion, the content is
    measured - and if measuring cannot settle it either, the text reader is the
    safer default, because a PDF that turns out to be pictures is still readable
    page by page in the text reader, whereas a novel shown as one giant picture
    per page is not. That last case is flagged through `needs_ask` so the caller
    can offer the user the correction instead of silently committing.

    This is the only entry point that reports ambiguity. `open_book()` uses it
    and keeps just the kind, because a book has to come back as one thing.
    """
    if prefer in (PDF_TEXT, PDF_COMIC):
        return PdfChoice(prefer)
    from .pdfdoc import classify

    info = classify(path)
    return PdfChoice(info.kind, info.needs_ask)


def _pdf_choice(path, prefer: str | None) -> str:
    """`pdf_choice()`'s kind alone, for `open_book()`'s internal use."""
    return pdf_choice(path, prefer).kind


def open_book(path, *, pdf_kind: str | None = None) -> BookBase:
    """Open `path` as whatever kind of book it is.

    `pdf_kind` is the caller's opinion about a PDF: `"text"`, `"comic"`, or
    `None`/`"auto"` to measure the file. It is ignored for everything else.

    Raises `BookError` with a user-facing message for anything the reader can
    act on: unknown format, a format scheduled for a later phase, a missing
    file, or a container whose contents do not match its name.
    """
    raw = str(path)
    if not raw:
        raise BookError("没有选择文件")

    target = os.path.abspath(raw)

    # A directory is checked before the format table: folders have no extension,
    # so the table has nothing to say about them.
    if os.path.isdir(target):
        return ComicBook.from_path(target)

    note = support_note(target)
    if note:
        raise BookError(note)

    # A single image is not in the format table (there is no one extension for
    # "an image"), so it is checked before the table lookup below.
    if os.path.isfile(target) and is_image(target):
        return ComicBook.from_path(target)

    fmt = format_for(target)
    if fmt is None:
        raise BookError("不支持的文件类型")

    if _ext_of(target) in DUAL_IDENTITY_EXTS:
        kind = _pdf_choice(target, pdf_kind)
        if kind == KIND_COMIC:
            return ComicBook.from_pdf(target)
        return Book.from_pdf(target)

    # Kindle containers are text books, but not *plain* ones: they have to be
    # unpacked first, and the unpacker is an optional dependency. Imported here
    # rather than at module level so that a user who never opens a Kindle book
    # never pays for the module (same reason `pdfdoc` is imported inside
    # `pdf_choice`).
    if _ext_of(target) in KINDLE_EXTS:
        from .mobidoc import open_mobi

        return open_mobi(target)

    # An EPUB is two shapes in one extension: a reflowable one is a text book, a
    # fixed-layout one is a stack of full-page images. The file says which in its
    # OPF, so ask it rather than guess from the name. `epub_layout()` reads the
    # manifest only and never raises - a broken EPUB falls through to
    # `Book.from_path()`, which reports the damage with the message it has always
    # used.
    if _ext_of(target) in EPUB_EXTS:
        from .epub import LAYOUT_PRE_PAGINATED, epub_layout

        layout, _progression, _spread = epub_layout(target)
        if layout == LAYOUT_PRE_PAGINATED:
            return ComicBook.from_epub(target)
        return Book.from_path(target)

    if fmt.kind == KIND_COMIC:
        return ComicBook.from_path(target)

    return Book.from_path(target)


def open_book_kind(path, *, pdf_kind: str | None = None) -> str:
    """What kind `path` would open as, without opening it.

    Used by drag-and-drop to accept or reject a payload cheaply. Directory and
    image checks do touch the disk (they must - there is no extension to go by),
    but nothing is parsed.

    A dual-identity file is reported as the table says (comic) rather than being
    classified: this runs for every file in a dropped payload, and measuring a
    PDF's text density is not something to do before deciding whether to accept a
    drop. `open_book()` re-decides from the content when the drop is accepted.
    """
    raw = str(path)
    if not raw:
        return ""
    target = os.path.abspath(raw)
    # Directories have no extension, so they are recognised by what they are.
    if os.path.isdir(target):
        return KIND_COMIC
    if not is_supported(target):
        return ""
    if is_image(target):
        return KIND_COMIC
    fmt = format_for(target)
    return fmt.kind if fmt is not None else ""
