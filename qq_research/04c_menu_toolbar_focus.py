# 04c: 从用户截图精确提取右键菜单 + 第二行工具栏
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
gray = img.mean(axis=2)
H, W = img.shape[:2]
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)

rep = {}

# 右键菜单: 在正文区找颜色 #2b2b2b 附近的大块区域
# body bg 是 #111111, menu bg 是 #2b2b2b, 差约 22 gray level
menu_mask = (gray > 35) & (gray < 55)
# 连通域分析: 找最大的水平连续块
from scipy import ndimage
labeled, num = ndimage.label(menu_mask)
sizes = ndimage.sum(menu_mask, labeled, range(1, num+1))
if len(sizes):
    largest = int(np.argmax(sizes) + 1)
    ys, xs = np.where(labeled == largest)
    if len(xs):
        rep["menu_bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        x0, y0, x1, y1 = rep["menu_bbox"]
        m_roi = img[y0:y1+1, x0:x1+1]
        mg = gray[y0:y1+1, x0:x1+1]
        rep["menu_bg"] = hexc(np.median(m_roi[(mg>35)&(mg<55)], axis=0))
        rep["menu_text_color"] = hexc(np.median(m_roi[mg>120], axis=0)) if (mg>120).any() else None
        # 菜单项: 文本行带
        tb = (mg > 120).sum(axis=1)
        items = []
        in_b = False
        for i, v in enumerate(tb):
            if v >= 3 and not in_b: s = i; in_b = True
            elif v < 3 and in_b:
                if i - s >= 3: items.append([s, i])
                in_b = False
        rep["menu_item_bands"] = items
        # 分隔线: 行内中等亮度且横贯大部分宽度
        for yy in range(mg.shape[0]):
            row = mg[yy, 4:-4]
            if len(row) and ((row > 50) & (row < 110)).mean() > 0.7:
                rep.setdefault("divider_rel_y", []).append(int(yy))
        # 菜单圆角: 通过 alpha 过渡区推断 (菜单 vs body 边界)
        # 简化: 看边界行/列的亮度渐变
        top_edge = gray[y0-2:y0+3, x0:x1+1].mean(axis=1)
        rep["menu_top_edge_grad"] = [round(float(v),1) for v in top_edge]

# 第二行工具栏: 在 y 50..100 区域找"已加书架"等亮色文本带
tb_region = gray[50:105, 18:414]
tb_bright = (tb_region > 60).sum(axis=1)
items = []
in_b = False
for i, v in enumerate(tb_bright):
    if v >= 3 and not in_b: s = i; in_b = True
    elif v < 3 and in_b:
        if i - s >= 3: items.append([s+50, i+50])
        in_b = False
rep["toolbar2_text_bands"] = items

# 第二行背景色 (在文本带之间的暗区)
if len(items) >= 2:
    y0b, y1b = items[0][0], items[-1][1]
    bg = img[y0b:y1b, 18:414]
    bg_g = gray[y0b:y1b, 18:414]
    rep["toolbar2_bg"] = hexc(np.median(bg[(bg_g>15)&(bg_g<40)], axis=0)) if ((bg_g>15)&(bg_g<40)).any() else None

# 导出菜单特写和工具栏特写
if "menu_bbox" in rep:
    x0, y0, x1, y1 = rep["menu_bbox"]
    m_crop = Image.fromarray(img[y0:y1+1, x0:x1+1])
    m_crop = m_crop.resize(((x1-x0+1)*3, (y1-y0+1)*3), Image.NEAREST)
    m_crop.save(BASE + r"\04_crop_menu_3x.png")

tb_crop = Image.fromarray(img[50:105, 18:414])
tb_crop = tb_crop.resize(((414-18)*3, (105-50)*3), Image.NEAREST)
tb_crop.save(BASE + r"\04_crop_toolbar2_3x.png")

with open(BASE + r"\04c_menu_toolbar_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
