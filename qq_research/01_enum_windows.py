# -*- coding: utf-8 -*-
"""枚举所有 QQ.exe 顶层窗口，输出几何/样式信息，用于锁定「迷你模式」窗口。
DPI 感知：PerMonitorV2，坐标为物理像素。
"""
import ctypes
import json
import sys

import win32api
import win32con
import win32gui
import win32process

# ---- PerMonitorV2 DPI 感知 ----
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def proc_name(pid):
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return win32process.GetModuleFileNameEx(h, 0).split("\\")[-1]
    except Exception:
        return "?"


def dpi_of(hwnd):
    try:
        return ctypes.windll.user32.GetDpiForWindow(hwnd)
    except Exception:
        return None


WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZE = 0x20000000
WS_VISIBLE = 0x10000000

rows = []


def cb(hwnd, _):
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    pn = proc_name(pid)
    if not (pn or "").lower().startswith("qq"):
        return True
    title = win32gui.GetWindowText(hwnd)
    cls = win32gui.GetClassName(hwnd)
    visible = bool(win32gui.IsWindowVisible(hwnd))
    iconic = bool(win32gui.IsIconic(hwnd))
    wrect = win32gui.GetWindowRect(hwnd)
    try:
        crect = win32gui.GetClientRect(hwnd)
        cpt = win32gui.ClientToScreen(hwnd, (0, 0))
        client_screen = (cpt[0], cpt[1], cpt[0] + crect[2], cpt[1] + crect[3])
    except Exception:
        client_screen = None
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)

    # DWM 圆角属性（Win11: 1 default, 2 round, 3 fullscreen? 实际 DWMWCP_DEFAULT=1, ROUND=2, ROUNDSMALL=3, DONOTROUND=4）
    rounded = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded)
    )  # DWMWA_WINDOW_CORNER_PREFERENCE = 33

    rows.append({
        "hwnd": hex(hwnd),
        "hwnd_int": hwnd,
        "pid": pid,
        "proc": pn,
        "title": title,
        "class": cls,
        "visible": visible,
        "minimized": iconic,
        "window_rect": wrect,
        "window_size": [wrect[2] - wrect[0], wrect[3] - wrect[1]],
        "client_rect_screen": client_screen,
        "client_size": [client_screen[2] - client_screen[0], client_screen[3] - client_screen[1]]
        if client_screen else None,
        "dpi": dpi_of(hwnd),
        "scale": round((dpi_of(hwnd) or 96) / 96.0, 3),
        "ex_style_hex": hex(ex),
        "style_hex": hex(style),
        "WS_EX_TOOLWINDOW": bool(ex & WS_EX_TOOLWINDOW),
        "WS_EX_LAYERED": bool(ex & WS_EX_LAYERED),
        "WS_EX_TOPMOST": bool(ex & WS_EX_TOPMOST),
        "WS_EX_TRANSPARENT": bool(ex & WS_EX_TRANSPARENT),
        "WS_EX_NOREDIRECTIONBITMAP": bool(ex & WS_EX_NOREDIRECTIONBITMAP),
        "WS_POPUP": bool(style & WS_POPUP),
        "WS_CAPTION": bool(style & WS_CAPTION),
        "WS_THICKFRAME": bool(style & WS_THICKFRAME),
        "dwm_corner_pref": rounded.value,
    })
    return True


win32gui.EnumWindows(cb, None)

out = sorted(rows, key=lambda r: (not r["visible"], r["window_size"][0] * r["window_size"][1]), reverse=True)
for r in out:
    print(json.dumps(r, ensure_ascii=False))

with open(r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research\windows_enum.json", "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)

print("\n==== summary ====")
for r in rows:
    print(f'{r["hwnd"]:>10} pid={r["pid"]:<6} vis={int(r["visible"])} min={int(r["minimized"])} '
          f'size={r["window_size"]} topmost={int(r["WS_EX_TOPMOST"])} layered={int(r["WS_EX_LAYERED"])} '
          f'tool={int(r["WS_EX_TOOLWINDOW"])} popup={int(r["WS_POPUP"])} caption={int(r["WS_CAPTION"])} '
          f'cls={r["class"]!r} title={r["title"]!r}')
