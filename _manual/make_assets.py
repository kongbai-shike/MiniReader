"""造手动验收素材：一本 TXT、一个带真图的 CBZ、一个图片文件夹、一张单图。

PNG 是手写的（struct + zlib），不依赖 Pillow。每一页是**纯色块**——够用来肉眼确认
"这一页渲染出来了、比例对不对、翻页换没换图"，而且一行的字节可以一次乘出来，
不必逐像素拼（逐像素的版本 600x900 跑了两分钟没跑完）。
"""
import struct
import zipfile
import zlib
from pathlib import Path

OUT = Path(r"C:\Users\pc\Desktop\随手文件\MiniReader\_manual")
OUT.mkdir(exist_ok=True)


def make_png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    """纯色 PNG。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(colour) * width  # filter type 0 + w 个 RGB 三元组
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


# ---- 1. TXT：五章，每章足够长到能滚 ----
parts = []
for i in range(1, 6):
    body = f"这是第{i}章独有的正文内容。" * 60
    parts.append(f"第{i}章 手动验收标题{i}\n{body}")
txt = OUT / "手动验收.txt"
txt.write_text("\n".join(parts), encoding="utf-8")
print(f"TXT  {txt.name}  {txt.stat().st_size} B")

# ---- 2. CBZ：六页真 PNG，宽高比各不相同 ----
sizes = [(600, 900), (900, 600), (700, 1000), (1000, 700), (800, 800), (500, 1200)]
colours = [(200, 60, 60), (60, 180, 90), (70, 110, 220), (230, 180, 50), (160, 80, 200), (40, 170, 190)]
cbz = OUT / "手动验收.cbz"
with zipfile.ZipFile(cbz, "w", zipfile.ZIP_DEFLATED) as zf:
    for i, ((w, h), c) in enumerate(zip(sizes, colours), start=1):
        zf.writestr(f"page_{i:02d}.png", make_png(w, h, c))
print(f"CBZ  {cbz.name}  {cbz.stat().st_size} B  ({len(sizes)} 页)")

# ---- 3. 图片文件夹 ----
folder = OUT / "手动验收文件夹"
folder.mkdir(exist_ok=True)
for i, ((w, h), c) in enumerate(zip(sizes[:4], colours[:4]), start=1):
    (folder / f"{i:02d}.png").write_bytes(make_png(w, h, c))
print(f"DIR  {folder.name}  ({len(list(folder.iterdir()))} 张)")

# ---- 4. 单张图片 ----
single = OUT / "手动验收单图.png"
single.write_bytes(make_png(900, 1200, (120, 140, 160)))
print(f"IMG  {single.name}  {single.stat().st_size} B")
