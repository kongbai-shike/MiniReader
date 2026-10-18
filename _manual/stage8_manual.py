r"""阶段 8 手动确认：CBR/RAR 的两条路 + 书里声明的双页策略 + 截图。

单测验的是"函数对不对"，这里验规格点名的那几条**看得见**的行为：

  1. 没装解压程序时打开 .cbr，提示装 WinRAR 或 7-Zip
  2. 有解压程序时 .cbr 能翻页（屏幕上真的换图）
  3. 设置页的「重新检测」能找到刚装的程序，取消不写配置
  4. 书里写 `rendition:spread=none` 时，双页模式一次只显示一页，且提示一次

**本机没有 WinRAR，也没有 7-Zip**（`Get-Command unrar,7z,7za` 全空，四个安装目录
都不存在），所以第 2 条和第 3 条的"找到"是打桩的：`rar.find()` 返回一个 `Tool`，
路径故意指向不存在的文件。这不是在糊弄——STORED 成员走 `rarfile` 的
`DirectReader`，纯 Python，**永远不需要那个程序真的存在**，所以解压、翻页、截图
全都是真跑。真正没验过的只有"命令行拼给一个真的 unrar.exe 时它认不认"，那条
只能在装了 WinRAR 的机器上手点一次。

样本是**手搓的真 RAR5**（不是假扩展名）：真的 PNG 字节，让 `QImageReader` 能解码，
这样"这一页有图"才不是空断言。手搓而不是找真本子，是为了精确控制成员顺序、图片
尺寸和"哪些成员算一页"。

跑法： py -3.12 _manual\stage8_manual.py
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

from minireader.core import rar  # noqa: E402
from minireader.core.comic import open_comic  # noqa: E402
from minireader.core.epub import LAYOUT_PRE_PAGINATED, epub_layout  # noqa: E402
from minireader.core.formats import (  # noqa: E402
    CURRENT_PHASE,
    KIND_COMIC,
    format_for,
    is_supported,
    support_note,
)
from minireader.core.opener import open_book  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.settings_dialog import SettingsDialog  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

SHOTS = os.path.join(ROOT, "_manual", "stage8_shots")

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
# 手搓一个真的 RAR5
# ---------------------------------------------------------------------------
#
# 这一段和 `tests/test_rar.py` 里的同名函数是同一份实现，刻意重复而不是从测试
# 里 import：手动脚本要能单独拷到别的机器上跑，不能依赖 tests/ 在不在。两处都
# 只在"造样本"这件事上，跑的是同一套 rarfile 解析器，所以重复不会漂。

RAR5_ID = b"Rar!\x1a\x07\x01\x00"


def _vint(value: int) -> bytes:
    """RAR5 的变长整数：每字节 7 位，高位表示还有后续。"""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _vstr(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _vint(len(raw)) + raw


def _block(block_type: int, payload: bytes, add_size: int = 0) -> bytes:
    """一个 RAR5 块：CRC32(头) + 头长度 + 类型 + 标志 + [数据区长度] + 载荷。"""
    flags = 0x2 if add_size else 0x0
    body = _vint(block_type) + _vint(flags)
    if add_size:
        body += _vint(add_size)
    body += payload
    head = _vint(len(body)) + body
    return struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF) + head


def _file_block(name: str, data: bytes) -> bytes:
    """一个 STORED 成员的 FILE 块 + 它的数据（compress_flags=0 → RAR_M0）。"""
    payload = _vint(0x4)  # HAS_CRC32
    payload += _vint(len(data))
    payload += _vint(0o644 << 16)
    payload += struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
    payload += _vint(0)  # 不压缩 → DirectReader，纯 Python
    payload += _vint(0)  # host_os = Windows
    payload += _vstr(name)
    return _block(0x2, payload, add_size=len(data)) + data


def build_rar(path: str, members: dict[str, bytes]) -> str:
    out = bytearray(RAR5_ID)
    out += _block(0x1, _vint(0))  # MAIN
    for name, data in members.items():
        out += _file_block(name, data)
    out += _block(0x5, _vint(0))  # ENDARC
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def fake_tool(path: str = r"C:\no\such\UnRAR.exe", kind: str = rar.TOOL_UNRAR) -> rar.Tool:
    """一个「已找到工具」的答案，路径故意不存在（STORED 成员不需要它）。"""
    return rar.Tool(path=path, kind=kind)


class stub_find:
    """把 `rar.find()` 换成"找到了"，`with` 结束自动还原。"""

    def __init__(self, tool: rar.Tool | None) -> None:
        self.tool = tool
        self._saved = None

    def __enter__(self):
        self._saved = rar.find
        rar.find = lambda force=False: self.tool
        return self

    def __exit__(self, *_exc):
        rar.find = self._saved
        return False


# ---------------------------------------------------------------------------
# 手搓 EPUB（第 4 条要一本写了 rendition:spread=none 的书）
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


def build_fixed_epub(path: str, *, pages: int, spread: str) -> str:
    """一本固定版式 EPUB，页数 `pages`，书里写 `rendition:spread=<spread>`。"""
    items = []
    spine = []
    files: dict[str, str | bytes] = {"mimetype": "application/epub+zip",
                                     "META-INF/container.xml": CONTAINER}
    for i in range(pages):
        img = f"images/p{i}.png"
        files[f"OEBPS/{img}"] = make_png(600, 900, i)
        items.append(f'    <item id="img{i}" href="{img}" media-type="image/png"/>')
        files[f"OEBPS/text/p{i}.xhtml"] = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            "  <head><title>page</title></head>\n"
            f'  <body><img src="../images/p{i}.png"/></body>\n'
            "</html>\n"
        )
        items.append(f'    <item id="p{i}" href="text/p{i}.xhtml"'
                     ' media-type="application/xhtml+xml"/>')
        spine.append(f'    <itemref idref="p{i}"/>')

    meta = ('    <meta property="rendition:layout">pre-paginated</meta>\n'
            f'    <meta property="rendition:spread">{spread}</meta>')
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "    <dc:title>双页策略样本</dc:title>\n"
        "    <dc:creator>测试作者</dc:creator>\n"
        '    <dc:identifier id="bookid">urn:uuid:stage8</dc:identifier>\n'
        "    <dc:language>ja</dc:language>\n" + meta + "\n"
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(items) + "\n  </manifest>\n"
        "  <spine>\n" + "\n".join(spine) + "\n  </spine>\n"
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
    tmp = tempfile.mkdtemp(prefix="mr-stage8-manual-")
    os.environ["APPDATA"] = tmp
    rar.forget()

    # 8 页竖版，成员名故意排成 p1 p2 ... p10 那样能验自然序（这里用 8 页够翻）。
    cbr = build_rar(
        os.path.join(tmp, "book.cbr"),
        {f"p{i}.png": make_png(600, 900, i) for i in range(1, 9)},
    )
    # 同一批图存成 CBZ，用来证明 .cbr 与 .cbz 的页数、页序完全一致。
    cbz = os.path.join(tmp, "book.cbz")
    with zipfile.ZipFile(cbz, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, 9):
            zf.writestr(f"p{i}.png", make_png(600, 900, i))
    # 一个真 ZIP 换了 .cbr 扩展名——很多"RAR 漫画"其实是这个。
    zip_in_cbr = os.path.join(tmp, "actually_zip.cbr")
    with zipfile.ZipFile(zip_in_cbr, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, 4):
            zf.writestr(f"p{i}.png", make_png(600, 900, i))

    none_book = build_fixed_epub(os.path.join(tmp, "none.epub"), pages=6, spread="none")
    auto_book = build_fixed_epub(os.path.join(tmp, "auto.epub"), pages=6, spread="auto")

    # ------------------------------------------------------------------
    print("=== 1. 格式表：.cbr/.rar 这一版解锁了 ===")
    check("CURRENT_PHASE 是 8", CURRENT_PHASE == 8, str(CURRENT_PHASE))
    check(".cbr 支持", is_supported("a.cbr"))
    check(".rar 支持", is_supported("a.rar"))
    check(".cbr 不再报相位", support_note("a.cbr") == "", repr(support_note("a.cbr")))
    fmt = format_for("a.cbr")
    check("格式行自称 RAR 漫画", fmt.label == "RAR 漫画", fmt.label)
    check("格式行 phase=8 且已就绪", fmt.phase == 8 and fmt.ready, f"{fmt.phase} {fmt.ready}")

    # ------------------------------------------------------------------
    print("\n=== 2. 本机真的没有解压程序（这一条不是断言，是记录） ===")
    real = rar.probe()
    print(f"      rar.probe() -> {real}")
    check("本机确实找不到工具（否则下面的打桩没必要）", real is None, str(real))

    # ------------------------------------------------------------------
    print('\n=== 3. 没有工具时打开 .cbr：报的是"去装一个" ===')
    try:
        open_comic(cbr)
        check("没工具时打不开", False, "居然成功了")
    except Exception as exc:
        check("没工具时打不开", True)
        check("文案就是那句装工具的提示", str(exc) == rar.MESSAGE_NO_TOOL, str(exc))
    window, loaded = open_window(cbr)
    check("窗口拒绝打开", not loaded)
    check("底栏显示同一句提示", rar.MESSAGE_NO_TOOL in window.warn_label.text(),
          window.warn_label.text())
    shot(window, "01_no_tool_message.png")
    window.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 4. 有工具时 .cbr 能列页、能翻页（屏幕上真的换图） ===")
    with stub_find(fake_tool()):
        book = open_comic(cbr)
        check("认成 RAR 漫画", book.source == "rar", book.source)
        check("自称 RAR 漫画", book.source_label == "RAR 漫画", book.source_label)
        check("8 页", len(book.pages) == 8, f"{len(book.pages)} 页")
        check("第一页取得到字节", book.read_page(0).startswith(b"\x89PNG"))
        zip_book = open_comic(cbz)
        check(".cbr 与 .cbz 页数一致", len(zip_book.pages) == len(book.pages),
              f"{len(zip_book.pages)} vs {len(book.pages)}")
        check(".cbr 与 .cbz 页序一致",
              [p.name for p in book.pages] == [p.name for p in zip_book.pages],
              str([p.name for p in book.pages]))

    with stub_find(fake_tool()):
        window2, loaded2 = open_window(cbr)
    check("窗口打开成功", loaded2)
    reader = window2._comic_reader
    check("走的是漫画 reader", window2.book.kind == KIND_COMIC, window2.book.kind)
    check("底栏说第 1/8 页", window2.chapter_label.text() == "第 1/8 页",
          window2.chapter_label.text())
    check("第一页真的有图（不是占位）", reader._page_view.has_page)
    shot(window2, "02_cbr_page1.png")

    seen: list[int] = []
    for _step in range(3):
        window2.step_chapter(1)
        pump()
        seen.append(reader.index)
        check(f"翻到第 {reader.index + 1} 页且这页有图",
              reader._page_view.has_page, f"index={reader.index}")
    check("三次翻页真的前进了", seen == [1, 2, 3], str(seen))
    check("底栏跟着走", window2.chapter_label.text() == "第 4/8 页",
          window2.chapter_label.text())
    shot(window2, "03_cbr_page4.png")
    window2.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 5. 真 ZIP 换了 .cbr 扩展名：走 zip 那条路，不碰解压程序 ===")
    rar.forget()
    with stub_find(None):  # 没有工具也不影响：它根本不是 RAR
        zip_named = open_comic(zip_in_cbr)
    check("认成普通压缩包", zip_named.source == "cbz", zip_named.source)
    check("3 页", len(zip_named.pages) == 3, f"{len(zip_named.pages)} 页")

    # ------------------------------------------------------------------
    print("\n=== 6. 工具缓存：成功记住、失败不记住 ===")
    rar.forget()
    check("forget() 之后 known() 是 None", rar.known() is None)
    # `remember()` 会重新 stat，所以"种下缓存"这条要用一个**真的存在**的文件。
    real_tool = os.path.join(tmp, "UnRAR.exe")
    with open(real_tool, "wb") as fh:
        fh.write(b"MZ")
    tool = rar.remember(real_tool, rar.TOOL_UNRAR)
    check("remember() 一个存在的路径就种下缓存", tool is not None, str(tool))
    check("remember() 之后 known() 立刻有值", rar.known() is not None)
    check("kind 也照原样存着", rar.known() is not None and rar.known().kind == "unrar")
    rar.forget()
    check("remember() 一个不存在的路径返回 None", rar.remember(r"C:\nope\x.exe", "unrar") is None)
    check("而且没有污染缓存", rar.known() is None)

    # ------------------------------------------------------------------
    print("\n=== 7. 设置页「重新检测」：取消不写，确定才写 ===")
    settings = Settings()
    dialog = SettingsDialog(settings)
    pump()
    check("初始标签说没找到", "没找到" in dialog.rar_status.text(), dialog.rar_status.text())
    with stub_find(fake_tool(r"C:\Program Files\WinRAR\UnRAR.exe")):
        dialog.rar_recheck_button.click()
        pump()
    check("按一下能找到（打桩的 WinRAR）", "已找到" in dialog.rar_status.text(),
          dialog.rar_status.text())
    check("标签说找到的是 WinRAR", "WinRAR" in dialog.rar_status.text(),
          dialog.rar_status.text())
    check("tooltip 给出完整路径",
          dialog.rar_status.toolTip() == r"C:\Program Files\WinRAR\UnRAR.exe",
          dialog.rar_status.toolTip())
    # 取消：apply_to 不该被调用，但即便调了也不该写（_rar_probe 是 None 时保留原值）。
    untouched = Settings()
    dialog.apply_to(untouched)
    check("按过检测后 apply_to 会写路径", untouched.unrar_path != "", untouched.unrar_path)
    check("并写下 checked 标记", untouched.unrar_checked)
    shot(dialog, "04_settings_recheck.png")
    dialog.deleteLater()
    pump()

    fresh = Settings()
    dialog2 = SettingsDialog(fresh)
    pump()
    written = dialog2.apply_to(Settings())
    check("没按过检测时 apply_to 不动这三个字段",
          (written.unrar_path, written.unrar_kind, written.unrar_checked) == ("", "", False),
          f"{written.unrar_path!r} {written.unrar_kind!r} {written.unrar_checked}")
    with stub_find(None):
        dialog2.rar_recheck_button.click()
        pump()
    check("找不到时标签说还是没找到", "还是没找到" in dialog2.rar_status.text(),
          dialog2.rar_status.text())
    dialog2.deleteLater()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 8. 书里写 rendition:spread=none：双页模式一次只显示一页 ===")
    layout, _progression, spread = epub_layout(none_book)
    check("判定为固定版式", layout == LAYOUT_PRE_PAGINATED, layout)
    check("读出 spread=none", spread == "none", spread)
    none_comic = open_book(none_book)
    check("ComicBook 带上了这个意见", none_comic.spread == "none", none_comic.spread)

    window3, loaded3 = open_window(none_book, width=1400, height=900)
    check("窗口打开成功", loaded3)
    r3 = window3._comic_reader
    r3.set_mode("spread")
    pump()
    check("切进双页模式", r3.mode == "spread", r3.mode)
    pair = r3._spread.pair
    check("跨页只显示一页", pair[1] is None, str(pair))
    check("底栏提示解释了为什么", "这本书要求单页显示" in window3.warn_label.text(),
          window3.warn_label.text())
    check("进度条跨度是 1", r3.current_position().span == 1, str(r3.current_position().span))
    shot(window3, "05_spread_none.png")

    before = window3.warn_label.text()
    r3._render()
    pump()
    check("再渲染一次不重复播报", window3.warn_label.text() == before,
          window3.warn_label.text())

    pages_seen: list[int] = []
    for _step in range(3):
        window3.step_chapter(1)
        pump()
        pages_seen.append(r3.index)
    check("一次前进一页（不是每隔一页显示一次）", pages_seen == [1, 2, 3], str(pages_seen))
    window3.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 9. 书里写 auto 时行为与阶段 5 一致（仍然两页一组） ===")
    _layout, _progression, spread2 = epub_layout(auto_book)
    check("读出 spread=auto", spread2 == "auto", spread2)
    window4, loaded4 = open_window(auto_book, width=1400, height=900)
    check("窗口打开成功", loaded4)
    r4 = window4._comic_reader
    # 六页竖版、前三页都是竖的 → 阶段 5 的封面启发式认为第 1 页是封面。这是
    # **既有**行为，不是阶段 8 引入的：auto 的意思是"阅读系统自己决定"，而我们的
    # 决定就是阶段 5 那一套。所以第 1 屏只有封面是对的。
    check("auto 书的第 1 屏仍按封面单独显示（阶段 5 的既有行为）",
          r4._spread.pair == (0, None), str(r4._spread.pair))
    r4.set_mode("spread")
    pump()
    check("切进双页模式", r4.mode == "spread", r4.mode)
    window4.step_chapter(1)
    pump()
    pair2 = r4._spread.pair
    check("第 2 屏开始两页一组", pair2[1] is not None, str(pair2))
    check("没有播「要求单页」的提示",
          "这本书要求单页显示" not in window4.warn_label.text(),
          window4.warn_label.text())
    shot(window4, "06_spread_auto.png")

    # 把封面开关关掉，第 1 屏就应该也是两页——证明 none 与 auto 的差别不是
    # "有没有封面"造成的。
    window4.close()
    pump()
    window5, loaded5 = open_window(auto_book, width=1400, height=900)
    check("第二个窗口打开成功", loaded5)
    r5 = window5._comic_reader
    r5.set_cover_alone(False)
    r5.set_mode("spread")
    pump()
    check("关掉封面单独显示后第 1 屏就是两页", r5._spread.pair == (0, 1),
          str(r5._spread.pair))
    shot(window5, "07_spread_auto_no_cover.png")
    window5.close()
    pump()

    rar.forget()
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
