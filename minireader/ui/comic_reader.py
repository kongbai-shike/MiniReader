"""The comic reader: one page at a time (mode A), or the whole book as a strip (mode B).

Structure, and why it is shaped this way:

    ComicReader(Reader, QWidget)          ← Reader FIRST, always
      └── _stack: QStackedWidget
            ├── _page_view: PageView      ← mode A
            └── _strip: StripView         ← mode B
            └── (mode C adds a second PageView)

`Reader` has to be first in the bases so its interface wins over Qt's, and it
deliberately declares no Qt-owned name - see `reader_base.py`. This class
therefore defines `viewport()` itself rather than inheriting one, because
`window.py:559` installs the edge-resize event filter on `self.reader.viewport()`
and `window.py:1843` uses the same object as the edge hot-zone surface.

The reader owns no pages of its own: `ComicBook` lists references, `PageCache`
decodes and remembers them, and this class decides *which* one to show and *how
big*. That keeps the Qt-free rule in `minireader.core.*` intact.

Mode C is a stub in this phase. `READING_MODES` already lists all three so the
title-bar button, the settings dialog and `progress.json` can use the final
vocabulary from the start; `IMPLEMENTED_MODES` is what the button actually
cycles, and asking for an unimplemented one says so instead of silently doing
nothing. Stage 5 adds the last entry to that tuple.
"""

from __future__ import annotations

from ..qt import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QPoint,
    QStackedWidget,
    QTimer,
    QWidget,
    Qt,
    Signal,
    event_global_pos,
    event_local_pos,
)
from ..core.book_base import KIND_COMIC, Position
from .comic_cache import DEFAULT_CACHE_MB, PageCache, probe_sizes
from .page_view import ZOOM_LABELS, ZOOM_TIPS, PageView, next_zoom
from .reader_base import Reader
from .spread import (
    SPREAD_ASPECT,
    anchor_of,
    is_first_spread,
    is_last_spread,
    looks_like_a_cover,
    needs_spread_width,
    next_anchor,
    pairs_pages,
    spread_at,
    spread_starts,
)
from .spread_view import SpreadView
from .strip_view import StripView

#: Every reading mode, in cycle order. Persisted in `Progress.mode`.
READING_MODES: tuple[str, ...] = ("single", "scroll", "spread")

#: Title-bar button text for each mode.
MODE_LABELS = {"single": "▭", "scroll": "▤", "spread": "▥"}

#: Title-bar button text for each binding. `rtl` is 右綴じ (Japanese manga, the
#: lower page number sits on the right), `ltr` is the western way round.
BINDING_LABELS = {"rtl": "右", "ltr": "左"}

#: Tooltip text for each binding, spelled out because a single 右/左 is easy to
#: misread as "which way the pages turn" rather than "where the book opens".
BINDING_TIPS = {"rtl": "装订方向：右綴じ（小页码在右）", "ltr": "装订方向：左綴じ（小页码在左）"}

#: Tooltip text for each mode.
MODE_TIPS = {"single": "单页", "scroll": "连续滚动", "spread": "双页"}

#: Which modes this build can actually render. Stage 5 appends "spread";
#: nothing else has to change.
IMPLEMENTED_MODES: tuple[str, ...] = ("single", "scroll", "spread")

#: A wheel event starts a page turn; anything arriving within this window of the
#: previous turn is swallowed. A trackpad flick fires 10+ wheel events, and
#: without this the reader jumps ten pages on one swipe.
WHEEL_DEBOUNCE_MS = 120

#: How many pages either side of the current one to decode ahead. The default;
#: the user's `comic_preload` setting overrides it through `set_preload()`.
PRELOAD_RADIUS = 1

#: Upper bound for `comic_preload`, matching `storage.Settings.clamp()`.
MAX_PRELOAD = 10

#: Upper bound for `comic_gutter`, matching `storage.Settings.clamp()` and
#: `ui/spread.MAX_GUTTER`.
MAX_GUTTER = 32


def boundary_notice(direction: int, index: int, total: int) -> str:
    """The one wording for "you are already at the end of the book".

    `direction` is -1 at the bottom and +1 at the top, matching
    `StripView.boundaryReached`. The wheel, the page-turn commands and the end of
    an auto-scroll all say this same sentence, so a user who reaches the end
    three different ways is told the same thing three times - and the page
    numbers are always the current ones, never a stale count.

    The strip holds the whole book, so there is no "next chapter" to go to and no
    Progress to write: the position has not changed. Saying so is the difference
    between a boundary and a broken app.
    """
    total = max(1, int(total))
    index = max(0, min(int(index), total - 1))
    where = "第一" if direction > 0 else "最后一"
    return f"已经是{where}页了（第 {index + 1}/{total} 页）"


def next_mode(current: str, modes: tuple[str, ...] = READING_MODES) -> str:
    """The mode after `current`, wrapping inside `modes`."""
    try:
        index = modes.index(current)
    except ValueError:
        return modes[0]
    return modes[(index + 1) % len(modes)]


