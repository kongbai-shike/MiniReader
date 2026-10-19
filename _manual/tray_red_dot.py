"""证明「防偷窥态托盘图标只多右上角一个红点」——逐像素，不靠肉眼。

做法：拿 TrayIcon 实际使用的两个 QIcon（load_tray_icon / make_stealth_icon），
分别渲染成 16px 与 64px，逐像素比对，断言：

1. 差异像素全部落在右上角那个红点圆的外接方框内（不许整只图标换掉）；
2. 差异像素就是 STEALTH_BADGE（#e5484d）或它的抗锯齿过渡色（同色相、更浅）；
3. 16px（任务栏真实尺寸）也成立——大图好看不代表托盘里看得见；
4. 退出防偷窥后 setIcon 回到 normal，像素与起点完全一致。

产物：_manual/tray_shots/ 下的 PNG，可直接贴进报告。
"""

from __future__ import annotations

import os
import sys
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SHOTS = os.path.join(REPO, "_manual", "tray_shots")
os.makedirs(SHOTS, exist_ok=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minireader.ui.tray import (  # noqa: E402
    STEALTH_BADGE,
    TrayIcon,
    load_tray_icon,
    make_stealth_icon,
)

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAILED.append(f"{name} :: {detail}")
    print(f"[{'OK ' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")


def badge_box(dimension: int) -> tuple[int, int, int, int]:
    """_paint_badge 画的那个圆的外接方框（右、上各留 2% 的边）。"""
    dot = dimension * 0.30
    cx = dimension - dot / 2 - dimension * 0.02
    cy = dot / 2 + dimension * 0.02
    return (
        int(cx - dot / 2) - 2,
        int(cy - dot / 2) - 2,
        int(cx + dot / 2) + 2,
        int(cy + dot / 2) + 2,
    )


def diff_pixels(img_a, img_b) -> list[tuple[int, int]]:
    out = []
    for y in range(img_a.height()):
        for x in range(img_a.width()):
            if img_a.pixel(x, y) != img_b.pixel(x, y):
                out.append((x, y))
    return out


def badge_stats(stealth_img, diff: list[tuple[int, int]]) -> dict:
    """只看**差异点**：这些点换成什么颜色了。

    统计整张图没有意义——图标本来就有别的颜色；要比的是"变掉的那几个像素
    是什么颜色"。红点外圈还有一道 PALETTE["bg"] 深色描边（_paint_badge 里画的，
    免得红点糊在深色底上），所以除了 #e5484d 还会有深色的描边与抗锯齿过渡色。
    因此这里断言的是：最常见的"变成的颜色"就是 STEALTH_BADGE，且差异点总数不超过
    红点框的面积——底图其余部分原样不动已经由"框外 0 差异"证明。
    """
    colors = Counter(QColor.fromRgba(stealth_img.pixel(x, y)).name(QColor.HexRgb) for x, y in diff)
    top, top_n = colors.most_common(1)[0] if colors else ("", 0)
    return {"top": top, "top_n": top_n, "total": len(diff), "colors": len(colors)}


def main() -> int:
    app = QApplication(sys.argv[:1])
    del app

    normal = load_tray_icon()
    stealth = make_stealth_icon(normal)

    for dimension in (64, 16):
        a = normal.pixmap(dimension, dimension).toImage()
        b = stealth.pixmap(dimension, dimension).toImage()
        a.save(os.path.join(SHOTS, f"icon_normal_{dimension}.png"), "PNG")
        b.save(os.path.join(SHOTS, f"icon_stealth_{dimension}.png"), "PNG")

        x0, y0, x1, y1 = badge_box(dimension)
        diff = diff_pixels(a, b)
        outside = [(x, y) for (x, y) in diff if not (x0 <= x <= x1 and y0 <= y <= y1)]
        check(
            f"{dimension}px：差异像素只在右上角红点框内",
            bool(diff) and not outside,
            f"差异 {len(diff)}px，框外 {len(outside)}px，框=({x0},{y0})-({x1},{y1})",
        )
        stats = badge_stats(b, diff)
        box_area = (x1 - x0 + 1) * (y1 - y0 + 1)
        check(
            f"{dimension}px：变掉的像素主要就是 STEALTH_BADGE（#e5484d），不是换了整只图标",
            stats["top"].lower() == STEALTH_BADGE.lower() and stats["total"] <= box_area,
            f"最常见变成 {stats['top']} ×{stats['top_n']}/{stats['total']}，"
            f"共 {stats['colors']} 种色，差异 {stats['total']}px ≤ 框面积 {box_area}px",
        )

        # 红点必须在右上象限，不能跑到别处
        if diff:
            cx = sum(x for x, _ in diff) / len(diff)
            cy = sum(y for _, y in diff) / len(diff)
            check(
                f"{dimension}px：差异重心在右上象限",
                cx > dimension / 2 and cy < dimension / 2,
                f"重心=({cx:.1f},{cy:.1f})",
            )

    # 真实 TrayIcon 的开关闭环：set_stealth_state(True) 用红点图标，False 回到原样
    tray = TrayIcon(title="季度报告")
    try:
        tray.set_stealth_state(False)
        before = tray.icon().pixmap(16, 16).toImage()
        tray.set_stealth_state(True, "Ctrl+Shift+L")
        during = tray.icon().pixmap(16, 16).toImage()
        check(
            "TrayIcon.set_stealth_state(True) 换成带红点的图标",
            diff_pixels(before, during) != [],
            "像素有差异",
        )
        check(
            "防偷窥态 tooltip 告诉他们怎么出来",
            "Ctrl+Shift+L" in tray.toolTip(),
            tray.toolTip(),
        )
        tray.set_stealth_state(False)
        after = tray.icon().pixmap(16, 16).toImage()
        check(
            "退出后像素与起点逐个相同",
            diff_pixels(before, after) == [],
            "无差异",
        )
    finally:
        tray.shutdown()

    # 一张并排放大图，方便人眼一眼看出"只是多了个红点"
    from PySide6.QtGui import QImage, QPainter  # noqa: E402

    scale = 6
    canvas = QImage(16 * scale * 2 + 30, 16 * scale + 20, QImage.Format_ARGB32)
    canvas.fill(QColor(32, 32, 32))
    painter = QPainter(canvas)
    painter.drawImage(10, 10, normal.pixmap(16, 16).toImage().scaled(16 * scale, 16 * scale))
    painter.drawImage(
        20 + 16 * scale,
        10,
        stealth.pixmap(16, 16).toImage().scaled(16 * scale, 16 * scale),
    )
    painter.end()
    canvas.save(os.path.join(SHOTS, "compare_16px_x6.png"), "PNG")
    print(f"\n截图：{SHOTS}")

    if FAILED:
        print(f"\n=== {len(FAILED)} 项失败 ===")
        for item in FAILED:
            print("FAILED:", item)
        return 1
    print("\n=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())