# -*- coding: utf-8 -*-
"""03-2 final: 空态静态视觉分析（PrintWindow 黑底图 + alpha 反源色）
双坐标系：
  B = 位图坐标（原点 = GetWindowRect 左上，431x620）
  V = 可见表面坐标（原点 = (18,18)，397x585，DWM 阴影边距之外）
"""
import ctypes
import json

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import numpy as np
import win32gui
import win32ui
from PIL import Image

HWND = 68382
OUT = r"c:\Users\pc\Desktop\随手文件\MiniReader\qq_research"
SURF = (18, 18, 414, 602)          # 可见表面在位图中的矩形（夹逼实测）
ALPHA_BG = 0.60                    # 窗体底色 alpha（夹逼实测）
ALPHA_CARD = 0.616                 # 卡片底 alpha（夹逼实测）
CORNER_R = 9                       # 白底覆盖曲线拟合（[推算] 9~10）


def find_mini():
    if win32gui.IsWindow(HWND) and win32gui.GetWindowText(HWND) == "迷你窗":
        return HWND
    hits = []
    win32gui.EnumWindows(
        lambda h, _: (hits.append(h) if (win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "迷你窗") else None),
        None)
    if not hits:
        raise RuntimeError("找不到「迷你窗」")
    return hits[0]


