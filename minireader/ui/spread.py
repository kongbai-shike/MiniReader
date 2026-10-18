"""Two-page spread arithmetic (mode C), as pure functions.

Everything here is arithmetic on page sizes and page counts - no Qt, no widgets,
no book. That is deliberate: the rules below are the part most likely to be got
subtly wrong (which page goes on which side, where a lone page lands, how two
pages of different shapes share one height), and pure functions can be tested
exhaustively without a QApplication. `ComicReader` only calls them.

Five rules, and why each is what it is:

**A spread is a pair, and the cover may stand alone.** With `cover_alone` the
sequence is ``[0]``, ``[1, 2]``, ``[3, 4]``, ... - the shape a printed manga has.
Without it, ``[0, 1]``, ``[2, 3]``, ... The anchors therefore are *not* simply
even numbers when the cover stands alone (they are 0, 1, 3, 5, ...), which is why
`spread_starts()` exists rather than callers doing ``range(0, n, 2)``.
**Right-binding (rtl) puts the lower page number on the right.** Japanese manga
is read right to left, so within a pair page 1 sits to the right of page 2. This
is implemented by *swapping the two widgets' order* in the row, not by mirroring
anything: the layout is the only thing that knows about direction, so the page
widgets themselves stay direction-agnostic.

**A lone page takes the leading slot.** In rtl the leading slot is the right one,
in ltr the left one. This covers both the cover (when it stands alone) and an odd
final page, and using one rule for both is what keeps the reading order
continuous: a spread always *begins* at the leading edge.

**Two pages share one height.** Sizing each page width-fit would make a pair of
differently-shaped pages different heights, so the seam between them would be
ragged - the thing that makes a two-page view look broken. `pair_size()` picks a
single height for the pair instead, then derives each width from it.

**The book may forbid pairing.** A fixed-layout EPUB can declare
`rendition:spread`, and ``none`` means "do not build synthetic spreads out of my
pages" - a picture book whose every page is a finished drawing, where pairing
would cut each drawing in half. `pairs_pages()` answers that, and every function
below takes `pairs=` so the caller can pass the answer through. `pairs=False`
degenerates cleanly to "one page per spread", which is why it needed no separate
code path anywhere else in this module: a one-page spread is still a spread, and
the reading order is unchanged.

Deliberately Qt-free, like `minireader.core.*`.
"""

from __future__ import annotations

#: The window must be at least this many times wider than tall before two pages
#: fit side by side without each being a useless sliver. A portrait (or square)
#: window falls back to single-page mode.
SPREAD_ASPECT = 1.4

#: Default gap between the two pages, in pixels. Zero because scanned manga
#: already carries its own margin; the setting exists for scans that were cropped
#: to the edge, where the two pages would otherwise touch.
DEFAULT_GUTTER = 0

#: `comic_gutter` is clamped to this, matching `storage.Settings.clamp()`.
MAX_GUTTER = 32

#: Substrings that mark a page as a cover. Matched case-folded against the page's
#: display name. "000" catches the very common `000.jpg` / `0001.png` scan naming,
#: where the zero-padded first page is the cover.
COVER_MARKERS = ("cover", "封面", "000")

#: The legal values of `rendition:spread`, matching
#: `minireader.core.epub.SPREAD_VALUES`. Duplicated rather than imported because
#: this module is deliberately Qt-free *and* core-free: it is arithmetic, and a
#: test can exercise it with three-line fake pages.
SPREAD_POLICIES = ("auto", "none", "landscape", "both")


def _fold(text: str) -> str:
    return str(text or "").casefold()


def pairs_pages(policy: str = "", landscape: bool = True) -> bool:
    """Whether this book's pages may be paired into synthetic spreads at all.

    `policy` is a `rendition:spread` value, or empty for "the book did not say".
    The four values answer exactly one question - *may* spreads be rendered - and
    deliberately say nothing about page shapes:

    * ``"none"`` - the book asks for no synthetic spreads, so every page is shown
      alone even in two-page mode.
    * ``"landscape"`` - spreads only while the reading area is landscape. The
      spec's wording is about the *device* orientation, not about wide pages, so
      this is a condition on the viewport and nothing else.
    * ``"both"`` - spreads regardless of orientation.
    * ``"auto"`` / ``""`` - the reading system decides. We decide yes, which is
      what stage 5 already did before this function existed; the difference is
      that the decision now has a name.

    The distinction that matters in practice is `none` versus everything else: a
    picture book whose every page is a finished full-page image declares `none`,
    and pairing those pages cuts each drawing in half.

    `landscape` is passed in rather than measured so the answer stays testable
    without a window; `ComicReader` asks `needs_spread_width()` for it.
    """
    value = _fold(policy).strip()
    if value == "none":
        return False
    if value == "landscape":
        return bool(landscape)
    return True


