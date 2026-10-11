"""Raw Win32 layer: everything the Qt API cannot express, or expresses less directly.

Design rules for this file:

* **Nothing here ever raises.** Every function is a guarded best-effort that returns a
  bool or a plain value. The app must still run on a non-Windows box or under the
  offscreen QPA plugin, where most of these calls have no meaning at all.
* **The Win32 calls are the fallback, not the first choice.** Qt already routes
  `setWindowOpacity()` through layered windows, and `Qt.Tool` already produces
  `WS_EX_TOOLWINDOW`. Those are used first because they are portable and already
  tested. What is implemented here is (a) what Qt simply does not expose - window text
  that differs from Qt's own title, forcing the tool-window bit on an existing HWND,
  reading a monitor's *work* area - and (b) a way to verify what Qt did.

## The DPI trap, which is the reason `monitor_work_area()` has two flavours

Qt reports widget geometry and screen geometry in **logical** (DPI-scaled) pixels.
`GetMonitorInfoW` returns **physical** pixels. On a 150% display a window at Qt-x=100
is at physical-x=150, so feeding a raw `rcWork` rect into `QRect.move()` puts the
window in the wrong place - and the bug only shows up on a scaled display, which is
exactly the kind of thing that ships broken.

So: `monitor_work_area(hwnd)` returns physical pixels and is documented as such, and
`logical_work_area(hwnd)` divides by the live device pixel ratio. The snapping code
takes its rects from Qt (`screen_rects()`) precisely to stay in one coordinate space.

## Layered windows and text quality

Setting `WS_EX_LAYERED` with `LWA_ALPHA` makes the whole window composited in
software. Mouse hit-testing is unaffected - `LWA_ALPHA` is not `LWA_COLORKEY`, so the
window does **not** become click-through - but Windows turns off ClearType
sub-pixel antialiasing for layered windows, so text edges go slightly soft the moment
opacity drops below 100%. That is why the default opacity is 100% and 失焦自动变淡
defaults to off: both are opt-in and the trade-off is stated in the settings panel.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
LWA_ALPHA = 0x00000002

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
_SWP_QUIET = SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE

MONITOR_DEFAULTTONEAREST = 0x00000002

_LONG_PTR = ctypes.c_ssize_t


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


if IS_WINDOWS:  # pragma: no cover - exercised on the real platform
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    # SetWindowLongPtrW only exists in 64-bit user32; 32-bit builds export
    # SetWindowLongW under the same name via the SDK macro.
    _set_long = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
    _get_long = getattr(_user32, "GetWindowLongPtrW", None) or _user32.GetWindowLongW

    _set_long.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
    _set_long.restype = _LONG_PTR
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_long.restype = _LONG_PTR

    _user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    _user32.SetWindowTextW.restype = wintypes.BOOL

    _user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND,
        wintypes.COLORREF,
        wintypes.BYTE,
        wintypes.DWORD,
    ]
    _user32.SetLayeredWindowAttributes.restype = wintypes.BOOL

    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR

    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL

    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL

    # GetDpiForWindow is Windows 10 1607+. Present on any supported machine, but
    # resolved defensively so an older user32 cannot break import.
    _get_dpi = getattr(_user32, "GetDpiForWindow", None)
    if _get_dpi is not None:
        _get_dpi.argtypes = [wintypes.HWND]
        _get_dpi.restype = wintypes.UINT
else:  # pragma: no cover
    _user32 = None
    _set_long = _get_long = _get_dpi = None


def hwnd_of(widget) -> int:
    """The native window handle behind a QWidget, or 0.

    `winId()` realises a handle without showing the window, so this is safe to call on
    a hidden widget - the self-check relies on that.
    """
    try:
        return int(widget.winId())
    except Exception:  # noqa: BLE001 - a destroyed widget must not raise
        return 0


def set_window_text(hwnd: int, text: str) -> bool:
    """Set the real window title, bypassing Qt's own cache.

    Used for 标题伪装. `QWidget.setWindowTitle()` is the primary path; this is what
    makes the change visible to anything that reads the HWND directly (window
    enumerators, capture tools, accessibility clients) even when Qt believes the title
    is unchanged.
    """
    if not IS_WINDOWS or not hwnd or _user32 is None:
        return False
    try:
        return bool(_user32.SetWindowTextW(wintypes.HWND(hwnd), str(text)))
    except Exception:  # noqa: BLE001
        return False


def set_layered_alpha(hwnd: int, opacity: float) -> bool:
    """Uniform window opacity via `WS_EX_LAYERED` + `LWA_ALPHA`.

    This is what `QWidget.setWindowOpacity()` does internally. Kept as an explicit
    implementation because it is the one place where the Windows call is documented to
    have a visible side effect (loss of ClearType), and because a caller may want to
    pin a value Qt's animation is not currently driving.

    `opacity` is 0.0-1.0; clamped, never raises. Returns False off Windows.
    """
    if not IS_WINDOWS or not hwnd or _user32 is None:
        return False
    try:
        alpha = int(round(max(0.0, min(1.0, float(opacity))) * 255))
        ex = _get_long(wintypes.HWND(hwnd), GWL_EXSTYLE)
        if not ex & WS_EX_LAYERED:
            _set_long(wintypes.HWND(hwnd), GWL_EXSTYLE, ex | WS_EX_LAYERED)
        return bool(
            _user32.SetLayeredWindowAttributes(wintypes.HWND(hwnd), 0, alpha, LWA_ALPHA)
        )
    except Exception:  # noqa: BLE001
        return False


def clear_layered(hwnd: int, opacity: float = 1.0) -> bool:
    """Return the window to opaque and drop `WS_EX_LAYERED`.

    Worth doing rather than leaving a 100%-alpha layered window in place: an opaque
    layered window still pays the software-compositing cost and still loses ClearType.
    """
    if not IS_WINDOWS or not hwnd or _user32 is None:
        return False
    try:
        ex = _get_long(wintypes.HWND(hwnd), GWL_EXSTYLE)
        if ex & WS_EX_LAYERED:
            _user32.SetLayeredWindowAttributes(wintypes.HWND(hwnd), 0, 255, LWA_ALPHA)
            _set_long(wintypes.HWND(hwnd), GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
        return True
    except Exception:  # noqa: BLE001
        return False


def set_tool_window(hwnd: int, enabled: bool = True) -> bool:
    """Add or remove `WS_EX_TOOLWINDOW`, i.e. hide/show the taskbar button.

    `Qt.Tool` is the portable way to get this and is what the app uses. Two Windows
    details make the raw form worth having:

    * The style change only takes effect for the *shell* after the frame is refreshed,
      hence `SWP_FRAMECHANGED` with the quiet flags (no move, resize, z-order or
      activation side effects).
    * `WS_EX_APPWINDOW` must be cleared at the same time, because it wins over
      `WS_EX_TOOLWINDOW`: setting only the tool bit can leave the taskbar button on
      screen and look like the call did nothing.
    """
    if not IS_WINDOWS or not hwnd or _user32 is None:
        return False
    try:
        ex = _get_long(wintypes.HWND(hwnd), GWL_EXSTYLE)
        if enabled:
            ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        else:
            ex = (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        _set_long(wintypes.HWND(hwnd), GWL_EXSTYLE, ex)
        _user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, _SWP_QUIET | SWP_FRAMECHANGED)
        return True
    except Exception:  # noqa: BLE001
        return False


def is_window_visible(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd or _user32 is None:
        return False
    try:
        return bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def device_pixel_ratio(hwnd: int, fallback: float = 1.0) -> float:
    """The window's DPI scale (1.0 at 96 dpi, 1.5 at 150%)."""
    if not IS_WINDOWS or not hwnd or _get_dpi is None:
        return fallback
    try:
        dpi = int(_get_dpi(wintypes.HWND(hwnd)))
        return (dpi / 96.0) if dpi > 0 else fallback
    except Exception:  # noqa: BLE001
        return fallback


