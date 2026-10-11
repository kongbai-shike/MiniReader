# 07: 关闭新标签还原 + SendInput 真实滚轮测试
import json, time, ctypes
import numpy as np
import win32gui, win32ui, win32con
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

rep = {}
hwnd = find_mini()
rect = win32gui.GetWindowRect(hwnd)

# 1) 关闭新页签 (× 在 B 约 163, 78)
lParam = ((78 & 0xFFFF) << 16) | (163 & 0xFFFF)
win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
time.sleep(0.05)
win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)
time.sleep(1.2)
after_close = np.array(printwindow(hwnd))
Image.fromarray(after_close).save(BASE + r"\07_after_close_tab.png")
# 验证是否回到阅读态: 正文区是否有文本带
g = after_close.mean(axis=2)
rows = (g[110:545, 30:400] > 100).sum(axis=1)
rep["back_to_reading"] = bool((rows >= 3).sum() > 20)

# 2) SendInput 真实滚轮
user32 = ctypes.windll.user32
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
orig = POINT(); user32.GetCursorPos(ctypes.byref(orig))

cx, cy = rect[0] + 200, rect[1] + 300
user32.SetCursorPos(cx, cy)
time.sleep(0.3)
before = np.array(printwindow(hwnd))

MOUSEEVENTF_WHEEL = 0x0800
for i in range(3):
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -120, 0)
    time.sleep(0.2)
time.sleep(1.0)
after = np.array(printwindow(hwnd))
Image.fromarray(after).save(BASE + r"\07_after_sendinput_wheel.png")

d_body = float(np.abs(after[110:545, 30:400].astype(int) - before[110:545, 30:400].astype(int)).mean())
d_prog = float(np.abs(after[560:596, 190:245].astype(int) - before[560:596, 190:245].astype(int)).mean())
rep["sendinput_wheel3"] = {"body_diff": round(d_body, 2), "progress_diff": round(d_prog, 2)}

# 垂直位移互相关 (正文行轮廓)
pb = before[110:545, 100:300].mean(axis=1)
pa = after[110:545, 100:300].mean(axis=1)
best_k, best_c = 0, -2
for k in range(-150, 151):
    a = np.roll(pb, k)[20:-20]; b = pa[20:-20]
    if a.std() < 1e-6 or b.std() < 1e-6: continue
    c = float(np.corrcoef(a, b)[0, 1])
    if c > best_c: best_c, best_k = c, k
rep["wheel_shift_px"] = best_k
rep["wheel_shift_corr"] = round(best_c, 3)

# 还原: 向上滚 3 格
for i in range(3):
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, 120, 0)
    time.sleep(0.2)
time.sleep(0.8)
user32.SetCursorPos(orig.x, orig.y)

with open(BASE + r"\07_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
