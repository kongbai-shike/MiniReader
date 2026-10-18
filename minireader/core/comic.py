"""A comic as a list of page references - never as pixels.

`ComicBook` does exactly three things: list the pages, hand back raw bytes for
one page, and hand back the first few KB of one page. Decoding is the UI's job.
That split is not stylistic: `minireader.core.*` must stay importable without
Qt (the tests enforce it for `store.py` and the same rule is stated in the
package docstring), so a `QPixmap` may never appear in this module.

Page sizes are the interesting case. The plan wants every page to have a known
width and height *before* it is decoded, because continuous-scroll mode needs
placeholder blocks of exactly the right height or the scrollbar thrashes while
images arrive. Measuring a header needs `QImageReader`, which is Qt, so the
sizes cannot be computed here. The resolution is that `ComicPage.width/height`
start at 0 and the UI fills them in through `set_page_size()` the first time it
probes a page header; `sizes_known` says whether that has happened. Core stays
Qt-free and the UI still gets its stable placeholder heights.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import ClassVar

from . import archives, cb7, images, rar
from .archives import HEAD_BYTES, ArchiveError
from .book_base import KIND_COMIC, BookBase
from .formats import IMAGE_EXTS
from .model import BookError, book_id, human_size

__all__ = ["ComicBook", "ComicPage", "open_comic", "open_epub_fixed"]

#: Sources whose pages live inside a container that needs an external extractor.
#: Both modules expose the same four functions (`read_page`, `read_page_head`,
#: `read_page_image_head`, `read_page_heads`), so each dispatch point below picks
#: the module and then makes one call, instead of growing a second copy of every
#: branch. Keyed by source name rather than tested with `==` so the mapping is
#: the only place the two names appear.
_EXTRACTED = {"rar": rar, "cb7": cb7}


@dataclass
class ComicPage:
    """One page, identified but not yet decoded.

    `ref` is what the container answers to: an archive member name for a zip
    (the raw one, which may be mojibake), an absolute path for a folder or a
    single image, and (in a later phase) an empty string for a PDF page that is
    addressed by index alone.

    `name` is the display name. For an archive whose Chinese filenames needed
    the cp437 -> GBK repair these two differ, and reads must go through `ref`.
    """

    index: int
    ref: str
    name: str
    width: int = 0
    height: int = 0

    @property
    def size_known(self) -> bool:
        """True once the UI has probed this page's header."""
        return self.width > 0 and self.height > 0


