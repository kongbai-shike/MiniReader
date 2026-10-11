# 04f: 菜单精确测量 (正确 ROI)
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)
rep = {}

# 菜单列 x=230 (菜单中部), 扫描 y
col = gray[:, 230]
segs = []
in_s = False
for i, v in enumerate(col):
    if v > 30 and not in_s: s = i; in_s = True
    elif v <= 30 and in_s:
        if i - s > 10: segs.append([s, i])
        in_s = False
if in_s: segs.append([s, len(col)])
rep["menu_y_segs_x230"] = [s for s in segs if 200 < s[0] < 600]

# 菜单行 y=300, 扫描 x
row = gray[300, :]
segs = []
in_s = False
for i, v in enumerate(row):
    if v > 30 and not in_s: s = i; in_s = True
    elif v <= 30 and in_s:
        if i - s > 10: segs.append([s, i])
        in_s = False
if in_s: segs.append([s, len(row)])
rep["menu_x_segs_y300"] = [s for s in segs if 100 < s[0] < 420]

if rep["menu_y_segs_x230"] and rep["menu_x_segs_y300"]:
    y0, y1 = rep["menu_y_segs_x230"][0]
    x0, x1 = rep["menu_x_segs_y300"][0]
    rep["menu_bbox"] = [x0, y0, x1, y1]
    rep["menu_size"] = [x1 - x0, y1 - y0]
    roi = img[y0:y1, x0:x1]; g = gray[y0:y1, x0:x1]
    bg = roi[(g > 30) & (g < 60)]
    rep["menu_bg"] = hexc(np.median(bg, axis=0)) if len(bg) else None
    tx = roi[g > 120]
    rep["menu_text"] = hexc(np.median(tx, axis=0)) if len(tx) else None
    # 项行带
    tb = (g > 120).sum(axis=1)
    bands = []
    in_b = False
    for i, v in enumerate(tb):
        if v >= 3 and not in_b: s = i; in_b = True
        elif v < 3 and in_b:
            if i - s >= 3: bands.append([s + y0, i + y0])
            in_b = False
    rep["item_bands"] = bands
    if len(bands) > 1:
        rep["item_pitch"] = [bands[i+1][0] - bands[i][0] for i in range(len(bands)-1)]
    # 分隔线: 在菜单内找中等亮度横贯行
    for yy in range(y0, y1):
        r = gray[yy, x0+5:x1-5]
        if len(r) and ((r > 45) & (r < 100)).mean() > 0.6:
            rep.setdefault("divider_y", []).append(yy)
    # 菜单圆角: 顶边左端起点的过渡
    # 顶边第一行>30的 x 范围
    for dy in range(0, 12):
        r = gray[y0+dy, x0-5:x0+30]
        rep.setdefault("corner_top", []).append([dy, [round(float(v),1) for v in r]])
    # 导出特写
    c = Image.fromarray(roi).resize(((x1-x0)*4, (y1-y0)*4), Image.NEAREST)
    c.save(BASE + r"\04_crop_menu_real_4x.png")

with open(BASE + r"\04f_menu_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
