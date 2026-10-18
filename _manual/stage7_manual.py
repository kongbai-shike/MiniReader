r"""阶段 7 手动确认：固定版式 EPUB 的三条用户路径 + 截图。

单测验的是"判定对不对"，这里验规格点名的那三条**看得见**的行为：

  1. 一本真实的固定版式漫画 EPUB 能逐页翻
  2. 装订方向自动跟随 page-progression-direction
  3. reflowable EPUB 行为与阶段 6 一致（仍然是文字书，能读正文）

和第 6 阶段的手动脚本不同，这个必须走 `minireader.ui`：第 1 条要的是"屏幕上真的
翻得动"，只有真的 `MiniWindow` 能回答。所以它照 `stage5_manual.py` 的写法——离屏
平台 + 真窗口 + 截图。

样本是**手搓的真 EPUB**（不是假 zip）：真的 PNG 字节，让 `QImageReader` 能解码，
这样"这一页有图"才不是空断言。手搓而不是找真书，是为了让 `page-progression-direction`
这类元数据能被精确控制——手边没有三本分别写了 rtl / ltr / 什么都没写的日漫。

跑法： py -3.12 _manual\stage7_manual.py
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

from minireader.core.comic import open_epub_fixed  # noqa: E402
from minireader.core.epub import LAYOUT_PRE_PAGINATED, epub_layout  # noqa: E402
from minireader.core.formats import KIND_COMIC, KIND_TEXT  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

SHOTS = os.path.join(ROOT, "_manual", "stage7_shots")

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def pump(n: int = 4) -> None:
    for _ in range(n):
        _app.processEvents()


def shot(window, name: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    window.grab().save(path, "PNG")
    print(f"      截图 {path}")
    return path


# ---------------------------------------------------------------------------
# 手搓 PNG：真的能被 QImageReader 解码（假字节只够验核心层，验不了 UI）
# ---------------------------------------------------------------------------


def make_png(w: int, h: int, tag: int = 0) -> bytes:
    """一张纯色 PNG，左边一条按页号变灰的竖条，方便在截图里认页。"""
    rows = []
    for _y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            value = 40 + (tag % 7) * 25 if x < w // 8 else 205
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


# ---------------------------------------------------------------------------
# 手搓 EPUB
# ---------------------------------------------------------------------------

CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0"'
    ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="OEBPS/content.opf"'
    ' media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)


def page_xhtml(*hrefs: str) -> str:
    """一页的 XHTML。一个 href 用 `<img>`，多个用 SVG 拼版（真实书里的写法）。"""
    if len(hrefs) == 1:
        body = f'<img src="{hrefs[0]}"/>'
    else:
        inner = "".join(f'<image xlink:href="{h}" width="600" height="450"/>' for h in hrefs)
        body = (f'<svg xmlns="http://www.w3.org/2000/svg"'
                f' xmlns:xlink="http://www.w3.org/1999/xlink"'
                f' width="600" height="{450 * len(hrefs)}">{inner}</svg>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        "  <head><title>page</title></head>\n"
        f"  <body>{body}</body>\n"
        "</html>\n"
    )


def build_epub(
    path: str,
    *,
    pages: list[tuple[str, int, int]],
    meta: str = "",
    progression: str = "",
    text_body: str = "",
    one_page: bool = False,
) -> str:
    """写一个 EPUB。`pages` 是 [(页名, 宽, 高)]；`text_body` 非空时写成流式书。

    流式书没有固定版式的 XHTML 页，所以两种形态共用同一个写函数、靠参数分叉——
    这正好也是被测代码的处境（同一个扩展名，两种形态）。

    `one_page=True` 把所有图片塞进**同一个** XHTML（多图拼版），否则一图一页。
    """
    items = []
    spine = []
    files: dict[str, str | bytes] = {"mimetype": "application/epub+zip",
                                     "META-INF/container.xml": CONTAINER}
    # 所有成员名都要带上 `OEBPS/` 前缀：href 是相对 OPF 所在目录解析的，而 OPF 在
    # `OEBPS/` 里。少这一层前缀，OPF 里写的 `images/p1.png` 会指向 zip 根下的
    # `images/p1.png`——`_resolve()` 算得没错，是样本自己写错了位置，症状是
    # 「这本固定版式 EPUB 里没有找到任何页面图片」。
    for i, (name, w, h) in enumerate(pages):
        img = f"images/{name}.png"
        files[f"OEBPS/{img}"] = make_png(w, h, i)
        items.append(f'    <item id="img{i}" href="{img}" media-type="image/png"/>')

    if text_body:
        doc = (f'<?xml version="1.0" encoding="utf-8"?>\n'
               f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
               f'<body>{text_body}</body></html>\n')
        files["OEBPS/text/ch1.xhtml"] = doc
        items.append('    <item id="c1" href="text/ch1.xhtml"'
                     ' media-type="application/xhtml+xml"/>')
        spine.append('    <itemref idref="c1"/>')
    else:
        if one_page:
            hrefs = [f"../images/{name}.png" for name, _w, _h in pages]
            files["OEBPS/text/p0.xhtml"] = page_xhtml(*hrefs)
            items.append('    <item id="p0" href="text/p0.xhtml"'
                         ' media-type="application/xhtml+xml"/>')
            spine.append('    <itemref idref="p0"/>')
        else:
            for i, (name, _w, _h) in enumerate(pages):
                hrefs = [f"../images/{name}.png"]
                files[f"OEBPS/text/p{i}.xhtml"] = page_xhtml(*hrefs)
                items.append(f'    <item id="p{i}" href="text/p{i}.xhtml"'
                             ' media-type="application/xhtml+xml"/>')
                spine.append(f'    <itemref idref="p{i}"/>')

    direction = f' page-progression-direction="{progression}"' if progression else ""
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "    <dc:title>手动确认用的书</dc:title>\n"
        "    <dc:creator>测试作者</dc:creator>\n"
        '    <dc:identifier id="bookid">urn:uuid:stage7</dc:identifier>\n'
        "    <dc:language>ja</dc:language>\n" + meta + "\n"
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(items) + "\n  </manifest>\n"
        f"  <spine{direction}>\n" + "\n".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )
    files["OEBPS/content.opf"] = opf

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), files["mimetype"])
        for name, value in files.items():
            if name == "mimetype":
                continue
            zf.writestr(name, value if isinstance(value, bytes) else value.encode("utf-8"))
    return path


FIXED_META = (
    '    <meta property="rendition:layout">pre-paginated</meta>\n'
    '    <meta property="rendition:spread">auto</meta>'
)


def open_window(path: str, *, width: int = 1200, height: int = 800):
    settings = Settings(wheel_animation_ms=0)
    window = MiniWindow(settings, ProgressStore.load(), Library.load())
    window._quit_on_close = False
    window.resize(width, height)
    window.show()
    pump()
    ok = window.load_path(path)
    pump()
    return window, ok


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mr-stage7-manual-")
    os.environ["APPDATA"] = tmp

    # 8 页竖版日漫，书里声明从右往左。
    rtl_book = build_epub(
        os.path.join(tmp, "rtl.epub"),
        pages=[(f"p{i}", 600, 900) for i in range(1, 9)],
        meta=FIXED_META,
        progression="rtl",
    )
    # 同样的书，但书里什么都没说（绝大多数转换来的 EPUB2 就是这样）。
    silent_book = build_epub(
        os.path.join(tmp, "silent.epub"),
        pages=[(f"p{i}", 600, 900) for i in range(1, 5)],
        meta=FIXED_META,
    )
    # 一页拼版：两张图塞进同一个 XHTML，纵向拼成一页。
    multi_book = build_epub(
        os.path.join(tmp, "multi.epub"),
        pages=[("top", 600, 450), ("bottom", 600, 450)],
        meta=FIXED_META,
        one_page=True,
    )
    # 流式书：同一套判定必须放它走文字那条路。
    reflow_book = build_epub(
        os.path.join(tmp, "reflow.epub"),
        pages=[("unused", 10, 10)],
        text_body="<h1>第一章</h1><p>流式书的正文在这里。</p>",
    )

    # ------------------------------------------------------------------
    print("=== 1. 固定版式 EPUB 被认出来，且走漫画那条路 ===")
    layout, progression, spread = epub_layout(rtl_book)
    check("判定为固定版式", layout == LAYOUT_PRE_PAGINATED, layout)
    check("读出装订方向", progression == "rtl", progression)
    check("读出双页策略", spread == "auto", spread)
    book = open_book(rtl_book)
    check("kind 是漫画", book.kind == KIND_COMIC, book.kind)
    check("页数来自书脊", len(book.pages) == 8, f"{len(book.pages)} 页")
    check("自称固定版式 EPUB", book.source_label == "固定版式 EPUB", book.source_label)
    check("书里声明了右綴じ", book.binding == "rtl", book.binding)
    check("notes 里写了页数", any("共 8 页" in n for n in book.notes), str(book.notes))

    # ------------------------------------------------------------------
    print("\n=== 2. 窗口里真的翻得动（逐页翻 = 屏幕上真的换图） ===")
    window, loaded = open_window(rtl_book)
    check("窗口打开成功", loaded)
    reader = window._comic_reader
    check("落在单页模式", reader.mode == "single", reader.mode)
    check("底栏说第 1/8 页", window.chapter_label.text() == "第 1/8 页",
          window.chapter_label.text())
    # 单页模式下 `_page_view` 就是屏幕上那块区域；`reader.viewport()` 返回的是
    # 挂事件过滤器的那个控件（单页时两者相同），直接用 `_page_view` 更明确。
    check("第一页真的有图（不是占位）", reader._page_view.has_page)
    shot(window, "01_fixed_single.png")

    seen: list[int] = []
    for _step in range(3):
        window.step_chapter(1)
        pump()
        seen.append(reader.index)
        check(f"翻到第 {reader.index + 1} 页且这页有图",
              reader._page_view.has_page, f"index={reader.index}")
    check("三次翻页真的前进了", seen == [1, 2, 3], str(seen))
    check("底栏跟着走", window.chapter_label.text() == "第 4/8 页",
          window.chapter_label.text())
    shot(window, "02_fixed_page4.png")

    # ------------------------------------------------------------------
    print("\n=== 3. 装订方向自动跟随 page-progression-direction ===")
    window.resize(1400, 900)
    pump()
    reader.set_mode("spread")
    pump()
    check("切进双页", reader.mode == "spread", reader.mode)
    check("方向来自书里", reader.binding == "rtl", reader.binding)
    pair = reader._spread.pair
    left, right = reader._spread.pages_shown()
    print(f"      跨页 {pair}: 左={left} 右={right}")
    check("rtl 小页码在右槽", right == pair[0] and pair[0] is not None,
          f"pair={pair} left={left} right={right}")
    check("提示说方向来自书里", "书里声明" in window.comic_binding_button.toolTip(),
          window.comic_binding_button.toolTip())
    check("装订按钮在双页模式可见", window.comic_binding_button.isVisibleTo(window))
    shot(window, "03_fixed_spread_rtl.png")

    before = reader._spread.pages_shown()
    window.cycle_comic_binding()
    pump()
    after = reader._spread.pages_shown()
    check("点一下能推翻书里的声明", (before[0], before[1]) == (after[1], after[0]),
          f"{before} -> {after}")
    check("推翻后提示改说本书手动指定",
          "本书手动指定" in window.comic_binding_button.toolTip(),
          window.comic_binding_button.toolTip())
    shot(window, "04_fixed_spread_overridden.png")

    # ------------------------------------------------------------------
    print("\n=== 4. 书没写方向时，用户的设置说了算 ===")
    window.close()
    pump()
    window2, _ = open_window(silent_book)
    r2 = window2._comic_reader
    check("书没有意见", r2._binding_for(r2._book) == r2.binding_setting,
          f"binding={r2.binding} setting={r2.binding_setting}")
    check("用的是设置里的 rtl", r2.binding == r2.binding_setting == "rtl", r2.binding)
    window2.resize(1400, 900)
    pump()
    r2.set_mode("spread")
    pump()
    check("提示说跟随设置", "跟随设置" in window2.comic_binding_button.toolTip(),
          window2.comic_binding_button.toolTip())
    shot(window2, "05_fixed_no_opinion.png")
    window2.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 5. 多图拼版：一页两张图纵向拼起来 ===")
    multi = open_epub_fixed(multi_book)
    check("只有一页（两张图算一页）", len(multi.pages) == 1, f"{len(multi.pages)} 页")
    check("这一页报了 2 张图", multi.page_image_count(0) == 2,
          str(multi.page_image_count(0)))
    refs = multi.page_image_refs(0)
    check("两张图的引用都拿到了", len(refs) == 2 and all(refs), str(refs))
    window3, _ = open_window(multi_book)
    window3.resize(1000, 1200)
    pump()
    r3 = window3._comic_reader
    check("拼版页在窗口里也有图", r3._page_view.has_page)
    shot(window3, "06_fixed_multi_image.png")
    window3.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 6. reflowable EPUB 行为与阶段 6 一致 ===")
    layout2, progression2, _spread2 = epub_layout(reflow_book)
    check("流式书判定为 reflowable", layout2 != LAYOUT_PRE_PAGINATED, layout2)
    check("流式书没有装订方向声明", progression2 == "ltr", progression2)
    text_book = open_book(reflow_book)
    check("仍然是文字书", text_book.kind == KIND_TEXT, text_book.kind)
    check("正文读得出来", "流式书的正文在这里。" in text_book.text)
    check("章节来自标题", len(text_book.chapters) >= 1, f"{len(text_book.chapters)} 章")
    window4, loaded4 = open_window(reflow_book)
    check("窗口打开流式书", loaded4)
    check("走的是文字 reader", window4.book.kind == KIND_TEXT, window4.book.kind)
    check("装订按钮不出现（不是漫画）",
          not window4.comic_binding_button.isVisibleTo(window4))
    check("底栏按章计数", window4.chapter_label.text().startswith("第"),
          window4.chapter_label.text())
    shot(window4, "07_reflow_unchanged.png")
    window4.close()
    window.close()
    pump()

    shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{'=' * 60}")
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for _, name, detail in failed:
        print(f"  FAIL {name}  {detail}")
    print(f"截图目录：{SHOTS}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