def name_says_cover(name: str) -> bool:
    """True when a page's file name announces itself as a cover."""
    folded = _fold(name)
    return any(marker in folded for marker in COVER_MARKERS)


def looks_like_a_cover(book, default: bool = True) -> bool:
    """Whether page 1 should stand alone in two-page mode.

    The spec's conservative order, and the reason it is conservative: showing a
    real story page alone throws off every later pairing, so a wrong guess is
    much worse than not guessing. Two positive signals only, then the user's
    setting:

    1. The first page's *name* says cover / 封面 / 000.
    2. The first three pages are all portrait. A cover standing alone in a
       portrait book is the standard shape; in a landscape (wide) book the first
       page is usually already a proper spread, so pairing is right.

    Returns `default` when neither signal fires - i.e. the setting is the answer,
    and the reader never overrides the user on a hunch.
    """
    pages = list(getattr(book, "pages", []) or [])
    if not pages:
        return bool(default)
    if name_says_cover(getattr(pages[0], "name", "")):
        return True
    if len(pages) >= 3:
        first_three = pages[:3]
        if all(_is_portrait(page) for page in first_three):
            return True
    return bool(default)


def _is_portrait(page) -> bool:
    width = max(0, int(getattr(page, "width", 0) or 0))
    height = max(0, int(getattr(page, "height", 0) or 0))
    return height > width > 0


def spread_starts(total: int, cover_alone: bool, *, pairs: bool = True) -> list[int]:
    """The first page index of every spread, in reading order.

    ``total=6, cover_alone=True``  -> ``[0, 1, 3, 5]``   (``[0] [1,2] [3,4] [5]``)
    ``total=6, cover_alone=False`` -> ``[0, 2, 4]``      (``[0,1] [2,3] [4,5]``)

    Note the odd final anchor in the first example: with six pages and a lone
    cover, page 5 is left over and gets a spread to itself.

    `pairs=False` means the book forbade synthetic spreads (`rendition:spread`
    is ``none``), so every page is its own spread and the anchors are simply
    ``0..total-1``. That is the whole of the "no pairing" case: the rest of this
    module keeps working, because a one-page spread is still a spread.
    """
    total = max(0, int(total))
    if total <= 0:
        return []
    if not pairs:
        return list(range(total))
    if cover_alone:
        starts = [0]
        index = 1
        while index < total:
            starts.append(index)
            index += 2
        return starts
    return list(range(0, total, 2))


def spread_at(
    index: int, total: int, cover_alone: bool, *, pairs: bool = True
) -> tuple[int, int | None]:
    """The pages shown when the spread *containing* `index` is on screen.

    Returns ``(first, second_or_None)``. The caller may pass any page number, not
    just an anchor: `index=2` with `cover_alone=True` resolves to the ``(1, 2)``
    spread, so a saved position always lands on a spread that contains it.

    ``second is None`` means the page stands alone. That happens in three
    different situations, and all of them are handled here rather than by the
    caller:

    * **the book forbade spreads** (`pairs=False`) - every page stands alone;
    * **the cover**, when `cover_alone` is set - anchor 0 is a singleton, and the
      pairing only starts at anchor 1 (``[0] [1,2] [3,4]``);
    * **an odd final page** - the last anchor is `total - 1` and `anchor + 1`
      runs off the end.
    """
    total = max(0, int(total))
    if total <= 0:
        return (0, None)
    index = max(0, min(int(index), total - 1))
    # Walk forward to the last anchor that is <= index. That anchor is the spread
    # containing `index`, because the anchors are strictly increasing and each
    # spread runs from its anchor to the next one.
    anchor = 0
    for start in spread_starts(total, cover_alone, pairs=pairs):
        if start <= index:
            anchor = start
        else:
            break
    if not pairs:
        return (anchor, None)
    if cover_alone and anchor == 0:
        # The cover stands alone; pairing begins at anchor 1.
        return (0, None)
    second = anchor + 1
    return (anchor, second if second < total else None)


def anchor_of(index: int, total: int, cover_alone: bool, *, pairs: bool = True) -> int:
    """The first page index of the spread containing `index`."""
    return spread_at(index, total, cover_alone, pairs=pairs)[0]


