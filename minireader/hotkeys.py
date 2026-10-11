"""Global hotkey for the boss key, via Win32 RegisterHotKey.

Why RegisterHotKey and not a keyboard hook:

* `WH_KEYBOARD_LL` / the `keyboard` and `pynput` packages install a system-wide
  hook. That is precisely the "inject into other processes" behaviour the project
  forbids, it is blocked by UIPI against elevated windows, and endpoint-protection
  software flags it. RegisterHotKey asks the OS to notify *us* and touches nothing
  else. No admin rights, no hook, no injection.
* RegisterHotKey reports its own conflicts through GetLastError, so "the boss key
  silently does nothing because another app owns the chord" becomes a message the
  user can act on.

Two behaviours here were established by measurement, not by reading docs — see
tools/probe notes in the README:

* Qt delivers a single WM_HOTKEY to the native event filter TWICE (once from the
  message pump, once from the window procedure), and the two copies disagree on
  `MSG.time`, so they cannot be de-duplicated by timestamp. Toggling on every
  callback would toggle twice per keypress, i.e. appear to do nothing. Both copies
  arrive inside one event-loop turn, so the fix is to *coalesce*: queue a toggle
  and let it fire once on the next turn.
* `nativeEventFilter` must return `(bool, int)`. Returning a bare bool raises
  "invalid result from ... nativeEventFilter()" under PyQt5 5.15.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable

from .qt import QAbstractNativeEventFilter, QElapsedTimer, QTimer, native_platform_available

IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # holding the chord must not machine-gun the toggle

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ERROR_INVALID_WINDOW_HANDLE = 1400
ERROR_INVALID_PARAMETER = 87

_VK_NAMES: dict[str, int] = {
    "`": 0xC0, "~": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "space": 0x20, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28,
}
for _i in range(1, 25):
    _VK_NAMES[f"f{_i}"] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    _VK_NAMES[_c.lower()] = ord(_c)

_MOD_NAMES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN, "cmd": MOD_WIN,
}

# Display labels, used by format_hotkey() - which re-renders a spec from the parsed
# virtual-key code rather than echoing the text the user typed. The comprehension's
# filter drops every single-character key, which is right for the aliases ("escape")
# but leaves the punctuation unmapped, so `Ctrl+-` displayed as the raw code
# `Ctrl+0xbd`. The explicit passes below restore all of it.
_VK_LABELS = {v: k for k, v in _VK_NAMES.items() if len(k) > 1 or k.isalnum()}
for _punct in ("`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/"):
    _VK_LABELS[_VK_NAMES[_punct]] = _punct
# Capitals for the keys people actually write as capitals: Ctrl+A, Ctrl+F9, Ctrl+Home
# stays as-is because its alias is longer than one character.
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK_LABELS[ord(_c)] = _c
for _i in range(1, 25):
    _VK_LABELS[0x6F + _i] = f"F{_i}"
# Conventional spellings for the named keys, so the dialog shows "Ctrl+PageDown"
# rather than "Ctrl+pagedown".
for _name, _label in (
    ("space", "Space"), ("tab", "Tab"), ("esc", "Esc"), ("escape", "Esc"),
    ("insert", "Insert"), ("delete", "Delete"), ("home", "Home"), ("end", "End"),
    ("pageup", "PageUp"), ("pagedown", "PageDown"),
    ("left", "Left"), ("up", "Up"), ("right", "Right"), ("down", "Down"),
):
    _VK_LABELS[_VK_NAMES[_name]] = _label


class HotkeyError(Exception):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'Ctrl+Shift+`' -> (modifiers, virtual-key). Raises HotkeyError."""
    parts = [p.strip() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("热键是空的")
    mods = 0
    key: str | None = None
    for part in parts:
        low = part.lower()
        if low in _MOD_NAMES:
            mods |= _MOD_NAMES[low]
            continue
        if key is not None:
            raise HotkeyError(f"看不懂的热键：{spec}（末尾只能有一个按键）")
        key = low
    if key is None:
        raise HotkeyError(f"看不懂的热键：{spec}（缺少按键）")
    if key not in _VK_NAMES:
        raise HotkeyError(f"不支持的按键：{key}")
    vk = _VK_NAMES[key]
    # A bare letter/digit would steal that key from every other program, so require
    # a modifier unless it is an F-key (which is what they are for).
    is_fkey = 0x70 <= vk <= 0x87
    if mods == 0 and not is_fkey:
        raise HotkeyError("单独一个按键会抢走全系统的输入，请加上 Ctrl / Alt / Shift / Win")
    return mods, vk


def format_hotkey(spec: str) -> str:
    """Normalise for display: 'ctrl+shift+`' -> 'Ctrl+Shift+`'.

    A spec that cannot be parsed is returned unchanged rather than raising: this runs
    while building the UI, and a typo in settings.json must not take the window down.
    """
    spec = spec or ""
    try:
        mods, vk = parse_hotkey(spec)
    except HotkeyError:
        return spec
    out = []
    for bit, label in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")):
        if mods & bit:
            out.append(label)
    out.append(_VK_LABELS.get(vk, hex(vk)))
    return "+".join(out)


def _describe_error(code: int) -> str:
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "这个组合键已经被别的程序占用了，换一个"
    if code == ERROR_INVALID_WINDOW_HANDLE:
        return "窗口句柄无效（窗口还没创建好）"
    if code == ERROR_INVALID_PARAMETER:
        return "这个组合键系统不接受"
    return f"注册失败（Win32 错误 {code}）"


if IS_WINDOWS:
    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hWnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", wintypes.POINT),
        ]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
