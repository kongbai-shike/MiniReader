"""Mode C: two pages side by side in one view.

`SpreadView` is a plain `QWidget` holding two `PageView`s in a row. It owns no
book and no cache: `ComicReader` hands it a `(first, second_or_None)` pair plus
pixmaps for them, exactly as it does for the single-page and strip modes. All the
"which page goes where" decisions live in `spread.py` as pure functions; this file
only lays widgets out.

Three decisions worth recording:

**Why a shared height rather than two width-fits.** Sizing each page to half the
viewport independently gives two differently-shaped pages two different heights,
so the seam between them is ragged - the one thing that makes a two-page view look
broken. `spread.pair_size()` returns one height for the pair and derives each
width from it, so the bottoms always line up.

**Why the binding is implemented by swapping widgets, not by mirroring.** Right-
binding (右綴じ, Japanese) reads right to left, so the lower page number sits on
the *right*. Swapping the two slots' order is the whole implementation: the page
widgets know nothing about direction, and neither does the layout once it has been
told the order. A lone page takes the leading slot (right for rtl, left for ltr),
which keeps the reading order continuous - a spread always begins at the leading
edge.

**Why there is no scrollbar here.** A spread is a discrete position like single-
page mode, not a continuous scroll like the strip. The reader's `scrollbar()`
answers None in this mode, which is what makes `MiniWindow.toggle_autoscroll()`'s
existing gate refuse it for the right reason - no new code needed, see
`docs/DEVELOPMENT.md`.

The zoom arithmetic lives in `comic_cache.fit_size()` and `spread.pair_size()`,
both pure, so the interesting rules are unit-testable without a QApplication.
"""

from __future__ import annotations

from ..qt import QHBoxLayout, QSizePolicy, QWidget, Qt
from .page_view import PageView
from .spread import pair_size, visual_order

#: Which slot a lone page goes in, per binding: "right" for 右綴じ, "left" for
#: 左綴じ - the leading edge of the reading direction. Kept here (rather than in
#: `spread.py`) because it documents the *widget* mapping; `spread.visual_order()`
#: is the one that actually implements it.
LONE_SLOT = {"rtl": "right", "ltr": "left"}


