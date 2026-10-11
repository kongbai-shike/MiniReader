"""生成 MiniReader 的 .ico。

用法：
    py -3.12 tools/make_icon.py [输出路径]

为什么要有这个脚本，而不是直接把一个 .ico 提交进仓库：

1. 本项目的托盘图标是 QPainter 现画的（minireader/ui/tray.py），仓库里一个二进制
   资源都没有。exe 图标如果直接塞一个二进制进来，就成了唯一的例外——而且没人说得清
   它是从哪来的、改配色时也不敢动。用脚本生成，图标就变成了可复现的代码。
2. 只依赖 Qt（本来就有的依赖），不引入 Pillow。

.ico 的结构：文件头 + N 个目录项（每项 16 字节）+ N 张图片数据。
每张图片可以是 BMP(DIB) 也可以是 PNG。惯例是小的用 BMP、256 用 PNG——
BMP 版本在所有 Windows 上都能显示，PNG 版本体积小得多。这里就按这个惯例来。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PyQt5.QtGui import QImage

# Qt 只在生成图标时需要，所以放在 main 里 import，`--help` 之类不用先起 Qt。
SIZES = (16, 24, 32, 48, 64, 128, 256)
# 256 及以上用 PNG 负载：这些尺寸下 BMP 太大（256×256×4B = 256 KB vs PNG 几 KB）。
PNG_FROM = 128


def _colors():
    """配色跟 minireader/ui/theme.py 的 PALETTE 对齐：深底 + 蓝紫强调色。"""
    return {"bg": "#12131a", "accent": "#6f9ee8", "page": "#f2f4f8", "dim": "#4a5470"}


def draw(size: int):
    """画一张 size×size 的图标：深色圆角方块 + 三条正文行 + 右侧书签条。

    图形故意做成「一页纸」而不是字母 M：16×16 下字母认不出来，而几条横线的
    「这是一本书」在 16px 上依然成立。
    """
    import PyQt5.QtCore as QtCore
    import PyQt5.QtGui as QtGui

    c = _colors()
    img = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)

    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)

    # 底板：圆角深色方块，占满整格（ICO 自己不做边距，边距在这里留）
    pad = max(1.0, size * 0.04)
    radius = size * 0.22
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(c["bg"]))
    p.drawRoundedRect(
        QtCore.QRectF(pad, pad, size - 2 * pad, size - 2 * pad), radius, radius
    )

    # 正文：三条横线。line 从左边距一直到右边距，最后一条短一点（像段末）。
    left = size * 0.22
    right = size * 0.80
    thickness = max(1.0, size * 0.075)
    tops = (0.30, 0.475, 0.65)
    widths = (1.0, 1.0, 0.62)
    p.setBrush(QtGui.QColor(c["page"]))
    for top, w in zip(tops, widths):
        r = QtCore.QRectF(
            left,
            size * top - thickness / 2,
            (right - left) * w,
            thickness,
        )
        p.drawRoundedRect(r, thickness / 2, thickness / 2)

    # 书签条：右侧一条强调色的竖条，从顶部伸下来——「读到这儿了」
    bw = max(1.0, size * 0.11)
    p.setBrush(QtGui.QColor(c["accent"]))
    p.drawRoundedRect(
        QtCore.QRectF(size - size * 0.20, size * 0.20, bw, size * 0.34),
        bw * 0.3,
        bw * 0.3,
    )

    p.end()
    return img


def _bmp_payload(img) -> bytes:
    """把 QImage 转成 ICO 里那种 BMP 负载（BITMAPINFOHEADER + XOR + AND）。

    两个坑：
    * BITMAPINFOHEADER 里的 biHeight 要写 **两倍** 高度——ICO 的 BMP 是
      XOR 图（真彩色）+ AND 图（1bpp 掩码）叠起来的，高度算的是两者之和。
    * 像素行是自下而上、每行 4 字节对齐，且通道顺序是 BGRA 而不是 QImage 的 ARGB。
    """
    w, h = img.width(), img.height()
    img = img.convertToFormat(img.Format_ARGB32)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        w,           # biWidth
        h * 2,       # biHeight：XOR + AND
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        0,           # biSizeImage（BI_RGB 可以为 0）
        0, 0, 0, 0,  # 分辨率与调色板
    )
    rows = []
    for y in range(h - 1, -1, -1):  # 自下而上
        row = bytearray()
        for x in range(w):
            px = img.pixel(x, y)
            a = (px >> 24) & 0xFF
            r = (px >> 16) & 0xFF
            g = (px >> 8) & 0xFF
            b = px & 0xFF
            row += bytes((b, g, r, a))  # BGRA
        rows.append(bytes(row))
    xor = b"".join(rows)

    # AND 掩码：1bpp，每行按 4 字节对齐，全 0（透明度交给 alpha 通道）
    stride = ((w + 31) // 32) * 4
    and_mask = b"\x00" * (stride * h)
    return header + xor + and_mask


def _png_payload(img) -> bytes:
    import PyQt5.QtCore as QtCore

    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def build_ico(out_path: Path) -> list[tuple[int, int, str]]:
    """生成多尺寸 .ico，返回 [(尺寸, 字节数, 负载格式)] 供调用方核对。"""
    entries = []
    for size in SIZES:
        img = draw(size)
        if size >= PNG_FROM:
            entries.append((size, _png_payload(img), "png"))
        else:
            entries.append((size, _bmp_payload(img), "bmp"))

    # 目录项：宽高各 1 字节（256 记作 0）、颜色数 0、保留 0、平面 1、位深 32、
    # 负载长度 4 字节、偏移 4 字节。
    count = len(entries)
    offset = 6 + 16 * count
    directory = bytearray()
    for size, payload, _fmt in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset
        )
        offset += len(payload)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, count))  # 保留 0、类型 1(ICO)、张数
        fh.write(directory)
        for _size, payload, _fmt in entries:
            fh.write(payload)

    return [(s, len(p), f) for s, p, f in entries]


def verify_ico(path: Path) -> bool:
    """自查 .ico 是不是真的能用。

    不能依赖 Qt 读多尺寸：Qt 的 ICO 插件只会给你一张图（读过第一张就
    jumpToNextImage() == False），拿它验多尺寸必然假报失败。所以这里走两条：

    1. 自己按 ICO 结构解析目录项，核对每张图的尺寸/偏移/长度是否自洽，
       BMP 负载还要核对 BITMAPINFOHEADER 的字段。
    2. 让 **Windows 自己** 按 16/32/48/256 各要一次图标（LoadImageW）。这是
       外壳真正取图标走的那条路，比任何 Python 侧的解码都更接近「能不能用」。
    """
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    print(f"  文件头 : reserved={reserved} type={kind}(1=ICO) 张数={count}")
    ok = reserved == 0 and kind == 1 and count == len(SIZES)

    seen = []
    for i in range(count):
        dim, _dim2, _cc, _res, planes, bits, length, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + 16 * i
        )
        size = dim or 256
        seen.append(size)
        payload = raw[offset : offset + length]
        good = (
            len(payload) == length
            and offset + length <= len(raw)
            and planes == 1
            and bits == 32
        )
        if payload[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = "png"
            img = QImage.fromData(payload, "PNG")
            good = good and not img.isNull() and img.width() == size
        else:
            fmt = "bmp"
            (bisize, biw, bih, _pl, _bc, _comp) = struct.unpack_from("<IiiHHI", payload, 0)
            stride = ((size + 31) // 32) * 4
            want = 40 + size * size * 4 + stride * size
            good = good and bisize == 40 and biw == size and bih == size * 2 and len(payload) == want
        ok = ok and good
        if not good:
            print(f"  ✗ {size}×{size} 负载不自洽（{fmt}, {length} 字节）")
    print(f"  目录里的尺寸 : {seen}")
    ok = ok and seen == list(SIZES)

    # 让 Windows 自己取一次
    print("  Windows LoadImageW 逐个尺寸取图：")
    import ctypes

    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    ctypes.windll.user32.LoadImageW.restype = ctypes.c_void_p
    ctypes.windll.user32.LoadImageW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    for size in (16, 32, 48, 256):
        h = ctypes.windll.user32.LoadImageW(
            None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE
        )
        print(f"    {size:>3}×{size:<3} {'✓ 拿到句柄' if h else '✗ 失败'} (h={h})")
        ok = ok and bool(h)
        if h:
            ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(h))

    print("  ✓ 结构与外壳加载都通过" if ok else "  ✗ 有问题")
    return ok


def main(argv: list[str]) -> int:
    default = Path(__file__).resolve().parent.parent / "assets" / "minireader.ico"
    out = Path(argv[1]).resolve() if len(argv) > 1 else default

    # 起一个 QApplication：QImage/QPainter 在部分平台上没有 QGuiApplication 会警告。
    # 用 offscreen，避免生成图标时闪一个窗口。
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    rows = build_ico(out)
    total = out.stat().st_size
    print(f"已写出 {out}")
    print(f"  总大小 {total:,} 字节（{total / 1024:.1f} KB）")
    for size, nbytes, fmt in rows:
        print(f"    {size:>3}×{size:<3} {fmt:>3}  {nbytes:>7,} 字节")

    print("  自查：")
    ok = verify_ico(out)
    del app
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