class ComicReader(Reader, QWidget):
    """Renders a `ComicBook` one page at a time."""

    kind = KIND_COMIC

    #: Class-level view of the module constant, so a caller that already holds a
    #: reader can ask what this build renders without also importing the module
    #: (`window.apply_comic_settings()` does exactly that). Same tuple object.
    IMPLEMENTED_MODES = IMPLEMENTED_MODES

    #: A page turn happened - the window persists progress off this.
    scrolled = Signal()
    #: Right-click at this global position; the window owns the menu.
    contextMenuRequested = Signal(QPoint)
    #: Something the user should be told, already in their language.
    notice = Signal(str)
    #: `viewport()` now returns a different widget. The window hangs its
    #: edge-resize / hover-pause event filter on that widget, and a mode switch
    #: swaps it (the page view for mode A, the scroll area for mode B), so the
    #: window has to move the filter. Emitting is what stops that from being a
    #: step somebody has to remember to call.
    viewportChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._book = None
        self._index = 0
        self._mode = "single"
        self._zoom = "width"
        self._message = ""
        self._last_wheel_ms = 0
        self._notice_pending = ""
        self._preload = PRELOAD_RADIUS
        #: Pages currently decoded into the strip. Tracked so a band change only
        #: clears the slots that actually left the band instead of walking all
        #: 300 of them on every scroll.
        self._loaded: set[int] = set()
        #: `(book id, every page size)` as of the last `set_pages()`. Rebuilding
        #: the strip throws away 300 widgets, so it is skipped when nothing about
        #: the layout has changed - which is the common case on a `goto()`.
        self._strip_signature = None
        #: Mode C. `_gutter` comes from the settings; `_cover_alone` is resolved
        #: per book (`_resolve_cover_alone`) because it depends on the book's own
        #: page names and shapes, not just on the setting.
        #:
        #: The binding has three levels, so it needs three names. `_binding_setting`
        #: is what Settings says, `_binding_override` is what the user picked for
        #: the book currently open (empty when they have not picked), and `_binding`
        #: is the answer actually in use. The middle level exists because a
        #: fixed-layout EPUB states its own reading direction: without it, saving
        #: any settings page would silently discard the book's rtl and flip a
        #: Japanese manga back to left-bound.
        self._binding_setting = "rtl"
        self._binding_override = ""
        self._binding = "rtl"
        self._gutter = 0
        self._cover_setting = True
        self._cover_alone = True
        #: Whether the book currently open lets its pages be paired into
        #: synthetic spreads. A fixed-layout EPUB can say `rendition:spread`
        #: `none`, which means "do not build spreads out of my pages" - a picture
        #: book whose every page is a finished drawing. Resolved per book and per
        #: viewport shape (`_resolve_pairs`), not from a setting: it is the book's
        #: own answer, like `binding`.
        self._pairs = True
        #: The last `_pairs` value the user was told about, so the "this book
        #: asks for one page at a time" notice is said once per book instead of
        #: on every render. None means "nothing said yet for this book".
        self._pairs_told = None
        self.cache = PageCache(DEFAULT_CACHE_MB)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self._page_view = PageView(self._stack)
        self._stack.addWidget(self._page_view)

        # Mode B. Kept alive from construction like the page view, for the same
        # reason the window keeps both readers alive: rebuilding a widget means
        # re-wiring its signals, and anything forgotten is a silent regression.
        self._strip = StripView(self._stack)
        self._strip.bandChanged.connect(self._load_band)
        self._strip.currentChanged.connect(self._on_strip_page)
        self._strip.boundaryReached.connect(self._on_strip_boundary)
        self._stack.addWidget(self._strip)

        # Mode C. Same reasoning: one long-lived widget per mode, and the stack
        # decides which one is visible.
        self._spread = SpreadView(self._stack)
        self._stack.addWidget(self._spread)

        # A single-shot timer rather than a stored timestamp comparison: the
        # timer is what actually makes the debounce visible to a test, since
        # tests/test_ui_offscreen.py can pump the event loop and watch it fire.
        self._wheel_timer = QTimer(self)
        self._wheel_timer.setSingleShot(True)
        self._wheel_timer.setInterval(WHEEL_DEBOUNCE_MS)
        self._wheel_timer.timeout.connect(self._clear_wheel_gate)

        self._preload_timer = QTimer(self)
        self._preload_timer.setSingleShot(True)
        self._preload_timer.setInterval(0)
        self._preload_timer.timeout.connect(self._preload_neighbours)

        self._content_effect = None
        self._content_target = None

        self.setFocusPolicy(Qt.StrongFocus)
        self._page_view.setFocusPolicy(Qt.StrongFocus)

    # ---- Reader interface ---------------------------------------------------

    def viewport(self) -> QWidget:
        """The surface the window hangs its event filter on.

        `window.py:559` and `window.py:1843` both call this, and the returned
        widget is what the 8px edge-resize ring is measured against, so it must
        be the widget that actually occupies the reading area.

        Mode B returns the `QScrollArea` itself rather than a page slot. Qt sends
        `Enter` to the whole ancestor chain when the pointer arrives in the
        viewport, so one filter on the scroll area covers every slot - verified
        by probe, not assumed. Returning a slot would mean re-installing the
        filter on a widget that is destroyed and rebuilt by `set_pages()`.

        Mode C returns the `SpreadView` itself, for the same reason mode A returns
        the page view: it is the widget filling the reading area.
        """
        if self._mode == "scroll":
            return self._strip
        if self._mode == "spread":
            return self._spread
        return self._page_view

    def show_book(
        self,
        book,
        *,
        index: int = 0,
        offset: int = 0,
        at_end: bool = False,
        char_offset: int | None = None,
    ) -> None:
        """Display page `index` of `book`.

        `offset`, `at_end` and `char_offset` are the text reader's position
        vocabulary; a paged book has no use for them and they are ignored rather
        than rejected, because `MiniWindow.show_chapter()` passes all of them.

        A non-comic `book` is treated as "nothing to show" rather than trusted:
        every page access below (`book.pages`, `read_page`) exists only on
        `ComicBook`, and this method is reachable from shared window paths that
        once handed it a text `Book`. Refusing here turns a hard AttributeError
        in the UI thread into an empty page.
        """
        if book is not None and getattr(book, "kind", None) != KIND_COMIC:
            book = None
        if self._book is not None and book is not None and book.id != self._book.id:
            # A different book: the old pages are dead weight and their memory
            # budget belongs to the new one. The strip's slots describe the old
            # book's page count and sizes, so they go too.
            self.cache.forget_book(self._book.id)
            self._forget_strip()
            # A direction the user picked belongs to the book it was picked for.
            # Carrying it across would silently override the next book's own
            # `page-progression-direction`, which is exactly what the override is
            # meant to be able to do on purpose, not by accident.
            self._binding_override = ""
            # The "this book forbids spreads" notice is per book, so the fact
            # that the previous book was already told must not suppress it.
            self._pairs_told = None
        self._book = book
        self._index = book.clamp(index) if book is not None else 0
        # A new book can change the cover decision (its own names and shapes), so
        # resolve it here rather than trusting a value computed for the old book -
        # `apply_comic_settings()` runs before any book exists, so the first answer
        # for a book always comes from this line.
        if book is not None:
            self._cover_alone = looks_like_a_cover(book, self._cover_setting)
        self._apply_binding()
        self._render()

    def _binding_for(self, book) -> str:
        """The direction `book` asks for, else the setting.

        A fixed-layout EPUB states its reading direction in the spine's
        `page-progression-direction`; `ComicBook.binding` carries that up. Every
        other source leaves it empty, which means "no opinion" rather than "ltr".
        """
        own = getattr(book, "binding", "") if book is not None else ""
        if own in ("rtl", "ltr"):
            return own
        return self._binding_setting

    def _apply_binding(self) -> None:
        """Recompute `binding` from the three levels and push it to mode C."""
        self._binding = self._binding_override or self._binding_for(self._book)
        if self._mode == "spread":
            self._spread.set_binding(self._binding)

    def _forget_strip(self) -> None:
        """Make the strip forget the book it was showing."""
        self._loaded.clear()
        self._strip_signature = None
        self._strip.set_pages([])

    def apply_style(self, settings) -> None:
        """Re-colour the reading surface after the settings dialog changed it.

        Only the "this page is broken" text takes a colour from Settings: the
        page itself is an image, and the background is painted by the top-level
        window's `paintEvent` (including its alpha), so a stylesheet here would
        punch an opaque hole in a translucent window.
        """
        self._page_view.set_text_colour(settings.fg)
        self._page_view.update()
        self._strip.set_text_colour(settings.fg)
        self._spread.set_text_colour(settings.fg)

    def stop(self) -> None:
        """Release the page cache and cancel pending work.

        Called by the window when it switches to the other reader, so a comic's
        decoded pages do not sit in memory while a novel is being read.
        """
        self._wheel_timer.stop()
        self._preload_timer.stop()
        self._strip.stop()
        self.cache.clear()

    # ---- paging -------------------------------------------------------------

    def next_page(self) -> None:
        self._step(1)

    def prev_page(self) -> None:
        self._step(-1)

    def _step(self, delta: int) -> None:
        """Move one position, and at an end say so.

        Modes A and B move one *page*; mode C moves one *spread*, which is two
        pages - or one, for the cover and an odd final page. The spec fixes the
        semantics as "下一跨页始终是 index+2", which is exactly what
        `spread.next_anchor()` gives when the anchors step by 2.

        Mode A stays silent at the ends: a single page has nothing to scroll, the
        bottom-bar buttons are already greyed out there, and turning a page at the
        end of the book is not a surprising outcome that needs explaining. Modes B
        and C have a whole book to travel through, so a command that does nothing
        at all reads as a frozen app - there the boundary is announced.
        """
        if self._book is None:
            return
        if self._mode == "spread":
            self._step_spread(delta)
            return
        target = self._book.clamp(self._index + delta)
        if target == self._index:
            if self._mode == "scroll":
                self.say_boundary(-delta)
            return
        self._index = target
        self._render()
        self.scrolled.emit()

    def _step_spread(self, delta: int) -> None:
        book = self._book
        if book is None or not book.pages:
            return
        total = len(book.pages)
        self._resolve_cover_alone()
        self._resolve_pairs()
        target = next_anchor(
            self._index, total, self._cover_alone, delta, pairs=self._pairs
        )
        if target == self._index:
            # Already at the end in this direction. Mode C announces it - the same
            # rule mode B uses, for the same reason. `delta > 0` means "toward the
            # end of the book", which is `say_boundary`'s -1.
            self.say_boundary(-1 if delta > 0 else 1)
            return
        self._index = book.clamp(target)
        self._render()
        self.scrolled.emit()

    def goto(self, position) -> None:
        """Jump to a `Position` from `current_position()`."""
        if self._book is None:
            return
        target = self._book.clamp(int(position.index))
        if target == self._index:
            return
        self._index = target
        self._render()
        self.scrolled.emit()

    def say_boundary(self, direction: int) -> None:
        """Announce that the book has no further page in `direction`.

        `direction` is -1 at the bottom and +1 at the top, the same convention as
        `StripView.boundaryReached`. Public because the window's page-turn
        command (`step_chapter`, which the context menu, the bottom-bar capsules
        and the hotkeys all go through) needs the same sentence as the wheel
        does - and it must be the *same* sentence, so the wording lives in
        `boundary_notice()` and nowhere else.

        Mode A stays silent. A single page has nothing to scroll, the bottom-bar
        buttons are already greyed out there, and turning a page at the end of the
        book is not a surprising outcome that needs explaining. Mode B has a whole
        book to travel through, so a command that does nothing at all reads as a
        frozen app and does need explaining.
        """
        if self._book is None or self._mode not in ("scroll", "spread"):
            return
        self.notice.emit(boundary_notice(direction, self._index, self.total_positions()))

    # ---- progress -----------------------------------------------------------

    def current_position(self) -> Position:
        """Where the reader is, in the bottom bar's vocabulary.

        Mode C reports `span=2` so the label reads "第 5-6/40 页" - the spread is
        one position even though it shows two pages. A page standing alone (the
        cover, or an odd final page) reports `span=1`, because "第 6-7/40 页" on a
        40-page book would be a lie about what is on screen.

        `Progress.page` records the spread's first page, so reopening the book
        re-enters mode C at the same spread rather than starting over.
        """
        book = self._book
        if book is None:
            return Position(kind=KIND_COMIC, index=0, offset=0, total=0)
        span = 1
        if self._mode == "spread" and book.pages:
            self._resolve_cover_alone()
            self._resolve_pairs()
            _first, second = spread_at(
                self._index, len(book.pages), self._cover_alone, pairs=self._pairs
            )
            span = 2 if second is not None else 1
        return Position(
            kind=KIND_COMIC,
            index=self._index,
            offset=0,
            total=book.position_count,
            span=span,
        )

    def total_positions(self) -> int:
        return self._book.position_count if self._book is not None else 0

    def progress_percent(self) -> float:
        return self.current_position().percent

    def scrollbar(self):
        """Mode B's scrollbar, or None in the other modes.

        `AutoScroller` only ever calls `value()`, `maximum()` and `setValue()`,
        so handing it the strip's own bar drives continuous scrolling with no
        change to `autoscroll.py` at all. The window swaps the scroller's target
        when the reader changes (see `MiniWindow._rebind_reader_views`), because
        the one built at startup is bound to the text view's bar.

        Modes A and C return None: a single page and a spread are discrete
        positions, not continuous scrolls.
        """
        if self._mode == "scroll":
            return self._strip.verticalScrollBar()
        return None

    # ---- text-shaped methods, so shared window paths are no-ops here ---------
    #
    # The window was written for a text book and reaches for these on paths that
    # are not really about text: the translucent-background rebuild anchor, the
    # content-alpha preview, the bookmark excerpt, the autoscroll resume check.
    # Each of those paths already guards with `char_count() > 0` or compares a
    # maximum, so answering 0/"" makes them correctly do nothing instead of
    # raising - and the window keeps its single `self.reader` call site list.
    #
    # `verticalScrollBar()` is NOT declared here: it is Qt's own method on
    # QWidget's subclasses and the window calls it at window.py:559 to build the
    # one AutoScroller for the session. Shadowing it would break text scrolling.
    # The neutral accessor is `scrollbar()` above; the two remaining call sites
    # that genuinely need a bar were rewritten to use it.

    def char_count(self) -> int:
        """A comic has no characters, so every char-based path is a no-op."""
        return 0

    def first_visible_char(self) -> int:
        return 0

    def scroll_to_char(self, pos: int) -> None:
        """No characters to scroll to. Present so the rebuild path cannot raise."""

    def current_offset(self) -> int:
        """Pixel offset means nothing for a page; position is `_index`."""
        return 0

    def max_offset(self) -> int:
        """No scrolling in the single-page mode, so the range is empty."""
        return 0

    # ---- comic controls -----------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Switch reading mode.

        Switching modes carries the *page*, not the pixel offset. Going A -> B
        puts the current page at the top of the strip; going B -> A shows
        whichever page was under the middle of the viewport, which is the page
        the bottom bar was reporting. Going A <-> C shows the spread that
        *contains* the current page, because a spread is two pages wide and a
        page number alone does not pick one. Either way the reader does not
        silently move somewhere else.

        Asking for `spread` on a portrait window is refused with a notice, and the
        mode is left alone: two pages side by side in a portrait are each a
        useless sliver. `SPREAD_ASPECT` is the threshold, and it is checked here
        rather than in the caller so that **every** caller - the button, the
        settings dialog, a restored preference, a saved Progress - gets the same
        answer.
        """
        if mode not in READING_MODES:
            mode = READING_MODES[0]
        if mode not in IMPLEMENTED_MODES:
            # A generic sentence on purpose. `MODE_PENDING` used to carry a
            # per-mode "coming in stage N" wording, and stage 5 deleted it once
            # all three modes landed - so there is no table left to look the
            # sentence up in. Leaving a reference to it here was a latent
            # `NameError`: unreachable while `IMPLEMENTED_MODES` covers
            # `READING_MODES`, but armed the moment a future phase adds a mode
            # without implementing it. The exact sentence belongs to the phase
            # that ships the mode; until then any non-empty explanation will do.
            self.notice.emit("这个阅读模式还没做")
            return
        if mode == self._mode:
            return
        if mode == "spread" and not self._spread_fits():
            self.notice.emit("窗口太窄，已切回单页")
            return
        self._mode = mode
        self._stack.setCurrentWidget(self._mode_widget())
        self._render()
        self.viewportChanged.emit()

    def _mode_widget(self) -> QWidget:
        if self._mode == "scroll":
            return self._strip
        if self._mode == "spread":
            return self._spread
        return self._page_view

    def _spread_fits(self) -> bool:
        """True when the reading area is wide enough for two pages.

        Degenerate sizes (0 width or height, before the first layout) count as
        "does not fit": refusing is the safe answer, because entering mode C far
        too early would show two empty slivers and there is no way back except
        the mode button.
        """
        size = self.size()
        return needs_spread_width(size.width(), size.height(), SPREAD_ASPECT)

    def _render_strip(self) -> None:
        """Mode B: size every slot, then decode only what is near the viewport.

        The whole book is laid out here - one slot per page, each with its real
        aspect ratio - but only the pages near the viewport are decoded. That
        split is what makes a 300-page archive open instantly while still having
        a scrollbar that is already the right length and does not move as images
        arrive.

        `probe_sizes()` reads the container once for all 300 pages (24 ms
        measured) rather than once per page (252 ms); see `comic_cache`.
        """
        self._preload_timer.stop()
        book = self._book
        if book is None or not book.pages:
            self._loaded.clear()
            self._strip_signature = None
            self._strip.set_pages([])
            self._message = ""
            return

        sizes = self._page_sizes(book)
        signature = (book.id, tuple(sizes))
        if signature != self._strip_signature:
            self._loaded.clear()
            self._strip.set_pages(sizes)
            self._strip_signature = signature
        self._strip.set_zoom(self._zoom)
        self._strip.scroll_to_page(self._index)
        self._load_band(*self._strip.band())
        self._message = ""

    def _page_sizes(self, book) -> list[tuple[int, int]]:
        """Every page's `(width, height)`, probing only what is not known yet.

        A PDF arrives with all its page boxes filled in by `open_pdf()`, so this
        is a no-op for one; a CBZ needs a probe pass the first time it is shown
        in mode B, and nothing at all afterwards because `set_page_size()` has
        recorded the answers on the book.

        An unknown page stays `(0, 0)` and `StripView` lays it out at one
        viewport height. That is a guess, but it is a guess that cannot collapse
        the scroll range - and a page whose header is unreadable would fail to
        decode too, so its real height would never arrive.
        """
        total = len(book.pages)
        sizes = [(0, 0)] * total
        unknown = []
        for page in book.pages:
            if page.size_known:
                sizes[page.index] = (page.width, page.height)
            else:
                unknown.append(page.index)
        if unknown:
            measured = probe_sizes(book, unknown)
            for index, (width, height) in measured.items():
                if width > 0 and height > 0:
                    book.set_page_size(index, width, height)
                    sizes[index] = (width, height)
        return sizes

    def _load_band(self, first: int, last: int) -> None:
        """Decode `first..last` inclusive and put every other slot back."""
        book = self._book
        if book is None or first > last:
            return
        wanted = set(range(first, last + 1))
        for index in sorted(self._loaded - wanted):
            # Out of range: drop the pixels, keep the block. The slot keeps its
            # real height, so the scrollbar does not move.
            self._strip.clear_page(index)
        self._loaded &= wanted
        for index in range(first, last + 1):
            if index in self._loaded:
                continue
            pixmap, message = self.cache.load(book, index)
            self._strip.set_page(index, pixmap, message)
            self._loaded.add(index)
            if message:
                self.notice.emit(message)

    def _on_strip_page(self, index: int) -> None:
        """The page under the middle of the viewport changed.

        This is mode B's equivalent of a page turn: the window's page number,
        the bottom bar and the debounced progress save all follow from it. No
        `Progress` is written here directly - the window owns that, and it
        debounces.
        """
        index = self._book.clamp(index) if self._book is not None else 0
        if index == self._index:
            return
        self._index = index
        self.scrolled.emit()

    def _on_strip_boundary(self, direction: int) -> None:
        """The user kept scrolling past an end of the book.

        The strip holds the whole book, so there is no next chapter to turn to -
        and the position has not changed, so nothing is written to Progress. All
        that is left is to say where the end is.
        """
        if self._book is None:
            return
        self.notice.emit(boundary_notice(direction, self._index, self.total_positions()))

    def cycle_mode(self) -> None:
        """Cycle through the modes this build can actually render.

        Deliberately over `IMPLEMENTED_MODES`, not the full vocabulary: a button
        that lands on a mode it cannot draw would be a dead press. `set_mode()`
        still accepts the future names so the settings dialog can persist a
        preference for one and say why it is not in effect yet.
        """
        self.set_mode(next_mode(self._mode, IMPLEMENTED_MODES))

    @property
    def zoom(self) -> str:
        return self._zoom

    def set_zoom(self, zoom: str) -> None:
        """Change the zoom, in every mode.

        The strip recomputes every slot height from the new zoom and keeps the
        current page at the top, so changing zoom while scrolled does not throw
        the reader somewhere else in the book. The spread re-derives both pages
        from one shared height, so it needs a full re-layout.
        """
        self._zoom = zoom if zoom in ZOOM_LABELS else "width"
        self._page_view.set_zoom(self._zoom)
        self._strip.set_zoom(self._zoom)
        self._spread.set_zoom(self._zoom)
        if self._mode == "scroll":
            self._strip.scroll_to_page(self._index)
        elif self._mode == "spread":
            self._render_spread()

    # ---- two-page mode (mode C) settings ----------------------------------

    def set_binding(self, binding: str) -> None:
        """rtl (右綴じ) or ltr (左綴じ). Applied live when mode C is showing.

        This is the setting's entry point, not the user's: it is what
        `apply_comic_settings()` calls on every settings change. It records the
        preference and lets a book's own direction keep winning, so saving the
        settings dialog does not flip a right-bound manga. `cycle_binding()` is
        the entry point that overrides the book.
        """
        self._binding_setting = binding if binding in ("rtl", "ltr") else "rtl"
        self._apply_binding()

    @property
    def binding(self) -> str:
        return self._binding

    @property
    def binding_setting(self) -> str:
        """What Settings says, ignoring the book's opinion and any override."""
        return self._binding_setting

    def clear_binding_override(self) -> None:
        """Go back to the book's own direction (or the setting if it has none)."""
        self._binding_override = ""
        self._apply_binding()

    def set_gutter(self, pixels: int) -> None:
        """Gap between the two pages, in pixels (0-32, clamped like the setting)."""
        self._gutter = max(0, min(int(pixels), MAX_GUTTER))
        if self._mode == "spread":
            self._spread.set_gutter(self._gutter)

    @property
    def gutter(self) -> int:
        return self._gutter

    def set_cover_alone(self, cover_alone: bool) -> None:
        """Whether page 1 stands alone. The setting; the book may still override."""
        self._cover_setting = bool(cover_alone)
        # Re-resolve now rather than waiting for the next mode-C render. Otherwise
        # `cover_alone` keeps reporting the previous answer while mode A is showing,
        # so the settings dialog and the reader disagree about what is set.
        if self._book is not None:
            self._cover_alone = looks_like_a_cover(self._book, self._cover_setting)
        if self._mode == "spread":
            self._render_spread()

    @property
    def cover_alone(self) -> bool:
        return self._cover_alone

    def cycle_binding(self) -> None:
        """rtl <-> ltr. The spec's "装订方向切换" entry point.

        This is the user's override, so it is recorded separately from the
        setting: the next book still gets its own direction, and
        `clear_binding_override()` returns to it.

        The notice is emitted here rather than by the window because only this
        object knows where the previous answer came from - the book, the user, or
        the setting - and that provenance is the whole point of the message: a
        reader whose book declared `rtl` needs to know a click *overrode* the book
        rather than failed to change anything.
        """
        self._binding_override = "ltr" if self._binding == "rtl" else "rtl"
        self._apply_binding()
        self.notice.emit(self.binding_tip())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Drop out of mode C when the window becomes too narrow for two pages.

        The guard lives here, not only in `set_mode()`, because the user can drag a
        landscape window down to portrait while a spread is on screen. Without it
        the two pages keep shrinking into useless slivers with no way out except
        the mode button. Single-page mode is always valid, so that is where it
        lands - and the page number carries over, because `_index` is a page
        number in every mode.
        """
        super().resizeEvent(event)
        if self._mode == "spread" and not self._spread_fits():
            self._mode = "single"
            self._stack.setCurrentWidget(self._page_view)
            self._render_page()
            self.viewportChanged.emit()
            self.notice.emit("窗口太窄，已切回单页")

    def cycle_zoom(self) -> None:
        self.set_zoom(next_zoom(self._zoom))

    def mode_tip(self) -> str:
        return f"阅读模式：{MODE_TIPS.get(self._mode, self._mode)}"

    def zoom_tip(self) -> str:
        return f"缩放：{ZOOM_TIPS.get(self._zoom, self._zoom)}"

    def mode_label(self) -> str:
        return MODE_LABELS.get(self._mode, MODE_LABELS["single"])

    def zoom_label(self) -> str:
        return ZOOM_LABELS.get(self._zoom, ZOOM_LABELS["width"])

    def binding_label(self) -> str:
        return BINDING_LABELS.get(self._binding, BINDING_LABELS["rtl"])

    def binding_tip(self) -> str:
        """Tooltip for the binding button, saying where the answer came from.

        Three levels feed `_binding`, and the user cannot tell them apart from the
        glyph alone: a direction the book declared, one the user picked for this
        book, and the global setting. Without the provenance a reader who flips
        the button, reopens the book and sees it flip back would think the click
        was lost - so the tip says which level won and what to do about it.
        """
        if self._binding_override:
            return f"{BINDING_TIPS.get(self._binding, '')}（本书手动指定，再点一次继续切换）"
        own = getattr(self._book, "binding", "") if self._book is not None else ""
        if own in ("rtl", "ltr"):
            return f"{BINDING_TIPS.get(self._binding, '')}（书里声明的，点击可覆盖）"
        return f"{BINDING_TIPS.get(self._binding, '')}（跟随设置）"

    @property
    def book(self):
        return self._book

    @property
    def index(self) -> int:
        return self._index

    @property
    def message(self) -> str:
        return self._message

    # ---- content opacity ----------------------------------------------------
    #
    # Same shape as ReaderView's, and for the same reason: the window's 🎚 popup
    # drives whichever reader is current, so both must answer to these names.
    # The effect goes on whichever view is showing, never on the top-level window.

    def _alpha_target(self):
        """The widget the content-alpha effect belongs on right now."""
        if self._mode == "scroll":
            return self._strip
        if self._mode == "spread":
            return self._spread
        return self._page_view

    def apply_content_alpha(self, alpha: int) -> None:
        alpha = max(0, min(int(alpha), 255))
        if alpha >= 255:
            if self._content_effect is not None:
                self._content_effect.setOpacity(1.0)
                if self._content_target is not None:
                    self._content_target.setGraphicsEffect(None)
                self._content_effect = None
                self._content_target = None
            return
        target = self._alpha_target()
        if self._content_effect is None or target is not self._content_target:
            # The mode changed while an effect was live (or none existed yet): the
            # effect object belongs to one widget, so a new target needs a new one.
            if self._content_target is not None:
                self._content_target.setGraphicsEffect(None)
            self._content_effect = QGraphicsOpacityEffect(target)
            target.setGraphicsEffect(self._content_effect)
            self._content_target = target
        self._content_effect.setOpacity(alpha / 255.0)

    def preview_content_alpha(self, alpha: int) -> None:
        alpha = max(0, min(int(alpha), 255))
        target = self._alpha_target()
        if self._content_effect is None or target is not self._content_target:
            if alpha >= 255:
                return
            if self._content_target is not None:
                self._content_target.setGraphicsEffect(None)
            self._content_effect = QGraphicsOpacityEffect(target)
            target.setGraphicsEffect(self._content_effect)
            self._content_target = target
        self._content_effect.setOpacity(alpha / 255.0)

    def content_opacity(self) -> float:
        if self._content_effect is None or self._content_target is not self._alpha_target():
            return 1.0
        return self._content_effect.opacity()

    # ---- rendering ----------------------------------------------------------

    def _render(self) -> None:
        """Show the current position in whichever mode is active."""
        if self._mode == "scroll":
            self._render_strip()
            return
        if self._mode == "spread":
            self._render_spread()
            return
        self._render_page()

    def _render_spread(self) -> None:
        """Mode C: show the spread containing the current page.

        The pair comes from `spread.py`, which owns every "which page goes where"
        decision (cover alone, lone final page, binding). This method only hands
        the pair to the view and decodes the two pages in it.
        """
        self._preload_timer.stop()
        book = self._book
        if book is None or not book.pages:
            self._message = ""
            self._spread.clear()
            return
        self._resolve_cover_alone()
        self._resolve_pairs()
        self._spread.bind_book(book)
        self._spread.set_binding(self._binding)
        self._spread.set_gutter(self._gutter)
        self._spread.set_zoom(self._zoom)
        pair = spread_at(
            self._index, len(book.pages), self._cover_alone, pairs=self._pairs
        )
        # The spread is the position, so `_index` moves to the spread's first
        # page. That is what the bottom bar reports and what Progress saves.
        self._index = book.clamp(pair[0])
        self._spread.set_spread(pair)
        self._say_pairs_once()
        self._message = ""
        for page_index in (pair[0], pair[1]):
            if page_index is None:
                continue
            pixmap, message = self.cache.load(book, page_index)
            self._spread.set_page(page_index, pixmap, message)
            if message:
                self._message = message
                self.notice.emit(message)

    def _resolve_cover_alone(self) -> None:
        """Decide whether page 1 stands alone, for this book.

        Called per render rather than cached on the book because it reads the
        book's own page names and shapes, which are fixed per book - and the whole
        decision is cheap (one name check, one shape comparison).
        """
        if self._book is None:
            return
        self._cover_alone = looks_like_a_cover(self._book, self._cover_setting)

    def _resolve_pairs(self) -> None:
        """Decide whether this book's pages may be paired into spreads.

        Two inputs, and both are per book rather than per setting:

        * `ComicBook.spread` - the container's `rendition:spread`, empty for
          every source that cannot have an opinion. `pairs_pages()` reads it.
        * the viewport - only `landscape` cares, and the question it asks ("is
          the reading area landscape?") is the same one mode C already asks
          before it will engage, so `_spread_fits()` answers it. Reusing it
          keeps the two answers from disagreeing: a viewport too narrow for two
          pages is not landscape by any useful definition here.

        One consequence worth stating plainly, because it looks like a bug
        otherwise: since mode C refuses to engage at all in a narrow viewport,
        `landscape` and `both` behave identically in practice. The difference
        only shows up at the pure-function level, and that is where it is
        tested.

        Deliberately no `notice.emit()` here. This runs from `current_position()`
        too, which the window calls while painting the bottom bar; emitting from
        a query would put a notice on screen from a code path that is supposed to
        only read. The saying happens in `_say_pairs_once()`, from the render.
        """
        if self._book is None:
            self._pairs = True
            self._pairs_told = None
            return
        self._pairs = pairs_pages(
            getattr(self._book, "spread", ""), landscape=self._spread_fits()
        )

    def _say_pairs_once(self) -> None:
        """Tell the user, once per book, that this book forbids pairing.

        Without it, the two-page button on such a book looks broken: the pages
        come up one at a time and nothing explains why. Once per book, not once
        per render - hence `_pairs_told`, which `show_book()` resets when the
        book changes.
        """
        if self._pairs == self._pairs_told:
            return
        self._pairs_told = self._pairs
        if not self._pairs and self._mode == "spread":
            self.notice.emit("这本书要求单页显示，双页模式一次只显示一页")

    def _render_page(self) -> None:
        """Mode A: decode and show the current page, then queue the neighbours."""
        self._preload_timer.stop()
        book = self._book
        if book is None or not book.pages:
            self._message = ""
            self._page_view.clear()
            return

        pixmap, message = self.cache.load(book, self._index)
        self._message = message
        self._page_view.set_page(pixmap, message)
        page = book.page(self._index)
        if pixmap is not None and page is not None and not page.size_known:
            # The decoded pixmap is the authoritative page size, and it is what
            # mode B needs for its placeholder blocks. Recording it here means
            # the size is known after the first visit rather than requiring a
            # separate probing pass.
            #
            # Only when it is not already known. For a PDF the page box is known
            # from the document itself, and the pixmap is that box times
            # `RENDER_SCALE` - recording the rendered pixels would report the same
            # document's first page at twice the size of its other pages.
            book.set_page_size(self._index, pixmap.width(), pixmap.height())
        if message:
            self.notice.emit(message)
        self._preload_timer.start()

    def _preload_neighbours(self) -> None:
        """Decode the pages either side, so a page turn does not stall.

        Runs on a zero-delay timer so the page turn itself paints first.
        """
        book = self._book
        if book is None:
            return
        for delta in range(1, self._preload + 1):
            for index in (self._index + delta, self._index - delta):
                if 0 <= index < len(book.pages):
                    self.cache.load(book, index)

    def set_cache_mb(self, budget_mb: int) -> None:
        self.cache.set_budget_mb(budget_mb)

    def set_preload(self, pages: int) -> None:
        """How many pages either side to decode ahead. 0 turns preloading off."""
        try:
            self._preload = max(0, min(int(pages), MAX_PRELOAD))
        except (TypeError, ValueError):
            self._preload = PRELOAD_RADIUS

    # ---- input --------------------------------------------------------------

    def _clear_wheel_gate(self) -> None:
        self._last_wheel_ms = 0

    def _wheel_open(self) -> bool:
        """True when a wheel notch is allowed to turn the page."""
        return not self._wheel_timer.isActive()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Mode A turns a page; mode B scrolls.

        In mode B the wheel normally never reaches here: the strip's own
        `wheelEvent` accepts it (or reports a boundary) first. This branch covers
        the case where it does arrive anyway - the pointer is over a sliver of
        this widget rather than the strip - and routes it to the strip, because
        "the wheel scrolls the book" is what continuous mode promises. Turning a
        page instead would make the same gesture mean two different things
        depending on which pixel the pointer sat on.
        """
        if self._mode == "scroll":
            self._strip.wheelEvent(event)
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            event.ignore()
            return
        event.accept()
        if not self._wheel_open():
            # Inside the debounce window: swallow the notch so one trackpad flick
            # turns exactly one page.
            return
        self._wheel_timer.start()
        if delta < 0:
            self.next_page()
        else:
            self.prev_page()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if key in (Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown, Qt.Key_Space):
            self.next_page()
            event.accept()
            return
        if key in (Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp, Qt.Key_Backspace):
            self.prev_page()
            event.accept()
            return
        if key == Qt.Key_Home:
            self.goto(Position(kind=KIND_COMIC, index=0, total=self.total_positions()))
            event.accept()
            return
        if key == Qt.Key_End:
            self.goto(
                Position(
                    kind=KIND_COMIC,
                    index=max(0, self.total_positions() - 1),
                    total=self.total_positions(),
                )
            )
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Emit the global position; the window owns the menu.

        Never calls `super()`: Qt's default menu offers cut/copy/select-all,
        none of which mean anything over a comic page.
        """
        self.contextMenuRequested.emit(event_global_pos(event))
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Click the left third to go back, the right two thirds to go forward.

        Mirrors how every comic reader on the market behaves, and it is the only
        way to turn a page on a touchscreen.
        """
        if event.button() == Qt.LeftButton:
            if event_local_pos(event).x() < self.width() / 3.0:
                self.prev_page()
            else:
                self.next_page()
            event.accept()
            return
        super().mousePressEvent(event)