@dataclass
class ComicBook(BookBase):
    """A paged book: CBZ, ZIP, an image folder, or a single image."""

    kind: str = KIND_COMIC

    id: str = ""
    title: str = ""
    path: str = ""
    size: int = 0
    mtime: float = 0.0
    added_at: float = field(default_factory=time.time)

    #: Where the pages come from: "cbz", "rar", "cb7", "folder", "image", "pdf"
    #: or "epub".
    source: str = "cbz"

    pages: list[ComicPage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    #: Page index -> the several images that page is made of, in display order.
    #: Only a fixed-layout EPUB ever fills this in; empty means "one page, one
    #: image", which is what every other source means. A dict rather than a field
    #: on ComicPage so the common case costs nothing.
    _page_images: dict[int, tuple[str, ...]] = field(default_factory=dict, repr=False)

    #: "ltr" or "rtl" - the binding this book asks for, when the container says.
    #: Empty means "no opinion, use the setting". A fixed-layout EPUB sets it from
    #: `page-progression-direction`, and the window applies it as a per-book
    #: override without touching the user's global setting.
    binding: str = ""

    #: `rendition:spread` as declared by the container: "auto", "none",
    #: "landscape", "both", or empty when the book never said. Only a
    #: fixed-layout EPUB can have an opinion; every other source leaves it empty,
    #: which reads the same as "auto" - the reading system decides - so the two
    #: cases do not need telling apart the way "ltr" and "said nothing" do.
    #: `minireader.ui.spread.pairs_pages()` is what interprets it.
    spread: str = ""

    # -- what the window needs -------------------------------------------------

    @property
    def position_count(self) -> int:
        return len(self.pages)

    @property
    def position_label(self) -> str:
        return "页"

    def clamp(self, index: int) -> int:
        """Keep a page number inside the book. Mirrors `Book.clamp()`."""
        if not self.pages:
            return 0
        return max(0, min(int(index), len(self.pages) - 1))

    def describe(self) -> str:
        parts = [self.source_label]
        # A folder reports 0 bytes (its own directory entry), which would read as
        # "0 B" in the tooltip; only show a size when there is one.
        if self.size:
            parts.append(human_size(self.size))
        parts.append(f"{len(self.pages)} 页")
        if self.pages and self.pages[0].size_known:
            parts.append(f"{self.pages[0].width}×{self.pages[0].height}")
        return " · ".join(parts)

    @property
    def source_label(self) -> str:
        return {
            "cbz": "漫画压缩包",
            "rar": "RAR 漫画",
            "cb7": "7z 漫画",
            "folder": "图片文件夹",
            "image": "单张图片",
            "pdf": "PDF 文档",
            "epub": "固定版式 EPUB",
        }.get(self.source, "漫画")

    def to_record(self) -> dict:
        """The shelf row. Mirrors `Book.to_record()`'s keys."""
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "encoding": self.source_label,
            "chapters": len(self.pages),
            "rule": "comic",
            "added_at": self.added_at,
            "source": self.kind,
        }

    # -- page access ----------------------------------------------------------

    def page(self, index: int) -> ComicPage | None:
        """The page at `index`, or None when out of range."""
        if 0 <= index < len(self.pages):
            return self.pages[index]
        return None

    def read_page(self, index: int) -> bytes:
        """Raw, still-encoded bytes of one page. Raises `ArchiveError`.

        A PDF page has no bytes of its own - it is drawn. So it is rendered here
        and handed back as BMP, which keeps this method's contract ("bytes Qt can
        decode") true for every source, and lets the cache, the decoder and the
        page view stay exactly as they are.

        A fixed-layout EPUB page can hold more than one image (a spread page that
        the producer exported as several pieces). This method returns the FIRST
        one - the whole page is assembled by `read_page_image()` and stitched by
        the UI. That keeps the "one page, one byte string" contract true for the
        overwhelming majority of pages, where a page really is one image.
        """
        page = self.page(index)
        if page is None:
            raise ArchiveError("这一页不存在")
        extractor = _EXTRACTED.get(self.source)
        if extractor is not None:
            # A RAR or a 7z is NOT a zip, so it cannot share `archives`' reader
            # even though its refs look identical. Named explicitly and ahead of
            # the zip branch, for the reason constraint 25 gives: a source that
            # falls through to the filesystem path fails one page at a time, with
            # a message about a missing file, instead of once at open time.
            return extractor.read_page(self.path, page.ref)
        if self.source in ("cbz", "epub"):
            # An EPUB is a zip too, so the same reader works; the refs stored on
            # its pages are zip member names, exactly like a CBZ.
            return archives.read_member(self.path, page.ref)
        if self.source == "pdf":
            # Imported here, not at module level: pdfdoc imports this module, and
            # pypdfium2 is optional on top of that.
            from . import pdfdoc

            return pdfdoc.render_page_bmp(self.path, index)

        return images.read_file(page.ref)

    def page_image_count(self, index: int) -> int:
        """How many images one page is made of. 1 for every source but EPUB.

        Only a fixed-layout EPUB can answer more than 1: its producer may have
        exported one logical page as several image files, which the reader stacks
        vertically. Everything else - a CBZ page, a folder image, a PDF page - is
        a single picture by definition.
        """
        refs = self.page_image_refs(index)
        return len(refs)

    def page_image_refs(self, index: int) -> tuple[str, ...]:
        """The container refs that make up one page, in display order.

        Falls back to `(page.ref,)` for every source that does not carry a list,
        so callers can treat all sources uniformly.
        """
        page = self.page(index)
        if page is None:
            return ()
        extra = self._page_images.get(index) if self._page_images else None
        if extra:
            return extra
        return (page.ref,) if page.ref else ()

    def read_page_image(self, index: int, image_index: int) -> bytes:
        """The bytes of one image inside a multi-image page. Raises `ArchiveError`.

        For a single-image page this is exactly `read_page()`. It exists so the
        stitching code does not have to know which source it is looking at.
        """
        refs = self.page_image_refs(index)
        if not 0 <= image_index < len(refs):
            raise ArchiveError("这一页不存在")
        ref = refs[image_index]
        extractor = _EXTRACTED.get(self.source)
        if extractor is not None:
            return extractor.read_page(self.path, ref)
        if self.source in ("cbz", "epub"):
            return archives.read_member(self.path, ref)
        if self.source == "pdf":
            from . import pdfdoc

            return pdfdoc.render_page_bmp(self.path, index)
        return images.read_file(ref)

    def read_page_image_head(
        self, index: int, image_index: int, limit: int = HEAD_BYTES
    ) -> bytes:
        """The head of one image inside a page - the probe counterpart of
        `read_page_image()`. Same fallback shape, so a single-image page answers
        exactly like `read_page_head()`."""
        refs = self.page_image_refs(index)
        if not 0 <= image_index < len(refs):
            raise ArchiveError("这一页不存在")
        ref = refs[image_index]
        extractor = _EXTRACTED.get(self.source)
        if extractor is not None:
            return extractor.read_page_head(self.path, ref, limit)
        if self.source in ("cbz", "epub"):
            return archives.read_member_head(self.path, ref, limit)
        if self.source == "pdf":
            from . import pdfdoc

            return pdfdoc.render_page_bmp(self.path, index)[:limit]
        return images.read_file_head(ref, limit=limit)

    def read_page_head(self, index: int, limit: int = HEAD_BYTES) -> bytes:
        """The first `limit` bytes of one page - enough for a header probe.

        For a PDF this is the head of the *rendered* page, which means it costs a
        full render. Callers should prefer `ComicPage.width`/`height`, which
        `open_pdf` fills in from the document's own page boxes - no drawing at
        all. This path exists so a page whose size was somehow not known still
        gets measured rather than crashing.
        """
        page = self.page(index)
        if page is None:
            raise ArchiveError("这一页不存在")
        extractor = _EXTRACTED.get(self.source)
        if extractor is not None:
            return extractor.read_page_head(self.path, page.ref, limit)
        if self.source in ("cbz", "epub"):
            return archives.read_member_head(self.path, page.ref, limit)
        if self.source == "pdf":
            from . import pdfdoc

            return pdfdoc.render_page_bmp(self.path, index)[:limit]
        return images.read_file_head(page.ref, limit=limit)

    def set_page_size(self, index: int, width: int, height: int) -> bool:
        """Record a probed page size. Returns True when it changed anything.

        The UI calls this after `QImageReader.size()`; the return value lets the
        caller skip a repaint when the size was already known.
        """
        page = self.page(index)
        if page is None:
            return False
        width = max(0, int(width))
        height = max(0, int(height))
        if (page.width, page.height) == (width, height):
            return False
        page.width = width
        page.height = height
        return True

    def read_page_heads(self, indices, limit: int = HEAD_BYTES) -> list[bytes]:
        """`read_page_head()` for many pages at once, aligned with `indices`.

        Exists purely so the continuous-scroll mode can size every placeholder
        block in one pass: opening a zip once instead of once per page measured
        10x faster on a 300-page archive (252 ms -> 24 ms). See
        `archives.read_member_heads()`.

        Always returns exactly `len(indices)` items, with `b""` for a page that
        is missing, out of range, or unreadable - the caller turns that into
        "size unknown" rather than losing the layout.

        A PDF is deliberately NOT rendered here: `open_pdf()` already filled in
        every page box, so `sizes_known()` is True before anything is drawn and
        rendering 300 pages just to re-measure them would be minutes of work for
        an answer the document already gave. It answers `b""` for every page,
        which is the honest "I have no header to offer" - and the caller skips
        pages whose size is known, so this is never reached in practice.
        """
        wanted = [max(0, int(i)) for i in indices]
        if self.source == "pdf":
            return [b"" for _ in wanted]
        refs = [page.ref if page is not None else None for page in map(self.page, wanted)]
        extractor = _EXTRACTED.get(self.source)
        if extractor is not None:
            return extractor.read_page_heads(self.path, refs, limit)
        if self.source in ("cbz", "epub"):
            # An EPUB is a zip too, so the same batch reader works. It has to be
            # named explicitly: falling through to `images.read_file_heads()`
            # would treat zip member names as filesystem paths, and the result
            # would be a list of empty heads - silently "every page unknown",
            # which is exactly the failure the 10x batch path exists to avoid.
            return archives.read_member_heads(self.path, refs, limit)
        return images.read_file_heads(refs, limit)

    def sizes_known(self) -> bool:
        """True when every page has been measured."""
        return all(page.size_known for page in self.pages)

    # -- text-shaped attributes, so shared window paths degrade honestly --------
    #
    # MiniWindow was written for a text book and touches a handful of Book-only
    # attributes on paths that are not chapter navigation: the "encoding might be
    # wrong" hint, the reload path, the bookmark excerpt. Rather than sprinkle
    # `if kind == "comic"` through the window, a comic answers each one with the
    # honest value - nothing is encoded, nothing is reloadable, no characters
    # exist - and those paths then behave sensibly instead of raising.

    #: A comic's "encoding" is its container, and it is never uncertain.
    #: ClassVar so they stay class constants and do not become dataclass fields.
    encoding_label: ClassVar[str] = "漫画"
    confident: ClassVar[bool] = True

    @property
    def chapter_count(self) -> int:
        """Alias of `position_count`, for the window's shared end-of-book checks."""
        return len(self.pages)

    @property
    def chapters(self) -> list:
        """Empty: a comic has no chapters, so the TOC falls back to page numbers."""
        return []

    def chapter(self, index: int):
        """None, like `Book.chapter()` for an out-of-range index."""
        return None

    def chapter_body(self, index: int) -> str:
        """A comic has no text body. Present so shared paths cannot raise."""
        return ""

    def split_summary(self) -> str:
        return f"共 {len(self.pages)} 页"

    @property
    def text(self) -> str:
        """A comic has no text. Present so `--check` can print a length of 0."""
        return ""

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_path(cls, path, **kwargs) -> "ComicBook":
        """Open `path` as a comic, guessing the container from what it is.

        A directory is a folder of images, a zip container is a CBZ, and
        anything else must be a single image - the format table already decided
        the extension is one we accept, so the failure modes here are about the
        *contents* being wrong, not the name.
        """
        return open_comic(path)

    @classmethod
    def from_pdf(cls, path, **kwargs) -> "ComicBook":
        """Open a PDF as a stack of pages to draw.

        Separate from `from_path()` because a PDF is not guessed at: the caller
        has already decided (by `pdfdoc.classify()`, or because the user said so)
        that this document should be read as pictures.
        """
        return open_pdf(path)

    @classmethod
    def from_epub(cls, path, **kwargs) -> "ComicBook":
        """Open a fixed-layout EPUB as a stack of pages.

        Also not guessed at here: `opener.open_book()` reads the OPF's
        `rendition:layout` first and only calls this when the book says it is
        pre-paginated. A reflowable EPUB must keep going through
        `Book.from_path()` - it is a text book, and reading it as pictures would
        find nothing at all.
        """
        return open_epub_fixed(path)