class SpreadView(QWidget):
    """Two pages side by side, sharing one height."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pair: tuple[int, int | None] = (0, None)
        self._binding = "rtl"
        self._gutter = 0
        self._zoom = "width"
        self._book = None

        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(self._gutter)

        # `_slots[0]` is the LEFT slot, `_slots[1]` the RIGHT one. Which page
        # number each shows is decided per pair by `_order()`, so a lone page in
        # rtl lands in `_slots[1]` without anyone having to move a widget.
        self._slots = [PageView(self), PageView(self)]
        for slot in self._slots:
            # Stretch 0, not 1: `_relayout()` sets an exact width on each slot, and
            # a stretch factor makes the layout override it (measured: a slot pinned
            # to 200 px wide still reported 200, but `setFixedHeight(300)` came back
            # as 800 because the row was distributing the leftover height).
            self._row.addWidget(slot, 0)
            # Ignored/Preferred, not Expanding: "Expanding" is a request to grow,
            # which fights the fixed size `_relayout()` just computed.
            slot.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        # No focus of its own: the page-turn keys and clicks are handled by
        # ComicReader, which is what the tests drive.
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # -- content ------------------------------------------------------------

    def bind_book(self, book) -> None:
        """The book whose page sizes this view sizes against.

        Page sizes come from the book (probed in mode B, or from the PDF's page
        boxes), never from a decoded pixmap - the same rule as `StripView`, and for
        the same reason: the layout must not depend on what has been decoded.
        """
        self._book = book
        self._relayout()

    def set_spread(self, pair: tuple[int, int | None]) -> None:
        """Show `(first, second_or_None)`.

        `pair` is in **reading** order - the lower page number first. This method
        applies the binding, so no caller has to think about left versus right.
        """
        first = max(0, int(pair[0]))
        second = None if pair[1] is None else max(0, int(pair[1]))
        self._pair = (first, second)
        left, right = self._order(self._pair)
        self._slots[0].set_page(None, self._label(left))
        self._slots[1].set_page(None, self._label(right))
        self._relayout()

    def set_page(self, index: int, pixmap, message: str = "") -> None:
        """Put one page's image into whichever slot currently shows it.

        Keyed by **page number**, not by slot, so the reader can decode pages in
        any order without having to track where each one landed.
        """
        left, right = self._order(self._pair)
        for slot, shown in ((self._slots[0], left), (self._slots[1], right)):
            if shown is not None and shown == int(index):
                slot.set_page(pixmap, message)
                return

    def clear(self) -> None:
        """Blank both slots."""
        self._pair = (0, None)
        for slot in self._slots:
            slot.clear()

    def _order(self, pair: tuple[int, int | None]) -> tuple[int | None, int | None]:
        """`(left_page, right_page)` for `pair`, honouring the binding."""
        return visual_order(pair, self._binding)

    @staticmethod
    def _label(index: int | None) -> str:
        return "" if index is None else f"第 {index + 1} 页"

    @property
    def pair(self) -> tuple[int, int | None]:
        return self._pair

    def pages_shown(self) -> tuple[int | None, int | None]:
        """`(left_page, right_page)` as currently laid out."""
        return self._order(self._pair)

    @property
    def anchor(self) -> int:
        return self._pair[0]

    def left_slot(self) -> PageView:
        return self._slots[0]

    def right_slot(self) -> PageView:
        return self._slots[1]

    # -- settings -----------------------------------------------------------

    def set_binding(self, binding: str) -> None:
        """rtl (右綴じ: lower page number on the right) or ltr (左綴じ)."""
        binding = binding if binding in LONE_SLOT else "rtl"
        if binding == self._binding:
            return
        self._binding = binding
        # Re-laying out the same pair is what performs the swap; there is no other
        # step, which is the point of implementing the binding this way.
        self.set_spread(self._pair)

    @property
    def binding(self) -> str:
        return self._binding

    def set_gutter(self, pixels: int) -> None:
        """Gap between the two pages, in pixels."""
        pixels = max(0, int(pixels))
        if pixels == self._gutter:
            return
        self._gutter = pixels
        self._relayout()

    @property
    def gutter(self) -> int:
        return self._gutter

    def set_text_colour(self, colour: str) -> None:
        for slot in self._slots:
            slot.set_text_colour(colour)

    def set_zoom(self, zoom: str) -> None:
        """Zoom applies to the *pair*, not per page, so it is remembered here and
        consumed by `_relayout()` through `pair_size()`."""
        self._zoom = zoom
        self._relayout()

    @property
    def zoom(self) -> str:
        return self._zoom

    # -- geometry -----------------------------------------------------------

    def _sizes(self) -> list[tuple[int, int]]:
        """The `(width, height)` of each shown page, in slot order."""
        sizes: list[tuple[int, int]] = []
        for index in self._order(self._pair):
            page = self._book.page(index) if (index is not None and self._book is not None) else None
            if page is not None and page.size_known:
                sizes.append((page.width, page.height))
            else:
                sizes.append((0, 0))
        return sizes

    def _relayout(self) -> None:
        """Recompute the shared height and both widths from the current viewport.

        Arithmetic only - page sizes come from the book, so nothing here decodes
        and nothing here does I/O.
        """
        self._row.setSpacing(self._gutter)
        sizes = self._sizes()
        if not any(size[0] > 0 for size in sizes):
            # Nothing measured yet: split the width evenly and let the heights be
            # the viewport's, so an unm easured book still paints something sane
            # rather than a zero-height pair.
            half = max(1, self.width() // 2)
            for slot in self._slots:
                slot.setFixedWidth(half)
                slot.setFixedHeight(max(1, self.height()))
            return
        height, widths = pair_size(sizes, (self.width(), self.height()), self._gutter)
        for slot, width in zip(self._slots, widths):
            if width > 0:
                slot.setFixedWidth(max(1, width))
            else:
                # A page with no known size gets the other half's width: it shows
                # a "broken page" message, not a zero-width sliver.
                slot.setFixedWidth(max(1, self.width() // 2))
            slot.setFixedHeight(max(1, height))
        self._row.activate()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Both pages are sized from the viewport, so a resize changes them. Pure
        # arithmetic again - no decode, no I/O.
        self._relayout()
