# 04b: 标签栏底色细分 + 特写导出 + 用户截图(tooltip/右键菜单/第二行工具栏)分析
import json
import numpy as np
from PIL import Image

BASE = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
img = np.array(Image.open(BASE + r"\04_reading_printwindow.png").convert("RGB"))
gray = img.mean(axis=2)
def hexc(px): return "#%02x%02x%02x" % tuple(int(c) for c in px)
rep = {}

# 1) 标签 pill: 在 y60..96 内找比周围亮的连续区域
strip = gray[60:98, 18:414]
colmax = strip.max(axis=0)
# pill 边界: 行均值略高的区域
rowmean = strip.mean(axis=1)
rep["tab_strip_rows_mean"] = [round(float(v),1) for v in rowmean]
# 扫描 pill: 在 y64..92 内, 每列中位亮度
colmed = np.median(gray[64:94, 18:414], axis=0)
pill_cols = np.where(colmed > 20)[0]
if len(pill_cols):
    rep["tab_pill_x"] = [int(pill_cols.min()+18), int(pill_cols.max()+18)]
    px = img[66:92, pill_cols.min()+18:pill_cols.max()+18]
    g = gray[66:92, pill_cols.min()+18:pill_cols.max()+18]
    inner = px[(g>25)&(g<70)]
    rep["tab_pill_bg"] = hexc(np.median(inner, axis=0)) if len(inner) else None
# 标签右侧 "+" 区域
plus = gray[64:94, 150:220] > 60
rep["tab_plus_x"] = [int(np.where(plus.sum(axis=0)>0)[0].min()+150), int(np.where(plus.sum(axis=0)>0)[0].max()+150)] if plus.any() else None
# 关闭 x 在 pill 内?
rep["tab_strip_y_range"] = [64, 94]

# 2) 特写导出 (B 坐标)
crops = {
    "04_crop_tabstrip_3x.png": (18, 56, 240, 100),
    "04_crop_bottombar_3x.png": (18, 552, 414, 606),
    "04_crop_textline_3x.png": (60, 128, 400, 215),
    "04_crop_progress_4x.png": (185, 556, 250, 598),
}
for name, (x0, y0, x1, y1) in crops.items():
    c = Image.fromarray(img[y0:y1, x0:x1])
    s = 4 if "4x" in name else 3
    c = c.resize(((x1-x0)*s, (y1-y0)*s), Image.NEAREST)
    c.save(BASE + "\\" + name)

# 3) 用户截图分析: tooltip
urep = {}
for key, fn in [("eye", "04_tooltip_eye_user.png"), ("opacity", "04_tooltip_opacity_user.png"), ("pin", "04_tooltip_pin_user.png")]:
    u = np.array(Image.open(BASE + "\\" + fn).convert("RGB"))
    ug = u.mean(axis=2)
    h2, w2 = ug.shape
    # tooltip 气泡: 最暗的圆角矩形区域
    dark = ug < 60
    ys, xs = np.where(dark)
    info = {"img_size": [w2, h2]}
    if len(ys):
        info["tip_bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        inner = u[dark]
        info["tip_bg"] = hexc(np.median(inner, axis=0))
    bright = ug > 130
    if bright.any():
        info["tip_text_color"] = hexc(np.median(u[bright], axis=0))
    # 图标行背景(上半部分)
    top_half = u[:max(1, h2//3)]
    info["icon_row_bg"] = hexc(np.median(top_half.reshape(-1,3), axis=0))
    urep[key] = info

# 4) 用户截图: 右键菜单
m = np.array(Image.open(BASE + "\\04_context_menu_user.png").convert("RGB"))
mg = m.mean(axis=2)
mh, mw = mg.shape
# 菜单是中部深色圆角矩形, 找近似 #2b2b2b 的大块区域
menu_mask = (mg > 30) & (mg < 70)
colsum = menu_mask.sum(axis=0)
mcols = np.where(colsum > mh*0.15)[0]
minfo = {"img_size": [mw, mh]}
if len(mcols):
    x0, x1 = int(mcols.min()), int(mcols.max())
    rowsum = menu_mask[:, x0:x1+1].sum(axis=1)
    mrows = np.where(rowsum > (x1-x0)*0.6)[0]
    if len(mrows):
        y0, y1 = int(mrows.min()), int(mrows.max())
        minfo["menu_bbox"] = [x0, y0, x1, y1]
        inner = m[y0+2:y1-2, x0+2:x1-2]
        ig = mg[y0+2:y1-2, x0+2:x1-2]
        bgpx = inner[(ig>30)&(ig<70)]
        minfo["menu_bg"] = hexc(np.median(bgpx, axis=0)) if len(bgpx) else None
        tpx = inner[ig>120]
        minfo["menu_text_color"] = hexc(np.median(tpx, axis=0)) if len(tpx) else None
        # 分隔线: 行内亮度介于 60-110 且横贯
        for yy in range(y0, y1):
            row = mg[yy, x0+4:x1-4]
            if ((row>60)&(row<120)).mean() > 0.8:
                minfo.setdefault("divider_rows", []).append(yy - y0)
        # 菜单项高度估计: 文本带
        tb = (ig > 120).sum(axis=1)
        items = []
        in_b = False
        for i, v in enumerate(tb):
            if v >= 2 and not in_b: s = i; in_b = True
            elif v < 2 and in_b:
                if i - s >= 3: items.append([s, i])
                in_b = False
        minfo["menu_item_text_bands"] = items
        if len(items) > 1:
            minfo["menu_item_pitch"] = round(sum(items[i+1][0]-items[i][0] for i in range(len(items)-1))/(len(items)-1), 1)

# 5) 用户截图: 第二行工具栏 (已加书架 ≡ ⟳ Tt A- A+)
c = m  # 同一张图
# 该行在标签栏下方, 找 "已加书架" 等亮色文本行带 y 范围大约在标签行之下
# 用亮文本行带粗定位
bright_rows = (mg > 100).sum(axis=1)
bands = []
in_b = False
for i, v in enumerate(bright_rows):
    if v >= 3 and not in_b: s = i; in_b = True
    elif v < 3 and in_b:
        if i - s >= 3: bands.append([s, i, int(bright_rows[s:i].max())])
        in_b = False
minfo["bright_text_bands_top200"] = [b for b in bands if b[0] < 200]
urep["context_menu"] = minfo

with open(BASE + r"\04_user_shots_report.json", "w", encoding="utf-8") as f:
    json.dump(urep, f, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=1))
print(json.dumps(urep, ensure_ascii=False, indent=1))
