# -*- coding: utf-8 -*-
"""03-3: 黑/白背景夹逼
- 在迷你窗后方（它是 TOPMOST）依次铺纯白/纯黑无边框背景窗，屏幕实拍
- 定论：可见表面相对 GetWindowRect 的偏移；圆角覆盖；各区域源色与 alpha
- 不动鼠标键盘
"""
import ctypes
import json
import time
from ctypes import wintypes

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import numpy as np
import win32api
import win32con
import win32gui
import win32ui
from PIL import Image

HWND = 68382
OUT = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"

user32 = ctypes.windll.user32

# ---------- 背景窗 ----------
WHITE_BRUSH = win32gui.CreateSolidBrush(win32api.RGB(255, 255, 255))
BLACK_BRUSH = win32gui.CreateSolidBrush(win32api.RGB(0, 0, 0))
_classes = {}


def register(name, brush):
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = win32gui.DefWindowProc
    wc.lpszClassName = name
    wc.hbrBackground = brush
    wc.hInstance = win32api.GetModuleHandle(None)
    atom = win32gui.RegisterClass(wc)
    _classes[name] = atom


def show_backdrop(color, rect, inflate=60):
    cls = "AqBackdropW" if color == "white" else "AqBackdropB"
    l, t, r, b = rect
    l -= inflate; t -= inflate; r += inflate; b += inflate
    ex = win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
    h = win32gui.CreateWindowEx(ex, cls, None, win32con.WS_POPUP,
                                l, t, r - l, b - t, 0, 0, win32api.GetModuleHandle(None), None)
    win32gui.ShowWindow(h, win32con.SW_SHOWNOACTIVATE)
    win32gui.UpdateWindow(h)
    time.sleep(0.8)
    return h


