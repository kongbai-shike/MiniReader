# 04i: 菜单聚焦测量 ROI x225..385 y370..520
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)
rep = {}

X0, X1, Y0, Y1 = 225, 390, 370, 530
mask = np.zeros_like(gray, dtype=bool)
mask[Y0:Y1, X0:X1] = (gray[Y0:Y1, X0:X1] > 30) & (gray[Y0:Y1, X0:X1] < 58)
ys, xs = np.where(mask)
if len(xs):
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    rep["menu_bbox"] = [x0, y0, x1, y1]
    rep["menu_size"] = [x1-x0+1, y1-y0+1]
    roi = img[y0:y1+1, x0:x1+1]; g = gray[y0:y1+1, x0:x1+1]
    bg = roi[(g>32)&(g<56)]
    rep["menu_bg"] = hexc(np.median(bg, axis=0)) if len(bg) else None
    tx = roi[g>110]
    rep["menu_text"] = hexc(np.median(tx, axis=0)) if len(tx) else None
    tb = (g > 110).sum(axis=1)
    bands = []; in_b = False
    for i, v in enumerate(tb):
        if v >= 3 and not in_b: s = i; in_b = True
        elif v < 3 and in_b:
            if i-s >= 3: bands.append([s+y0, i+y0])
            in_b = False
    rep["item_bands"] = bands
    if len(bands) > 1:
        rep["item_pitch"] = [bands[i+1][0]-bands[i][0] for i in range(len(bands)-1)]
        rep["item_glyph_h"] = [b[1]-b[0] for b in bands]
    for yy in range(y0, y1+1):
        r = gray[yy, x0+6:x1-6]
        if len(r) and ((r>42)&(r<90)).mean() > 0.55:
            rep.setdefault("divider_y", []).append(yy)
    cx = (x0+x1)//2
    rep["top_edge"] = [[y, round(float(gray[y, cx]),1)] for y in range(y0-3, y0+4)]
    cy = (y0+y1)//2
    rep["left_edge"] = [[x, round(float(gray[cy, x]),1)] for x in range(x0-3, x0+4)]
    rep["bottom_edge"] = [[y, round(float(gray[y, cx]),1)] for y in range(y1-3, y1+4)]
    c = Image.fromarray(roi).resize(((x1-x0+1)*4, (y1-y0+1)*4), Image.NEAREST)
    c.save(BASE + r"\04_crop_menu_real_4x.png")

with open(BASE + r"\04i_menu_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
