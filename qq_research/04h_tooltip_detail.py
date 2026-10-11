# 04h: tooltip 精确测量
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)

for key, fn in [("eye", "04_tooltip_eye_user.png"), ("opacity", "04_tooltip_opacity_user.png"), ("pin", "04_tooltip_pin_user.png")]:
    img = np.array(Image.open(BASE + "\\" + fn).convert("RGB"))
    gray = img.mean(axis=2)
    H, W = gray.shape
    rep = {"img_size": [W, H]}
    # tooltip 气泡: 暗区
    dark = gray < 60
    ys, xs = np.where(dark)
    if len(xs):
        rep["tip_bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        x0, y0, x1, y1 = rep["tip_bbox"]
        roi = img[y0:y1+1, x0:x1+1]; g = gray[y0:y1+1, x0:x1+1]
        rep["tip_size"] = [x1-x0+1, y1-y0+1]
        bg = roi[(g>20)&(g<50)]
        rep["tip_bg"] = hexc(np.median(bg, axis=0)) if len(bg) else None
        tx = roi[g>110]
        rep["tip_text"] = hexc(np.median(tx, axis=0)) if len(tx) else None
        # 圆角: 顶边
        rep["tip_top"] = [[y, round(float(gray[y, (x0+x1)//2]),1)] for y in range(y0-2, y0+3)]
        rep["tip_left"] = [[x, round(float(gray[(y0+y1)//2, x]),1)] for x in range(x0-2, x0+3)]
    # 图标行背景 (上半亮区)
    bright = gray > 80
    ys2, xs2 = np.where(bright)
    if len(xs2):
        rep["icon_row_bbox"] = [int(xs2.min()), int(ys2.min()), int(xs2.max()), int(ys2.max())]
        rep["icon_row_bg"] = hexc(np.median(img[bright], axis=0))
    print(f"--- {key} ---")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
