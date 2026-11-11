"""Mode B: the whole book laid out as one vertical strip.

`StripView` is a `QScrollArea` whose content widget holds one `PageView` per page,
stacked top to bottom. It owns no book and no cache: the `ComicReader` hands it a
list of page sizes, then feeds it pixmaps for whichever pages are near the
viewport. That split keeps the interesting decisions - what to decode, when to
forget it - in one place.

Three decisions worth recording:

**Why the placeholder heights come from `ComicBook` page sizes, not from the
decoded pixmaps.** The scrollbar's range is the sum of the slot heights. If a
slot's height were only known once its image arrived, the range would grow as the
user scrolled and every page that finished decoding would shove the thumb
sideways. So every slot is laid out from `ComicPage.width/height` - probed from
the container header, or taken from the PDF's own page box - *before* a single
image is decoded. That is the only thing that makes the scrollbar hold still,
which is why `ComicReader._ensure_sizes()` pays a one-off probe pass when the
mode is entered.

**Why the vertical scrollbar is always on.** With `ScrollBarAsNeeded` a strip
that is *just* taller than the viewport makes the bar appear, which narrows the
viewport, which (in width-fit) makes every page shorter, which can make the strip
fit again - so the bar flickers on and off. Reserving its width permanently
removes the feedback path entirely, and a continuous comic strip is a scrollable
thing whether or not it currently overflows.

**Why it emits rather than decodes.** `bandChanged` says "these pages are near
the viewport"; the reader decides what to load. `currentChanged` says "the page
under the middle of the viewport is now N", which is what progress persistence
means in a mode with no discrete page turns. Both are cheap and synchronous, so
the bottom bar keeps up with a fast scroll; only the *decoding* pass is debounced.

The height arithmetic lives in `comic_cache.fit_size()`, the same pure function
mode A uses, so a page is never sized differently in the two modes.
"""

from __future__ import annotations

from bisect import bisect_right

from ..qt import (
    QEvent,
    QFrame,
    QScrollArea,
    QSizePolicy,
    QTimer,
    QVBoxLayout,
    QWidget,
    Qt,
    Signal,
)
from .comic_cache import fit_size
from .page_view import ZOOM_MODES, PageView

#: How many pages either side of the visible band to keep decoded. The reader
#: owns the actual loading; this is the default radius it asks for.
LAZY_RADIUS = 3

#: A scroll only settles into a decode pass after this long. Fast scrolling
#: through a 300-page archive would otherwise start hundreds of decodes that are
#: discarded before they can be painted.
LAZY_DEBOUNCE_MS = 60

#: How far one mouse wheel notch scrolls the strip, in pixels. Qt reports a
#: notch as 120 eighths of a degree; 100 px is a little under one short page, so
#: a notch moves the view without skipping past a whole page.
WHEEL_STEP_PX = 100.0


