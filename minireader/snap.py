"""Edge snapping and off-screen rescue, as pure geometry.

Deliberately Qt-free and display-free: a snap bug is a *maths* bug, and maths is
testable in milliseconds without a window manager. The window code passes in plain
`(x, y, w, h)` tuples, which is the same shape `storage.validate_geometry()` and
`qt.screen_rects()` already use.

## Coordinate space

Every rect here is in **Qt logical pixels**, both the window and the work areas. Do not
mix in `GetMonitorInfoW` output, which is physical pixels - see `win32.py`.

## Why snapping is done after the drag settles, not during it

Dragging goes through `QWindow.startSystemMove()`, which hands the gesture to Windows.
Windows applies its own Aero Snap while the mouse is down. Snapping mid-drag would
fight it and produce a window that jitters between two positions. So the window code
runs these functions once, ~140 ms after movement stops, when the OS has finished.
"""

from __future__ import annotations

Rect = tuple[int, int, int, int]

DEFAULT_DISTANCE = 14
_MIN_VISIBLE = 48
_MIN_GRAB = 10


def overlap_area(a: Rect, b: Rect) -> int:
    """Area of the intersection of two rects; 0 when they only touch."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    dy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return dx * dy


def active_work_area(x: int, y: int, w: int, h: int, work_areas: list[Rect]) -> Rect | None:
    """The work area the window "belongs to".

    Chosen by which one contains the window's centre - that is what a user perceives as
    the current screen - falling back to the largest overlap, then to the first area.
    A window straddling two monitors has its centre on exactly one of them.
    """
    if not work_areas:
        return None
    cx, cy = x + w / 2.0, y + h / 2.0
    for area in work_areas:
        ax, ay, aw, ah = area
        if ax <= cx < ax + aw and ay <= cy < ay + ah:
            return area
    rect = (x, y, w, h)
    best = max(work_areas, key=lambda a: overlap_area(rect, a))
    return best if overlap_area(rect, best) > 0 else work_areas[0]


def snap_position(
    x: int,
    y: int,
    w: int,
    h: int,
    work_areas: list[Rect],
    distance: int = DEFAULT_DISTANCE,
) -> tuple[int, int]:
    """Pull the window to the nearest work-area edge if it is within `distance`.

    The two axes are decided independently, so corners fall out for free (both axes
    snap) and an edge-hugging window on a second monitor works normally. Returns the
    input unchanged when nothing is close enough, when snapping is off (distance <= 0),
    or when there are no work areas at all.
    """
    if distance <= 0 or not work_areas:
        return x, y
    area = active_work_area(x, y, w, h, work_areas)
    if area is None:
        return x, y
    ax, ay, aw, ah = area

    # Left/right edges for x, top/bottom for y. The right and bottom targets are
    # offset by the window size so the window ends up *inside* the work area.
    new_x = _snap_axis(x, ax, ax + aw - w, distance)
    new_y = _snap_axis(y, ay, ay + ah - h, distance)
    return new_x, new_y


def _snap_axis(value: int, low: int, high: int, distance: int) -> int:
    """Snap one coordinate to `low` or `high`, whichever is nearer and in range."""
    if abs(value - low) <= distance:
        return low
    if abs(value - high) <= distance:
        return high
    return value


def reachable(rect: Rect, area: Rect, min_visible: int = _MIN_VISIBLE, min_grab: int = _MIN_GRAB) -> bool:
    """Can the user still grab this window inside `area`?

    Judged per axis, not by area. An 200x10 sliver of title bar is perfectly grabbable
    while 48x48 of a corner is not (the title bar may be off-screen), and an area test
    cannot tell those apart - 2000 px^2 would fail a 2304 px^2 threshold.
    """
    rx, ry, rw, rh = rect
    ax, ay, aw, ah = area
    overlap_x = max(0, min(rx + rw, ax + aw) - max(rx, ax))
    overlap_y = max(0, min(ry + rh, ay + ah) - max(ry, ay))
    return overlap_x >= min_visible and overlap_y >= min_grab


def clamp_position(
    x: int,
    y: int,
    w: int,
    h: int,
    work_areas: list[Rect],
    min_visible: int = _MIN_VISIBLE,
    min_grab: int = _MIN_GRAB,
) -> tuple[int, int]:
    """Keep the window grabbable: at least `min_visible` px wide and `min_grab` px of
    title bar inside some work area.

    A dragged-off-screen window is unrecoverable by mouse, and with the taskbar hidden
    there is no taskbar button to right-click either. This is the same reasoning as
    `validate_geometry()` applies at startup, extended to runtime dragging.

    Only enforces reachability - it will not fight a legitimate straddle between two
    monitors, because every work area is considered before anything moves.
    """
    if not work_areas:
        return x, y
    rect = (x, y, w, h)
    if any(reachable(rect, a, min_visible, min_grab) for a in work_areas):
        return x, y

    # Nothing is reachable: pull the window back into the nearest work area, keeping
    # only a sliver of it outside so the intent of the drag is still visible.
    area = active_work_area(x, y, w, h, work_areas)
    if area is None:
        return x, y
    ax, ay, aw, ah = area
    new_x = max(ax - w + min_visible, min(x, ax + aw - min_visible))
    new_y = max(ay, min(y, ay + ah - min_grab))
    return int(new_x), int(new_y)
