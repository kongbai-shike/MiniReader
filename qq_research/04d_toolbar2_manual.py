# 04d: 手动定位第二行工具栏并分析
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
H, W = img.shape[:2]
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)

rep = {}

# 第二行工具栏目测在 y 96..130 左右 (标签栏下方)
# 先扫描 y 80..140 找文本行带
for y_start in [80, 90, 96, 100]:
    region = gray[y_start:140, 18:414]
    bright = (region > 50).sum(axis=1)
    bands = []
    in_b = False
    for i, v in enumerate(bright):
        if v >= 3 and not in_b: s = i; in_b = True
        elif v < 3 and in_b:
            if i - s >= 3: bands.append([s + y_start, i + y_start])
            in_b = False
    rep[f"toolbar_scan_y{y_start}"] = bands

# 导出 y 90..140 区域特写
crop = Image.fromarray(img[90:140, 18:414])
crop = crop.resize(((414-18)*4, (140-90)*4), Image.NEAREST)
crop.save(BASE + r"\04_crop_toolbar2_4x.png")

# 也尝试从 PrintWindow 图提取第二行 (如果有的话)
pw = np.array(Image.open(BASE + r"\04_reading_printwindow.png").convert("RGB"))
pwg = pw.mean(axis=2)
for y_start in [80, 90, 96, 100]:
    region = pwg[y_start:140, 18:414]
    bright = (region > 50).sum(axis=1)
    bands = []
    in_b = False
    for i, v in enumerate(bright):
        if v >= 3 and not in_b: s = i; in_b = True
        elif v < 3 and in_b:
            if i - s >= 3: bands.append([s + y_start, i + y_start])
            in_b = False
    rep[f"pw_toolbar_scan_y{y_start}"] = bands

# 底部按钮从 PrintWindow 精确分析
bottom = pwg[550:608, 18:414]
# 找按钮边框 (中亮)
edge = (bottom > 50) & (bottom < 160)
# 通过列扫描找按钮区域
colsum = edge.sum(axis=0)
# 找连续高值段
segs = []
in_s = False
for i, v in enumerate(colsum):
    if v >= 2 and not in_s: s = i; in_s = True
    elif v < 2 and in_s:
        if i - s >= 4: segs.append([s + 18, i + 18])
        in_s = False
rep["bottom_edge_segments"] = segs

# 按钮内部文字 (更亮)
txt = (bottom > 100).sum(axis=0)
segs_txt = []
in_s = False
for i, v in enumerate(txt):
    if v >= 2 and not in_s: s = i; in_s = True
    elif v < 2 and in_s:
        if i - s >= 4: segs_txt.append([s + 18, i + 18])
        in_s = False
rep["bottom_text_segments"] = segs_txt

with open(BASE + r"\04d_toolbar2_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
