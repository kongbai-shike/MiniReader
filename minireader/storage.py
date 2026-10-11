"""Persistence: settings, library index, reading progress, window geometry.

Everything lands in %APPDATA%\\MiniReader as small JSON files, written atomically
(tmp + os.replace) so a crash mid-save can never leave a half-written config that
the next launch trips over.

Two rules inherited from the plugin's hard-won lessons:

1. The boss-key *hidden* state is NEVER persisted. A window that starts hidden on
   next launch, with no tray icon to bring it back, is unrecoverable. This file
   simply has no field for it (client.js:212-217 records the same reasoning).
2. Restored geometry is validated against the screens that exist *now*; a window
   restored onto a monitor that has been unplugged is invisible forever.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .app_paths import APP_NAME
from .core.model import Progress

# Below this much on-screen overlap the window cannot be grabbed, so we re-centre.
_MIN_VISIBLE_W = 160
_MIN_VISIBLE_H = 48
# 窗口最小尺寸。阶段 2 底栏重构后，光两个 87px 翻页胶囊 + 目录/设置图标 + 留白就要
# ~282px，240 的旧下限会让按钮互相吃掉；320×360 是三组入口仍可点、正文仍有数行的底线。
_MIN_W, _MIN_H = 320, 360


def data_dir() -> Path:
    """The per-user writable directory (settings.json, reading.db all land here).

    Thin wrapper over `app_paths.user_data_dir()`: on Windows that is
    %APPDATA%\\MiniReader, elsewhere ~/.config/MiniReader, and it is the same directory
    in source runs and frozen builds. Never the program directory - an exe installed
    under Program Files lives in a read-only tree.
    """
    from .app_paths import user_data_dir

    return user_data_dir()


def _read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # A corrupt or missing file must degrade to defaults, never crash the app.
        return default


def _write_json(path: Path, payload) -> tuple[bool, str]:
    """Atomic write. Returns (ok, message) - callers surface failures, never raise."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True, ""
    except OSError as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def validate_geometry(
    geo: dict | None,
    screens: list[tuple[int, int, int, int]],
    *,
    min_w: int = _MIN_W,
    min_h: int = _MIN_H,
) -> dict | None:
    """Return a usable {x,y,w,h} or None meaning "let the caller centre it".

    Pure function taking plain int tuples, so it is testable without a display.
    """
    if not isinstance(geo, dict) or not screens:
        return None
    try:
        x, y = int(geo["x"]), int(geo["y"])
        w, h = int(geo["w"]), int(geo["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None

    max_w = max(s[2] for s in screens)
    max_h = max(s[3] for s in screens)
    w = max(min_w, min(w, max_w))
    h = max(min_h, min(h, max_h))

    for sx, sy, sw, sh in screens:
        overlap_x = max(0, min(x + w, sx + sw) - max(x, sx))
        overlap_y = max(0, min(y + h, sy + sh) - max(y, sy))
        if overlap_x >= min(min_w, _MIN_VISIBLE_W) and overlap_y >= min(min_h, _MIN_VISIBLE_H):
            return {"x": x, "y": y, "w": w, "h": h}
    return None


def default_geometry(screen: tuple[int, int, int, int], w: int = 397, h: int = 585) -> dict:
    """Bottom-right corner of the given screen, inset a little - where a 摸鱼 window belongs.

    397x585 is the QQ 迷你窗实测尺寸（竖长阅读形）：正文默认 25px / 210% 行距下，
    一屏约 8 行，宽度只放一栏文字，整窗仍然只占桌面右下角一块。只在新安装/重置时
    生效——老用户已存的 geometry 一律保留（走 validate_geometry 那条路）。
    """
    sx, sy, sw, sh = screen
    w = min(w, max(_MIN_W, sw - 40))
    h = min(h, max(_MIN_H, sh - 40))
    return {"x": sx + sw - w - 36, "y": sy + sh - h - 72, "w": w, "h": h}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """Reader and window preferences. Every field has a working default.

    Hand-edited JSON is the expected way to change these (the README says so), which is
    why `clamp()` coerces types rather than trusting them: `"false"` as a *string* is
    truthy in Python, so a user writing `"dim_on_blur": "false"` would otherwise get the
    opposite of what they asked for.
    """

    # typography
    font_family: str = ""            # "" = whatever the system picks
    # 字号单位是像素（reader 用 QFont.setPixelSize），25px 对齐 QQ 迷你窗默认阅读字号。
    font_size: int = 25
    line_height: int = 210           # percent of the natural line height（2.1 倍）
    # 首行缩进，单位 = 字符个数（按当前正文字体的"国"字宽度换算成像素）。
    # 0 = 不缩进；中文小说默认 2 字符。
    paragraph_indent: int = 2
    margin: int = 16
    # colours —— 默认对齐 QQ 迷你窗实测源色（#1b1b1b 是夹逼法解出的底色 RGB，
    # 不含 alpha；alpha 在 bg_alpha 里，两边分开才不会二次衰减）。
    bg: str = "#1b1b1b"
    fg: str = "#d8d8da"
    border: str = "#3a3a3a"
    # 窗口背景的 alpha（0 = 全透明，255 = 不透明）。和 bg 一起决定 paintEvent 画出来的圆角
    # 底色：bg 给 RGB，bg_alpha 给透明度。正文控件本身透明，所以"背景"只有这一处。
    # 默认 153 = 60% 不透明度，即 QQ 迷你窗实测的底色 alpha。
    bg_alpha: int = 153
    # True：半透明窗口（WA_TranslucentBackground + paintEvent 画圆角底色，alpha 只透
    # 背景、文字保持清晰）。False：兼容模式，关掉逐像素半透明，改用整窗
    # setWindowOpacity；给"半透明在某些驱动/远程桌面上出问题"留的后路。两种模式下
    # "背景透明度"滑块是同一根、语义一致。
    translucent_bg: bool = True
    # 正文文字/正文控件的 alpha（0 = 文字全透明，255 = 不透明）。和 bg_alpha 是两个
    # 独立维度：bg_alpha 只透窗口底色、文字不动；content_alpha 只透正文、底色不动。
    # 半透明模式下两者互不影响；兼容模式做不到分离，content_alpha 不参与渲染。
    content_alpha: int = 255
    # scrolling
    wheel_lines: int = 3             # how many lines one wheel notch glides
    wheel_animation_ms: int = 180    # 0 disables the glide (instant, native feel)
    # auto-scroll：按像素/秒而不是"每行多少毫秒"。像素/秒在换字号之后仍然是同一个
    # 视觉速度；"每 N 毫秒滚一行"会在字号调大后凭空快一倍。
    autoscroll_speed: int = 60       # px/s
    autoscroll_pause_on_hover: bool = True
    # window
    always_on_top: bool = True
    hide_from_taskbar: bool = True
    geometry: dict | None = None
    # appearance
    opacity: int = 100               # percent; < 100 costs ClearType on Windows
    dim_on_blur: bool = False        # opt-in for the same reason
    dim_opacity: int = 55            # percent while the window is not active
    # window behaviour
    snap_enabled: bool = True
    snap_distance: int = 14          # px; 0 disables
    keep_on_screen: bool = True      # refuse to let the window be dragged out of reach
    disguise_title: str = ""         # "" = real title
    show_tray: bool = True
    # 已废弃（设置对话框里的复选框已移除）：✕ 现在一律收起窗口、退出只认托盘菜单。
    # 字段保留是为了让旧 settings.json 里的这个键不落入"未知键"分支，load/save 仍
    # 照常兼容它的值，但没有任何产品代码再据此改变行为。
    close_to_tray: bool = False      # deprecated: kept only for old-config compatibility
    # True：鼠标移出窗口时整窗 setWindowOpacity(0) 临时隐身，移回即恢复。这是**临时
    # 覆盖**，不改 opacity 字段，也不同于老板键的 hide()（真隐藏）。默认关闭。
    mouse_leave_hide: bool = False
    # global hotkeys ("" = unbound)
    boss_key: str = "Ctrl+Shift+`"
    hotkey_next: str = ""
    hotkey_prev: str = ""
    hotkey_opacity_up: str = ""
    hotkey_opacity_down: str = ""
    # session
    last_book: str | None = None

    # ---- io --------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        raw = _read_json(path or (data_dir() / "settings.json"), {})
        if not isinstance(raw, dict):
            raw = {}
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        obj = cls(**clean)
        obj.clamp()
        return obj

    def save(self, path: Path | None = None) -> tuple[bool, str]:
        self.clamp()
        return _write_json(path or (data_dir() / "settings.json"), asdict(self))

    def clamp(self) -> None:
        """Force every value into a range the UI can actually render.

        Every coercion here is total: a hand-edited settings.json with a wrong type in
        any field must come out usable, not raise. `_as_int`/`_as_bool` are what make
        that true, so no field may bypass them with a bare int().
        """
        for name in _BOOL_FIELDS:
            setattr(self, name, _as_bool(getattr(self, name), DEFAULT_SETTINGS[name]))
        self.font_size = max(10, min(_as_int(self.font_size, 25), 48))
        self.line_height = max(100, min(_as_int(self.line_height, 210), 320))
        self.paragraph_indent = max(0, min(_as_int(self.paragraph_indent, 2), 8))
        self.margin = max(0, min(_as_int(self.margin, 16), 80))
        self.wheel_lines = max(1, min(_as_int(self.wheel_lines, 3), 20))
        self.autoscroll_speed = max(10, min(_as_int(self.autoscroll_speed, 60), 400))
        self.wheel_animation_ms = max(0, min(_as_int(self.wheel_animation_ms, 180), 1000))
        # A floor of 30% keeps the window findable by eye; 0 would be a window that is
        # present but invisible, with no tray-independent way back.
        self.opacity = max(30, min(_as_int(self.opacity, 100), 100))
        # 下限是 0 而不是 20：半透明模式下"窗口不透明度"可以一路降到全透，找回来的
        # 办法是老板键/托盘（见 MiniWindow.can_hide），而不是靠留一点余辉。兼容模式
        # 下真正落到 setWindowOpacity 的值另有 0.05 的下限（见 MiniWindow.apply_opacity），
        # 因为那条路径上 0 等于连字一起消失。
        self.dim_opacity = max(0, min(_as_int(self.dim_opacity, 55), 100))
        # 窗口背景 alpha 是 paintEvent 用的 0-255 字节值，不是不透明度百分比。
        self.bg_alpha = max(0, min(_as_int(self.bg_alpha, 153), 255))
        # 正文 alpha 同理，0-255 字节值，落在 ReaderView 的 QGraphicsOpacityEffect 上。
        self.content_alpha = max(0, min(_as_int(self.content_alpha, 255), 255))
        # 60 px is generous for a magnetic pull - beyond that it reads as the window
        # jumping on its own rather than snapping.
        self.snap_distance = max(0, min(_as_int(self.snap_distance, 14), 60))
        for name, fallback in (("bg", "#1b1b1b"), ("fg", "#d8d8da"), ("border", "#3a3a3a")):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith("#"):
                setattr(self, name, fallback)
        self.disguise_title = _clean_title(self.disguise_title)
        for name, _field, _label in HOTKEY_FIELDS:
            value = getattr(self, name)
            setattr(self, name, value.strip() if isinstance(value, str) else "")

    def copy_into_geometry(self, x: int, y: int, w: int, h: int) -> None:
        self.geometry = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}

    # ---- hotkeys ---------------------------------------------------------

    def hotkeys(self) -> dict[str, str]:
        """{hotkey name: chord} for every bound chord, in registration order.

        Empty entries are dropped here rather than at the call site so that "unbound"
        has exactly one representation.
        """
        return {name: getattr(self, name) for name, _f, _l in HOTKEY_FIELDS if getattr(self, name)}

    def target_opacity(self, *, active: bool) -> int:
        """The opacity the window should currently have, in percent.

        Pure, so the dim-on-blur rule is unit-testable without a window manager.
        """
        if active or not self.dim_on_blur:
            return self.opacity
        return min(self.opacity, self.dim_opacity)


# bg_alpha / content_alpha 在磁盘上是 0-255 的字节值（paintEvent 和
# QGraphicsOpacityEffect 都要字节），但滑块给用户看的是 0-100 的百分比。这两个函数是
# 两者之间唯一的换算入口，两边都从这里走，免得某处写反了方向。
def alpha_to_percent(alpha: int) -> int:
    """0-255 的 alpha 字段 → 0-100 的滑块值。"""
    return int(round(max(0, min(int(alpha), 255)) / 255 * 100))


def percent_to_alpha(percent: int) -> int:
    """0-100 的滑块值 → 0-255 的 alpha 字段。"""
    return int(round(max(0, min(int(percent), 100)) / 100 * 255))


# ---------------------------------------------------------------------------
# settings helpers
# ---------------------------------------------------------------------------

# (field name, label) for every configurable global hotkey. Order matters: it is the
# order they are registered in and shown in the settings dialog.
HOTKEY_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("boss_key", "boss_key", "老板键（隐藏/显示）"),
    ("hotkey_next", "hotkey_next", "下一章"),
    ("hotkey_prev", "hotkey_prev", "上一章"),
    ("hotkey_opacity_up", "hotkey_opacity_up", "提高不透明度"),
    ("hotkey_opacity_down", "hotkey_opacity_down", "降低不透明度"),
)

