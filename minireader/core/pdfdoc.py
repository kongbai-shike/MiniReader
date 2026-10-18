"""Read PDFs, and decide which of the two readers each one belongs to.

A PDF has two identities and the extension cannot tell them apart. A novel that
was exported to PDF is a wall of text and belongs in the text reader; a comic
scanned to PDF is a stack of pictures and belongs in the comic reader. So this
module answers three questions:

* `classify()` - text or comic? Measured from the content, not guessed.
* `extract_text()` - the text side: every page's characters, one segment each.
* `render_page_bmp()` - the comic side: one page as bytes Qt can decode.

The classification is deliberately two-sided. Character density alone would call
a scanned page with a text watermark "text"; image coverage alone would call a
novel with a full-page background image "scanned". A page is only text if it has
real text in it (density >= `TEXT_DENSITY`), and only scanned if it has almost
no text AND is almost entirely covered by images. Anything else is ambiguous,
which is reported rather than silently decided.

**pypdfium2 is imported inside functions, never at module level.** It is an
optional dependency: a user who only reads TXT and CBZ never installs it, and
`import minireader.core` must not fail for them. `is_available()` answers whether
it is present without importing it.

**Why the comic side renders to BMP.** `minireader.core.*` is Qt-free by
contract (see docs/DEVELOPMENT.md), so a rendered page has to leave this module
as *bytes*, and the bytes have to be in a format Qt decodes without any plugin.
BMP is the only such format: PNG and JPEG both go through imageformats plugins,
which a PyInstaller build has to be told to collect. Writing a 32-bit bottom-up
BMP is also just a row reversal - pdfium already hands back the exact channel
order a BMP wants (see `render_page_bmp`).

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from .book_base import KIND_COMIC, KIND_TEXT
from .model import BookError

#: Shown when pypdfium2 is missing. Names the exact command, because "install a
#: PDF library" is not actionable and the user has to leave the app to do it.
PDFIUM_NOTE = "打开 PDF 需要安装 pypdfium2：pip install pypdfium2"

#: Characters per square metre of page area at or above which a page counts as
#: text. Measured on real files: a text PDF scores ~5700, a scanned one 0. The
#: gap is three orders of magnitude wide, so the exact threshold is not delicate.
TEXT_DENSITY = 200.0

#: Fraction of the page area that image objects must cover for a page with no
#: text to count as scanned. A full-page scan covers 1.0.
SCAN_COVERAGE = 0.7

#: Pages are rendered at this multiple of the PDF's own 72 dpi. 2.0 = 144 dpi,
#: which is sharp on screen and keeps a Letter page at 1224x1584 (~7.4 MB in the
#: cache, so ~33 pages fit the default 256 MB budget).
RENDER_SCALE = 2.0

#: How many pages the classifier samples. Five is enough to catch a PDF whose
#: first page is a cover image, and cheap: measuring a page's text and objects
#: costs about a millisecond.
SAMPLE_LIMIT = 5


@dataclass(frozen=True)
class PdfSegment:
    """One page's text, in the shape `Book.from_segments()` consumes.

    Deliberately its own type rather than `epub.Segment`: the two mean the same
    thing, but importing the EPUB parser to borrow a two-field dataclass would
    pull in `zipfile` and `xml.etree` every time a PDF is opened.
    """

    title: str
    text: str


@dataclass
class PdfInfo:
    """What `classify()` found out about a PDF.

    `kind` is `KIND_TEXT` or `KIND_COMIC`; `needs_ask` is True when the evidence
    was ambiguous and the window should ask the user once rather than commit.
    """

    pages: int
    kind: str
    density: float
    coverage: float
    sampled: int
    needs_ask: bool = False
    notes: list[str] = field(default_factory=list)


def is_available() -> bool:
    """Whether pypdfium2 can be imported, without importing it.

    `find_spec` walks `sys.path` only, so this stays cheap and safe to call on
    every open. It answers "installed", not "working" - a broken install is
    reported by `classify()` with the same user-facing message.
    """
    from importlib.util import find_spec

    try:
        return find_spec("pypdfium2") is not None
    except (ImportError, ValueError):
        # A module whose parent is missing raises rather than returning None.
        return False


def _pdfium():
    """Import pypdfium2 or raise the one error the user can act on.

    Every entry point calls this first, so a missing dependency produces
    `PDFIUM_NOTE` instead of a traceback about a module named pypdfium2.
    """
    try:
        import pypdfium2
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise BookError(PDFIUM_NOTE) from exc
    return pypdfium2


def _load(path):
    """Open a document, translating every failure into a user-facing message.

    pdfium reports all of these as one `PdfiumError` whose text is written for
    C programmers ("Failed to load document (PDFium: Incorrect password
    error)"). The mapping below is the whole point of this function.

    A missing file raises `FileNotFoundError`, not a `PdfiumError`, so that case
    is caught separately - otherwise it would escape as a traceback.
    """
    pdfium = _pdfium()
    target = os.path.abspath(str(path))
    try:
        return pdfium.PdfDocument(target)
    except FileNotFoundError as exc:
        raise BookError(f"找不到这个文件：{os.path.basename(target)}") from exc
    except OSError as exc:
        raise BookError(f"读不到这个文件：{exc.strerror or exc}") from exc
    except Exception as exc:  # PdfiumError subclasses RuntimeError
        text = str(exc)
        if "password" in text.lower():
            raise BookError("这个 PDF 有密码保护，打不开") from exc
        if "format" in text.lower() or "file" in text.lower():
            raise BookError("这个文件不是有效的 PDF") from exc
        # pdfium refuses to load a zero-page document, and reports it with the
        # baffling text "Failed to load document (PDFium: Success)". There is no
        # document to inspect, so there is nothing else it can be.
        raise BookError("这个 PDF 里没有页面") from exc


def _sample_indices(pages: int) -> list[int]:
    """Which pages to measure: all of them when short, else five spread out.

    Spread rather than the first five, because the first page of a scanned comic
    is often a cover with a title on it, and the first five of a novel may all be
    front matter.
    """
    if pages <= SAMPLE_LIMIT:
        return list(range(pages))
    step = pages / SAMPLE_LIMIT
    picked = [int(step * i) for i in range(SAMPLE_LIMIT)]
    # `int(step * i)` can repeat for small page counts and never reaches the last
    # page, which is often the only one that differs (an index, a colophon).
    picked[-1] = pages - 1
    return sorted(set(picked))


def _page_stats(page) -> tuple[int, float, float]:
    """(characters, area in points squared, image coverage) for one page.

    Coverage walks the page's objects rather than its raw content stream, and
    uses the high-level `get_bounds()`. The raw call
    (`raw.FPDFPageObj_GetBounds` with a ctypes float array) writes to the wrong
    place from Python and returns garbage - measured: a full-page scan reported
    `(792.0, 792.0, 792.0, 792.0)`.
    """
    width, height = page.get_size()
    area = abs(float(width) * float(height))

    chars = 0
    textpage = page.get_textpage()
    try:
        chars = int(textpage.count_chars())
    finally:
        textpage.close()

    covered = 0.0
    if area > 0:
        # Object access is the only call here that can fail on a damaged page,
        # and a page we cannot inspect is simply a page with no images on it -
        # not a reason to fail the whole book.
        try:
            for obj in page.get_objects():
                # 3 == FPDF_PAGEOBJ_IMAGE. Checked by value because the raw
                # constants module is an implementation detail of pypdfium2.
                if getattr(obj, "type", None) != 3:
                    continue
                x0, y0, x1, y1 = obj.get_bounds()
                covered += abs((x1 - x0) * (y1 - y0))
        except Exception:  # noqa: BLE001 - a damaged page has no coverage
            covered = 0.0

    return chars, area, (covered / area if area > 0 else 0.0)


def classify(path) -> PdfInfo:
    """Decide whether `path` is a text PDF or a scanned one.

    Returns `PdfInfo`; `needs_ask` is True when the evidence was ambiguous, which
    means the caller should confirm with the user instead of committing.
    """
    doc = _load(path)
    try:
        pages = len(doc)
        if pages <= 0:
            raise BookError("这个 PDF 里没有页面")

        indices = _sample_indices(pages)
        total_chars = 0
        total_area = 0.0
        total_cover = 0.0
        for i in indices:
            page = doc[i]
            try:
                chars, area, cover = _page_stats(page)
            finally:
                page.close()
            total_chars += chars
            total_area += area
            total_cover += cover

        sampled = len(indices)
        area_m = (total_area / sampled) / 1e6
        density = (total_chars / sampled) / area_m if area_m > 0 else 0.0
        coverage = total_cover / sampled
    finally:
        doc.close()

    if density >= TEXT_DENSITY:
        return PdfInfo(pages, KIND_TEXT, density, coverage, sampled)

    if coverage >= SCAN_COVERAGE:
        return PdfInfo(pages, KIND_COMIC, density, coverage, sampled)

    # Almost no text and almost no picture: a cover-only PDF, a blank template,
    # or a comic whose pages are drawn as vectors. Nothing measured can settle
    # it, so say so and let the window ask once.
    return PdfInfo(pages, KIND_TEXT, density, coverage, sampled, needs_ask=True)


def page_count(path) -> int:
    """How many pages, or raise `BookError` if the document will not open."""
    doc = _load(path)
    try:
        return len(doc)
    finally:
        doc.close()


def page_sizes(path) -> list[tuple[int, int]]:
    """Every page's size in points, in order.

    The comic reader uses these to lay pages out before anything is rendered, so
    opening a PDF does not decode page one before it can draw a scrollbar.
    """
    doc = _load(path)
    try:
        sizes = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                width, height = page.get_size()
            finally:
                page.close()
            sizes.append((int(round(float(width))), int(round(float(height)))))
        return sizes
    finally:
        doc.close()


def extract_text(path) -> list[PdfSegment]:
    """The text of every page as a segment, in order.

    One segment per page, which is how a PDF is actually organised: page breaks
    are real boundaries and the TXT chapter regex would both miss them and invent
    chapters out of in-text "第一章" mentions.

    Pages with no text at all are skipped by `Book.from_segments`, which also
    counts them, so a scan that was misread as text still produces a readable
    book rather than an empty one.
    """
    doc = _load(path)
    try:
        segments: list[PdfSegment] = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                textpage = page.get_textpage()
                try:
                    body = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            segments.append(PdfSegment(f"第 {i + 1} 页", _tidy(body)))
        return segments
    finally:
        doc.close()


def _tidy(body: str) -> str:
    """Normalise what pdfium hands back.

    pdfium separates lines with CRLF and ends a block with a trailing CR, which
    would otherwise show up as blank lines at every paragraph. Lines are kept
    (a PDF's line breaks are all the structure there is) but the stray CRs and
    the trailing whitespace they leave behind are not.
    """
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    # Collapse the runs of blank lines pdfium emits between text blocks.
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            blanks = 0
            out.append(line)
            continue
        blanks += 1
        if blanks <= 1:
            out.append("")
    return "\n".join(out).strip("\n")


def write_bmp(width: int, height: int, pixels: bytes, stride: int) -> bytes:
    """Wrap raw 32-bit pixels in a bottom-up BI_RGB BMP.

    BMP is the one image format Qt decodes with no plugin at all, and 32-bit
    BI_RGB wants exactly the byte order pdfium produces, so this is a row
    reversal and a 54-byte header - no per-pixel work, which is what makes it
    fast enough to run on every page turn (measured: 14.5 ms for a 1224x1584
    page, against 658 ms for the obvious per-pixel loop).

    Rows go bottom-up because that is what a positive `height` in the header
    means; `stride` is honoured rather than assumed to be `width * 4`, because
    pdfium pads 24-bit output and a caller may hand us either.
    """
    row_bytes = width * 4
    if stride < row_bytes:
        stride = row_bytes
    rows = [pixels[y * stride: y * stride + row_bytes] for y in range(height - 1, -1, -1)]
    body = b"".join(rows)

    header = b"BM" + struct.pack("<IHHI", 14 + 40 + len(body), 0, 0, 14 + 40)
    dib = struct.pack(
        "<IiiHHIIiiII",
        40,          # header size
        width,
        height,      # positive: rows are stored bottom-up
        1,           # planes
        32,          # bits per pixel
        0,           # BI_RGB: no compression, byte order is B, G, R, unused
        len(body),
        2835,        # 72 dpi, in pixels per metre
        2835,
        0,
        0,
    )
    return header + dib + body


def render_page_bmp(path, index: int, scale: float = RENDER_SCALE) -> bytes:
    """One page as BMP bytes, ready for `QPixmap.loadFromData`.

    `fill_color` is passed with a zero alpha on purpose: it is what makes pdfium
    hand back four channels instead of three. With alpha, `rev_byteorder` must
    stay False - the default output is already B, G, R, A, which is what a
    32-bit BMP stores, so the bytes are copied verbatim. Measured both ways
    against a page painted half red and half blue: `rev_byteorder=True` swaps
    red and blue on screen.
    """
    doc = _load(path)
    try:
        total = len(doc)
        if not 0 <= int(index) < total:
            raise BookError("这一页不存在")
        page = doc[int(index)]
        try:
            bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 0))
            try:
                width = int(bitmap.width)
                height = int(bitmap.height)
                stride = int(bitmap.stride)
                if width <= 0 or height <= 0:
                    raise BookError("这一页是空的")
                return write_bmp(width, height, bytes(bitmap.buffer), stride)
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        doc.close()