else:  # pragma: no cover - the app targets Windows
    _MSG = None
    _user32 = None


def _query_mods() -> tuple[int, int, int, int]:
    """(ctrl, alt, shift, meta) as the binding spells them.

    Qt 5 keeps these flat on `Qt`; Qt 6 nests them under `Qt.KeyboardModifier`.
    """
    from .qt import Qt

    def pick(*names):
        for name in names:
            value = getattr(Qt, name, None)
            if value is not None:
                return int(value)
        return 0

    return (
        pick("ControlModifier"),
        pick("AltModifier"),
        pick("ShiftModifier"),
        pick("MetaModifier"),
    )


# Qt key value -> the token `parse_hotkey` understands. Built lazily because it needs
# the enum names from whichever binding is loaded.
def _qt_key_table() -> dict[int, str]:
    from .qt import Qt

    table: dict[int, str] = {}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        value = getattr(Qt, f"Key_{letter}", None)
        if value is not None:
            table[int(value)] = letter.lower()
    for digit in "0123456789":
        value = getattr(Qt, f"Key_{digit}", None)
        if value is not None:
            table[int(value)] = digit
    for index in range(1, 25):
        value = getattr(Qt, f"Key_F{index}", None)
        if value is not None:
            table[int(value)] = f"f{index}"
    named = {
        "Key_QuoteLeft": "`", "Key_Minus": "-", "Key_Equal": "=",
        "Key_BracketLeft": "[", "Key_BracketRight": "]", "Key_Backslash": "\\",
        "Key_Semicolon": ";", "Key_Apostrophe": "'", "Key_Comma": ",",
        "Key_Period": ".", "Key_Slash": "/", "Key_Space": "space",
        "Key_Tab": "tab", "Key_Escape": "esc", "Key_Insert": "insert",
        "Key_Delete": "delete", "Key_Home": "home", "Key_End": "end",
        "Key_PageUp": "pageup", "Key_PageDown": "pagedown",
        "Key_Left": "left", "Key_Up": "up", "Key_Right": "right", "Key_Down": "down",
    }
    for attr, token in named.items():
        value = getattr(Qt, attr, None)
        if value is not None:
            table[int(value)] = token
    return table


def spec_from_qt(key: int, modifiers: int) -> str | None:
    """A Qt key press as a MiniReader hotkey spec, e.g. `Ctrl+Shift+``.

    Returns None when the press has no name in this vocabulary - a bare modifier, a
    media key, or anything else we cannot hand to `RegisterHotKey`. The result is NOT
    validated: a spec with no modifier is still returned, so the settings dialog can
    show the user what they pressed and explain why it is rejected, instead of silently
    swallowing the keystroke.
    """
    mods = int(modifiers or 0)
    ctrl, alt, shift, meta = _query_mods()
    parts: list[str] = []
    if mods & ctrl:
        parts.append("Ctrl")
    if mods & alt:
        parts.append("Alt")
    if mods & shift:
        parts.append("Shift")
    if mods & meta:
        parts.append("Win")
    token = _qt_key_table().get(int(key or 0))
    if token is None:
        return None
    parts.append(token)
    return "+".join(parts)


def register_all(
    manager,
    bindings: dict[str, str],
    hwnd: int,
    labels: dict[str, str] | None = None,
) -> dict[str, tuple[bool, str]]:
    """Register every bound chord, replacing whatever was registered before.

    Duplicate chords are detected *before* asking Windows: `RegisterHotKey` would report
    the second one as error 1409 "already registered by another program", which sends
    the user hunting for a phantom application instead of looking at their own two
    identical bindings. Returns {name: (ok, message)} in input order.
    """
    manager.unregister_all()
    labels = labels or {}
    results: dict[str, tuple[bool, str]] = {}
    claimed: dict[tuple[int, int], str] = {}
    for name, spec in bindings.items():
        if not spec:
            continue
        shown = labels.get(name, name)
        try:
            chord = parse_hotkey(spec)
        except HotkeyError as exc:
            results[name] = (False, str(exc))
            continue
        if chord in claimed:
            results[name] = (False, f"和「{claimed[chord]}」用了同一个组合键，已跳过")
            continue
        claimed[chord] = shown
        results[name] = manager.register(name, spec, hwnd)
    return results