_BOOL_FIELDS = (
    "always_on_top",
    "hide_from_taskbar",
    "dim_on_blur",
    "translucent_bg",
    "snap_enabled",
    "keep_on_screen",
    "show_tray",
    "close_to_tray",
    "mouse_leave_hide",
)

_FALSE_WORDS = {"false", "0", "no", "off", "none", "null", ""}
_MAX_TITLE = 120


def _as_bool(value, fallback: bool) -> bool:
    """Coerce a JSON value to bool, treating the strings "false"/"0"/"no" as False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _FALSE_WORDS:
            return False
        if text in {"true", "1", "yes", "on"}:
            return True
        return fallback
    return fallback


def _as_int(value, fallback: int) -> int:
    """Coerce a JSON value to int, falling back rather than raising.

    `clamp()` used to call `int()` directly, which is fine for values this program
    wrote and fatal for a hand-edited file: `{"snap_distance": "很近"}` is valid JSON,
    so it reaches `clamp()` and blows up with ValueError on startup. `load()` promises
    to degrade to defaults for a damaged file, and this is the gap that promise had.
    """
    if isinstance(value, bool):
        # True/False are ints in Python; a bool in a numeric field is a type error, and
        # silently reading it as 1/0 would hide it.
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") else fallback
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return fallback
    return fallback


def _clean_title(value) -> str:
    """A disguise title: printable, single line, bounded length."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable())
    return text.strip()[:_MAX_TITLE]


