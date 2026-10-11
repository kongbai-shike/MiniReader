# -*- coding: utf-8 -*-
"""03-1: UIA 控件树完整 dump -> 03_uia_tree.json
坐标基准：屏幕物理坐标（进程 PerMonitorV2 DPI 感知）。
"""
import ctypes
import json
import sys
import time

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import win32gui
from pywinauto import Application

HWND = 68382
OUT = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"


def find_mini():
    hwnd = HWND
    if win32gui.IsWindow(hwnd) and win32gui.GetWindowText(hwnd) == "迷你窗":
        return hwnd
    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "迷你窗":
            hits.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    if not hits:
        raise RuntimeError("找不到「迷你窗」，请确认窗口已打开")
    return hits[0]


def dump_element(el, depth, max_depth=40):
    info = el.element_info
    try:
        r = info.rectangle
        rect = [r.left, r.top, r.right, r.bottom]
        size = [r.right - r.left, r.bottom - r.top]
    except Exception:
        rect, size = None, None
    node = {
        "depth": depth,
        "control_type": getattr(info, "control_type", None),
        "name": getattr(info, "name", None),
        "auto_id": getattr(info, "automation_id", None),
        "class_name": getattr(info, "class_name", None),
        "rect": rect,
        "size": size,
        "enabled": getattr(info, "enabled", None),
        "offscreen": None,
    }
    try:
        node["offscreen"] = el.is_offscreen
    except Exception:
        pass
    if depth < max_depth:
        try:
            children = el.children()
        except Exception:
            children = []
        node["children"] = [dump_element(c, depth + 1, max_depth) for c in children]
    else:
        node["children"] = []
    return node


def main():
    hwnd = find_mini()
    print("hwnd:", hex(hwnd), win32gui.GetWindowRect(hwnd))
    app = Application(backend="uia").connect(handle=hwnd, timeout=10)
    win = app.window(handle=hwnd)
    t0 = time.time()
    tree = dump_element(win.wrapper_object(), 0)
    tree["_hwnd"] = hex(hwnd)
    tree["_window_rect"] = win32gui.GetWindowRect(hwnd)
    tree["_dump_seconds"] = round(time.time() - t0, 2)

    path = OUT + r"\03_uia_tree.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=1)

    # 统计 & 打印所有"可能是按钮"的节点摘要
    flat = []

    def walk(n):
        flat.append(n)
        for c in n.get("children", []):
            walk(c)

    walk(tree)
    print("total nodes:", len(flat), "dump time:", tree["_dump_seconds"], "s")
    print("saved:", path)
    print("\n== controls with type Button / named / interactive ==")
    for n in flat:
        ct = (n.get("control_type") or "")
        nm = (n.get("name") or "")
        if ct in ("Button", "MenuItem", "TabItem", "CheckBox", "RadioButton") or nm:
            print(f'd={n["depth"]:<2} {ct:<12} name={nm!r:<24} aid={n.get("auto_id")!r:<20} '
                  f'rect={n.get("rect")} size={n.get("size")}')


if __name__ == "__main__":
    main()
