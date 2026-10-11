"""把 .ico 里的各尺寸摊开渲染成一张预览图，方便肉眼检查。"""
import os, sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt5.QtCore import QSize, Qt, QRectF
from PyQt5.QtGui import QImage, QPainter, QColor, QFont
from PyQt5.QtWidgets import QApplication

import importlib.util

TOOLS = Path(__file__).resolve().parent / "tools"
spec = importlib.util.spec_from_file_location("make_icon", TOOLS / "make_icon.py")
mk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mk)

app = QApplication.instance() or QApplication([])

SIZES = mk.SIZES
CELL = 148          # 每个尺寸占的格子边长
ZOOM_MAX = 128      # 小于这个尺寸的放大到 CELL 看细节
canvas = QImage(QSize(CELL * len(SIZES), CELL + 60), QImage.Format_ARGB32)
canvas.fill(QColor("#f7f8fa"))   # 浅底：看深色图标在浅色资源管理器里的样子

p = QPainter(canvas)
p.setRenderHint(QPainter.Antialiasing, False)
p.setRenderHint(QPainter.SmoothPixmapTransform, False)
font = QFont()
font.setPointSize(8)

for i, size in enumerate(SIZES):
    img = mk.draw(size)
    x = i * CELL
    if size <= ZOOM_MAX:
        # 最近邻放大：能看清 16px 到底糊不糊
        scale = max(1, CELL // (size + 20))
        big = img.scaled(size * scale, size * scale, Qt.KeepAspectRatio, Qt.FastTransformation)
    else:
        big = img.scaled(CELL - 20, CELL - 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p.drawImage(x + (CELL - big.width()) // 2, (CELL - big.height()) // 2 + 10, big)
    p.setPen(QColor("#333"))
    p.setFont(font)
    p.drawText(QRectF(x, CELL + 14, CELL, 20), Qt.AlignCenter, f"{size}x{size}  (x{big.width()//size})")
    # 同时画一个 1:1 的原尺寸，看真实观感
    p.drawImage(x + (CELL - size) // 2, CELL + 34, img)
p.end()

out = Path(os.environ["TEMP"]) / "mr_icon_preview.png"
canvas.save(str(out))
print(out)
