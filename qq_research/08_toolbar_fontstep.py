# 08: 补采 - 悬停热区边界 / 工具栏动画 / 字号步进
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

user32 = ctypes.windll.user32
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

rep = {}
hwnd = find_mini()
if not hwnd: print("NOT FOUND"); raise SystemExit
rect = win32gui.GetWindowRect(hwnd)
rep["rect"] = rect

def toolbar_brightness(img):
    """工具栏行带亮度 (B: y 96..130, x 40..390)"""
    a = np.array(img).mean(axis=2)
    return float(a[96:130, 40:390].mean())

def glyph_stats(img):
    a = np.array(img).mean(axis=2)
    region = a[130:545, 30:400]
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

orig = POINT(); user32.GetCursorPos(ctypes.byref(orig))
rep["cursor_orig"] = [orig.x, orig.y]

# 0) 基线
base_img = printwindow(hwnd)
base_bright = toolbar_brightness(base_img)
rep["baseline_toolbar_brightness"] = round(base_bright, 2)
rep["baseline_glyph"] = glyph_stats(base_img)

# 1) 热区边界: 扫 y = 70..150 step 5, x=200
hot = []
for by in range(70, 151, 5):
    user32.SetCursorPos(rect[0] + 200, rect[1] + 300)  # 先移出热区复位
    time.sleep(0.35)
    user32.SetCursorPos(rect[0] + 200, rect[1] + by)
    time.sleep(0.7)
    img = printwindow(hwnd)
    b = toolbar_brightness(img)
    hot.append({"y": by, "brightness": round(b, 2), "open": b > base_bright + 1.5})
rep["hotzone_scan"] = hot

# 确认工具栏最终开启（热区中部）
user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
time.sleep(0.8)
tb_img = printwindow(hwnd)
tb_img.save(BASE + r"\08_toolbar_open.png")
tb_arr = np.array(tb_img).mean(axis=2)

# 2) 动画: 移出后重新进入, 按时间片采样亮度
user32.SetCursorPos(rect[0] + 200, rect[1] + 400)
time.sleep(1.0)
anim = []
t0 = time.time()
user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
for dt in [0.05, 0.1, 0.15, 0.2, 0.3, 0.45, 0.6, 0.9]:
    wait = t0 + dt - time.time()
    if wait > 0: time.sleep(wait)
    b = toolbar_brightness(printwindow(hwnd))
    anim.append({"t_ms": int((time.time()-t0)*1000), "brightness": round(b, 2)})
rep["anim_in"] = anim
# 移出动画
t0 = time.time()
user32.SetCursorPos(rect[0] + 200, rect[1] + 400)
anim_out = []
for dt in [0.05, 0.1, 0.15, 0.2, 0.3, 0.45, 0.6, 0.9]:
    wait = t0 + dt - time.time()
    if wait > 0: time.sleep(wait)
    b = toolbar_brightness(printwindow(hwnd))
    anim_out.append({"t_ms": int((time.time()-t0)*1000), "brightness": round(b, 2)})
rep["anim_out"] = anim_out

# 3) 字号步进: 重新唤出工具栏, 定位 A− / A＋, 各点一次
user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
time.sleep(0.8)
tb2 = printwindow(hwnd)
g = np.array(tb2).mean(axis=2)
band = g[96:130, 18:414]
cols = (band > 50).sum(axis=0)
segs = []; in_s = False
for i, v in enumerate(cols):
    if v >= 2 and not in_s: s = i; in_s = True
    elif v < 2 and in_s:
        if i - s >= 4: segs.append((s + 18, i + 18))
        in_s = False
if in_s: segs.append((s + 18, 432))
rep["toolbar_icon_segs"] = segs
# 预期 7 段: ‹ 已加书架 ≡ 🌙 Tt A− A＋ → A− 是第 6 段, A＋ 第 7 段
if len(segs) >= 7:
    aminus_x = (segs[5][0] + segs[5][1]) // 2
    aplus_x = (segs[6][0] + segs[6][1]) // 2
    by = 113  # 工具栏行中部
    # 点 A−
    user32.SetCursorPos(rect[0] + aminus_x, rect[1] + by)
    time.sleep(0.3)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(1.0)
    # 移到正文测字形 (移出热区工具栏会收起, 不影响正文测量)
    user32.SetCursorPos(rect[0] + 200, rect[1] + 400)
    time.sleep(0.8)
    img_minus = printwindow(hwnd)
    img_minus.save(BASE + r"\08_after_aminus.png")
    rep["glyph_after_aminus"] = glyph_stats(img_minus)
    # 点 A＋ 还原
    user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
    time.sleep(0.8)
    user32.SetCursorPos(rect[0] + aplus_x, rect[1] + by)
    time.sleep(0.3)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(1.0)
    user32.SetCursorPos(rect[0] + 200, rect[1] + 400)
    time.sleep(0.8)
    img_plus = printwindow(hwnd)
    rep["glyph_after_aplus_restore"] = glyph_stats(img_plus)
else:
    rep["font_step"] = "segs<7, 未点击"

user32.SetCursorPos(orig.x, orig.y)
with open(BASE + r"\08_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