class StripView(QScrollArea):
    """Every page of a comic in one scrolling column."""

    #: `(first, last)` inclusive page indices that should be decoded.
    bandChanged = Signal(int, int)
    #: The page under the middle of the viewport changed.
    currentChanged = Signal(int)
    #: The user kept scrolling past an end: -1 at the bottom, +1 at the top.
    boundaryReached = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # See the module docstring: reserving the width is what stops the
        # appear/disappear feedback loop in width-fit layout.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._content = QWidget(self)
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        # Pages stack from the top; the stretch absorbs the slack when the whole
        # book is shorter than one screen.
        self._layout.addStretch(1)
        self.setWidget(self._content)

        # The slots paint the pages, so the pointer is over a slot - never over
        # the scroll area itself - whenever the user is reading. Qt delivers a
        # wheel event only to the widget under the pointer (measured: it does NOT
        # propagate to ancestors), and `QScrollArea`'s own wheel handling is
        # driven from its internal viewport, so a notch over a slot would reach
        # neither this class nor the scrollbar. Installing a filter on the
        # content widget and its children routes the whole subtree back here.
        self._content.installEventFilter(self)
        self.viewport().installEventFilter(self)

        self._slots: list[PageView] = []
        self._sizes: list[tuple[int, int]] = []
        self._heights: list[int] = []
        self._offsets: list[int] = []
        self._zoom = ZOOM_MODES[0]
        self._fg = ""
        self._current = 0
        self._syncing = False
        #: Which end has already been announced: -1 bottom, +1 top, 0 neither.
        #: The first notch past an end reports it; every further notch at the
        #: same end is swallowed so a trackpad flick does not spray the same
        #: sentence ten times. Cleared as soon as the bar leaves that end.
        self._boundary_latched = 0

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LAZY_DEBOUNCE_MS)
        self._timer.timeout.connect(self._apply_band)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ---- content ------------------------------------------------------------

    def set_pages(self, sizes) -> None:
        """Lay out one slot per `(width, height)` pair.

        Called with every page's size, known or not: a page whose size could not
        be probed gets a viewport-height block rather than a zero-height one, so
        a single unreadable page cannot collapse the strip's scroll range.
        """
        self._sizes = [(max(0, int(w)), max(0, int(h))) for w, h in sizes]
        self._rebuild()

    def count(self) -> int:
        return len(self._slots)

    def slot(self, index: int) -> PageView | None:
        """The widget for one page, or None. Tests use this to inspect a block."""
        if 0 <= index < len(self._slots):
            return self._slots[index]
        return None

    def set_page(self, index: int, pixmap, message: str = "") -> None:
        slot = self.slot(index)
        if slot is not None:
            slot.set_page(pixmap, message)

    def clear_page(self, index: int) -> None:
        """Put a slot back to its placeholder - the page scrolled out of range."""
        slot = self.slot(index)
        if slot is not None:
            slot.set_page(None, self.placeholder(index))

    def placeholder(self, index: int) -> str:
        """What an unloaded slot shows. The page number, so a fast scroll still
        tells you where you are."""
        return f"第 {index + 1} 页"

    # ---- zoom and colour ----------------------------------------------------

    def set_zoom(self, zoom: str) -> None:
        """Change the zoom and recompute every slot height.

        Returns early when the zoom has not actually changed: `_relayout()` walks
        every slot, so on a 300-page book it is ~3 ms of pure arithmetic, and
        `ComicReader._render_strip()` calls this on every `goto()` to keep the
        strip and the zoom setting in step. Doing that work when nothing changed
        would put a needless 3 ms in front of every page turn.
        """
        zoom = zoom if zoom in ZOOM_MODES else ZOOM_MODES[0]
        if zoom == self._zoom:
            return
        self._zoom = zoom
        for slot in self._slots:
            slot.set_zoom(self._zoom)
        self._relayout()

    @property
    def zoom(self) -> str:
        return self._zoom

    def set_text_colour(self, colour: str) -> None:
        """The colour of the placeholder and "broken page" text."""
        self._fg = colour
        for slot in self._slots:
            slot.set_text_colour(colour)

    # ---- geometry -----------------------------------------------------------

    def slot_height(self, index: int) -> int:
        """The laid-out height of one slot, in pixels."""
        if 0 <= index < len(self._heights):
            return self._heights[index]
        return 0

    def page_top(self, index: int) -> int:
        """The y offset of one page inside the strip."""
        if 0 <= index < len(self._offsets):
            return self._offsets[index]
        return 0

    def total_height(self) -> int:
        return sum(self._heights)

    def _slot_height(self, index: int) -> int:
        width, height = self._sizes[index]
        if width <= 0 or height <= 0:
            return max(1, self.viewport().height())
        fitted = fit_size(
            (width, height),
            (self.viewport().width(), self.viewport().height()),
            self._zoom,
        )
        return max(1, fitted[1])

    def _rebuild(self) -> None:
        """Throw the slots away and make one per page."""
        self._timer.stop()
        for slot in self._slots:
            self._layout.removeWidget(slot)
            slot.setParent(None)
            slot.deleteLater()
        self._slots = []
        self._heights = []
        self._offsets = []
        self._current = 0
        self._boundary_latched = 0
        self._syncing = True
        try:
            for index in range(len(self._sizes)):
                slot = PageView(self._content)
                slot.set_zoom(self._zoom)
                if self._fg:
                    slot.set_text_colour(self._fg)
                slot.set_page(None, self.placeholder(index))
                # Per-slot, not inherited: an event filter on a parent is never
                # consulted for an event Qt delivers straight to a child, and the
                # wheel goes to the child under the pointer (see `eventFilter`).
                slot.installEventFilter(self)
                self._layout.insertWidget(index, slot)
                self._slots.append(slot)
            self._relayout()
            self.verticalScrollBar().setValue(0)
        finally:
            self._syncing = False

    def _relayout(self) -> None:
        """Recompute every slot height, then the prefix sums that index them."""
        self._heights = [self._slot_height(i) for i in range(len(self._slots))]
        for slot, height in zip(self._slots, self._heights):
            slot.setFixedHeight(height)
        offsets = []
        running = 0
        for height in self._heights:
            offsets.append(running)
            running += height
        self._offsets = offsets
        # Tell Qt how tall the content is *now*, not on its next layout pass.
        # Without this the range is stated correctly by `_sync_scroll_range()`
        # and then collapsed to `(0, 0)` one event-loop turn later, because the
        # content widget is still sized for the previous page count (or for no
        # pages at all). A bar sitting at page 200 gets clamped to 0 by that
        # collapse and never comes back: measured, reopening a book at page 8
        # landed on page 2, and the strip quietly reported the wrong page.
        self._content.setMinimumHeight(running)
        self._sync_scroll_range()

    def _sync_scroll_range(self) -> None:
        """State the scrollbar's range now, instead of waiting for Qt's layout.

        `QScrollArea` recomputes the range when the content widget is resized,
        and with `setWidgetResizable(True)` that happens on a *later* event-loop
        turn than `set_pages()`. Until then the range is still `(0, 0)` and
        `setValue()` clamps silently to 0 - so entering mode B on page 200 left
        the strip showing page 1 while the bottom bar still said 200, and the
        first wheel notch did nothing. Both were real, and both came from
        trusting a range that had not been computed yet.

        The slot heights are ours and they already add up to the content height,
        so the range can be stated immediately. Qt's own recomputation later
        arrives at the same number, so this does not fight the layout.
        """
        bar = self.verticalScrollBar()
        slack = max(0, self.total_height() - self.viewport().height())
        if bar.maximum() != slack:
            bar.setRange(0, slack)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Width-fit heights depend on the viewport width, so a resize changes
        # every slot. Heights are still known before any image is decoded, so
        # this is arithmetic, not I/O.
        if self._slots:
            self._relayout()

    # ---- position -----------------------------------------------------------

    def _index_at(self, y: int) -> int:
        """The page whose vertical span contains `y`."""
        if not self._offsets:
            return 0
        index = bisect_right(self._offsets, max(0, int(y))) - 1
        return max(0, min(index, len(self._offsets) - 1))

    def visible_range(self) -> tuple[int, int]:
        """The pages that overlap the viewport, inclusive. `(0, -1)` when empty."""
        if not self._slots:
            return (0, -1)
        top = self.verticalScrollBar().value()
        bottom = top + max(1, self.viewport().height())
        return (self._index_at(top), self._index_at(bottom - 1))

    def band(self, radius: int = LAZY_RADIUS) -> tuple[int, int]:
        """`visible_range()` widened by `radius`, clamped to the book."""
        if not self._slots:
            return (0, -1)
        first, last = self.visible_range()
        return (max(0, first - radius), min(len(self._slots) - 1, last + radius))

    def current_index(self) -> int:
        """The page under the middle of the viewport.

        The spec's rule, and the one progress persistence uses: with no discrete
        page turns, "where am I" can only mean "what is in the middle of the
        screen".
        """
        if not self._slots:
            return 0
        centre = self.verticalScrollBar().value() + max(1, self.viewport().height()) // 2
        return self._index_at(centre)

    def scroll_to_page(self, index: int) -> None:
        """Put one page's top edge at the top of the viewport."""
        if not self._slots:
            return
        index = max(0, min(int(index), len(self._slots) - 1))
        # A short page can leave the viewport centre past its own bottom, which
        # would immediately re-report a later page as current. `goto()` means
        # "this page", so the answer is pinned for the duration of the move.
        self._syncing = True
        try:
            self._current = index
            self.verticalScrollBar().setValue(self._offsets[index])
        finally:
            self._syncing = False
        self._timer.start()

    # ---- scrolling ----------------------------------------------------------

    def _on_scroll(self, _value: int) -> None:
        self._timer.start()
        if self._syncing:
            return
        bar = self.verticalScrollBar()
        if bar.value() > bar.minimum() and bar.value() < bar.maximum():
            # Moving again re-arms both ends: coming back to a boundary later
            # must announce it again.
            self._boundary_latched = 0
        index = self.current_index()
        if index != self._current:
            self._current = index
            self.currentChanged.emit(index)

    def _apply_band(self) -> None:
        first, last = self.band()
        if last >= first:
            self.bandChanged.emit(first, last)

    def page_step(self) -> int:
        """How far one page-turn key or button moves the strip."""
        return max(1, self.viewport().height())

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Scroll, and say so when there is nowhere left to go.

        Drives the scrollbar directly instead of calling `super()`. `QScrollArea`
        only handles a wheel event that arrives at its internal viewport, so the
        inherited handler is a no-op for the notches this class forwards from the
        page slots - the gesture would work over the margin and do nothing over
        the page, which is exactly backwards.

        At an end the notch is swallowed and reported once: the strip holds the
        whole book, so there is no next chapter to jump to, and a wheel that does
        nothing at all reads as a broken app. `_boundary_latched` remembers which
        end has been announced and is cleared by `_on_scroll()` as soon as the
        bar actually moves again, so arriving back at the same end later speaks
        up again.
        """
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            event.ignore()
            return
        bar = self.verticalScrollBar()
        if bar.maximum() > bar.minimum():
            if delta < 0 and bar.value() >= bar.maximum():
                self._announce_boundary(-1)
                event.accept()
                return
            if delta > 0 and bar.value() <= bar.minimum():
                self._announce_boundary(1)
                event.accept()
                return
        # `delta` is in eighths of a degree, and one mouse notch is 120 of them.
        # Converting to whole notches keeps a trackpad's fractional deltas
        # proportional instead of rounding every small movement away.
        notches = delta / 120.0
        target = int(round(bar.value() - notches * WHEEL_STEP_PX))
        bar.setValue(max(bar.minimum(), min(target, bar.maximum())))
        event.accept()

    def _announce_boundary(self, direction: int) -> None:
        """Emit `boundaryReached` unless this end is already latched."""
        if self._boundary_latched == direction:
            return
        self._boundary_latched = direction
        self.boundaryReached.emit(direction)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        """Take wheel notches aimed at any page slot.

        See the comment in `__init__`: a wheel event is delivered to the widget
        under the pointer and does not propagate to ancestors, so without this
        the single most important gesture in continuous mode - scroll the book -
        would do nothing whenever the pointer happened to be over a page.

        Everything else is passed straight through: the slots have no other
        input behaviour, and swallowing unrelated events here would break the
        window's edge-resize filter, which is installed on this same subtree.
        """
        if event.type() == QEvent.Wheel and self._is_inside(obj):
            self.wheelEvent(event)
            return True
        return super().eventFilter(obj, event)

    def _is_inside(self, obj) -> bool:
        """True when `obj` is this scroll area or one of its descendants.

        The filter is installed on several widgets, and `QObject` identity is not
        enough to know whether one of them is still part of this strip - a slot
        that `_rebuild()` has just reparented to None is not.
        """
        if not isinstance(obj, QWidget):
            return False
        widget = obj
        while widget is not None:
            if widget is self:
                return True
            widget = widget.parentWidget()
        return False

    def stop(self) -> None:
        self._timer.stop()
