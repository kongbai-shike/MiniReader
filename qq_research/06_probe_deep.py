# 06: 深挖 - 悬停字形变色 / 滚轮3格翻页 / pill 净底色 / 关闭新标签还原
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

rep = {}
hwnd = find_mini()
rect = win32gui.GetWindowRect(hwnd)

# ---------- 1) pill 净底色: 从新标签图 "新页签" pill 采样 ----------
nt = np.array(Image.open(BASE + r"\05_after_plus_click.png").convert("RGB"))
ntg = nt.mean(axis=2)
# 新标签 pill 目测 x 75..160, y 62..94. 找该区内 g 20..50 像素的众数
region = ntg[62:96, 75:165].astype(int)
px = nt[62:96, 75:165]
mask = (region >= 19) & (region <= 50)
vals = region[mask]
if len(vals):
    hist = np.bincount(vals, minlength=256)
    top = hist.argsort()[::-1][:5]
    rep["pill_hist"] = [(int(b), int(hist[b])) for b in top]
    mode_bin = int(top[0])
    sel = px[np.abs(region - mode_bin) <= 1]
    rep["pill_bg_color"] = hexc(np.median(sel, axis=0))
    rep["pill_bg_gray"] = mode_bin
    rep["pill_bg_count"] = int(hist[mode_bin])

# ---------- 2) 悬停: 字形 vs 背景分离 ----------
user32 = ctypes.windll.user32
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
orig = POINT(); user32.GetCursorPos(ctypes.byref(orig))
base = np.array(printwindow(hwnd)).astype(int)
hover = {}
for name, bx in {"eye": 218, "opacity": 261, "pin": 305}.items():
    user32.SetCursorPos(rect[0] + bx, rect[1] + 39)
    time.sleep(0.9)
    h = np.array(printwindow(hwnd)).astype(int)
    x0, x1, y0, y1 = bx-13, bx+13, 26, 52
    b_roi, h_roi = base[y0:y1, x0:x1], h[y0:y1, x0:x1]
    bg_b = b_roi.mean(axis=2); bg_h = h_roi.mean(axis=2)
    glyph_b = (bg_b > 60); glyph_h = (bg_h > 60)
    entry = {}
    # 悬停背景色: 悬停后非字形区的中位色
    nb = h_roi[~glyph_h & (bg_h > 15)]
    if len(nb): entry["hover_bg"] = hexc(np.median(nb, axis=0))
    # 字形色前后
    if glyph_b.any(): entry["glyph_before"] = hexc(np.median(b_roi[glyph_b], axis=0))
    if glyph_h.any(): entry["glyph_after"] = hexc(np.median(h_roi[glyph_h], axis=0))
    # 高亮块范围
    diffmask = np.abs(bg_h - bg_b) > 6
    ys, xs = np.where(diffmask)
    if len(xs):
        entry["hl_bbox_rel"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        entry["hl_size"] = [int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1)]
    hover[name] = entry
    user32.SetCursorPos(rect[0] + 200, rect[1] + 300)
    time.sleep(0.4)
rep["hover2"] = hover
user32.SetCursorPos(orig.x, orig.y)

# ---------- 3) 滚轮 3 格 ----------
def wheel(delta, n=1):
    cx, cy = rect[0] + 200, rect[1] + 300
    wParam = (delta & 0xFFFF) << 16
    lParam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    for _ in range(n):
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wParam, lParam)
        time.sleep(0.15)

before = np.array(printwindow(hwnd))
wheel(-120, 3)
time.sleep(1.0)
after3 = np.array(printwindow(hwnd))
Image.fromarray(after3).save(BASE + r"\06_after_wheel3.png")
d_body = float(np.abs(after3[110:545, 30:400].astype(int) - before[110:545, 30:400].astype(int)).mean())
d_prog = float(np.abs(after3[560:596, 190:245].astype(int) - before[560:596, 190:245].astype(int)).mean())
rep["wheel3_down"] = {"body_diff": round(d_body, 2), "progress_diff": round(d_prog, 2)}
# 垂直位移估计: 行轮廓互相关
pb = before[110:545, 100:300].mean(axis=1)
pa = after3[110:545, 100:300].mean(axis=1)
corr = [float(np.corrcoef(np.roll(pb, k)[20:-20], pa[20:-20])[0,1]) for k in range(-120, 121)]
best_k = int(np.argmax(corr) - 120)
rep["wheel3_shift_px"] = best_k
rep["wheel3_shift_corr"] = round(corr[best_k + 120], 3)

with open(BASE + r"\06_probe2_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