# Track every field's default so `clamp()` can fall back without hard-coding twice.
DEFAULT_SETTINGS = {f.name: f.default for f in fields(Settings)}


# ---------------------------------------------------------------------------
# library + progress
# ---------------------------------------------------------------------------

@dataclass
class Library:
    """An index of opened books. Chapter text is re-read from the source file -
    for a 3 MB novel that is a few milliseconds, and it keeps the app-data folder
    from growing into a duplicate of the user's book collection."""

    books: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "Library":
        raw = _read_json(path or (data_dir() / "library.json"), [])
        if isinstance(raw, dict):
            raw = raw.get("books", [])
        books = [b for b in raw if isinstance(b, dict) and b.get("path")] if isinstance(raw, list) else []
        return cls(books)

    def save(self, path: Path | None = None) -> tuple[bool, str]:
        return _write_json(path or (data_dir() / "library.json"), {"books": self.books})

    def record(self, rec: dict, limit: int = 100) -> None:
        """Insert or refresh an entry, most-recent first."""
        self.books = [b for b in self.books if b.get("id") != rec.get("id")]
        self.books.insert(0, rec)
        del self.books[limit:]

    def find(self, ident: str) -> dict | None:
        for b in self.books:
            if b.get("id") == ident:
                return b
        return None


@dataclass
class ProgressStore:
    """Reading position per book id, plus which book was open last."""

    last_book: str | None = None
    by_id: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "ProgressStore":
        raw = _read_json(path or (data_dir() / "progress.json"), {})
        if not isinstance(raw, dict):
            return cls()
        by_id = raw.get("by_id")
        if not isinstance(by_id, dict):
            by_id = {}
        by_id = {k: v for k, v in by_id.items() if isinstance(v, dict)}
        last = raw.get("last_book")
        return cls(last_book=last if isinstance(last, str) else None, by_id=by_id)

    def save(self, path: Path | None = None) -> tuple[bool, str]:
        return _write_json(path or (data_dir() / "progress.json"), {"last_book": self.last_book, "by_id": self.by_id})

    def get(self, ident: str) -> Progress:
        return Progress.from_dict(self.by_id.get(ident))

    def put(self, ident: str, progress: Progress) -> None:
        self.by_id[ident] = progress.to_dict()
        self.last_book = ident
        # Keep the file from growing without bound if someone opens thousands of books.
        if len(self.by_id) > 500:
            for key in list(self.by_id)[:-500]:
                self.by_id.pop(key, None)
