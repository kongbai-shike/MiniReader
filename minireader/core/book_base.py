"""The shape every book shares, and the one position type the UI speaks.

Two things live here:

1. `BookBase` - the plain base class that `Book` (text) and `ComicBook` (comic)
   both inherit. It is deliberately NOT `abc.ABC`: `Book` is a `@dataclass`, and
   more importantly the reader side of this abstraction (`ui/reader_base.Reader`)
   has to sit in front of Qt widgets, where `abc.ABCMeta` collides with
   `sip.wrappertype` and raises `TypeError: metaclass conflict`. One plain-class
   convention for both sides keeps the rule easy to remember.

2. `Position` - the single progress representation. The bottom bar needs to say
   either "第 3/12 章" or "第 5-6/40 页", and doing that with one frozen value
   object means `_update_status()` needs no if/else on the book kind.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

from dataclasses import dataclass

# The two book kinds. Strings rather than an Enum on purpose: they are written
# into settings.json and progress.json, and a plain string survives a hand edit
# and a downgrade without a migration table.
KIND_TEXT = "text"
KIND_COMIC = "comic"

# What each kind counts, for the bottom bar's "第 x/y ?" suffix.
POSITION_LABELS = {KIND_TEXT: "章", KIND_COMIC: "页"}


@dataclass(frozen=True)
class Position:
    """Where a reader is, in a form both readers can produce.

    `offset` keeps the meaning it already has for text - a scrollbar PIXEL value
    (`core/model.py`'s `Progress` docstring records that decision). Comics leave
    it 0. It is NOT the same thing as `store.Bookmark.offset`, which is a
    chapter-internal character index; those two were kept different on purpose
    (pixels break on a font change, characters do not) and must not be merged.

    `span` is how many positions this one covers: 2 in the two-page comic mode,
    1 everywhere else. It only affects `label()`.
    """

    kind: str = KIND_TEXT
    index: int = 0
    offset: int = 0
    total: int = 0
    span: int = 1

    @property
    def label(self) -> str:
        """The bottom bar's left-hand cluster, e.g. "第 3/12 章" or "第 5-6/40 页"."""
        unit = POSITION_LABELS.get(self.kind, POSITION_LABELS[KIND_TEXT])
        total = max(0, int(self.total))
        index = max(0, int(self.index))
        span = max(1, int(self.span))
        if span > 1:
            # Clamp the upper bound to the book. Without this, a two-page spread on
            # the last page of a six-page book reads "第 6-7/6 页" - a page that does
            # not exist. It falls back to the single form, which is the truth: one
            # page is on screen.
            last = min(index + span, total)
            if last > index + 1:
                return f"第 {index + 1}-{last}/{total} {unit}"
        return f"第 {index + 1}/{total} {unit}"

    @property
    def percent(self) -> float:
        """0-100, counting the current position as reached.

        `Book` has always counted this way - chapter 1 of 10 is 10%, not 0% -
        so the text reader's percentage does not change when it starts going
        through here.
        """
        total = max(0, int(self.total))
        if not total:
            return 0.0
        done = min(max(0, int(self.index)) + 1, total)
        return done * 100.0 / total

    def text(self) -> str:
        """`percent` formatted the way the status bar has always shown it."""
        return f"{self.percent:.2f}%"


class BookBase:
    """What the window needs from any book, without knowing which kind it is.

    Subclasses set the class attributes and, where they have the data, override
    the methods. Everything here has a working default so a half-built subclass
    still opens instead of raising in the middle of a paint.
    """

    #: KIND_TEXT or KIND_COMIC.
    kind: str = KIND_TEXT

    #: How many positions this book has: chapters for text, pages for comics.
    #:
    #: The default reads `chapter_count` by duck typing so that `Book` satisfies
    #: this interface without gaining a single new line of its own - `Book`'s own
    #: `chapter_count` property is the answer for a text book, and `ComicBook`
    #: overrides this with `len(self.pages)`.
    @property
    def position_count(self) -> int:
        return int(getattr(self, "chapter_count", 0) or 0)

    #: "章" or "页".
    @property
    def position_label(self) -> str:
        return POSITION_LABELS.get(self.kind, POSITION_LABELS[KIND_TEXT])

    #: True when this book is paged rather than scrolled. The window uses it to
    #: decide which bottom-bar controls make sense, never to branch on format.
    @property
    def is_paged(self) -> bool:
        return self.kind == KIND_COMIC

    def position(self, index: int = 0, offset: int = 0, span: int = 1) -> Position:
        """Build a Position already carrying this book's kind and total."""
        return Position(
            kind=self.kind,
            index=max(0, int(index)),
            offset=max(0, int(offset)),
            total=self.position_count,
            span=max(1, int(span)),
        )

    def describe(self) -> str:
        """One line for the title tooltip. Subclasses override."""
        return f"{self.position_count} {self.position_label}"

    def to_record(self) -> dict:
        """The shelf row written into library.json. Subclasses override."""
        return {}

    @classmethod
    def from_path(cls, path, **kwargs) -> "BookBase":
        """Open `path`. Each subclass implements its own."""
        raise NotImplementedError