def printwindow(hwnd, rect):
    l, t, r, b = rect
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    sdc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    sdc.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, sdc.GetSafeHdc(), 3)
    bi = bmp.GetInfo()
    img = Image.frombuffer("RGB", (bi["bmWidth"], bi["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    sdc.DeleteDC(); mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    win32gui.DeleteObject(bmp.GetHandle())
    return img


def hext(rgb):
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in rgb)


def vrect(b):
    """位图矩形 -> 可见表面矩形"""
    x0, y0, x1, y1 = b
    ox, oy = SURF[0], SURF[1]
    return [x0 - ox, y0 - oy, x1 - ox, y1 - oy]


def segments_from_cols(cols, min_w=6, merge_gap=9):
    segs, x, W = [], 0, len(cols)
    while x < W:
        if cols[x]:
            x0 = x
            while x < W and cols[x]:
                x += 1
            segs.append([x0, x - 1])
        else:
            x += 1
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [s for s in merged if s[1] - s[0] + 1 >= min_w]


def main():
    hwnd = find_mini()
    rect = win32gui.GetWindowRect(hwnd)
    W, H = rect[2] - rect[0], rect[3] - rect[1]
    img = printwindow(hwnd, rect)
    img.save(OUT + r"\03_state1_full.png")
    arr = np.asarray(img).astype(float)
    g = arr.mean(2)
    R = {"window_rect_B": list(rect), "size_B": [W, H],
         "surface_rect_B": list(SURF), "surface_size_V": [SURF[2] - SURF[0] + 1, SURF[3] - SURF[1] + 1],
         "corner_radius_V_est": CORNER_R,
         "alpha": {"window_bg": ALPHA_BG, "card": ALPHA_CARD}}

    # ---------- 虚线框 ----------
    dash = (g > 40) & (g < 150)
    top_y = int(np.argmax(dash[48:140, 20:W - 20].sum(1)) + 48)
    bot_y = int(np.argmax(dash[top_y + 200:H - 12, 20:W - 20].sum(1)) + top_y + 200)
    coll = dash[top_y + 5:bot_y - 5, :].sum(0)
    left_x = int(np.argmax(coll[8:60])) + 8
    right_x = int(np.argmax(coll[W - 60:W - 8])) + W - 60
    dh = np.median(arr[top_y, 20:W - 20][dash[top_y, 20:W - 20]], 0)
    dv = np.median(arr[top_y + 5:bot_y - 5, left_x][dash[top_y + 5:bot_y - 5, left_x]], 0)
    frame_B = [left_x, top_y, right_x, bot_y]
    R["dash_frame"] = {"rect_B": frame_B, "rect_V": vrect(frame_B),
                       "color_on_black": hext(dh), "color_source_est": hext(np.clip(dh / ALPHA_BG, 0, 255))}
    print("frame B:", frame_B)

    # ---------- 顶栏 7 图标 ----------
    m = (g[18:60, :] > 40)
    icons = []
    sem = ["后退", "前进", "隐身", "透明度调节", "置顶", "最小化", "关闭"]
    for i, (x0, x1) in enumerate(segments_from_cols(m.any(0), 6, 9)):
        ys = np.where(m[:, x0:x1 + 1].any(1))[0] + 18
        y0, y1 = int(ys.min()), int(ys.max())
        sub = arr[y0:y1 + 1, x0:x1 + 1]
        sg = sub.mean(2)
        peak = float(sg.max())
        core = sub[sg > max(45, peak - 25)]
        glyph_cb = np.median(core, 0) if len(core) else sub.max(0)
        icons.append({
            "semantic": sem[i] if i < len(sem) else f"btn{i}",
            "glyph_rect_B": [x0, y0, x1, y1], "glyph_rect_V": vrect([x0, y0, x1, y1]),
            "glyph_px": [x1 - x0 + 1, y1 - y0 + 1],
            "center_B": [(x0 + x1) // 2, (y0 + y1) // 2],
            "glyph_color_on_black": hext(glyph_cb),
            "glyph_source_est": hext(np.clip(glyph_cb / ALPHA_BG, 0, 255)),
            "state": "disabled_dim" if peak < 90 else "active",
        })
        if i:
            icons[-1]["pitch_to_prev_B"] = icons[-1]["center_B"][0] - icons[i - 1]["center_B"][0]
    R["topbar_icons"] = icons

    # 顶栏几何（V 坐标）
    gb_y0 = min(i["glyph_rect_B"][1] for i in icons) - SURF[1]
    gb_y1 = max(i["glyph_rect_B"][3] for i in icons) - SURF[1]
    R["topbar"] = {
        "glyph_band_V": [gb_y0, gb_y1],
        "glyph_height_V": gb_y1 - gb_y0 + 1,
        "left_inset_V": icons[0]["glyph_rect_V"][0],
        "right_inset_V": R["surface_size_V"][0] - 1 - icons[-1]["glyph_rect_V"][2],
        "back_forward_pitch_V": icons[1]["center_B"][0] - icons[0]["center_B"][0],
        "right_group_pitch_V": [icons[i]["pitch_to_prev_B"] for i in range(2, 7)],
        "drag_area_V": [icons[1]["glyph_rect_V"][2] + 1, gb_y0 - 4,
                        icons[2]["glyph_rect_V"][0] - 1, gb_y1 + 4],
        "same_bg_as_body": True,
    }

    # ---------- 主标题两行 ----------
    def text_band(y0, y1, x0, x1, thresh, min_count, min_h=8, gap=3):
        prof = (g[y0:y1, x0:x1] > thresh).sum(1)
        idx = np.where(prof > min_count)[0]
        bands = []
        if len(idx):
            s = p = int(idx[0])
            for v in idx[1:]:
                if v - p <= gap:
                    p = int(v)
                else:
                    if p - s + 1 >= min_h:
                        bands.append((s + y0, p + y0))
                    s = p = int(v)
            if p - s + 1 >= min_h:
                bands.append((s + y0, p + y0))
        return bands

    fx0, fx1 = left_x + 8, right_x - 8
    # 三条文本带：主标题1、主标题2、副标题（副标题字小、色暗）
    bands = text_band(top_y + 150, top_y + 340, fx0, fx1, 110, 25, min_h=10, gap=5)
    heads = []
    for (a, b) in bands[:2]:
        mm = g[a:b + 1, fx0:fx1] > 110
        xx = np.where(mm.any(0))[0] + fx0
        px = arr[a:b + 1, fx0:fx1][mm]
        heads.append({"rect_B": [int(xx.min()), a, int(xx.max()), b],
                      "rect_V": vrect([int(xx.min()), a, int(xx.max()), b]),
                      "h_px": b - a + 1,
                      "color_on_black": hext(np.median(px, 0)),
                      "color_source_est": hext(np.clip(np.median(px, 0) / ALPHA_BG, 0, 255))})
    R["heading_lines"] = heads

    # ---------- 副标题 + 左右分隔线 ----------
    sub = None
    sb = bands[2] if len(bands) >= 3 else None
    if sb is None:
        sb2 = text_band(heads[-1]["rect_B"][3] + 14, heads[-1]["rect_B"][3] + 60, fx0, fx1, 70, 12, min_h=10, gap=6)
        sb = sb2[0] if sb2 else None
    if sb:
        a, b = sb
        mm = (g[a:b + 1, fx0:fx1] > 70) & (g[a:b + 1, fx0:fx1] < 175)
        xx = np.where(mm.any(0))[0] + fx0
        px = arr[a:b + 1, fx0:fx1][mm]
        tx0, tx1 = int(xx.min()), int(xx.max())

        # 分隔线：在文本行附近扫描"低亮长横线"所在行（左侧区间统计）
        line_row_scores = []
        for yy in range(a - 3, b + 4):
            row = g[yy, fx0:tx0 - 10]
            line_row_scores.append(((row > 24) & (row < 60)).sum())
        myc = int(a - 3 + int(np.argmax(line_row_scores)))
        line = g[myc, fx0:fx1]
        lp = np.where((line > 24) & (line < 60))[0] + fx0
        groups, cur = [], []
        for X in lp:
            if tx0 - 8 <= X <= tx1 + 8:
                if cur:
                    groups.append((cur[0], cur[-1])); cur = []
                continue
            if cur and X - cur[-1] > 3:
                groups.append((cur[0], cur[-1])); cur = []
            cur.append(int(X))
        if cur:
            groups.append((cur[0], cur[-1]))
        sub = {"text_rect_B": [int(xx.min()), a, int(xx.max()), b],
               "text_rect_V": vrect([int(xx.min()), a, int(xx.max()), b]),
               "text_color_on_black": hext(np.median(px, 0)),
               "text_source_est": hext(np.clip(np.median(px, 0) / ALPHA_BG, 0, 255)),
               "line_y_B": myc, "line_y_V": myc - SURF[1],
               "line_segments_B": groups,
               "line_color_on_black": hext(np.median(arr[myc, lp], 0)) if len(lp) else None}
    R["subtitle_divider"] = sub

    # ---------- 三卡片 ----------
    card_y0, card_y1 = bot_y - 200, bot_y - 60
    cmask = (g[card_y0:card_y1, 30:W - 30] > 20) & (g[card_y0:card_y1, 30:W - 30] < 50)
    cols = cmask.any(0)
    csegs = segments_from_cols(cols, min_w=50, merge_gap=4)
    # 用行确认高度
    cards = []
    for x0, x1 in csegs:
        rows = np.where(cmask[:, x0 - 30:x1 - 30 + 1].any(1))[0] + card_y0
        if len(rows) < 40:
            continue
        y0, y1 = int(rows.min()), int(rows.max())
        cards.append([x0 + 30, y0, x1 + 30, y1])
    cards.sort()
    labels = ["聊天", "小说", "自选股"]
    cout = []
    for i, (x0, y0, x1, y1) in enumerate(cards):
        fill = np.median(arr[y0 + 6:y0 + 14, x0 + 6:x0 + 14].reshape(-1, 3), 0)
        # 标签文字：卡片底部 6~24px
        lab_m = g[y1 - 26:y1 - 6, x0 + 6:x1 - 6] > 70
        lab_px = arr[y1 - 26:y1 - 6, x0 + 6:x1 - 6][lab_m]
        # 图标：卡片上半部亮像素
        ic_m = g[y0 + 8:y0 + 46, x0 + 8:x1 - 8] > 70
        ic_px = arr[y0 + 8:y0 + 46, x0 + 8:x1 - 8][ic_m]
        iy = np.where(ic_m.any(1))[0]
        ix = np.where(ic_m.any(0))[0]
        cout.append({
            "label": labels[i], "rect_B": [x0, y0, x1, y1], "rect_V": vrect([x0, y0, x1, y1]),
            "size_B": [x1 - x0 + 1, y1 - y0 + 1],
            "fill_on_black": hext(fill), "fill_source_est": hext(np.clip(fill / ALPHA_CARD, 0, 255)),
            "gap_prev_B": (x0 - cards[i - 1][2] - 1) if i else None,
            "label_color_on_black": hext(np.median(lab_px, 0)) if len(lab_px) else None,
            "icon_rect_B": [x0 + 8 + int(ix.min()), y0 + 8 + int(iy.min()),
                            x0 + 8 + int(ix.max()), y0 + 8 + int(iy.max())] if len(iy) else None,
            "icon_color_on_black": hext(np.median(ic_px, 0)) if len(ic_px) else None,
        })
    R["cards"] = cout

    # ---------- 中央手机插画 ----------
    phone_m = (g[top_y + 40:heads[0]["rect_B"][1] - 24, left_x + 30:right_x - 30] > 70)
    py = np.where(phone_m.any(1))[0]; px_ = np.where(phone_m.any(0))[0]
    pa, pb = int(py.min()) + top_y + 40, int(py.max()) + top_y + 40
    pxx0, pxx1 = int(px_.min()) + left_x + 30, int(px_.max()) + left_x + 30
    _xoff = left_x + 30
    pcol = arr[pa:pb + 1, pxx0:pxx1 + 1][
        phone_m[pa - (top_y + 40):pb - (top_y + 40) + 1,
                pxx0 - _xoff:pxx1 - _xoff + 1]]
    R["phone_illustration"] = {"rect_B": [pxx0, pa, pxx1, pb], "rect_V": vrect([pxx0, pa, pxx1, pb]),
                               "color_on_black": hext(np.median(pcol, 0)),
                               "color_source_est": hext(np.clip(np.median(pcol, 0) / ALPHA_BG, 0, 255))}

    # ---------- 颜色总表 ----------
    body_cb = np.median(arr[120:200, 120:320].reshape(-1, 3), 0)
    R["palette"] = {
        "window_bg_source": hext(body_cb / ALPHA_BG), "window_bg_source_rgb": [int(round(v)) for v in body_cb / ALPHA_BG],
        "window_bg_alpha": ALPHA_BG, "window_bg_on_black": hext(body_cb),
        "dash": R["dash_frame"]["color_on_black"], "dash_source": R["dash_frame"]["color_source_est"],
        "heading": heads[0]["color_on_black"], "heading_source": heads[0]["color_source_est"],
        "subtitle": sub["text_color_on_black"] if sub else None,
        "divider_line": sub["line_color_on_black"] if sub else None,
        "card_fill": cout[0]["fill_on_black"], "card_fill_source": cout[0]["fill_source_est"],
        "card_label": cout[0]["label_color_on_black"],
        "card_icon": cout[0]["icon_color_on_black"],
        "active_icon": next(i["glyph_color_on_black"] for i in icons if i["semantic"] == "隐身"),
        "disabled_arrow": icons[0]["glyph_color_on_black"],
        "phone_illustration": R["phone_illustration"]["color_on_black"],
    }

    # ---------- 特写 ----------
    def crop(box, name, scale=3):
        c = img.crop(box).resize(((box[2] - box[0]) * scale, (box[3] - box[1]) * scale), Image.NEAREST)
        p = OUT + "\\" + name; c.save(p); return p

    crops = [
        crop((0, 0, W, 62), "03_crop_topbar_3x.png"),
        crop((left_x - 14, top_y - 14, left_x + 120, top_y + 120), "03_crop_frame_corner_4x.png", 4),
    ]
    for i, ic in enumerate(icons):
        x0, y0, x1, y1 = ic["glyph_rect_B"]
        crops.append(crop((x0 - 12, y0 - 10, x1 + 13, y1 + 11), f'03_icon_{i}_{ic["semantic"]}_4x.png', 4))
    for h in heads:
        x0, y0, x1, y1 = h["rect_B"]
        crops.append(crop((x0 - 10, y0 - 6, x1 + 10, y1 + 7), f'03_heading_{y0}.png', 2))
    if sub:
        x0, y0, x1, y1 = sub["text_rect_B"]
        crops.append(crop((60, y0 - 8, 372, y1 + 8), "03_subtitle_divider_2x.png", 2))
    for c in cout:
        x0, y0, x1, y1 = c["rect_B"]
        crops.append(crop((x0 - 6, y0 - 6, x1 + 7, y1 + 7), f'03_card_{c["label"]}_3x.png'))
    R["crops"] = crops

    with open(OUT + r"\03_geometry.json", "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=2)
    print(json.dumps(R["topbar"], ensure_ascii=False, indent=1))
    print("icons:")
    for i in icons:
        print(" ", i["semantic"], i["glyph_rect_B"], i["glyph_px"], i["glyph_color_on_black"], i["state"], i.get("pitch_to_prev_B"))
    print("heading:", [(h["rect_B"], h["h_px"], h["color_on_black"]) for h in heads])
    print("subtitle:", sub)
    print("cards:", [(c["label"], c["rect_B"], c["size_B"], c["fill_on_black"], c["gap_prev_B"], c["label_color_on_black"]) for c in cout])
    print("phone:", R["phone_illustration"]["rect_B"], R["phone_illustration"]["color_on_black"])
    print("palette:", json.dumps(R["palette"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
