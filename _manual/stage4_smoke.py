r"""模式 B 冒烟：真实事件投递下，接线是否成立。

比单测更接近真实：用 QTest 投递真滚轮/真点击/真滚动条移动，而不是直接调方法。
这里验的是"接线对不对"，细节边界由 tests/test_stage4_scroll.py 守。

跑法： py -3.12 _manual\stage4_smoke.py
"""

from __future__ import annotations

import os
import struct
import sys
import zipfile
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import BINDING, QApplication, QPoint, QPointF, QWheelEvent, Qt  # noqa: E402

_app = QApplication.instance() or QApplication([])

# `QtTest` 必须从**当前生效的绑定**取，不能写死 PyQt5：`minireader.qt` 会优先选
# PySide6，而 PySide6 的 `QTest.mouseClick` 不认 PyQt5 的控件类型（实测报
# "argument 1 has unexpected type 'PageView'"）。
QTest = __import__(f"{BINDING}.QtTest", fromlist=["QTest"]).QTest

from minireader.core.opener import open_book  # noqa: E402
from minireader.ui.comic_reader import IMPLEMENTED_MODES, ComicReader  # noqa: E402

SIZES = [(120, 180), (180, 120), (150, 150), (90, 270), (140, 140), (200, 100)]


def make_png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\x44" * (w * 3) for _ in range(h))

    def c(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + c(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + c(b"IDAT", zlib.compress(raw)) + c(b"IEND", b""))


def slot_at(reader, index: int = 0):
    """The page slot the pointer would be over - where a real wheel lands."""
    slot = reader._strip.slot(index)
    return slot if slot is not None else reader._strip


def wheel(widget, notches: int) -> None:
    """Send a wheel event to `widget` **and let it bubble up by hand**.

    Qt delivers a wheel event only to the widget under the pointer, so this
    mirrors what the real app does: the event starts at the page slot and walks
    up until something accepts it. Sending it to the slot alone would test
    nothing, because a slot does not handle wheels.
    """
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    ev = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, 120 * notches), Qt.NoButton,
                     Qt.NoModifier, Qt.NoScrollPhase, False)
    _app.sendEvent(widget, ev)
    _app.processEvents()


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    return ok


def main() -> None:
    path = os.path.join(os.environ["TEMP"], "mr-stage4-smoke.cbz")
    with zipfile.ZipFile(path, "w") as zf:
        for i, (w, h) in enumerate(SIZES):
            zf.writestr(f"page_{i + 1}.png", make_png(w, h))
    book = open_book(path)
    results = []

    print(f"IMPLEMENTED_MODES = {IMPLEMENTED_MODES}")

    reader = ComicReader()
    reader.resize(600, 400)
    reader.show()
    reader.show_book(book, index=0)
    _app.processEvents()

    print("\n-- 模式 A 基线")
    results.append(check("mode", reader.mode, "single"))
    results.append(check("scrollbar() 是 None", reader.scrollbar(), None))
    results.append(check("viewport 是 page_view", reader.viewport() is reader._page_view, True))

    print("\n-- 切进模式 B")
    reader.set_mode("scroll")
    _app.processEvents()
    results.append(check("mode", reader.mode, "scroll"))
    results.append(check("槽位数", reader._strip.count(), len(SIZES)))
    results.append(check("viewport 是 strip", reader.viewport() is reader._strip, True))
    results.append(check("scrollbar() 是真条", reader.scrollbar() is reader._strip.verticalScrollBar(), True))
    results.append(check("每页尺寸已探到", book.sizes_known(), True))
    results.append(check("占位块高度非零", all(reader._strip.slot_height(i) > 0 for i in range(len(SIZES))), True))

    total_h = reader._strip.total_height()
    print(f"      total_height = {total_h}")

    print("\n-- 滚轮滚动（不翻页）")
    before_index, before_bar = reader.index, reader.scrollbar().value()
    wheel(slot_at(reader), -3)
    after_index, after_bar = reader.index, reader.scrollbar().value()
    print(f"      index {before_index} -> {after_index}, bar {before_bar} -> {after_bar}")
    results.append(check("滚动条动了", after_bar > before_bar, True))

    print("\n-- 滚到底：边界提示")
    seen: list[str] = []
    reader.notice.connect(seen.append)
    bar = reader.scrollbar()
    bar.setValue(bar.maximum())
    _app.processEvents()
    wheel(slot_at(reader), -1)
    results.append(check("到底发了一次提示", len(seen), 1))
    print(f"      notice = {seen[0] if seen else '(无)'}")
    wheel(slot_at(reader), -1)
    results.append(check("再滚一格不重复", len(seen), 1))

    print("\n-- 滚到顶：边界提示")
    bar.setValue(bar.minimum())
    _app.processEvents()
    seen.clear()
    wheel(slot_at(reader), 1)
    results.append(check("到顶发了一次提示", len(seen), 1))
    print(f"      notice = {seen[0] if seen else '(无)'}")

    print("\n-- 进度反算：视口中心线所在的页")
    # 不是"滚到第 3 页顶就报第 3 页"：规格说的是**视口中心线**所在的页。
    # 所以把某页顶部对齐视口顶之后，中心线可能已经落进下一页——这是设计，
    # 不是 bug。期望值必须按**滚动条实际落到的值**算：靠后的页会撞到
    # maximum 而被夹住（第 5 页的 top=720 超出 max=680）。
    for target in (0, 2, 4):
        bar.setValue(reader._strip.page_top(target))
        _app.processEvents()
        actual = bar.value()
        centre = actual + reader._strip.viewport().height() // 2
        want = reader._strip._index_at(centre)
        results.append(check(f"page_top({target}) 时中心页", reader.index, want))
        print(f"      请求 top={reader._strip.page_top(target)} 实际={actual} "
              f"centre={centre} -> 第 {reader.index + 1} 页")
    keep = reader.index

    print("\n-- 切回模式 A，页码保持")
    reader.set_mode("single")
    _app.processEvents()
    results.append(check("mode", reader.mode, "single"))
    results.append(check("页码保持", reader.index, keep))
    results.append(check("scrollbar() 回到 None", reader.scrollbar(), None))

    print("\n-- 点击翻页（模式 B 下仍然成立）")
    reader.set_mode("scroll")
    _app.processEvents()
    slot = reader._strip.slot(reader.index)
    before = reader.index
    QTest.mouseClick(slot, Qt.LeftButton, Qt.NoModifier, QPoint(int(slot.width() * 0.8), 20))
    _app.processEvents()
    print(f"      index {before} -> {reader.index}")
    results.append(check("点右侧翻到下一页", reader.index, before + 1))

    reader.stop()
    os.remove(path)

    print(f"\n==== {sum(results)}/{len(results)} 通过 ====")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
