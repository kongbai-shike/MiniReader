# -*- coding: utf-8 -*-
"""锁定窗口截图：先 PrintWindow(PW_RENDERFULLCONTENT)，黑屏则降级屏幕 BitBlt 实拍。"""
import ctypes
from ctypes import wintypes

import win32gui
import win32ui
import win32con
from PIL import Image

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

HWND = 68382  # 0x10b1e 「迷你窗」
OUT = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def try_printwindow(hwnd, rect):
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    # PW_RENDERFULLCONTENT = 3
    ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)
    bi = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (bi["bmWidth"], bi["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    # 清理
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bmp.GetHandle())
    return ok, img


def black_ratio(img):
    g = img.convert("L")
    hist = g.histogram()
    total = sum(hist)
    dark = sum(hist[:16])  # 亮度<16 的像素占比
    return dark / total


def screen_grab(rect):
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    hdc_screen = win32gui.GetDC(0)
    mfc = win32ui.CreateDCFromHandle(hdc_screen)
    save_dc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    save_dc.SelectObject(bmp)
    save_dc.BitBlt((0, 0), (w, h), mfc, (left, top), win32con.SRCCOPY)
    bi = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (bi["bmWidth"], bi["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    save_dc.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(0, hdc_screen)
    win32gui.DeleteObject(bmp.GetHandle())
    return img


def main():
    hwnd = HWND
    assert win32gui.IsWindow(hwnd), "hwnd 失效"
    print("title:", win32gui.GetWindowText(hwnd))
    rect = win32gui.GetWindowRect(hwnd)
    print("rect:", rect, "size:", rect[2]-rect[0], rect[3]-rect[1])
    print("foreground hwnd:", win32gui.GetForegroundWindow())

    # 1) PrintWindow
    ok, img_pw = try_printwindow(hwnd, rect)
    br = black_ratio(img_pw)
    p_pw = OUT + r"\confirm_printwindow.png"
    img_pw.save(p_pw)
    print(f"PrintWindow ok={ok} dark_ratio={br:.3f} -> {p_pw}")

    # 2) 屏幕实拍（无论 PrintWindow 是否黑屏都存一份，方便对比）
    img_sc = screen_grab(rect)
    br2 = black_ratio(img_sc)
    p_sc = OUT + r"\confirm_screengrab.png"
    img_sc.save(p_sc)
    print(f"ScreenGrab dark_ratio={br2:.3f} -> {p_sc}")

    # 3) 选最终确认图
    chosen = p_pw if (ok and br < 0.5) else p_sc
    final = OUT + r"\00_confirm.png"
    Image.open(chosen).save(final)
    print("FINAL CONFIRM SHOT:", final, "(source:", "PrintWindow" if chosen == p_pw else "ScreenGrab", ")")


if __name__ == "__main__":
    main()
