"""The palette, and a WCAG contrast check that runs as a test.

One source of truth for colour. The window stylesheet, the settings dialog and the tray
icon all read from here, so a palette change cannot leave the dialog unreadable while
the main window stays fine.

Two themes live here: `dark` (the QQ-measured source colours the app shipped with) and
`light`. **Every** module that paints reads `PALETTE` by key at call time, so switching
themes is a matter of rewriting that one dict in place - see `apply_theme()` and the
warning on it. Rebinding the name instead would leave every importer pointing at the old
dict and the switch would fail silently.

`contrast_ratio()` exists because "the dark theme looks fine" is not a check. Small
secondary text on a dark background is the classic failure - the original muted grey
was 4.37:1, just under the 4.5:1 AA floor, and it was only visible because it was
measured. `tests/test_theme.py` asserts every pair the app actually renders, in **both**
themes.

Reference: WCAG 2.1 relative luminance and contrast ratio.
"""

from __future__ import annotations

# 两套主题。键名两套完全一致——所有 QSS 与 paint 代码都按下标取值，缺一个键就是
# KeyError，所以这里用同一张 11 键的表，只是值不同。
THEMES: dict[str, dict[str, str]] = {
    "dark": {
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
    },
    "light": {
        # 白昼配色。与深色相反，层级靠"压暗"表达：底色最浅、浮起面纯白、控件面回到底色。
        # 11 对 TEXT_PAIRS 全部过 AA 4.5，最紧的是 accent/panel = 4.73。
        "bg": "#f7f7f8",       # reader background
        "panel": "#f7f7f8",    # title bar / status bar / dialog body
        "card": "#ffffff",     # raised surfaces: dialog group boxes, opacity popup
        "field": "#ffffff",    # inputs and secondary buttons
        "hover": "#ebebee",    # tool button hover block
        "border": "#d8d8dc",   # frames, input borders, separators
        # text
        "fg": "#1e1e21",       # body text and chapter titles
        "muted": "#5a5a61",    # status bar, hints
        "faint": "#7c7c84",    # 进度百分比专用（装饰档，3.87:1，不进 TEXT_PAIRS）
        "warn": "#8a5a00",     # registration failures, low-confidence encoding
        "accent": "#2f6fbf",   # focus rings and the slider groove
    },
}

# 主题名元组。storage.py 的白名单、设置页下拉的顺序、顶栏按钮的循环都以它为准；
# 顺序即"下一档"的顺序（🌙 按钮点一下切下一个）。
THEME_NAMES: tuple[str, ...] = tuple(THEMES)

DEFAULT_THEME = "dark"

# 主题的中文名。键必须与 THEME_NAMES 完全一致（有测试钉这条）。
# 放这里而不是各自定义：顶栏 🌙 的 tooltip 与设置页下拉都要用，两处文案必须一样；
# 而 settings_dialog.py 是被 window.py import 的，不能反过来去 import window.py。
THEME_LABELS: dict[str, str] = {
    "dark": "暗夜",
    "light": "白昼",
}

# QQ 迷你窗实测出来的、**不属于 11 键调色板**的颜色：右键菜单、翻页胶囊描边、
# tooltip、禁用/按下/聚焦态、滑块禁用三色、滚动条 rgba。它们是"某一档亮度"的
# 具体取值而不是语义角色，所以单独一张同构表，而不是硬写进 QSS。
#
# 表存在的理由和 PALETTE 一样：QSS 源码里不许再出现 6 位 hex 字面量，否则白昼
# 主题下就会出现"白底白字"——那类 bug 只能在真桌面上撞见，测不出来。
EXTRAS: dict[str, dict[str, str]] = {
    "dark": {
        # 正文右键菜单（QQ 04_context_menu_user.png 实测）
        "menu_bg": "#2b2b2b",
        "menu_fg": "#bdbdc0",
        "menu_hover": "#3a3a3a",
        "menu_border": "#3a3a3a",
        "menu_disabled": "#6f6f72",
        # 底栏翻页胶囊（QQ 实测描边/文字）
        "capsule_border": "#6e6e6f",
        "capsule_fg": "#a3a3a3",
        # tooltip（QQ 实测：底 #262626、字 #9b9b9b）
        "tooltip_bg": "#262626",
        "tooltip_fg": "#9b9b9b",
        "tooltip_border": "#262626",
        # 正文选中底色
        "selection_bg": "#444a63",
        # 控件交互态
        "focus_border": "#5a5a5a",
        "pressed_bg": "#454545",
        "handle_hover": "#4a4a4a",
        # 禁用态
        "disabled_fg": "#6f6f72",
        "disabled_bg": "#262626",
        "disabled_border": "#333333",
        # 滑块：悬停手柄刻意比常态更亮（深色下的"点亮"方向）
        "slider_handle_hover": "#ffffff",
        "slider_groove_disabled": "#2a2a2a",
        "slider_subpage_disabled": "#3d4a63",
        "slider_handle_disabled": "#6a6a6a",
        # 主按钮（橙底）上的文字与禁用态
        "primary_disabled_bg": "#6a5626",
        "primary_disabled_fg": "#3a3a3a",
        # 滚动条。深色下用白色 rgba 提亮；白昼必须反过来，否则白底上看不见。
        "scrollbar_handle": "rgba(255,255,255,0.18)",
        "scrollbar_handle_hover": "rgba(255,255,255,0.30)",
    },
    "light": {
        "menu_bg": "#ffffff",
        "menu_fg": "#1e1e21",
        "menu_hover": "#ebebee",
        "menu_border": "#d8d8dc",
        "menu_disabled": "#8e8e96",     # 3.25:1，禁用态不要求 AA
        "capsule_border": "#bcbcc2",    # 1.77:1，纯装饰描边，不进 TEXT_PAIRS
        "capsule_fg": "#5a5a61",        # 6.39:1
        "tooltip_bg": "#ffffff",
        "tooltip_fg": "#3c3c42",        # 10.95:1
        "tooltip_border": "#c9c9cf",
        "selection_bg": "#c9d8f2",      # 选中字仍是 fg，11.55:1
        "focus_border": "#8a8a92",      # 3.43:1，1px 描边属非文本对比度
        "pressed_bg": "#dedee3",        # 12.40:1
        "handle_hover": "#b8b8c0",      # 滚动条手柄悬停：比常态更深，方向与深色相反
        "disabled_fg": "#8e8e96",
        "disabled_bg": "#e4e4e7",
        "disabled_border": "#c9c9cf",
        # 方向翻转：深色下悬停手柄提亮到 #ffffff，白昼下必须压暗，否则白压白。
        "slider_handle_hover": "#3c3c42",
        "slider_groove_disabled": "#e4e4e7",
        "slider_subpage_disabled": "#c9d8f2",
        "slider_handle_disabled": "#a0a0a6",
        "primary_disabled_bg": "#e9d3ad",
        "primary_disabled_fg": "#8e8e96",
        "scrollbar_handle": "rgba(0,0,0,0.18)",
        "scrollbar_handle_hover": "rgba(0,0,0,0.30)",
    },
}

