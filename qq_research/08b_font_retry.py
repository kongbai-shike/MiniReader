# 08b: 字号步进重试 - 基于目测位置直接点击 A−/A＋
import json, time, ctypes
import numpy as np
import win32gui, win32con
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
def find_mini():
    hits = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "迷你窗":
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
    return img

def glyph_stats(img):
    a = np.array(img).mean(axis=2)
    region = a[110:545, 30:400]
    row_bright = (region > 100).sum(axis=1)
    bands = []; in_b = False
    for i, v in enumerate(row_bright):
        if v >= 3 and not in_b: s = i; in_b = True
        elif v < 3 and in_b:
            if i - s >= 4: bands.append((s, i))
            in_b = False
    if len(bands) > 1:
        hs = [b1-b0 for b0, b1 in bands]
        pitch = [bands[i+1][0]-bands[i][0] for i in range(len(bands)-1)]
        return {"lines": len(bands), "glyph_h": round(sum(hs)/len(hs),1), "pitch": round(sum(pitch)/len(pitch),1)}
    return {"lines": len(bands)}

import win32ui

user32 = ctypes.windll.user32
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

hwnd = find_mini()
rect = win32gui.GetWindowRect(hwnd)
orig = POINT(); user32.GetCursorPos(ctypes.byref(orig))
rep = {}

# 基线
base = printwindow(hwnd)
rep["baseline"] = glyph_stats(base)

# 先移入热区等工具栏出现 (y 100 在窗口内)
user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
time.sleep(1.5)

# 点击 A− (目测 B x≈300, y≈113)
ax, ay = 300, 113
user32.SetCursorPos(rect[0] + ax, rect[1] + ay)
time.sleep(0.3)
user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.05)
user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(1.2)

# 移出到正文区测量
user32.SetCursorPos(rect[0] + 200, rect[1] + 400)
time.sleep(1.0)
img_minus = printwindow(hwnd)
img_minus.save(BASE + r"\08b_after_aminus.png")
rep["after_aminus"] = glyph_stats(img_minus)

# 再移回热区点 A＋ (目测 B x≈340, y≈113)
user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
time.sleep(1.5)
user32.SetCursorPos(rect[0] + 340, rect[1] + ay)
time.sleep(0.3)
user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.05)
user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(1.2)

user32.SetCursorPos(rect[0] + 200, rect[1] + 400)
time.sleep(1.0)
img_plus = printwindow(hwnd)
rep["after_aplus"] = glyph_stats(img_plus)

user32.SetCursorPos(orig.x, orig.y)
with open(BASE + r"\08b_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
