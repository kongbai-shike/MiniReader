# 04: 阅读态静态分析 - 基于 04_reading_printwindow.png (B 坐标 431x620, SURF=(18,18,414,602))
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_reading_printwindow.png").convert("RGB"))
H, W = img.shape[:2]
gray = img.mean(axis=2)
rep = {}

def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)

# 1) 正文文字带: 扫描内容区 y 100..540, 亮像素行
content = gray[100:545, 30:400]
row_bright = (content > 100).sum(axis=1)
bands = []
in_b = False
for i, v in enumerate(row_bright):
    if v >= 3 and not in_b: start = i; in_b = True
    elif v < 3 and in_b:
        if i - start >= 4: bands.append((start + 100, i + 100))
        in_b = False
if in_b: bands.append((start + 100, 545))
rep["text_bands"] = bands
if len(bands) > 1:
    heights = [b1 - b0 for b0, b1 in bands]
    gaps = [bands[i+1][0] - bands[i][1] for i in range(len(bands) - 1)]
    rep["glyph_h_avg"] = round(sum(heights) / len(heights), 1)
    rep["line_gap_avg"] = round(sum(gaps) / len(gaps), 1)
    rep["line_pitch_avg"] = round(sum(bands[i+1][0] - bands[i][0] for i in range(len(bands)-1)) / (len(bands)-1), 1)

# 2) 文字颜色: 取亮像素(g>100)的中位色
mask = gray[100:545, 30:400] > 100
pxs = img[100:545, 30:400][mask]
if len(pxs):
    rep["text_color_median"] = hexc(np.median(pxs, axis=0))
    rep["text_color_mean"] = hexc(pxs.mean(axis=0))
    rep["text_px_count"] = int(len(pxs))

# 3) 正文区背景色 (暗像素中位)
mask_bg = gray[110:540, 40:390] < 40
bgpx = img[110:540, 40:390][mask_bg]
rep["body_bg_median"] = hexc(np.median(bgpx, axis=0)) if len(bgpx) else None

# 4) 标签栏: y 60..100 区间亮度结构
tab_region = gray[58:100, 18:414]
rep["tab_strip_max"] = int(tab_region.max())
# 标签底色采样 (标签块内部, 约 x 30..120, y 66..92)
rep["tab_bg"] = hexc(np.median(img[70:90, 35:110].reshape(-1, 3), axis=0))
rep["tab_text_color"] = hexc(np.median(img[66:94, 20:140][gray[66:94, 20:140] > 80], axis=0)) if (gray[66:94, 20:140] > 80).any() else None

# 5) 底栏按钮: y 550..605, 检测圆角矩形描边 (中亮像素)
bottom = gray[550:608, 18:414]
edge_mask = (bottom > 50) & (bottom < 160)
cols = edge_mask.sum(axis=0)
segs = []
in_s = False
for i, v in enumerate(cols):
    if v >= 2 and not in_s: s = i; in_s = True
    elif v < 2 and in_s:
        if i - s >= 6: segs.append((s + 18, i + 18))
        in_s = False
if in_s: segs.append((s + 18, 432))
rep["bottom_segments"] = segs
# 按钮纵向范围
rows = edge_mask.sum(axis=1)
ys = np.where(rows >= 4)[0]
rep["bottom_btn_y"] = [int(ys.min() + 550), int(ys.max() + 550)] if len(ys) else None
# 描边颜色
epx = img[550:608, 18:414][edge_mask]
rep["bottom_border_color"] = hexc(np.median(epx, axis=0)) if len(epx) else None
# 按钮内文字颜色 (亮)
btn_txt = gray[550:608, 18:414] > 120
bpx = img[550:608, 18:414][btn_txt]
rep["bottom_text_color"] = hexc(np.median(bpx, axis=0)) if len(bpx) else None
# 进度文本位置: 底栏中部亮像素列范围
mid = gray[555:600, 180:260] > 80
mcols = np.where(mid.sum(axis=0) >= 1)[0]
rep["progress_x_range"] = [int(mcols.min() + 180), int(mcols.max() + 180)] if len(mcols) else None

# 6) 顶栏在阅读态是否变化: 复用行带检测 y18..60 g>40
top = gray[18:60, 18:414]
tcols = (top > 40).sum(axis=0)
tsegs = []
in_s = False
for i, v in enumerate(tcols):
    if v >= 2 and not in_s: s = i; in_s = True
    elif v < 2 and in_s:
        if i - s >= 6: tsegs.append((s + 18, i + 18))
        in_s = False
if in_s: tsegs.append((s + 18, 432))
rep["topbar_segments"] = tsegs

with open(BASE + r"\04_reading_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