def monitor_work_area(hwnd: int) -> tuple[int, int, int, int] | None:
    """The nearest monitor's work area (excludes the taskbar) in **physical** pixels.

    Physical, not logical: see the DPI trap in the module docstring. Use
    `logical_work_area()` if the result will be compared with Qt geometry.
    """
    if not IS_WINDOWS or not hwnd or _user32 is None:
        return None
    try:
        monitor = _user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return (int(work.left), int(work.top), int(work.right - work.left), int(work.bottom - work.top))
    except Exception:  # noqa: BLE001
        return None


def logical_work_area(hwnd: int, fallback_dpr: float = 1.0) -> tuple[int, int, int, int] | None:
    """`monitor_work_area()` converted into the coordinate space Qt uses."""
    rect = monitor_work_area(hwnd)
    if rect is None:
        return None
    dpr = device_pixel_ratio(hwnd, fallback_dpr) or 1.0
    if dpr == 1.0:
        return rect
    x, y, w, h = rect
    return (int(round(x / dpr)), int(round(y / dpr)), int(round(w / dpr)), int(round(h / dpr)))


def describe() -> str:
    """One line for `--check`, so a bug report shows whether this layer loaded."""
    if not IS_WINDOWS:
        return f"非 Windows（{sys.platform}），Win32 层整体停用"
    have = []
    if _get_dpi is not None:
        have.append("GetDpiForWindow")
    if hasattr(_user32, "SetWindowLongPtrW"):
        have.append("SetWindowLongPtrW")
    return "user32 已加载" + (f"（{', '.join(have)}）" if have else "")
