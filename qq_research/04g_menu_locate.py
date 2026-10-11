# 04g: 菜单全局定位 - 找 #2b2b2b 连通块
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
H, W = gray.shape
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)
rep = {}

# 菜单 bg ~43 (#2b), body bg ~17 (#11). 掩码: 33..55
mask = (gray > 33) & (gray < 56)
# 只保留正文区以下 y>250, 且列方向连续
sub = mask[250:600, :]
colsum = sub.sum(axis=0)
xs = np.where(colsum > 50)[0]
rep["menu_cols"] = [int(xs.min()), int(xs.max())] if len(xs) else None
if len(xs):
    x0, x1 = int(xs.min()), int(xs.max())
    rowsum = mask[250:600, x0:x1+1].sum(axis=1)
    ys = np.where(rowsum > (x1-x0)*0.5)[0]
    rep["menu_rows"] = [int(ys.min())+250, int(ys.max())+250] if len(ys) else None
    if len(ys):
        y0, y1 = int(ys.min())+250, int(ys.max())+250
        roi = img[y0:y1+1, x0:x1+1]; g = gray[y0:y1+1, x0:x1+1]
        rep["menu_bbox"] = [x0, y0, x1, y1]
        rep["menu_size"] = [x1-x0+1, y1-y0+1]
        bg = roi[(g>33)&(g<56)]
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
        # 分隔线
        for yy in range(y0, y1+1):
            r = gray[yy, x0+5:x1-5]
            if len(r) and ((r>45)&(r<95)).mean() > 0.6:
                rep.setdefault("divider_y", []).append(yy)
        # 圆角: 顶边渐变
        rep["top_edge"] = [[y, round(float(gray[y, (x0+x1)//2]),1)] for y in range(y0-3, y0+3)]
        rep["left_edge"] = [[x, round(float(gray[(y0+y1)//2, x]),1)] for x in range(x0-3, x0+3)]
        c = Image.fromarray(roi).resize(((x1-x0+1)*4, (y1-y0+1)*4), Image.NEAREST)
        c.save(BASE + r"\04_crop_menu_real_4x.png")

with open(BASE + r"\04g_menu_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
