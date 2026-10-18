r"""阶段 5 冒烟：模式 C（双页）在窗口层是否成立。

单测（tests/test_stage5_spread.py）守细节；这个脚本用**真实事件投递 + 真实窗口**
验"接线对不对"：模式切换、按钮、自动滚动闸门、设置推送、竖屏退化。

跑法： py -3.12 _manual\stage5_smoke.py
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from minireader.storage import Settings  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402


def make_png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\x66" * (w * 3) for _ in range(h))

    def c(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + c(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + c(b"IDAT", zlib.compress(raw)) + c(b"IEND", b""))


def make_cbz(path: str, names, sizes) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        for name, (w, h) in zip(names, sizes):
            zf.writestr(name, make_png(w, h))
    return path


def make_window(settings, path=None, size=(1200, 800)):
    """A window the way the app builds one, with real stores in the temp %APPDATA%."""
    from minireader.storage import Library, ProgressStore

    window = MiniWindow(settings, ProgressStore.load(), Library.load())
    window._quit_on_close = False
    window.resize(*size)
    window.show()
    pump()
    if path is not None:
        window.load_path(path)
        pump()
    return window
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    return ok


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    return ok


def pump() -> None:
    for _ in range(3):
        _app.processEvents()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="mr-stage5-smoke-")
    os.environ["APPDATA"] = tmp
    # 6 张竖版页，第一张叫 cover.jpg ——触发"文件名含 cover"这条规则。
    cbz = make_cbz(os.path.join(tmp, "c.cbz"),
                   [f"page_{i}.png" for i in range(6)],
                   [(600, 900)] * 6)
    cover = make_cbz(os.path.join(tmp, "cover.cbz"),
                     ["cover.jpg"] + [f"p{i}.png" for i in range(1, 6)],
                     [(600, 900)] * 6)

    results: list[bool] = []
    settings = Settings(wheel_animation_ms=0)

    print("=== 1. 横屏窗口，默认模式是 spread ===")
    settings.comic_default_mode = "spread"
    w = make_window(settings, cbz)
    reader = w._comic_reader
    results.append(check("模式切进 spread", reader.mode, "spread"))
    results.append(check("viewport 是 SpreadView", w.reader.viewport() is reader._spread, True))
    results.append(check("第一屏是封面（单独）", reader._spread.pair, (0, None)))
    results.append(check("左槽空、右槽是第 1 页（rtl）", reader._spread.pages_shown(), (None, 0)))

    print("\n=== 2. 翻页：一个跨页 = 两页 ===")
    seen_labels = []
    seen_pairs = []
    for _ in range(4):
        w.step_chapter(1)
        pump()
        seen_labels.append(w.chapter_label.text())
        seen_pairs.append(reader._spread.pair)
    print(f"      跨页：{seen_pairs}")
    print(f"      底栏：{seen_labels}")
    # 从封面起翻 4 次。封面单独一屏，之后每步两页：[0] -> (1,2) -> (3,4) -> (5)
    # -> 第 4 步已经在落单页上，属于边界提示。
    results.append(check("第 1 步是 (1,2)", seen_pairs[0], (1, 2)))
    results.append(check("第 2 步是 (3,4)", seen_pairs[1], (3, 4)))
    results.append(check("第 3 步是落单页 (5)", seen_pairs[2], (5, None)))
    results.append(check("第 4 步停住", seen_pairs[3], (5, None)))
    results.append(check("第 1 步标签", seen_labels[0], "第 2-3/6 页"))
    results.append(check("第 3 步标签（落单页）", seen_labels[2], "第 6/6 页"))
    results.append(check("第 4 步标签不变", seen_labels[3], "第 6/6 页"))

    print("\n=== 3. 自动滚动在模式 C 下不可用 ===")
    warnings: list[str] = []
    original_warning = w.set_warning
    w.set_warning = lambda text: warnings.append(text)  # type: ignore[method-assign]
    w.toggle_autoscroll()
    pump()
    results.append(check("没有滚动条", reader.scrollbar(), None))
    results.append(check("自动滚动没启动", w.autoscroller.is_running(), False))
    results.append(check("给出了解释", any("自动滚动" in t for t in warnings), True))
    w.set_warning = original_warning  # type: ignore[method-assign]

    print("\n=== 4. 装订方向切换 ===")
    # 走到一个**落单页**再比：落单页在 rtl 里占右槽、ltr 里占左槽，最容易看出差别。
    w.step_chapter(1)
    pump()
    last_pair = reader._spread.pair
    results.append(check("停在落单页", last_pair[1], None))
    before = reader._spread.pages_shown()
    reader.cycle_binding()
    pump()
    after = reader._spread.pages_shown()
    print(f"      落单页 {last_pair}: rtl {before} -> ltr {after}")
    results.append(check("落单页从右槽换到左槽", (before, after), ((None, 5), (5, None))))
    reader.set_binding("rtl")
    pump()

    print("\n=== 4b. 普通跨页的左右也对调 ===")
    w.step_chapter(-2)
    pump()
    mid = reader._spread.pair
    rtl_order = reader._spread.pages_shown()
    reader.set_binding("ltr")
    pump()
    ltr_order = reader._spread.pages_shown()
    print(f"      跨页 {mid}: rtl {rtl_order} -> ltr {ltr_order}")
    results.append(check("rtl 小页码在右", rtl_order, (mid[1], mid[0])))
    results.append(check("ltr 小页码在左", ltr_order, (mid[0], mid[1])))
    reader.set_binding("rtl")
    pump()

    print("\n=== 5. 封面判断：文件名命中优先 ===")
    w.load_path(cover)
    pump()
    results.append(check("cover.jpg 单独显示", reader._spread.pair, (0, None)))

    print("\n=== 6. 竖屏窗口：set_mode('spread') 被拒 ===")
    narrow = make_window(Settings(wheel_animation_ms=0), cbz, size=(500, 900))
    notes: list[str] = []
    narrow._comic_reader.notice.connect(notes.append)
    narrow._comic_reader.set_mode("spread")
    pump()
    results.append(check("模式仍是 single", narrow._comic_reader.mode, "single"))
    results.append(check("给出了提示", any("太窄" in n for n in notes), True))
    print(f"      notice = {notes}")

    print("\n=== 7. 横屏拖窄：自动退回单页 ===")
    wide = make_window(Settings(wheel_animation_ms=0), cbz)
    wide._comic_reader.set_mode("spread")
    pump()
    results.append(check("起初是 spread", wide._comic_reader.mode, "spread"))
    notes2: list[str] = []
    wide._comic_reader.notice.connect(notes2.append)
    wide.resize(480, 900)
    pump()
    results.append(check("拖窄后回到 single", wide._comic_reader.mode, "single"))
    results.append(check("给出了提示", any("太窄" in n for n in notes2), True))
    print(f"      notice = {notes2}")

    print("\n=== 8. 切回模式 A / 模式 B 不崩，页码保持 ===")
    wide.resize(1200, 800)
    pump()
    wide._comic_reader.set_mode("spread")
    pump()
    wide.goto_chapter(3)
    pump()
    keep = wide.chapter_index
    wide.cycle_comic_mode()
    pump()
    results.append(check("切到 single", wide._comic_reader.mode, "single"))
    results.append(check("页码保持", wide.chapter_index, keep))
    wide._comic_reader.set_mode("spread")
    pump()
    results.append(check("再切回 spread", wide._comic_reader.mode, "spread"))
    results.append(check("仍在同一页", wide.chapter_index, keep))
    print(f"      页码 {keep}，跨页 {wide._comic_reader._spread.pair}")

    print("\n=== 9. 进度持久化：page 是跨页的第一页 ===")
    wide.persist_progress()
    saved = w.progress if hasattr(w, "progress") else None
    print(f"      chapter_index = {wide.chapter_index}")

    print("\n=== 10. 设置推送：gutter / binding / cover_alone ===")
    settings2 = Settings(wheel_animation_ms=0)
    settings2.comic_gutter = 16
    settings2.comic_binding = "ltr"
    settings2.comic_cover_alone = False
    w2 = make_window(settings2, cbz)
    r2 = w2._comic_reader
    results.append(check("gutter 推下去了", r2.gutter, 16))
    results.append(check("binding 推下去了", r2.binding, "ltr"))
    # `cover_alone` 是**用户设置的默认值**，不是最终答案：自动判型（前三页都是
    # 竖版）优先于它。这条断言的是"设置被记住了"，而"是不是真单独显示封面"由
    # 规则的优先级决定，见下一条。
    results.append(check("设置被记住", r2._cover_setting, False))
    print(f"      实际 cover_alone = {r2.cover_alone}（自动判型优先于设置）")
    # 自动判型说"是封面"（本夹具 6 页全是竖版），所以即使设置关掉也单独显示。
    results.append(check("自动判型优先于设置", r2._spread.pair, (0, None)))

    print("\n=== 10b. 自动判型不命中时，设置才生效 ===")
    # 横版页：形状规则不命中、文件名也不命中，于是回落到用户设置。
    wide_pages = make_cbz(os.path.join(tmp, "wide.cbz"),
                          [f"w{i}.png" for i in range(6)],
                          [(1200, 700)] * 6)
    settings3 = Settings(wheel_animation_ms=0)
    settings3.comic_default_mode = "spread"
    settings3.comic_cover_alone = False
    w3 = make_window(settings3, wide_pages)
    r3 = w3._comic_reader
    results.append(check("横版书不判为封面", r3.cover_alone, False))
    results.append(check("于是封面不单独显示", r3._spread.pair, (0, 1)))
    settings4 = Settings(wheel_animation_ms=0)
    settings4.comic_default_mode = "spread"
    settings4.comic_cover_alone = True
    w4 = make_window(settings4, wide_pages)
    results.append(check("开关打开就单独显示", w4._comic_reader._spread.pair, (0, None)))

    for window in (w, narrow, wide, w2, w3, w4):
        window._quit_on_close = False
        window.close()
    pump()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== {sum(results)}/{len(results)} 通过 ====")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
