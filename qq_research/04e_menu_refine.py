# 04e: 右键菜单精确测量
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)

rep = {}

# 菜单区域: 目测 x 240..420, y 320..600
# 找菜单边界: 颜色从 body bg (#111) 跳到 menu bg (#2b2b2b)
# 在 x 300 列扫描 y 250..620
col = gray[:, 300]
# 找 > 35 的段
segs = []
in_s = False
for i, v in enumerate(col):
    if v > 35 and not in_s: s = i; in_s = True
    elif v < 35 and in_s:
        if i - s > 10: segs.append([s, i])
        in_s = False
rep["menu_y_segments_at_x300"] = [s for s in segs if s[0] > 200]

# 在 y 400 行扫描 x
row = gray[400, :]
segs = []
in_s = False
for i, v in enumerate(row):
    if v > 35 and not in_s: s = i; in_s = True
    elif v < 35 and in_s:
        if i - s > 10: segs.append([s, i])
        in_s = False
rep["menu_x_segments_at_y400"] = [s for s in segs if s[0] > 150]

# 取菜单主体区域采样
menu_roi = img[320:600, 240:420]
mg = gray[320:600, 240:420]
inner = menu_roi[(mg > 35) & (mg < 55)]
rep["menu_bg"] = hexc(np.median(inner, axis=0)) if len(inner) else None

# 菜单文字
txt = menu_roi[mg > 120]
rep["menu_text_color"] = hexc(np.median(txt, axis=0)) if len(txt) else None

# 菜单项行带
tb = (mg > 120).sum(axis=1)
bands = []
in_b = False
for i, v in enumerate(tb):
    if v >= 3 and not in_b: s = i; in_b = True
    elif v < 3 and in_b:
        if i - s >= 3: bands.append([s + 320, i + 320])
        in_b = False
rep["menu_item_bands"] = bands

# 分隔线
for yy in range(320, 600):
    r = gray[yy, 240:420]
    if ((r > 50) & (r < 110)).mean() > 0.7:
        rep.setdefault("divider_y", []).append(yy)

# 菜单圆角: 边界过渡
# 顶部边缘
for y in range(315, 330):
    r = gray[y, 300]
    rep.setdefault("menu_top_edge", []).append([y, int(r)])

# 左侧边缘
for x in range(235, 250):
    c = gray[400, x]
    rep.setdefault("menu_left_edge", []).append([x, int(c)])

# 菜单宽高
if "menu_y_segments_at_x300" in rep and rep["menu_y_segments_at_x300"]:
    y0, y1 = rep["menu_y_segments_at_x300"][0]
    rep["menu_height"] = y1 - y0
if "menu_x_segments_at_y400" in rep and rep["menu_x_segments_at_y400"]:
    x0, x1 = rep["menu_x_segments_at_y400"][0]
    rep["menu_width"] = x1 - x0

with open(BASE + r"\04e_menu_refine_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
