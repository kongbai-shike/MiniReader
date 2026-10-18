r"""阶段 4 实测：把一本 300 页 CBZ 的每一页尺寸都问出来要多久。

这个数字决定了模式 B 的占位块高度策略。规格要求"任何页面在加载前都要有真实
width/height"，而代价就是打开时得先把 300 页的文件头都读一遍。问题是这笔钱
一次付清（打开时卡一下）还是滚到哪儿付哪儿（滚动条边滚边长、拇指被推着走）。

量三件事：

  1. 串行 `comic_cache.probe_size()`：每页重开一次 ZipFile（阶段 2 的实现）
  2. 批量 `comic_cache.probe_sizes()`：**仓库里真正用的那个**（一次 ZipFile）
  3. `StripView.set_pages()`：造 300 个槽位本身的成本

再拆一次"读"和"问"，看时间花在容器上还是 QImageReader 上——这个拆分决定了
优化该往哪儿做。

跑法： py -3.12 _manual\stage4_probe_bench.py
"""

from __future__ import annotations

import os
import struct
import sys
import time
import zipfile
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from minireader.core import archives  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.ui import comic_cache  # noqa: E402
from minireader.ui.strip_view import StripView  # noqa: E402

PAGES = 300
LIMIT = 64 * 1024


def make_png(width: int, height: int) -> bytes:
    """一张**不可压缩**的 PNG：让 64 KB 的文件头读取真的读满。

    纯色 PNG 会被压到几百字节，`read(64KB)` 立刻返回——那样量的是"打开 zip
    成员"而不是真实漫画页的成本。
    """
    raw = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def timed(label: str, fn, repeat: int = 3) -> float:
    """跑 `repeat` 次取最好成绩（第一轮包含冷启动）。"""
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - start) * 1000.0
        best = elapsed if best is None else min(best, elapsed)
    print(f"{label:<48} {best:8.1f} ms")
    return best


def main() -> None:
    target = os.path.join(os.environ["TEMP"], f"mr-stage4-bench-{PAGES}.cbz")
    if not os.path.exists(target):
        print(f"造夹具：{PAGES} 页，每页约 65 KB…")
        with zipfile.ZipFile(target, "w") as zf:
            for i in range(PAGES):
                zf.writestr(f"page_{i + 1:04d}.png", make_png(120, 180))
    size_mb = os.path.getsize(target) / (1024.0 * 1024.0)
    print(f"夹具：{target}")
    print(f"       {PAGES} 页 / {size_mb:.1f} MB\n")

    book = open_book(target)
    refs = [page.ref for page in book.pages]
    print(f"页数确认：{len(book.pages)}，打开后 sizes_known={book.sizes_known()}\n")

    # 预热：第一次 QImageReader 调用会把 Qt 的图片插件拉起来，不该算进任何一边。
    comic_cache.probe_size(book, 0)

    print("---- 把 300 页尺寸全部问出来 ----")
    t_serial = timed(
        "1. 串行 probe_size（每页重开 ZipFile）",
        lambda: [comic_cache.probe_size(book, i) for i in range(len(book.pages))],
    )
    t_batch = timed(
        "2. 批量 probe_sizes（仓库里真正用的）",
        lambda: comic_cache.probe_sizes(book, range(len(book.pages))),
    )

    # 读 / 问 拆分：把时间归因到容器还是 QImageReader。
    t_read = timed("   其中：只批量读文件头（不问尺寸）",
                   lambda: archives.read_member_heads(target, refs, LIMIT))
    heads = archives.read_member_heads(target, refs, LIMIT)
    t_ask = timed("   其中：只逐页问尺寸（头已在内存里）",
                  lambda: [comic_cache.size_from_head(h) for h in heads])

    print()
    print(f"批量比串行快 {t_serial / t_batch:.2f}×")
    print(f"读 / 问 拆分：{t_read:.1f} ms 读容器 + {t_ask:.1f} ms 问 QImageReader")

    # 正确性：两条路必须给出完全一样的答案，否则占位块高度会错。
    serial = {i: comic_cache.probe_size(book, i) for i in range(len(book.pages))}
    batch = comic_cache.probe_sizes(book, range(len(book.pages)))
    print(f"两种做法尺寸完全一致：{serial == batch}")

    print()
    print("---- 造 300 个占位槽位 ----")
    strip = StripView()
    strip.resize(900, 700)
    strip.show()
    _app.processEvents()
    sizes = [batch[i] for i in range(PAGES)]
    timed("3. StripView.set_pages(300 个槽位)", lambda: strip.set_pages(sizes))
    print(f"   总高 total_height = {strip.total_height()} px")
    print(f"   第一个槽位高 = {strip.slot_height(0)} px "
          f"（viewport {strip.viewport().width()}x{strip.viewport().height()}）")

    print()
    print("---- 合计：切进模式 B 的一次性成本 ----")
    print(f"   串行探测 + 布局：约 {t_serial:.0f} ms 探测（规格关心的那个数字）")
    print(f"   批量探测 + 布局：约 {t_batch:.0f} ms 探测（实际实现的数字）")
    print(f"   实测 set_mode('scroll') 端到端见 stage4_manual_300.py")

    strip.stop()
    strip.deleteLater()


if __name__ == "__main__":
    main()
