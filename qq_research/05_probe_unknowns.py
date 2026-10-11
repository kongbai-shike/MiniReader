# 05: 补采 5 个 [未知] - pill 净底色 / 悬停变色 / 滚轮翻页 / ＋多标签
# 注意: 会短暂移动真实鼠标(SetCursorPos)并还原; 滚轮与点击用 PostMessage 不动光标
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
    return img, (l, t, r, b)

rep = {}
hwnd = find_mini()
if not hwnd:
    print("NOT FOUND"); raise SystemExit
rect = win32gui.GetWindowRect(hwnd)
rep["rect"] = rect

# ---------- 1) pill 净底色 (离线分析现有 PrintWindow 图) ----------
pw = np.array(Image.open(BASE + r"\04_reading_printwindow.png").convert("RGB"))
pwg = pw.mean(axis=2)
# pill 区域 y 64..92, x 28..150. 直方图找 18<g<70 的众数
region = pwg[64:92, 28:150].astype(int)
px = pw[64:92, 28:150]
mask = (region >= 19) & (region <= 70)
vals = region[mask]
if len(vals):
    hist = np.bincount(vals, minlength=256)
    top_bins = hist.argsort()[::-1][:6]
    rep["pill_bg_hist_top"] = [(int(b), int(hist[b])) for b in top_bins]
    mode_bin = int(top_bins[0])
    sel = px[np.abs(region - mode_bin) <= 1]
    rep["pill_bg_mode_color"] = "#%02x%02x%02x" % tuple(int(c) for c in np.median(sel, axis=0))
    rep["pill_bg_mode_gray"] = mode_bin

# ---------- 2) 悬停变色: SetCursorPos 真实移动 + PrintWindow diff ----------
user32 = ctypes.windll.user32
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
orig = POINT(); user32.GetCursorPos(ctypes.byref(orig))
rep["cursor_orig"] = [orig.x, orig.y]

icons = {"eye": 218, "opacity": 261, "pin": 305}  # B 坐标 x 中心, y=39
ICON_Y = 39
hover = {}
# 基线
base_img, _ = printwindow(hwnd)
base = np.array(base_img).astype(int)
for name, bx in icons.items():
    sx, sy = rect[0] + bx, rect[1] + ICON_Y
    user32.SetCursorPos(sx, sy)
    time.sleep(0.9)  # 等悬停态生效
    himg, _ = printwindow(hwnd)
    h = np.array(himg).astype(int)
    # diff 在图标 20x20 邻域
    x0, x1 = bx - 12, bx + 12
    y0, y1 = ICON_Y - 12, ICON_Y + 12
    d = np.abs(h[y0:y1, x0:x1] - base[y0:y1, x0:x1]).sum(axis=2)
    changed = d > 30
    entry = {"changed_px": int(changed.sum())}
    if changed.any():
        before = base[y0:y1, x0:x1][changed]
        after = h[y0:y1, x0:x1][changed]
        entry["before_median"] = "#%02x%02x%02x" % tuple(int(c) for c in np.median(before, axis=0))
        entry["after_median"] = "#%02x%02x%02x" % tuple(int(c) for c in np.median(after, axis=0))
    hover[name] = entry
    # 移开
    user32.SetCursorPos(rect[0] + 200, rect[1] + 300)
    time.sleep(0.4)
rep["hover"] = hover
user32.SetCursorPos(orig.x, orig.y)

# ---------- 3) 滚轮翻页: PostMessage WM_MOUSEWHEEL ----------
def wheel(delta):
    cx, cy = rect[0] + 200, rect[1] + 300
    wParam = (delta & 0xFFFF) << 16
    lParam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wParam, lParam)

before_img, _ = printwindow(hwnd)
before = np.array(before_img)
wheel(-120)  # 向下滚一格
time.sleep(0.8)
after_img, _ = printwindow(hwnd)
after = np.array(after_img)
# 比较正文区 (B: y110..545, x30..400) 和进度区 (y560..596, x190..245)
body_diff = float(np.abs(after[110:545, 30:400].astype(int) - before[110:545, 30:400].astype(int)).mean())
prog_diff = float(np.abs(after[560:596, 190:245].astype(int) - before[560:596, 190:245].astype(int)).mean())
rep["wheel_down"] = {"body_mean_absdiff": round(body_diff, 2), "progress_mean_absdiff": round(prog_diff, 2)}
after_img.save(BASE + r"\05_after_wheel_down.png")
# 回滚一格还原
wheel(120)
time.sleep(0.8)
back_img, _ = printwindow(hwnd)
back = np.array(back_img)
body_back = float(np.abs(back[110:545, 30:400].astype(int) - before[110:545, 30:400].astype(int)).mean())
rep["wheel_up_restore_diff"] = round(body_back, 2)

# ---------- 4) ＋ 多标签: PostMessage 左键点击 (B: 198, 78) ----------
plus_sx, plus_sy = rect[0] + 198, rect[1] + 78
lParam = ((78 & 0xFFFF) << 16) | (198 & 0xFFFF)  # 客户区坐标
win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
time.sleep(0.05)
win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)
time.sleep(1.2)
plus_img, _ = printwindow(hwnd)
plus_img.save(BASE + r"\05_after_plus_click.png")
plus_arr = np.array(plus_img)
tab_diff = float(np.abs(plus_arr[56:100, 18:414].astype(int) - before[56:100, 18:414].astype(int)).mean())
rep["plus_click_tabstrip_diff"] = round(tab_diff, 2)

with open(BASE + r"\05_probe_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
