# 04k: 收尾采样 - 第二行工具栏颜色 / 标签 pill / 底栏按钮精确几何
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)
rep = {}

# --- 用户截图(446x607): 第二行工具栏 ---
u = np.array(Image.open(BASE + r"\04_context_menu_user.png").convert("RGB"))
ug = u.mean(axis=2)
# 行带 y 95..130 中的图标/文字颜色
row = ug[95:130, 30:420]
rep["tb2_row_mean"] = round(float(row.mean()), 1)
# 图标颜色 (中亮)
ico = u[95:130, 30:420][(row > 70) & (row < 200)]
rep["tb2_icon_color"] = hexc(np.median(ico, axis=0)) if len(ico) else None
# 该行背景: 暗像素
bg = u[95:130, 30:420][row < 40]
rep["tb2_bg"] = hexc(np.median(bg, axis=0)) if len(bg) else None
# 与正文 bg 比较
body = u[200:300, 100:200][ug[200:300, 100:200] < 40]
rep["body_bg_user"] = hexc(np.median(body, axis=0)) if len(body) else None

# --- PrintWindow: 标签 pill ---
pw = np.array(Image.open(BASE + r"\04_reading_printwindow.png").convert("RGB"))
pwg = pw.mean(axis=2)
# pill: 目测 x 28..150, y 60..96. 采样其内部平坦区
for rx0, rx1 in [(30, 145), (35, 100), (40, 80)]:
    r = pwg[66:94, rx0:rx1]
    px = pw[66:94, rx0:rx1]
    inner = px[(r > 18) & (r < 45)]
    rep[f"pill_bg_x{rx0}_{rx1}"] = hexc(np.median(inner, axis=0)) if len(inner) else None
# pill 边界: 在 y=78 行扫 x 18..414, 找亮度台阶
line = pwg[78, :]
steps = []
for x in range(19, 413):
    if abs(int(line[x+1]) - int(line[x])) >= 4:
        steps.append((x, int(line[x]), int(line[x+1])))
rep["pill_edge_steps_y78"] = steps[:30]
# pill 垂直边界: 在 x=60 列扫 y 56..100
col = pwg[:, 60]
vsteps = []
for y in range(57, 99):
    if abs(int(col[y+1]) - int(col[y])) >= 4:
        vsteps.append((y, int(col[y]), int(col[y+1])))
rep["pill_edge_steps_x60"] = vsteps

# --- 底栏按钮精确几何 (PrintWindow) ---
# 左按钮: 边框段 [32,46] 左弧 + [105,119] 右弧, y 564..592
# 按钮外边界: 在 y=578 行扫
bline = pwg[578, :]
bedges = []
for x in range(19, 413):
    if abs(int(bline[x+1]) - int(bline[x])) >= 6:
        bedges.append((x, int(bline[x]), int(bline[x+1])))
rep["bottom_btn_edges_y578"] = bedges
# 垂直边界 x=75
bcol = pwg[:, 75]
bedges2 = []
for y in range(550, 606):
    if abs(int(bcol[y+1]) - int(bcol[y])) >= 6:
        bedges2.append((y, int(bcol[y]), int(bcol[y+1])))
rep["bottom_btn_edges_x75"] = bedges2
# 按钮文字/边框颜色
bb = pw[564:593, 32:120]; bg2 = pwg[564:593, 32:120]
rep["btn_border_color"] = hexc(np.median(bb[(bg2>50)&(bg2<160)], axis=0))
rep["btn_text_color"] = hexc(np.median(bb[bg2>120], axis=0)) if (bg2>120).any() else None
# 进度文本颜色
pg = pw[560:596, 199:237]; pgg = pwg[560:596, 199:237]
rep["progress_color"] = hexc(np.median(pg[pgg>80], axis=0)) if (pgg>80).any() else None
# 进度文本高度
tb = (pgg > 80).sum(axis=1)
ys = np.where(tb >= 1)[0]
rep["progress_glyph_h"] = int(ys.max()-ys.min()+1) if len(ys) else None

with open(BASE + r"\04k_final_report.json", "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
