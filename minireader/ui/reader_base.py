"""What the window needs from a reader, without knowing which kind it is.

`Reader` is a PLAIN class - no `abc.ABC`, no `typing.Protocol`, and it must be
listed FIRST in a concrete reader's bases:

    class ReaderView(Reader, QTextEdit):   # 原来: class ReaderView(QTextEdit)
    class ComicReader(Reader, QWidget):    # 阶段 2

All three of those details are load-bearing and were measured, not guessed:

* **No ABC/Protocol.** `QTextEdit`'s metaclass is `sip.wrappertype`, which is not
  a subclass of `abc.ABCMeta`. Writing `class ReaderView(Reader, QTextEdit)` with
  `Reader` an ABC raises `TypeError: metaclass conflict: the metaclass of a
  derived class must be a (non-strict) subclass of the metaclasses of all its
  bases`. `typing.Protocol` fails identically. A plain class has no metaclass of
  its own, so it mixes in cleanly.

* **Base first.** With `Reader` first in the MRO, anything `Reader` defines
  SHADOWS the Qt method of the same name. That is why this class deliberately
  does NOT declare `show()` or `viewport()`:

      class Reader:
          def show(self, book): ...       # ← w.show() then raises TypeError:
                                              missing 1 required positional
                                              argument: 'book'
          def viewport(self): return None # ← window.py:559 does
                                              self.reader.viewport()
                                              .installEventFilter(self)
                                              -> AttributeError: 'NoneType'

  `QWidget.show()` and `QTextEdit.viewport()` are used by Qt itself and by four
  existing tests. So the "open this book" entry point is named `show_book()`,
  and `viewport()` is simply left to the concrete reader: `ReaderView` inherits
  the real one from `QTextEdit`, and `ComicReader` will define its own.

  (Renaming `viewport()` on the comic side instead of declaring it here is what
  keeps `window.py:1843-1849 _edge_surfaces()` and the autoscroll hover-pause
  hook working for both readers.)

Names below were checked against `QWidget`/`QTextEdit` with `hasattr`: `hide`,
`close`, `update`, `repaint`, `resize`, `move` are all Qt-owned and are avoided
for the same reason. `kind`, `next_page`, `prev_page`, `goto`,
`current_position`, `total_positions`, `progress_percent`, `scrollbar`, `stop`,
`apply_style` and `show_book` are free on both bindings.
"""

from __future__ import annotations

from ..core.book_base import KIND_TEXT


class Reader:
    """The interface a reader must satisfy. Bodies raise, deliberately.

    Every method raises `NotImplementedError` rather than returning a neutral
    value: a concrete reader that forgets one should fail loudly in its own
    test, not silently render nothing in the window.
    """

    #: KIND_TEXT or KIND_COMIC. Matches the book this reader renders.
    kind: str = KIND_TEXT

    # ---- content ---------------------------------------------------------

    def show_book(
        self,
        book,
        *,
        index: int = 0,
        offset: int = 0,
        at_end: bool = False,
        char_offset: int | None = None,
    ) -> None:
        """Display position `index` of `book`.

        Named `show_book`, not `show`, because `QWidget.show()` is Qt's own and
        would be shadowed by a base-first mixin (see the module docstring).

        `char_offset` is a chapter-internal CHARACTER index, used when the pixel
        `offset` cannot be trusted (a font change re-wraps the document, so the
        old pixel value points somewhere else). Text readers honour it; a paged
        reader may ignore it.
        """
        raise NotImplementedError

    def apply_style(self, settings) -> None:
        """Re-apply typography/colour after the settings dialog changed them."""
        raise NotImplementedError

    def stop(self) -> None:
        """Release transient state - running animation, image cache, timers.

        Called by the window when it switches to the other reader. Must be safe
        to call when nothing is running.
        """
        raise NotImplementedError

    # ---- paging ----------------------------------------------------------

    def next_page(self) -> None:
        """Advance one position, or one page-worth of scroll in a text book."""
        raise NotImplementedError

    def prev_page(self) -> None:
        """Go back one position."""
        raise NotImplementedError

    def goto(self, position) -> None:
        """Jump to a `Position` produced by `current_position()`."""
        raise NotImplementedError

    # ---- progress --------------------------------------------------------

    def current_position(self):
        """Return a `Position` describing where the reader is now."""
        raise NotImplementedError

    def total_positions(self) -> int:
        """How many positions the current book has."""
        raise NotImplementedError

    def progress_percent(self) -> float:
        """0-100, counting the current position as reached."""
        raise NotImplementedError

    # ---- window integration ---------------------------------------------

    def scrollbar(self):
        """The vertical `QScrollBar` this reader scrolls, or None.

        Returning a real scrollbar is what lets the existing `AutoScroller`
        drive a comic in continuous-scroll mode: it only ever calls `value()`,
        `maximum()` and `setValue()`, and knows nothing about `ReaderView`.
        """
        return None