def _title_for(path: str, source: str) -> str:
    """Book title: the file (or folder) name with its extension removed."""
    base = os.path.basename(path.rstrip("/\\")) or path
    if source == "folder":
        return base or "未命名"
    stem, ext = os.path.splitext(base)
    if ext.lower() in IMAGE_EXTS or ext:
        return stem or base or "未命名"
    return base or "未命名"


def _stat_of(path: str) -> tuple[int, float]:
    try:
        info = os.stat(path)
    except OSError:
        return 0, 0.0
    return int(info.st_size), float(info.st_mtime)


def _refs_or_book_error(listing) -> list[tuple[str, str]]:
    """Run a page-listing call, translating `ArchiveError` into `BookError`.

    Both types mean the same thing to a user - a message to read - and the
    difference is only who catches them. `MiniWindow.load_path()` catches
    `BookError` and shows the text verbatim; a bare `ArchiveError` would fall
    through to its generic handler and reach the reader as
    "打不开：ArchiveError: 这个文件夹里没有能看的图片", with a Python class name
    in the middle of a Chinese sentence.
    """
    try:
        return listing()
    except ArchiveError as exc:
        raise BookError(str(exc)) from exc


def open_comic(path) -> ComicBook:
    """Open any supported comic container.

    Raises `BookError` (the type `MiniWindow.load_path()` already catches and
    turns into a warning) for everything the user can act on.
    """
    raw = str(path)
    target = os.path.abspath(raw)

    if os.path.isdir(target):
        refs = _refs_or_book_error(
            lambda: [(r, os.path.basename(r)) for r in images.list_folder_pages(target)]
        )
        source = "folder"
    elif os.path.isfile(target):
        if archives.looks_like_archive(target):
            refs = _refs_or_book_error(
                lambda: [(e.ref, e.name) for e in archives.list_archive_pages(target)]
            )
            source = "cbz"
        elif rar.looks_like_rar(target):
            # Tested AFTER the zip branch on purpose: a large share of the .cbr
            # files in the wild are really ZIPs with the other extension (the
            # tools that made them just wrote whatever name the user asked for).
            # Reading one through the zip path costs nothing and avoids a
            # dependency on an external program for a book that never needed one.
            refs = _refs_or_book_error(
                lambda: [(e.ref, e.name) for e in rar.list_pages(target)]
            )
            source = "rar"
        elif cb7.looks_like_cb7(target):
            # Tested after the zip and RAR branches for the same reason RAR is
            # tested after zip: the signature is the only trustworthy answer, and
            # a `.cb7` that is really a zip should be read by the zip reader,
            # which needs no external program at all.
            refs = _refs_or_book_error(
                lambda: [(e.ref, e.name) for e in cb7.list_pages(target)]
            )
            source = "cb7"
        elif images.is_image_file(target):
            refs = _refs_or_book_error(
                lambda: [(r, os.path.basename(r)) for r in images.list_single_page(target)]
            )
            source = "image"
        else:
            raise BookError("这个文件既不是压缩包，也不是图片")
    else:
        raise BookError(f"找不到这个文件：{os.path.basename(raw) or raw}")

    if not refs:
        raise BookError("这本书里没有能看的图片")

    size, mtime = _stat_of(target)
    pages = [
        ComicPage(index=i, ref=ref, name=name)
        for i, (ref, name) in enumerate(refs)
    ]

    book = ComicBook(
        id=book_id(target),
        title=_title_for(target, source),
        path=target,
        size=size,
        mtime=mtime,
        source=source,
        pages=pages,
    )

    if source == "image":
        book.notes.append("这是一张单独的图片")
    if source == "folder" and len(pages) == 1:
        book.notes.append("这个文件夹里只有一张图片")
    return book


