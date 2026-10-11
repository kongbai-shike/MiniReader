# 04j: 菜单边界精确定位 - 利用菜单与正文区底色差异
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)
rep = {}

# 菜单 bg ~ #2b2b2b (43), 正文 bg ~ #111111 (17). 找 38..50 的矩形
mask = (gray > 36) & (gray < 52)
# 在 y 200..600, x 150..420 范围内
sub = mask[200:600, 150:420]
# 列扫描: 菜单列的 36-52 像素比例高
col_ratio = sub.mean(axis=0)
menu_cols = np.where(col_ratio > 0.5)[0]
if len(menu_cols):
    rep["menu_x"] = [int(menu_cols.min())+150, int(menu_cols.max())+150]
    x0, x1 = rep["menu_x"]
    row_ratio = mask[200:600, x0:x1+1].mean(axis=1)
    menu_rows = np.where(row_ratio > 0.6)[0]
    if len(menu_rows):
        rep["menu_y"] = [int(menu_rows.min())+200, int(menu_rows.max())+200]
        y0, y1 = rep["menu_y"]
        rep["menu_size"] = [x1-x0+1, y1-y0+1]
        roi = img[y0:y1+1, x0:x1+1]
        g = gray[y0:y1+1, x0:x1+1]
        bg = roi[(g>36)&(g<52)]
        rep["menu_bg"] = hexc(np.median(bg, axis=0))
        tx = roi[g>110]
        rep["menu_text"] = hexc(np.median(tx, axis=0)) if len(tx) else None
        # 项行带
        tb = (g > 110).sum(axis=1)
        bands = []; in_b = False
        for i, v in enumerate(tb):
            if v >= 3 and not in_b: s = i; in_b = True
            elif v < 3 and in_b:
                if i-s >= 3: bands.append([s+y0, i+y0])
                in_b = False
        rep["item_bands"] = bands
        rep["item_pitch"] = [bands[i+1][0]-bands[i][0] for i in range(len(bands)-1)]
        # 分隔线
        for yy in range(y0, y1+1):
            r = gray[yy, x0+8:x1-8]
            if len(r) and ((r>42)&(r<95)).mean() > 0.5:
                rep.setdefault("divider_y", []).append(yy)
        # 圆角过渡
        cx = (x0+x1)//2
        rep["top_edge"] = [[y, round(float(gray[y, cx]),1)] for y in range(y0-4, y0+5)]
        cy = (y0+y1)//2
        rep["left_edge"] = [[x, round(float(gray[cy, x]),1)] for x in range(x0-4, x0+5)]
        rep["bottom_edge"] = [[y, round(float(gray[y, cx]),1)] for y in range(y1-4, y1+5)]
        rep["right_edge"] = [[x, round(float(gray[cy, x]),1)] for x in range(x1-4, x1+5)]
        c = Image.fromarray(roi).resize(((x1-x0+1)*4, (y1-y0+1)*4), Image.NEAREST)
        c.save(BASE + r"\04_crop_menu_real_4x.png")

with open(BASE + r"\04j_menu_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
