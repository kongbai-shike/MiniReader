"""The palette, and a WCAG contrast check that runs as a test.

One source of truth for colour. The window stylesheet, the settings dialog and the tray
icon all read from here, so a palette change cannot leave the dialog unreadable while
the main window stays fine.

`contrast_ratio()` exists because "the dark theme looks fine" is not a check. Small
secondary text on a dark background is the classic failure - the original muted grey
was 4.37:1, just under the 4.5:1 AA floor, and it was only visible because it was
measured. `tests/test_theme.py` asserts every pair the app actually renders.

Reference: WCAG 2.1 relative luminance and contrast ratio.
"""

from __future__ import annotations

PALETTE: dict[str, str] = {
    # surfaces —— 对齐 QQ 迷你窗实测源色：底色 #1b1b1b，浮起面（分组框/浮层）略亮，
    # 控件面再亮一档。深色 UI 的层级靠"抬亮"而不是"压暗"表达。
    "bg": "#1b1b1b",       # reader background（源色，配合 bg_alpha 呈现半透明）
    "panel": "#1b1b1b",    # title bar / status bar / dialog body —— 与主窗一致
    "card": "#212121",     # raised surfaces: dialog group boxes, opacity popup
    "field": "#2d2d2d",    # inputs and secondary buttons
    "hover": "#2d2d2d",    # tool button hover block（QQ 实测 26×26 圆角块的源色）
    "border": "#3a3a3a",   # frames, input borders, separators
    # text
    "fg": "#d8d8da",       # body text and chapter titles
    "muted": "#9b9b9b",    # status bar, hints  (>= 4.5:1 on panel, see the test)
    "faint": "#67676a",    # 进度百分比专用：刻意弱化，不进 TEXT_PAIRS（装饰性信息）
    "warn": "#e0b050",     # registration failures, low-confidence encoding
    "accent": "#6f9ee8",   # focus rings and the slider groove
}

# Every (foreground, background) pair the UI actually paints, with the role name used
# in failure messages. Keep in sync with QSS in `ui/window.py` and `ui/settings_dialog.py`.
TEXT_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("正文", "fg", "bg"),
    ("状态栏", "muted", "panel"),
    ("提示文字", "muted", "panel"),
    ("警告文字", "warn", "panel"),
    ("标题栏", "fg", "panel"),
    ("对话框正文", "fg", "panel"),
    ("对话框次要文字", "muted", "panel"),
    ("对话框分组内文字", "fg", "card"),
    ("对话框分组内次要文字", "muted", "card"),
    ("输入框文字", "fg", "field"),
    ("强调色", "accent", "panel"),
)

AA_NORMAL = 4.5   # normal-size text
AA_LARGE = 3.0    # >= 18.66 px bold or >= 24 px


def _channel(value: float) -> float:
    """One sRGB channel (0-1) to linear light."""
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a `#rgb` or `#rrggbb` string.

    Raises ValueError on anything else. Treating a malformed colour as black instead
    would turn the audit below into a rubber stamp - a typo in the palette would be
    measured as black, and on a dark background that happens to produce a *plausible*
    ratio. The palette is a module constant and every caller passes it, so a loud
    failure costs nothing and catches exactly the mistake that matters.
    """
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"不是 #rgb / #rrggbb 颜色：{color!r}")
    try:
        r, g, b = (int(text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError as exc:
        raise ValueError(f"不是 #rgb / #rrggbb 颜色：{color!r}") from exc
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio, 1.0 (identical) to 21.0 (black on white).

    Raises ValueError for a colour that is not `#rgb`/`#rrggbb` (see
    `relative_luminance`).
    """
    a = relative_luminance(foreground)
    b = relative_luminance(background)
    lighter, darker = (a, b) if a >= b else (b, a)
    return (lighter + 0.05) / (darker + 0.05)


def audit() -> list[tuple[str, float, bool]]:
    """[(role, ratio, passes_AA)] for every pair the UI paints."""
    out = []
    for role, fg_key, bg_key in TEXT_PAIRS:
        ratio = contrast_ratio(PALETTE[fg_key], PALETTE[bg_key])
        out.append((role, ratio, ratio >= AA_NORMAL))
    return out