class HotkeyManager(QAbstractNativeEventFilter):
    """Owns every global hotkey in the process and dispatches them by name."""

    def __init__(self, on_activate: Callable[[str], None]) -> None:
        super().__init__()
        self._on_activate = on_activate
        self._id_to_name: dict[int, str] = {}
        self._next_id = 1
        self._hwnd: int | None = None
        self._queued_name: str | None = None
        self._attached = False
        # Debounce floor. The coalescing below already collapses the double
        # delivery; this only covers a future Qt that splits it across turns.
        self.debounce_ms = 60
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_fire = -(10 ** 9)

    # ---- lifecycle -------------------------------------------------------

    @property
    def available(self) -> bool:
        """False offscreen/minimal or off Windows: RegisterHotKey needs a real HWND."""
        return bool(IS_WINDOWS and _user32 is not None and native_platform_available())

    def attach(self, app) -> None:
        if not self._attached:
            app.installNativeEventFilter(self)
            self._attached = True

    def detach(self, app) -> None:
        if self._attached:
            try:
                app.removeNativeEventFilter(self)
            except Exception:  # noqa: BLE001 - never let teardown raise
                pass
            self._attached = False

    # ---- registration ----------------------------------------------------

    def register(self, name: str, spec: str, hwnd: int) -> tuple[bool, str]:
        """Register one chord. Returns (ok, human-readable message)."""
        try:
            mods, vk = parse_hotkey(spec)
        except HotkeyError as exc:
            return False, str(exc)
        if not self.available:
            return False, "当前环境没有真实窗口句柄，无法注册全局热键"
        if not hwnd:
            return False, "窗口还没创建好，稍后再试"

        hotkey_id = self._next_id
        self._next_id += 1
        ok = _user32.RegisterHotKey(wintypes.HWND(hwnd), hotkey_id, mods | MOD_NOREPEAT, vk)
        if not ok:
            code = ctypes.get_last_error()
            return False, _describe_error(code)
        self._id_to_name[hotkey_id] = name
        self._hwnd = hwnd
        return True, f"{format_hotkey(spec)}"

    def unregister_all(self) -> None:
        if not self.available:
            self._id_to_name.clear()
            return
        for hotkey_id in list(self._id_to_name):
            try:
                _user32.UnregisterHotKey(wintypes.HWND(self._hwnd) if self._hwnd else None, hotkey_id)
            except Exception:  # noqa: BLE001
                pass
        self._id_to_name.clear()

    # ---- delivery --------------------------------------------------------

    def nativeEventFilter(self, eventType, message):  # noqa: N802 - Qt override
        try:
            et = eventType if isinstance(eventType, bytes) else bytes(eventType)
        except Exception:  # noqa: BLE001
            et = b""
        if et == b"windows_generic_MSG" and _MSG is not None:
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == WM_HOTKEY:
                    name = self._id_to_name.get(int(msg.wParam))
                    if name:
                        self._queue(name)
            except Exception:  # noqa: BLE001 - a filter must never raise into Qt
                pass
        return False, 0

    def _queue(self, name: str) -> None:
        """Collapse the (measured) duplicate delivery into a single activation."""
        if self._queued_name is not None:
            return
        self._queued_name = name
        QTimer.singleShot(0, self._fire)

    def _fire(self) -> None:
        name, self._queued_name = self._queued_name, None
        if name is None:
            return
        now = self._clock.elapsed()
        if now - self._last_fire < self.debounce_ms:
            return
        self._last_fire = now
        try:
            self._on_activate(name)
        except Exception:  # noqa: BLE001 - a UI slot must not kill the filter
            pass


class NullHotkeys:
    """Used when global hotkeys are unavailable, so callers need no branching."""

    available = False
    debounce_ms = 0

    def __init__(self, on_activate: Callable[[str], None] | None = None) -> None:
        self._on_activate = on_activate

    def attach(self, app) -> None:  # noqa: ARG002
        return

    def detach(self, app) -> None:  # noqa: ARG002
        return

    def register(self, name: str, spec: str, hwnd: int) -> tuple[bool, str]:  # noqa: ARG002
        return False, "本版本只用 Win32 全局热键，当前环境不支持"

    def unregister_all(self) -> None:
        return


def make_manager(on_activate: Callable[[str], None]):
    return HotkeyManager(on_activate) if IS_WINDOWS else NullHotkeys(on_activate)