def open_pdf(path) -> ComicBook:
    """Open a PDF as a comic: one page per rendered page.

    `ref` is empty on purpose - a PDF page has no name to look up, it is
    addressed by index alone, which is what `ComicPage.ref`'s docstring already
    reserves the empty string for.

    The page sizes come from the document's own page boxes, in points, so
    `sizes_known()` is True before anything is drawn. The comic reader lays a page
    out at the size the *render* returns, which is `RENDER_SCALE` times larger,
    and `set_page_size()` corrects the record the first time each page is shown.
    """
    from . import pdfdoc

    raw = str(path)
    target = os.path.abspath(raw)
    if not os.path.isfile(target):
        raise BookError(f"找不到这个文件：{os.path.basename(raw) or raw}")

    sizes = pdfdoc.page_sizes(target)
    if not sizes:
        raise BookError("这个 PDF 里没有页面")

    size, mtime = _stat_of(target)
    pages = [
        ComicPage(index=i, ref="", name=f"第 {i + 1} 页", width=w, height=h)
        for i, (w, h) in enumerate(sizes)
    ]

    book = ComicBook(
        id=book_id(target),
        title=_title_for(target, "pdf"),
        path=target,
        size=size,
        mtime=mtime,
        source="pdf",
        pages=pages,
    )
    book.notes.append(f"按漫画打开，共 {len(pages)} 页")
    return book