def grab_screen(rect):
    l, t, r, b = rect
    w, h = r - l, b - t
    hdc = win32gui.GetDC(0)
    mfc = win32ui.CreateDCFromHandle(hdc)
    sdc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    sdc.SelectObject(bmp)
    sdc.BitBlt((0, 0), (w, h), mfc, (l, t), win32con.SRCCOPY)
    bi = bmp.GetInfo()
    img = Image.frombuffer("RGB", (bi["bmWidth"], bi["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    sdc.DeleteDC(); mfc.DeleteDC()
    win32gui.ReleaseDC(0, hdc)
    win32gui.DeleteObject(bmp.GetHandle())
    return img


def printwindow(hwnd, rect):
    l, t, r, b = rect
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    sdc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    sdc.SelectObject(bmp)
    user32.PrintWindow(hwnd, sdc.GetSafeHdc(), 3)
    bi = bmp.GetInfo()
    img = Image.frombuffer("RGB", (bi["bmWidth"], bi["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    sdc.DeleteDC(); mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    win32gui.DeleteObject(bmp.GetHandle())
    return img


def main():
    assert win32gui.IsWindow(HWND) and win32gui.GetWindowText(HWND) == "迷你窗"
    rect = win32gui.GetWindowRect(HWND)
    W, H = rect[2] - rect[0], rect[3] - rect[1]
    register("AqBackdropW", WHITE_BRUSH)
    register("AqBackdropB", BLACK_BRUSH)

    # 保证迷你窗在最前（不激活）
    win32gui.SetWindowPos(HWND, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

    # 黑底实拍
    hb = show_backdrop("black", rect)
    img_bs = grab_screen(rect)
    img_bs.save(OUT + r"\03_alpha_black_screen.png")
    win32gui.DestroyWindow(hb)
    time.sleep(0.4)

    # 白底实拍
    hw = show_backdrop("white", rect)
    img_ws = grab_screen(rect)
    img_ws.save(OUT + r"\03_alpha_white_screen.png")
    win32gui.DestroyWindow(hw)

    # PrintWindow（黑合成）
    img_pw = printwindow(HWND, rect)
    img_pw.save(OUT + r"\03_alpha_printwindow.png")

    B = np.asarray(img_bs).astype(float)   # 压黑实拍
    Wh = np.asarray(img_ws).astype(float)  # 压白实拍
    P = np.asarray(img_pw).astype(float)   # PrintWindow
    gB, gW, gP = B.mean(2), Wh.mean(2), P.mean(2)

    report = {"window_rect": list(rect), "size": [W, H]}

    # ---------- 可见表面边界（压黑实拍：表面比纯黑亮） ----------
    cov = gB > 8
    def edge_left(y):
        idx = np.where(cov[y, :W // 2])[0]
        return int(idx.min()) if len(idx) else None

    def edge_top(x):
        idx = np.where(cov[:H // 2, x])[0]
        return int(idx.min()) if len(idx) else None

    lefts = [v for v in (edge_left(y) for y in range(40, H - 40, 3)) if v is not None]
    tops = [v for v in (edge_top(x) for x in range(40, W - 40, 3)) if v is not None]
    rights = []
    for y in range(40, H - 40, 3):
        idx = np.where(cov[y, W // 2:])[0]
        if len(idx): rights.append(int(idx.max()) + W // 2)
    bots = []
    for x in range(40, W - 40, 3):
        idx = np.where(cov[H // 2:, x])[0]
        if len(idx): bots.append(int(idx.max()) + H // 2)
    surface = {
        "left_med": int(np.median(lefts)), "right_med": int(np.median(rights)),
        "top_med": int(np.median(tops)), "bottom_med": int(np.median(bots)),
    }
    surface["visual_size"] = [surface["right_med"] - surface["left_med"] + 1,
                              surface["bottom_med"] - surface["top_med"] + 1]
    report["surface_edges_screen_black"] = surface

    # ---------- 圆角：白底实拍（透明区=白），左上角覆盖曲线 ----------
    covW = gW < 240  # 被窗体挡住 => 暗于白
    curve = {}
    for y in range(0, 40):
        idx = np.where(covW[y, 0:60])[0]
        curve[y] = int(idx.min()) if len(idx) else None
    # 圆拟合：(x-cx)^2+(y-cy)^2=r^2；上边平台起点≈cx-r，左边平台≈cy-r
    flat_top = curve[min([y for y in range(0, 40) if curve[y] is not None], default=0)]
    # 用多个弧点最小二乘拟合 r，圆心用平台交点估计
    pts = [(curve[y], y) for y in range(6, 30) if curve[y] is not None]
    r_fit = None
    if pts:
        # 取平台边 x=left_med（竖直边），y 平台 = top_med
        cx0, cy0 = surface["left_med"], surface["top_med"]
        rs = [np.hypot(x - cx0, y - cy0) for x, y in pts]
        r_fit = round(float(np.median(rs)), 1)
    report["corner_white"] = {"curve_top_left": curve, "radius_fit": r_fit,
                              "flat_top_first_x": flat_top}

    # ---------- PW 与实拍黑的坐标偏移校验（用虚线顶边 y 比较） ----------
    def dash_row(g, x0=20, x1=None, lo=40, hi=110):
        x1 = x1 or g.shape[1] - 20
        m = (g > lo) & (g < hi)
        counts = m[:, x0:x1].sum(axis=1)
        return int(np.argmax(counts[50:140]) + 50)
    dy_pw = dash_row(gP)
    dy_bs = dash_row(gB)
    dy_ws = dash_row(gW)
    report["dash_top_row"] = {"printwindow": dy_pw, "screen_black": dy_bs, "screen_white": dy_ws,
                              "offset_pw_minus_screen": dy_pw - dy_bs}

    # ---------- alpha 夹逼 ----------
    # Cb = S*a（黑底，实拍）; Cw = S*a + (1-a)*255（白底实拍）
    # => a = 1-(Cw-Cb)/255 ; S = Cb/a
    points = {
        "body_center": (W // 2, 300),
        "topbar_drag": (150, 33),
        "card_fill": (122, 423),
        "frame_dash": None,  # 单独取虚线像素
        "icon_glyph": (218, 36),
        "heading_text": (215, 360),
    }
    alpha_out = {}
    for name, pt in points.items():
        if pt is None:
            continue
        cx, cy = pt
        cb = B[cy - 3:cy + 4, cx - 3:cx + 4].reshape(-1, 3).mean(0)
        cw = Wh[cy - 3:cy + 4, cx - 3:cx + 4].reshape(-1, 3).mean(0)
        a = np.clip(1 - (cw - cb) / 255.0, 0, 1)
        S = np.clip(cb / np.maximum(a, 1e-6), 0, 255)
        alpha_out[name] = {
            "at": [cx, cy],
            "Cb_rgb": [round(v, 1) for v in cb],
            "Cw_rgb": [round(v, 1) for v in cw],
            "alpha_rgb": [round(v, 3) for v in a],
            "alpha_mean": round(float(a.mean()), 3),
            "source_rgb": [int(round(v)) for v in S],
            "source_hex": "#%02x%02x%02x" % tuple(int(round(v)) for v in S),
        }

    # 窗体底色 alpha 用大面积稳健估计（避开图标/文字/卡片/虚线）
    def region_alpha(y0, y1, x0, x1):
        cb = B[y0:y1, x0:x1].reshape(-1, 3)
        cw = Wh[y0:y1, x0:x1].reshape(-1, 3)
        a = 1 - (cw - cb) / 255.0
        med_a = np.median(a, axis=0)
        med_cb = np.median(cb, axis=0)
        S = np.clip(med_cb / np.maximum(med_a, 1e-6), 0, 255)
        return {"alpha_rgb": [round(float(v), 3) for v in med_a],
                "alpha_mean": round(float(med_a.mean()), 3),
                "Cb_hex": "#%02x%02x%02x" % tuple(med_cb.astype(int)),
                "source_hex": "#%02x%02x%02x" % tuple(S.astype(int)),
                "source_rgb": [int(v) for v in S]}

    alpha_out["body_region"] = region_alpha(120, 200, 120, 320)
    alpha_out["topbar_region"] = region_alpha(24, 46, 110, 180)
    report["alpha"] = alpha_out

    # PW vs 实拍黑 一致性（表面内部均值差）
    inside = np.zeros((H, W), bool)
    inside[surface["top_med"] + 30: surface["bottom_med"] - 30,
           surface["left_med"] + 30: surface["right_med"] - 30] = True
    diff = np.abs(P - B)
    report["pw_vs_screen_black_mean_abs_diff_inside"] = round(float(diff[inside].mean()), 2)

    with open(OUT + r"\03_alpha_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(surface, ensure_ascii=False))
    print("corner radius fit:", r_fit, "curve:", {k: v for k, v in curve.items() if v is not None})
    print("dash rows:", report["dash_top_row"])
    print("body_region:", alpha_out["body_region"])
    print("topbar_region:", alpha_out["topbar_region"])
    for k in ("card_fill", "icon_glyph", "heading_text"):
        print(k, alpha_out[k])
    print("pw-vs-screen diff:", report["pw_vs_screen_black_mean_abs_diff_inside"])
    print("saved 03_alpha_report.json")


if __name__ == "__main__":
    main()
