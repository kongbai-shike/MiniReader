r"""阶段 4 手动确认：300 页 CBZ 上的真实行为。

单测用的是 12 页的小夹具，验的是"接线对不对"。这个脚本用 **300 页**的真规模，
验规格里点名的那五条：

  1. 滚动流畅（滚动一次的总耗时）
  2. 不抖（滚到底之前/之后，每一页的槽位高度和滚动条范围都不变）
  3. 自动滚动可用（AutoScroller 真的把条推下去）
  4. 进度反算正确（视口中心页 == 底栏页码）
  5. 滚到底看到提示、切回模式 A 页码正确

跑法： py -3.12 _manual\stage4_manual_300.py
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

from minireader.qt import QApplication, QPoint, QPointF, QWheelEvent, Qt  # noqa: E402

_app = QApplication.instance() or QApplication([])

from minireader.core.opener import open_book  # noqa: E402
from minireader.ui.comic_reader import ComicReader, boundary_notice  # noqa: E402

PAGES = 300
#: 尺寸各不相同，这样"高度是按宽高比算的"能被看见，而不是碰巧都一样。
SIZES = [(600 + (i % 7) * 30, 900 + (i % 5) * 40) for i in range(PAGES)]


def make_png(w: int, h: int) -> bytes:
    """纯色 PNG：够大能测出布局成本，又不必真的存 300 张复杂图。"""
    raw = b"".join(b"\x00" + bytes((i * 7 % 256, 90, 160)) * w for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def wheel_on(widget, notches: int) -> None:
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    ev = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, 120 * notches), Qt.NoButton,
                     Qt.NoModifier, Qt.NoScrollPhase, False)
    _app.sendEvent(widget, ev)
    _app.processEvents()


def ok(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")
    return condition


def main() -> None:
    target = os.path.join(os.environ["TEMP"], f"mr-stage4-manual-{PAGES}.cbz")
    if not os.path.exists(target):
        print(f"造 {PAGES} 页夹具…")
        with zipfile.ZipFile(target, "w") as zf:
            for i, (w, h) in enumerate(SIZES):
                zf.writestr(f"page_{i + 1:04d}.png", make_png(w, h))
    print(f"夹具 {target}  ({os.path.getsize(target) / 1048576:.1f} MB)\n")

    book = open_book(target)
    reader = ComicReader()
    reader.resize(900, 700)
    reader.show()
    reader.show_book(book, index=0)
    _app.processEvents()

    results: list[bool] = []
    notices: list[str] = []
    reader.notice.connect(notices.append)

    print("=== 1. 切进模式 B 的一次性成本 ===")
    start = time.perf_counter()
    reader.set_mode("scroll")
    _app.processEvents()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    strip = reader._strip
    print(f"      set_mode('scroll') 用时 {elapsed_ms:.1f} ms")
    print(f"      槽位数 {strip.count()}，总高 {strip.total_height()} px")
    print(f"      已解码 {len(reader._loaded)} 页（懒加载：远小于 {PAGES}）")
    results.append(ok("槽位数等于页数", strip.count() == PAGES))
    results.append(ok("每页都有真实高度", all(strip.slot_height(i) > 0 for i in range(PAGES))))
    results.append(ok("只解码了视口附近的页", len(reader._loaded) < 30,
                      f"实际 {len(reader._loaded)} 页"))
    results.append(ok("打开耗时在可接受范围（< 1500ms）", elapsed_ms < 1500,
                      f"{elapsed_ms:.0f} ms"))

    print("\n=== 2. 不抖：滚动前后范围与高度恒定 ===")
    heights_before = [strip.slot_height(i) for i in range(PAGES)]
    max_before = strip.verticalScrollBar().maximum()
    total_before = strip.total_height()

    # 连续滚到书末，每一页都过一遍——这正是"边滚边补尺寸"会露馅的路径。
    scroll_start = time.perf_counter()
    bar = strip.verticalScrollBar()
    for step in range(0, PAGES, 10):
        bar.setValue(strip.page_top(min(step, PAGES - 1)))
        _app.processEvents()
    _app.processEvents()
    time.sleep(0.15)
    _app.processEvents()
    scroll_ms = (time.perf_counter() - scroll_start) * 1000.0

    heights_after = [strip.slot_height(i) for i in range(PAGES)]
    max_after = strip.verticalScrollBar().maximum()
    results.append(ok("滚动后每页高度一字不变", heights_before == heights_after))
    results.append(ok("滚动条范围没有随解码变化", max_before == max_after,
                      f"{max_before} -> {max_after}"))
    results.append(ok("总高不变", total_before == strip.total_height()))
    print(f"      滚完 300 页共 {scroll_ms:.0f} ms（{scroll_ms / 30:.1f} ms/次跳转）")

    print("\n=== 3. 自动滚动可用 ===")
    from minireader.ui.autoscroll import AutoScroller

    scroller = AutoScroller(strip.verticalScrollBar())
    bar.setValue(0)
    _app.processEvents()
    scroller.set_speed(400)
    scroller.start()
    print(f"      is_running = {scroller.is_running()}")
    # 用假时钟推几拍：真实定时器在 offscreen 下不保证按 40ms 到点。
    clock = [0.0]
    scroller._clock = lambda: clock[0]
    scroller._last = None
    scroller._on_tick()
    for _ in range(10):
        clock[0] += 0.1
        scroller._on_tick()
    moved = bar.value()
    scroller.stop()
    results.append(ok("自动滚动真的推动了滚动条", moved > 0, f"bar = {moved}"))

    print("\n=== 4. 进度反算正确 ===")
    mismatches = 0
    for step in range(0, PAGES, 25):
        index = min(step, PAGES - 1)
        bar.setValue(strip.page_top(index))
        _app.processEvents()
        centre = bar.value() + strip.viewport().height() // 2
        want = strip._index_at(centre)
        if reader.index != want:
            mismatches += 1
            print(f"      第 {index} 页: reader.index={reader.index} 期望={want}")
    results.append(ok("视口中心页 == reader.index（全程一致）", mismatches == 0,
                      f"{mismatches} 处不符"))
    results.append(ok("位置对象报的页码和 index 一致",
                      reader.current_position().index == reader.index,
                      f"第 {reader.index + 1}/{PAGES} 页"))

    print("\n=== 5. 滚到底看到提示 ===")
    notices.clear()
    bar.setValue(bar.maximum())
    _app.processEvents()
    wheel_on(strip.slot(PAGES - 1), -1)
    first = list(notices)
    for _ in range(9):
        wheel_on(strip.slot(PAGES - 1), -1)
    print(f"      第一次提示: {first}")
    print(f"      连滚 10 次后共收到 {len(notices)} 条")
    results.append(ok("到底发了提示", len(first) == 1))
    results.append(ok("提示文案含「最后一页」", bool(first) and "最后一页" in first[0]))
    results.append(ok("连滚不刷屏", len(notices) == 1, f"共 {len(notices)} 条"))

    notices.clear()
    bar.setValue(bar.minimum())
    _app.processEvents()
    wheel_on(strip.slot(0), 1)
    results.append(ok("到顶发了提示", len(notices) == 1 and "第一页" in notices[0],
                      notices[0] if notices else "(无)"))

    print("\n=== 6. 切回模式 A 页码正确 ===")
    bar.setValue(strip.page_top(150))
    _app.processEvents()
    expected = reader.index
    reader.set_mode("single")
    _app.processEvents()
    results.append(ok("模式回到 single", reader.mode == "single"))
    results.append(ok("页码保持", reader.index == expected,
                      f"第 {expected + 1} 页 -> 第 {reader.index + 1} 页"))
    results.append(ok("位置标签正确",
                      reader.current_position().label == f"第 {reader.index + 1}/{PAGES} 页",
                      reader.current_position().label))

    print("\n=== 7. 再切回模式 B 仍然停在同一页 ===")
    reader.set_mode("scroll")
    _app.processEvents()
    strip = reader._strip
    first, last = strip.visible_range()
    results.append(ok("回到模式 B 仍在同一页", reader.index == expected,
                      f"第 {reader.index + 1} 页"))
    results.append(ok("该页真的在屏幕上", first <= expected <= last,
                      f"可见 {first}..{last}"))

    reader.stop()
    os.remove(target)

    print(f"\n==== {sum(results)}/{len(results)} 通过 ====")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