# **活动调色板**。所有消费方都是 `from .theme import PALETTE` 之后按下标取值，所以
# 切换主题只能就地改写这一个 dict（clear + update），不能把 PALETTE 重新绑定到
# THEMES[name]——那样已 import 的模块仍持有旧字典，切换会静默失效且不报错。
PALETTE: dict[str, str] = dict(THEMES[DEFAULT_THEME])

# EXTRAS 的活动副本，规则与 PALETTE 完全相同（就地改写，不重新绑定）。
ACTIVE_EXTRAS: dict[str, str] = dict(EXTRAS[DEFAULT_THEME])

# 当前主题名。仅用于展示与"下一档"计算，判断配色请一律读 PALETTE。
CURRENT: str = DEFAULT_THEME

# 每个主题下的正文默认配色。深色取 Settings.bg/fg 的出厂值；白昼取白底深字。
# 这两对**故意**和 Settings 的默认值分开写：改 Settings 默认值不该悄悄改掉
# 「体贴规则」的判据。
READ_DEFAULTS: dict[str, tuple[str, str]] = {
    "dark": ("#1b1b1b", "#d8d8da"),
    "light": ("#ffffff", "#1e1e21"),
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
    """[(role, ratio, passes_AA)] for every pair the UI paints, in the active theme."""
    out = []
    for role, fg_key, bg_key in TEXT_PAIRS:
        ratio = contrast_ratio(PALETTE[fg_key], PALETTE[bg_key])
        out.append((role, ratio, ratio >= AA_NORMAL))
    return out


def apply_theme(name: str) -> str:
    """把 `name` 装进活动调色板，返回真正生效的主题名。

    非法名（含 None/空串/大小写不符）一律回落 dark 并返回 "dark"——设置对话框和磁盘
    上的值都可能被手改坏，静默回落比抛异常好：一个坏字段不该让窗口打不开。

    ★ 这里必须 clear + update **同一个** dict 对象。写成 `PALETTE = THEMES[name]` 会把
    名字重新绑定到新字典，而 window.py / settings_dialog.py / toc_panel.py / tray.py /
    page_view.py 都是 `from .theme import PALETTE` 拿到的旧引用，结果是切换后界面
    一点不变、也不报任何错（tests/test_theme.py 的 id() 断言就是钉这条）。
    """
    global CURRENT
    key = name if name in THEMES else DEFAULT_THEME
    PALETTE.clear()
    PALETTE.update(THEMES[key])
    ACTIVE_EXTRAS.clear()
    ACTIVE_EXTRAS.update(EXTRAS[key])
    CURRENT = key
    return key


def next_theme(name: str | None = None) -> str:
    """`name` 之后的下一档主题名（按 THEME_NAMES 顺序循环）。"""
    key = name if name in THEMES else CURRENT
    order = THEME_NAMES
    return order[(order.index(key) + 1) % len(order)]


def read_defaults(name: str | None = None) -> tuple[str, str]:
    """主题 `name`（默认当前）对应的正文默认配色 (bg, fg)。

    「体贴规则」的判据：正文配色仍等于**原主题**的默认值，才说明用户从没动过它，
    这时切主题可以顺手跟过去；否则一律保留用户的选择。
    """
    key = name if name in THEMES else CURRENT
    return READ_DEFAULTS[key]
