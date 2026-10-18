"""Decode pages, and remember the ones we decoded recently.

Three jobs, all of which the plan measured before writing:

**Decoding.** A page arrives as raw bytes - out of a zip member, off the disk -
and becomes a `QPixmap`. Every failure is reported as a friendly string rather
than an exception, because a comic archive with three corrupt pages in it should
still be readable for the other four hundred.

**The cache.** An `OrderedDict` used as an LRU, keyed by `(book_id, page_index)`.
The limit is a MEMORY budget, not a page count: a 4000x6000 PNG decodes to about
96 MB, so "keep 20 pages" would be 2 GB of RAM on a high-resolution archive and
50 MB on a low-resolution one. `QPixmap.width() * height() * 4` is the accounting
unit, which is what the plan specifies.

**The probe.** `probe_size()` answers "how big is this page?" WITHOUT decoding
it, by handing the first 64 KB to `QImageReader`. That is the only way the
continuous-scroll mode can lay out placeholder blocks of the right height before
the images arrive - without it the scrollbar thrashes as pages load.

Nothing here writes to disk and nothing here knows about window.py.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from ..qt import QBuffer, QByteArray, QColor, QImageReader, QPainter, QPixmap, Qt

#: Default cache budget in MB. The plan fixed 256 MB as the default with a
#: 64-1024 range, and the settings dialog exposes it.
DEFAULT_CACHE_MB = 256

#: How many bytes of a page to read when probing its size. Every image header Qt
#: understands is far smaller than this.
PROBE_BYTES = 64 * 1024


def size_from_head(head: bytes) -> tuple[int, int]:
    """(width, height) from a page's leading bytes, without decoding it.

    The one place `QImageReader` is asked about a size. `probe_size()` (one page,
    read on its own) and `probe_sizes()` (many pages, read in one pass) both end
    up here so the two paths cannot drift - a placeholder height computed one way
    and a page drawn the other would make the strip jump.

    Never raises: (0, 0) means "unknown", and the caller lays that page out at the
    viewport size.
    """
    if not head:
        return (0, 0)
    try:
        # `payload` must stay a named local for the whole call. QBuffer's C++
        # constructor takes `QByteArray *` - a *borrowed* pointer - so SIP does not
        # take ownership of what it is handed. Writing `QBuffer(QByteArray(head))`
        # therefore leaves the buffer pointing at freed memory the moment the
        # temporary is collected, and the crash is a non-deterministic access
        # violation (measured: 1 run in 8, and 5/5 once the call is put in a loop).
        payload = QByteArray(head)
        buffer = QBuffer(payload)
        buffer.open(QBuffer.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
    except Exception:  # noqa: BLE001
        return (0, 0)
    if not size.isValid():
        return (0, 0)
    return (max(0, size.width()), max(0, size.height()))


def probe_size(book, index: int) -> tuple[int, int]:
    """(width, height) of a page without decoding it. (0, 0) when unknown.

    Never raises: an unreadable page gets (0, 0) and the caller falls back to
    laying it out at the viewport size. A comic with one broken page must not
    take the whole book down.

    One page at a time. To size a whole book, use `probe_sizes()`: this function
    reopens the container for every call, which is the right trade for a single
    page and the wrong one for three hundred.
    """
    try:
        count = book.page_image_count(index)
    except Exception:  # noqa: BLE001 - any container failure means "unknown"
        return (0, 0)
    if count <= 1:
        try:
            head = book.read_page_head(index, limit=PROBE_BYTES)
        except Exception:  # noqa: BLE001
            return (0, 0)
        return size_from_head(head)
    # A page made of several images is as tall as all of them stacked and as wide
    # as the widest one - which is exactly what `stitch()` will produce, so the
    # placeholder block has the right height before the images are decoded.
    return _stacked_size(book, index, count)


def _stacked_size(book, index: int, count: int) -> tuple[int, int]:
    """The size `stitch()` will give this page, measured from headers only.

    Two passes on purpose: `stitch()` scales every piece to the WIDEST piece, so
    the final height depends on a width that is only known after all pieces have
    been measured. Accumulating in one pass would answer for the first piece's
    width and be wrong for every page whose pieces differ in width.
    """
    sizes: list[tuple[int, int]] = []
    for image_index in range(count):
        try:
            head = book.read_page_image_head(index, image_index, limit=PROBE_BYTES)
        except Exception:  # noqa: BLE001
            continue
        width, height = size_from_head(head)
        if width > 0 and height > 0:
            sizes.append((width, height))
    if not sizes:
        return (0, 0)
    width = max(size[0] for size in sizes)
    height = sum(max(1, round(h * (width / w))) for w, h in sizes)
    return (width, height)


def probe_sizes(book, indices) -> dict[int, tuple[int, int]]:
    """Sizes for many pages, reading the container **once**.

    Returns `{index: (width, height)}` for every index asked about, with
    `(0, 0)` for the ones that could not be measured - the caller decides what an
    unknown page should look like, and a missing key would force it to guess.

    Measured on a 300-page CBZ: 252 ms one page at a time versus 24 ms this way,
    because the expensive part was never the inflate, it was building a fresh
    `ZipFile` (central directory plus 300 `ZipInfo` objects) for each page. That
    10x is what makes "every placeholder block has its real height in the first
    frame" affordable instead of a 0.25 s stall on open.
    """
    wanted = [int(i) for i in indices]
    if not wanted:
        return {}
    # A page made of several images cannot be measured from one header, so those
    # pages are pulled out and answered separately. This is the fixed-layout EPUB
    # case; every other source has `page_image_count() == 1` and stays on the
    # single-pass path below.
    multi: list[int] = []
    single: list[int] = []
    for index in wanted:
        try:
            count = book.page_image_count(index)
        except Exception:  # noqa: BLE001
            count = 1
        (multi if count > 1 else single).append(index)
    out: dict[int, tuple[int, int]] = {index: (0, 0) for index in wanted}
    for index in multi:
        try:
            count = book.page_image_count(index)
        except Exception:  # noqa: BLE001
            continue
        out[index] = _stacked_size(book, index, count)
    if not single:
        return out
    try:
        heads = book.read_page_heads(single, limit=PROBE_BYTES)
    except Exception:  # noqa: BLE001 - a broken container means every size unknown
        return out
    # `zip(..., strict=False)` semantics on purpose: if a container ever answered
    # with a short list, the pages it did not mention are "unknown" rather than a
    # silently truncated layout.
    for index, head in zip(single, heads):
        out[index] = size_from_head(head)
    return out


def decode(data: bytes) -> tuple[QPixmap | None, str]:
    """Decode encoded image bytes into a QPixmap.

    Returns `(pixmap, "")` on success and `(None, message)` on failure, where the
    message is already in the user's language. `QPixmap.loadFromData` returning
    False is the ordinary "this file is not really a JPEG" case and is the reason
    this returns a message instead of raising.
    """
    if not data:
        return None, "这一页是空的"
    pixmap = QPixmap()
    try:
        ok = pixmap.loadFromData(QByteArray(data))
    except Exception as exc:  # noqa: BLE001
        return None, f"这一页解码失败：{exc}"
    if not ok or pixmap.isNull():
        return None, "这一页不是有效的图片"
    return pixmap, ""


def stitch(pixmaps) -> tuple[QPixmap | None, str]:
    """Stack several decoded images into one page, top to bottom.

    A fixed-layout EPUB may export one logical page as several image files (a
    double-page spread cut into halves, or a page whose text was layered on as a
    separate image). The reader shows them as one page, which means gluing them
    into one pixmap here rather than teaching every view about page groups.

    The images are scaled to a common width - the widest one - keeping each one's
    aspect ratio, because they are pieces of the same page and a piece that is
    narrower than the page is a piece that must not be stretched. A single image
    is returned untouched, so the common case costs nothing.

    Returns `(pixmap, "")` or `(None, message)` like `decode()`.
    """
    kept = [pixmap for pixmap in pixmaps if pixmap is not None and not pixmap.isNull()]
    if not kept:
        return None, "这一页是空的"
    if len(kept) == 1:
        return kept[0], ""

    width = max(pixmap.width() for pixmap in kept)
    height = 0
    scaled: list[QPixmap] = []
    for pixmap in kept:
        if pixmap.width() != width:
            factor = width / pixmap.width() if pixmap.width() else 1.0
            pixmap = pixmap.scaled(
                width,
                max(1, round(pixmap.height() * factor)),
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )
        scaled.append(pixmap)
        height += pixmap.height()
    if width <= 0 or height <= 0:
        return None, "这一页是空的"

    canvas = QPixmap(width, height)
    canvas.fill(QColor(0, 0, 0, 0))
    painter = QPainter(canvas)
    try:
        offset = 0
        for pixmap in scaled:
            painter.drawPixmap(0, offset, pixmap)
            offset += pixmap.height()
    finally:
        # end() must run even if a draw raises: Qt asserts about a painter that
        # is still active when its device goes away.
        painter.end()
    return canvas, ""


class PageCache:
    """An LRU of decoded pages with a memory budget.

    Deliberately not a QObject and not a QCache: the accounting has to be in
    bytes, and `QCache` counts items.
    """

    def __init__(self, budget_mb: int = DEFAULT_CACHE_MB) -> None:
        self._items: OrderedDict[tuple[str, int], QPixmap] = OrderedDict()
        self._bytes = 0
        self._budget = 0
        self.set_budget_mb(budget_mb)

    # -- budget ---------------------------------------------------------------

    @property
    def budget_mb(self) -> int:
        return self._budget // (1024 * 1024)

    def set_budget_mb(self, budget_mb: int) -> None:
        """Change the budget, evicting immediately if the new one is smaller."""
        try:
            value = int(budget_mb)
        except (TypeError, ValueError):
            value = DEFAULT_CACHE_MB
        # The settings dialog clamps to 64-1024; clamp again here so a hand
        # edited settings.json cannot make the cache useless or unbounded.
        value = max(64, min(value, 1024))
        self._budget = value * 1024 * 1024
        self._evict()

    # -- accounting -----------------------------------------------------------

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def mb_used(self) -> float:
        return self._bytes / (1024.0 * 1024.0)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: tuple[str, int]) -> bool:
        return key in self._items

    @staticmethod
    def _cost(pixmap: QPixmap) -> int:
        return max(1, pixmap.width()) * max(1, pixmap.height()) * 4

    def _evict(self) -> None:
        while self._items and self._bytes > self._budget:
            _key, pixmap = self._items.popitem(last=False)
            self._bytes -= self._cost(pixmap)

    # -- access ---------------------------------------------------------------

    def get(self, book_id: str, index: int) -> QPixmap | None:
        """The cached page, moved to the most-recently-used end."""
        key = (book_id, int(index))
        pixmap = self._items.get(key)
        if pixmap is None:
            return None
        self._items.move_to_end(key)
        return pixmap

    def put(self, book_id: str, index: int, pixmap: QPixmap) -> None:
        """Store a page, evicting from the cold end until the budget is met."""
        if pixmap is None or pixmap.isNull():
            return
        key = (book_id, int(index))
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= self._cost(old)
        self._items[key] = pixmap
        self._bytes += self._cost(pixmap)
        self._evict()

    def load(self, book, index: int) -> tuple[QPixmap | None, str]:
        """Cached decode of one page. Returns `(pixmap, message)`."""
        cached = self.get(book.id, index)
        if cached is not None:
            return cached, ""
        try:
            count = book.page_image_count(index)
        except Exception:  # noqa: BLE001 - a container that cannot count is a
            count = 1      # container that will fail on read anyway
        if count <= 1:
            try:
                data = book.read_page(index)
            except Exception as exc:  # noqa: BLE001 - container errors are user-facing
                return None, str(exc) or "读不出这一页"
            pixmap, message = decode(data)
        else:
            # A page made of several images: decode each and glue them into one.
            # Done here rather than in the views so mode A, B and C all get the
            # same single pixmap for a page, which is what they already know how
            # to display.
            pieces = []
            message = ""
            for image_index in range(count):
                try:
                    data = book.read_page_image(index, image_index)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc) or "读不出这一页"
                    continue
                piece, piece_message = decode(data)
                if piece is None:
                    message = message or piece_message
                    continue
                pieces.append(piece)
            pixmap, stitch_message = stitch(pieces)
            message = message or stitch_message
        if pixmap is not None:
            self.put(book.id, index, pixmap)
        return pixmap, message

    def forget_book(self, book_id: str) -> int:
        """Drop every page of one book. Returns how many entries went."""
        keys = [key for key in self._items if key[0] == book_id]
        for key in keys:
            pixmap = self._items.pop(key)
            self._bytes -= self._cost(pixmap)
        return len(keys)

    def clear(self) -> None:
        """Drop everything - used when the window hides or the book closes."""
        self._items.clear()
        self._bytes = 0

    def stats(self) -> str:
        """One line for the tooltip, e.g. "缓存 12 页 / 84.3 MB"."""
        return f"缓存 {len(self._items)} 页 / {self.mb_used:.1f} MB"


def fit_size(
    source: tuple[int, int],
    viewport: tuple[int, int],
    zoom: str,
) -> tuple[int, int]:
    """The on-screen size for a page, honouring the zoom mode.

    `zoom` is "width" (fit the viewport's width), "height" (fit its height) or
    "original" (no scaling). Both fits keep the aspect ratio, so a page is never
    stretched. A page smaller than the viewport is never enlarged in either fit
    mode - blowing up a 600px-wide scan to 1400px only makes it blurry, and the
    "original" mode is right there if the user wants the real pixels.

    Pure arithmetic, so it is unit-testable without a QApplication.
    """
    src_w, src_h = max(0, int(source[0])), max(0, int(source[1]))
    view_w, view_h = max(0, int(viewport[0])), max(0, int(viewport[1]))
    if src_w <= 0 or src_h <= 0:
        return (view_w, view_h)
    if zoom == "original":
        return (src_w, src_h)

    if zoom == "height":
        if view_h <= 0:
            return (src_w, src_h)
        scale = view_h / src_h
    else:  # "width" and anything unrecognised
        if view_w <= 0:
            return (src_w, src_h)
        scale = view_w / src_w

    scale = min(scale, 1.0)
    return (max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale))))


def scale_pixmap(pixmap: QPixmap, size: tuple[int, int]) -> QPixmap:
    """Scale a page to `size`, picking the resampling mode by direction.

    Downscaling uses `SmoothTransformation` (worth the cost, it is the difference
    between readable text and aliased mush) and upscaling uses
    `FastTransformation` (smooth upscaling of a scan is expensive and looks no
    better - it just costs a frame).
    """
    width, height = max(1, int(size[0])), max(1, int(size[1]))
    if pixmap.width() == width and pixmap.height() == height:
        return pixmap
    shrinking = width < pixmap.width() or height < pixmap.height()
    mode = Qt.SmoothTransformation if shrinking else Qt.FastTransformation
    return pixmap.scaled(width, height, Qt.KeepAspectRatio, mode)