def open_epub_fixed(path) -> ComicBook:
    """Open a fixed-layout EPUB as a comic: one page per spine item.

    The parsing lives in `epub.parse_fixed()` - this function's only job is to
    turn its pages into `ComicPage`s and carry the container's own answers
    (reading direction, spread policy) onto the book, where the window can pick
    them up.

    Page sizes are NOT filled in: an EPUB page is a JPEG or PNG in the zip, and
    measuring it needs `QImageReader`, which lives in the UI. So `width`/`height`
    start at 0 exactly like a CBZ page, and the same probe path fills them.

    A page made of several images keeps them all: `_page_images` records the list
    and `ComicPage.ref` points at the first one, which is what "the page's
    representative image" means when there is more than one.
    """
    from .epub import SPREAD_VALUES, EpubError, parse_fixed

    raw = str(path)
    target = os.path.abspath(raw)
    if not os.path.isfile(target):
        raise BookError(f"找不到这个文件：{os.path.basename(raw) or raw}")

    try:
        content = parse_fixed(target)
    except EpubError as exc:
        raise BookError(str(exc)) from exc

    if not content.pages:
        raise BookError("这本固定版式 EPUB 里没有找到任何页面图片")

    size, mtime = _stat_of(target)
    pages: list[ComicPage] = []
    page_images: dict[int, tuple[str, ...]] = {}
    for index, fixed in enumerate(content.pages):
        pages.append(
            ComicPage(index=index, ref=fixed.ref, name=f"第 {index + 1} 页")
        )
        if len(fixed.images) > 1:
            page_images[index] = fixed.images

    book = ComicBook(
        id=book_id(target),
        title=content.title or _title_for(target, "epub"),
        path=target,
        size=size,
        mtime=mtime,
        source="epub",
        pages=pages,
        notes=list(content.notes),
        _page_images=page_images,
        binding=content.progression if content.progression in ("rtl", "ltr") else "",
        spread=content.spread if content.spread in SPREAD_VALUES else "",
    )
    if content.author:
        book.notes.append(f"作者：{content.author}")
    book.notes.append(f"固定版式 EPUB，共 {len(pages)} 页")
    return book
