# 04: 阅读态实况采集 - PrintWindow 黑底合成图 + 几何
import json, ctypes, win32gui, win32ui, win32con
from PIL import Image

def find_mini():
    hits = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd): return
        t = win32gui.GetWindowText(hwnd)
        if t == "迷你窗":
            hits.append(hwnd)
    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else None

def printwindow(hwnd):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hwndDC = win32gui.GetWindowDC(hwnd)
    mfcDC = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfcDC, w, h)
    saveDC.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 3)
    info = bmp.GetInfo(); bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle()); saveDC.DeleteDC(); mfcDC.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwndDC)
    return img, (l, t, r, b)

hwnd = find_mini()
if not hwnd:
    print("NOT FOUND"); raise SystemExit
img, rect = printwindow(hwnd)
img.save(r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research\04_reading_printwindow.png")
geo = {"hwnd": hwnd, "rect": rect, "w": rect[2]-rect[0], "h": rect[3]-rect[1]}
with open(r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research\04_reading_geometry.json", "w") as f:
    json.dump(geo, f, indent=2)
print(geo)
