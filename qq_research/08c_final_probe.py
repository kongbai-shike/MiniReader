# 08c: 解析 Tt 面板截图(图标位/面板配色) + 热区重扫 + 关闭行为 + A−/A＋ 精确步进
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

def hexc(a): return "#%02x%02x%02x" % tuple(int(c) for c in a)
def glyph_stats(img):
    a = np.array(img).mean(axis=2)
    region = a[150:545, 30:400]
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

rep = {}

# ---- A. 解析 08b 截图: 工具栏图标位 + Tt 面板配色 ----
img = np.array(Image.open(BASE + r"\08b_after_aminus.png").convert("RGB"))
g = img.mean(axis=2)
# 工具栏行带: 扫 y 95..140 找图标行 (亮像素最多的带)
rowbright = [(y, int((g[y, 30:410] > 60).sum())) for y in range(95, 140)]
band_y = max(range(95, 136, 1), key=lambda y: (g[y:y+10, 30:410] > 60).sum())
rep["toolbar_band_y"] = band_y
band = g[band_y:band_y+22, 18:414]
cols = (band > 55).sum(axis=0)
segs = []; in_s = False
for i, v in enumerate(cols):
    if v >= 2 and not in_s: s = i; in_s = True
    elif v < 2 and in_s:
        if i - s >= 3: segs.append((s+18, i+18))
        in_s = False
if in_s: segs.append((s+18, 432))
# 合并相邻(<10px)段
merged = []
for s in segs:
    if merged and s[0] - merged[-1][1] < 10: merged[-1] = (merged[-1][0], s[1])
    else: merged.append(s)
rep["toolbar_icons"] = merged
# 工具栏行底色
bg = img[band_y:band_y+22, 30:410]
bgg = g[band_y:band_y+22, 30:410]
dark = bg[bgg < 40]
rep["toolbar_bg"] = hexc(np.median(dark, axis=0)) if len(dark) else None
# Tt 面板配色: 分组标题"字体"色、选中按钮(系统字体/适中有底色块)、未选按钮
# 面板背景区
panel = img[160:560, 30:400]; pg = g[160:560, 30:400]
rep["panel_bg"] = hexc(np.median(panel[pg < 30], axis=0)) if (pg < 30).any() else None
# 按钮块 (中亮 #2d 系)
btn = panel[(pg > 35) & (pg < 70)]
rep["panel_btn_bg"] = hexc(np.median(btn, axis=0)) if len(btn) else None
# 文字
t1 = panel[pg > 120]
rep["panel_text_bright"] = hexc(np.median(t1, axis=0)) if len(t1) else None
t2 = panel[(pg > 70) & (pg <= 120)]
rep["panel_text_dim"] = hexc(np.median(t2, axis=0)) if len(t2) else None

# ---- B. 热区重扫 y 40..150 (低亮度=工具栏开) ----
user32 = ctypes.windll.user32
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
orig = POINT(); user32.GetCursorPos(ctypes.byref(orig))
hwnd = find_mini()
rect = win32gui.GetWindowRect(hwnd)

# 先点 Tt 关闭面板 (从截图测得 Tt 位置)
tt_x = None
if len(merged) >= 5:
    tt_x = (merged[4][0] + merged[4][1]) // 2
aminus_x = (merged[5][0] + merged[5][1]) // 2 if len(merged) >= 6 else None
aplus_x = (merged[6][0] + merged[6][1]) // 2 if len(merged) >= 7 else None
rep["click_x"] = {"Tt": tt_x, "A-": aminus_x, "A+": aplus_x}

def band_bright(img):
    a = np.array(img).mean(axis=2)
    return float(a[band_y:band_y+20, 40:400].mean())

if tt_x:
    user32.SetCursorPos(rect[0] + 200, rect[1] + 100)
    time.sleep(1.2)
    user32.SetCursorPos(rect[0] + tt_x, rect[1] + band_y + 11)
    time.sleep(0.3)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(1.0)

# 热区扫描 (面板已关)
user32.SetCursorPos(rect[0] + 200, rect[1] + 450)
time.sleep(1.5)
closed_ref = band_bright(printwindow(hwnd))
rep["closed_band_brightness"] = round(closed_ref, 2)
hot = []
for by in list(range(40, 155, 5)):
    user32.SetCursorPos(rect[0] + 200, rect[1] + 450)
    time.sleep(0.6)
    user32.SetCursorPos(rect[0] + 200, rect[1] + by)
    time.sleep(0.7)
    b = band_bright(printwindow(hwnd))
    hot.append({"y": by, "b": round(b, 2), "open": b < closed_ref - 1.5})
rep["hotzone_scan2"] = hot

# ---- C. 关闭行为: 开 -> 移到 y450 -> 计时采样 ----
user32.SetCursorPos(rect[0] + 200, rect[1] + 90)
time.sleep(1.0)
t0 = time.time()
user32.SetCursorPos(rect[0] + 200, rect[1] + 450)
close_seq = []
for dt in [0.2, 0.5, 1.0, 1.5, 2.0, 3.0]:
    wait = t0 + dt - time.time()
    if wait > 0: time.sleep(wait)
    close_seq.append({"t_ms": int((time.time()-t0)*1000), "b": round(band_bright(printwindow(hwnd)), 2)})
rep["close_seq"] = close_seq

# ---- D. A−/A＋ 步进 ----
if aminus_x and aplus_x:
    base = printwindow(hwnd)
    rep["glyph_baseline"] = glyph_stats(base)
    # 唤出工具栏
    user32.SetCursorPos(rect[0] + 200, rect[1] + 90)
    time.sleep(1.0)
    user32.SetCursorPos(rect[0] + aminus_x, rect[1] + band_y + 11)
    time.sleep(0.3)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(1.2)
    user32.SetCursorPos(rect[0] + 200, rect[1] + 450)
    time.sleep(1.0)
    im = printwindow(hwnd); im.save(BASE + r"\08c_after_aminus.png")
    rep["glyph_after_aminus"] = glyph_stats(im)
    # A＋ 还原
    user32.SetCursorPos(rect[0] + 200, rect[1] + 90)
    time.sleep(1.0)
    user32.SetCursorPos(rect[0] + aplus_x, rect[1] + band_y + 11)
    time.sleep(0.3)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(1.2)
    user32.SetCursorPos(rect[0] + 200, rect[1] + 450)
    time.sleep(1.0)
    im2 = printwindow(hwnd)
    rep["glyph_after_restore"] = glyph_stats(im2)

user32.SetCursorPos(orig.x, orig.y)
with open(BASE + r"\08c_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