def next_anchor(
    index: int, total: int, cover_alone: bool, delta: int, *, pairs: bool = True
) -> int:
    """The anchor `delta` spreads away from the one containing `index`.

    Clamped to the first/last spread, so the caller can compare the result with
    the current anchor to detect "already at the end" and say so.
    """
    starts = spread_starts(total, cover_alone, pairs=pairs)
    if not starts:
        return 0
    current = anchor_of(index, total, cover_alone, pairs=pairs)
    try:
        position = starts.index(current)
    except ValueError:  # pragma: no cover - anchor_of only returns real anchors
        position = 0
    target = max(0, min(position + int(delta), len(starts) - 1))
    return starts[target]


def is_last_spread(index: int, total: int, cover_alone: bool, *, pairs: bool = True) -> bool:
    starts = spread_starts(total, cover_alone, pairs=pairs)
    if not starts:
        return True
    return anchor_of(index, total, cover_alone, pairs=pairs) >= starts[-1]


def is_first_spread(index: int, total: int, cover_alone: bool, *, pairs: bool = True) -> bool:
    starts = spread_starts(total, cover_alone, pairs=pairs)
    if not starts:
        return True
    return anchor_of(index, total, cover_alone, pairs=pairs) <= starts[0]


def visual_order(pair: tuple[int, int | None], binding: str) -> tuple[int | None, int | None]:
    """`(left_page, right_page)` for a spread, honouring the binding.

    rtl (右綴じ, Japanese): the lower page number is on the **right**, so the pair
    is swapped. ltr (左綴じ): the lower number is on the left, i.e. unchanged.

    A lone page (`second is None`) goes to the leading slot: right for rtl, left
    for ltr. The trailing slot is None.
    """
    first, second = pair
    right_to_left = str(binding).lower() != "ltr"
    if second is None:
        return (None, first) if right_to_left else (first, None)
    return (second, first) if right_to_left else (first, second)


def pair_size(
    sizes: list[tuple[int, int]],
    viewport: tuple[int, int],
    gutter: int = DEFAULT_GUTTER,
) -> tuple[int, list[int]]:
    """A shared height and each page's width, so the seam between them is level.

    `sizes` is one or two `(width, height)` pairs. Returns
    ``(height, [width, ...])`` in the order given.

    The height is the largest one that satisfies all three constraints at once:

    * it fits the viewport's height;
    * the pages side by side plus the gutter fit the viewport's width;
    * it never exceeds the smallest source height - the project never enlarges a
      page (`comic_cache.fit_size()` makes the same promise for one page, and
      blowing up a scan only makes it blurry).

    A page with an unknown size `(0, 0)` is given the height of the other one
    (or the viewport's height when both are unknown) and a width of zero, which
    the caller renders as an empty slot rather than as a zero-width page.
    """
    view_w = max(0, int(viewport[0]))
    view_h = max(0, int(viewport[1]))
    gap = max(0, int(gutter))
    clean: list[tuple[int, int]] = []
    for size in sizes:
        width = max(0, int(size[0]))
        height = max(0, int(size[1]))
        clean.append((width, height))

    known = [(w, h) for w, h in clean if w > 0 and h > 0]
    if not known:
        return (view_h, [0 for _ in clean])

    # Constraint 2: widths add up. Each page's width is height * (w/h), so the
    # total width scales linearly with the height - one division gives the cap.
    ratios = [w / h for w, h in known]
    height = float(view_h)
    total_ratio = sum(ratios)
    if total_ratio > 0 and view_w > 0:
        width_cap = max(0, view_w - gap * max(0, len(clean) - 1))
        height = min(height, width_cap / total_ratio)
    # Constraint 3: never enlarge.
    height = min(height, float(min(h for _w, h in known)))
    height = max(1.0, height)

    widths: list[int] = []
    for width, source_h in clean:
        if width <= 0 or source_h <= 0:
            widths.append(0)
        else:
            widths.append(max(1, int(round(height * width / source_h))))
    return (max(1, int(round(height))), widths)


def needs_spread_width(width: int, height: int, ratio: float = SPREAD_ASPECT) -> bool:
    """True when the reading area is wide enough for two pages.

    Pure so the trigger can be tested without resizing a window. A degenerate
    height (0) is treated as "not wide enough": the answer must not depend on a
    not-yet-laid-out widget.
    """
    width = max(0, int(width))
    height = max(0, int(height))
    if height <= 0:
        return False
    return width > height * float(ratio)
