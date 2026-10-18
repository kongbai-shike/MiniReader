r"""阶段 5 手动确认：横屏双页的实际观感 + 截图。

单测和冒烟脚本验的是"接线对不对"，这里验规格点名的那五条**看得见**的行为：

  1. 横屏打开漫画，切到双页
  2. 日漫从右往左翻（rtl：小页码在右）
  3. 封面单独显示
  4. 接缝水平（两页同高）
  5. 竖屏自动退回单页并提示

顺便把每一步截图存到 `_manual/stage5_shots/`，报告里引用。

跑法： py -3.12 _manual\stage5_manual.py
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

from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

SHOTS = os.path.join(ROOT, "_manual", "stage5_shots")


def make_png(w: int, h: int, tag: bytes = b"") -> bytes:
    """一张纯色 PNG，中间写一个可读的编号（用色块拼，不依赖字体）。"""
    rows = []
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            # 纯色 + 一个简单的"页码条"：靠左的竖条按页号改变灰度。
            value = 40 + (int(tag or b"0") % 7) * 20 if x < w // 8 else 200
            row += bytes((value, 90, 160))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(name: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + name + data
                + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1))
            + chunk(b"IEND", b""))


def make_cbz(path: str, sizes, names=None) -> str:
    if names is None:
        names = [f"page_{i + 1:03d}.png" for i in range(len(sizes))]
    with zipfile.ZipFile(path, "w") as zf:
        for i, (name, (w, h)) in enumerate(zip(names, sizes)):
            zf.writestr(name, make_png(w, h, str(i).encode()))
    return path


def shot(window, name: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    window.grab().save(path, "PNG")
    print(f"      截图 {path}")
    return path


def pump(n: int = 4) -> None:
    for _ in range(n):
        _app.processEvents()


def ok(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")
    return condition


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="mr-stage5-manual-")
    os.environ["APPDATA"] = tmp
    # 8 页竖版，第一页叫 cover.jpg —— 触发"文件名是封面"这条规则。
    comic = make_cbz(
        os.path.join(tmp, "manga.cbz"),
        [(600, 900)] * 8,
        names=["cover.jpg"] + [f"p{i:02d}.png" for i in range(2, 9)],
    )
    # 横版页：用来验"设置才生效"那条。
    wide = make_cbz(os.path.join(tmp, "wide.cbz"), [(1200, 700)] * 8)

    results: list[bool] = []

    print("=== 1. 横屏打开漫画，切到双页 ===")
    settings = Settings(wheel_animation_ms=0)
    window = MiniWindow(settings, ProgressStore.load(), Library.load())
    window._quit_on_close = False
    window.resize(1200, 800)
    window.show()
    pump()
    window.load_path(comic)
    pump()
    reader = window._comic_reader
    results.append(ok("开局是单页", reader.mode == "single"))
    shot(window, "01_single_landscape.png")
    window._comic_reader.set_mode("spread")
    pump()
    results.append(ok("切进双页", reader.mode == "spread"))
    # 不把 ▥ 打进 detail 里：中文控制台是 GBK，那个字符编不出去会直接抛
    # UnicodeEncodeError（实测），而这只是显示问题，不该让整个验收脚本挂掉。
    results.append(ok("按钮文案是双页", window.comic_mode_button.text() == "▥"))
    shot(window, "02_spread_cover.png")

    print("\n=== 2. 封面单独显示 ===")
    pair = reader._spread.pair
    results.append(ok("封面单独占一屏", pair == (0, None), str(pair)))
    left, right = reader._spread.pages_shown()
    results.append(ok("封面在右槽（rtl）", (left, right) == (None, 0), str((left, right))))
    results.append(ok("底栏说第 1 页", window.chapter_label.text() == "第 1/8 页",
                      window.chapter_label.text()))
    results.append(ok("封面页真的有图", reader._spread.right_slot().has_page))

    print("\n=== 3. 日漫从右往左翻（rtl：小页码在右） ===")
    for step in range(3):
        window.step_chapter(1)
        pump()
        pair = reader._spread.pair
        left, right = reader._spread.pages_shown()
        print(f"      跨页 {pair}: 左={left} 右={right} 底栏={window.chapter_label.text()}")
        results.append(ok(f"第 {step + 1} 次翻页 rtl 小页码在右", right == pair[0]))
        results.append(ok(f"第 {step + 1} 次翻页两页同屏", pair[1] is not None))
    shot(window, "03_spread_rtl.png")

    print("\n=== 4. 接缝水平（两页同高） ===")
    lh = reader._spread.left_slot().height()
    rh = reader._spread.right_slot().height()
    print(f"      左页高 {lh} px，右页高 {rh} px")
    results.append(ok("两页高度相同", lh == rh))
    results.append(ok("高度不超过视口", lh <= reader._spread.height()))

    print("\n=== 4b. 切到左綴じ，左右对调 ===")
    before = reader._spread.pages_shown()
    reader.cycle_binding()
    pump()
    after = reader._spread.pages_shown()
    print(f"      rtl {before} -> ltr {after}")
    results.append(ok("左右对调", (before, after) == ((before[0], before[1]), (before[1], before[0]))))
    shot(window, "04_spread_ltr.png")
    reader.cycle_binding()
    pump()

    print("\n=== 4c. gutter 可调 ===")
    reader.set_gutter(24)
    pump()
    results.append(ok("间距落到布局", reader._spread._row.spacing() == 24,
                      str(reader._spread._row.spacing())))
    shot(window, "05_spread_gutter24.png")
    reader.set_gutter(0)
    pump()

    print("\n=== 5. 竖屏自动退回单页并提示 ===")
    notes: list[str] = []
    reader.notice.connect(notes.append)
    window.resize(500, 900)
    pump()
    results.append(ok("模式退回单页", reader.mode == "single", reader.mode))
    results.append(ok("给出了提示", any("太窄" in n for n in notes), str(notes)))
    results.append(ok("页码没丢", window.chapter_index == reader.index))
    shot(window, "06_portrait_fallback.png")

    print("\n=== 5b. 竖屏下主动切双页也被拒 ===")
    notes.clear()
    reader.set_mode("spread")
    pump()
    results.append(ok("仍是单页", reader.mode == "single"))
    results.append(ok("同样给了提示", any("太窄" in n for n in notes)))

    print("\n=== 6. 横版书 + 关闭封面开关：设置才生效 ===")
    settings2 = Settings(wheel_animation_ms=0)
    settings2.comic_cover_alone = False
    window2 = MiniWindow(settings2, ProgressStore.load(), Library.load())
    window2._quit_on_close = False
    window2.resize(1200, 800)
    window2.show()
    pump()
    window2.load_path(wide)
    pump()
    r2 = window2._comic_reader
    r2.set_mode("spread")
    pump()
    results.append(ok("横版书不判成封面", r2.cover_alone is False))
    results.append(ok("于是首页就配对", r2._spread.pair == (0, 1), str(r2._spread.pair)))
    shot(window2, "07_wide_pair.png")

    for w in (window, window2):
        w._quit_on_close = False
        w.close()
    pump()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== {sum(results)}/{len(results)} 通过 ====")
    print(f"截图目录：{SHOTS}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
