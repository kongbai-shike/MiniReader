r"""第二批手动确认：FB2 / DOCX / HTML / CB7 四条看得见的行为。

单测验的是"解析对不对"，这里验规格点名的那几条**看得见**的行为：

   1. FB2：章节来自 `<section>`，注释 body 不被当正文，作者进底栏
   2. DOCX：能打开、段落连成正文，标题来自 `docProps/core.xml`
   3. HTML：`<meta charset="gbk">` 的国标网页不变成问号，章节按"第 N 章"行来
   4. CB7：没装 7-Zip 时报"请先安装"；打桩成有工具时能开、能列页、能取字节
   5. 设置页的「重新检测」找到 7-Zip（和 CBR 共用一份缓存）

**本机没有 7-Zip**（`Get-Command 7z,7za` 全空，两个安装目录都不存在），也没有
py7zr（第二批刻意不引入）。所以第 4 条的"能开"是打桩的 `cb7.find()` 返回一个
`Tool`，路径故意指向不存在的文件。这不是糊弄——**列页和解压两条路全都要真起
那个程序**，和 CBR 的 STORED 成员不同，所以打桩只证明了"探测/派发/缓存/错误文案"
这条链是对的，**没有**证明"命令行拼给真的 7z.exe 时它认不认"。报告里就这么写。

真 ZIP 换了 `.cb7` 扩展名照旧走 zip 路径，这一点不打桩也该成立，是顺带的护栏。

样本全部手搓：FB2 是字符串、DOCX 是 `zipfile` 写两个成员、HTML 是字符串、
CB7 是 `7z\xbc\xaf\x27\x1c` 签名加 32 字节零。

跑法： py -3.12 _manual\stage9_manual.py
"""

from __future__ import annotations

import os
import shutil
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

from minireader.core import cb7  # noqa: E402
from minireader.core import formats  # noqa: E402
from minireader.core.comic import open_comic  # noqa: E402
from minireader.core.fb2 import parse_fb2  # noqa: E402
from minireader.core.htmldoc import parse_html  # noqa: E402
from minireader.core.opener import open_book, open_book_kind  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.settings_dialog import SettingsDialog  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402

SHOTS = os.path.join(ROOT, "_manual", "stage9_shots")

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


def open_window(path: str, *, width: int = 1100, height: int = 760):
    settings = Settings(wheel_animation_ms=0)
    window = MiniWindow(settings, ProgressStore.load(), Library.load())
    window._quit_on_close = False
    window.resize(width, height)
    window.show()
    pump()
    ok = window.load_path(path)
    pump()
    return window, ok


class stub_find:
    """把 `cb7.find()` 换成"找到了"，`with` 结束自动还原。"""

    def __init__(self, tool) -> None:
        self.tool = tool

    def __enter__(self):
        self._saved = cb7.find
        cb7.find = lambda force=False: self.tool
        return self

    def __exit__(self, *_exc):
        cb7.find = self._saved
        return False


def fake_tool(path: str = r"C:\Program Files\7-Zip\7z.exe") -> cb7.rar.Tool:
    return cb7.rar.Tool(path=path, kind="7z")


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

FB2_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description>
    <title-info>
      <book-title>战争与和平</book-title>
      <author><first-name>列夫</first-name><last-name>托尔斯泰</last-name></author>
    </title-info>
  </description>
  <body>
    <title><p>第一卷</p></title>
    <section><title><p>第一章</p></title><p>一八零五年七月，朝中正在传说……</p></section>
    <section><title><p>第二章</p></title><p>第二天，皇帝在莫斯科近郊检阅军队。</p>
      <section><title><p>第一节</p></title><p>这是一个嵌套的小节，不该另起一章。</p></section>
    </section>
  </body>
  <body name="notes">
    <section><p>译者注：这一页讲的是奥斯特里茨战役。</p></section>
  </body>
</FictionBook>
"""

HTML_TEXT = """<!doctype html>
<html><head><meta charset="gbk"><title>国标网页</title></head>
<body>
<script>var tracker = 1;</script>
<style>.quiet{color:#333}</style>
<h1>第一章 开始</h1><p>这是国标编码的中文正文，用来测试解码。</p>
<h1>第二章 继续</h1><p>第二段的正文也在这里，同样不长。</p>
</body></html>
"""


def make_docx(path: str, paragraphs: tuple[str, ...]) -> str:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<cp:coreProperties'
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>读书笔记</dc:title><dc:creator>王小</dc:creator>"
        "</cp:coreProperties>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document)
        zf.writestr("docProps/core.xml", core)
    return path


def make_cb7(path: str = "a.cb7") -> str:
    """7z 签名 + 32 字节零。真 7z 的头是 LZMA 压的，这里只需要"是 7z 文件"。"""
    with open(path, "wb") as fh:
        fh.write(b"7z\xbc\xaf\x27\x1c" + bytes(28))
    return path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mr-stage9-manual-")
    os.environ["APPDATA"] = tmp
    cb7.forget()

    fb2_path = os.path.join(tmp, "war.fb2")
    with open(fb2_path, "w", encoding="utf-8") as fh:
        fh.write(FB2_TEXT)
    docx_path = make_docx(
        os.path.join(tmp, "notes.docx"),
        ("第一段笔记。", "第二段笔记。", "第三段笔记。"),
    )
    html_path = os.path.join(tmp, "gbk.html")
    with open(html_path, "w", encoding="gbk") as fh:
        fh.write(HTML_TEXT)

    # ------------------------------------------------------------------
    print("=== 1. 格式表：第二批四行都在且已就绪 ===")
    for ext in (".fb2", ".docx", ".html", ".htm", ".cb7"):
        fmt = formats.format_for("a" + ext)
        check(f"{ext} 有一行且已就绪", fmt is not None and fmt.ready,
              f"{fmt.label} phase={fmt.phase} ready={fmt.ready}" if fmt else "None")
    check(".htm 与 .html 共用一行",
          formats.format_for("a.htm") is formats.format_for("a.html"))
    check("FB2/DOCX/HTML 是文字格式",
          all(formats.kind_for("a" + e) == "text" for e in (".fb2", ".docx", ".html")))
    check("CB7 是漫画", formats.kind_for("a.cb7") == "comic")
    text = formats.dialog_filters()
    check("文件对话框里有 .fb2", "*.fb2" in text)
    check("文件对话框里有 .docx", "*.docx" in text)
    check("文件对话框里有 .cb7", "*.cb7" in text)

    # ------------------------------------------------------------------
    print("\n=== 2. FB2：章节来自 <section>，注释不当正文 ===")
    content = parse_fb2(fb2_path)
    check("书名来自 <description>", content.title == "战争与和平", content.title)
    check("作者拼得出来", content.author == "列夫 托尔斯泰", content.author)
    check("两个顶层 section 是两章",
          [s.title for s in content.segments] == ["第一章", "第二章"],
          str([s.title for s in content.segments]))
    body = "\n".join(s.text for s in content.segments)
    check("嵌套小节不另起一章，标题留在正文里",
          "第一节" in body and "译者注" not in body)
    check("notes 解释了有注释没读", any("注释/尾注" in n for n in content.notes),
          str(content.notes))

    book = open_book(fb2_path)
    check("窗口走文字 reader", book.kind == "text", book.kind)
    check("书名和作者都进去了", book.title == "战争与和平", book.title)
    check("describe 说 FB2", "FB2" in book.describe(), book.describe())
    window, loaded = open_window(fb2_path)
    check("FB2 在窗口里打开成功", loaded)
    check("目录是两章", window.toc_panel.model.source_count() == 2,
          str(window.toc_panel.model.source_count()))
    check("第一章正文读得出来", "一八零五年七月" in book.text)
    check("正文译注没有混进来", "译者注" not in book.text)
    shot(window, "01_fb2.png")
    window.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 3. DOCX：能打开，段落连成正文 ===")
    docx_book = open_book(docx_path)
    check("标题来自 core.xml", docx_book.title == "读书笔记", docx_book.title)
    check("作者来自 core.xml", docx_book.author == "王小", docx_book.author)
    check("三段都读到了（段间空行是 HTML 解析的既有行为，见 test_text_formats）",
          docx_book.text.replace("\n\n", "\n") == "第一段笔记。\n第二段笔记。\n第三段笔记。",
          repr(docx_book.text))
    check("describe 说 DOCX", "DOCX" in docx_book.describe(), docx_book.describe())
    window2, loaded2 = open_window(docx_path)
    check("DOCX 在窗口里打开成功", loaded2)
    check("走的是文字 reader", window2.book.kind == "text", window2.book.kind)
    check("底栏有字数", window2.status_label.text() != "", window2.status_label.text())
    shot(window2, "02_docx.png")
    window2.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 4. HTML：国标网页不变问号，章节按第 N 章来 ===")
    html_content = parse_html(html_path)
    check("标题来自 <title>", html_content.title == "国标网页", html_content.title)
    check("<meta charset=\"gbk\"> 赢了统计判断", "编码按 GBK 判断" in " ".join(html_content.notes),
          str(html_content.notes))
    joined = "\n".join(s.text for s in html_content.segments)
    check("没有问号乱码", "�" not in joined, "有 U+FFFD")
    check("中文正文完整", "这是国标编码的中文正文" in joined)
    check("script/style 不进正文", "tracker" not in joined and "#333" not in joined)
    check("两章", [s.title for s in html_content.segments] == ["第一章 开始", "第二章 继续"],
          str([s.title for s in html_content.segments]))
    window3, loaded3 = open_window(html_path)
    check("HTML 在窗口里打开成功", loaded3)
    check("目录两章", window3.toc_panel.model.source_count() == 2,
          str(window3.toc_panel.model.source_count()))
    shot(window3, "03_html_gbk.png")
    window3.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 5. CB7：没装 7-Zip 时报安装 ===")
    cb7_path = make_cb7(os.path.join(tmp, "a.cb7"))
    with stub_find(None):
        try:
            open_book(cb7_path)
            check("打开失败", False, "竟然没抛错")
        except Exception as exc:
            check("抛的是 BookError", type(exc).__name__ == "BookError", type(exc).__name__)
            check("文案就是那句", str(exc) == cb7.MESSAGE_NO_TOOL, str(exc))
    window4, loaded4 = open_window(cb7_path)
    check("窗口拒绝打开", not loaded4)
    check("底栏给出同一句", cb7.MESSAGE_NO_TOOL in window4.warn_label.text(),
          window4.warn_label.text())
    shot(window4, "04_cb7_no_tool.png")
    window4.close()
    pump()

    # ------------------------------------------------------------------
    print("\n=== 6. CB7：有 7-Zip 时能列页、能取字节（打桩） ===")
    page_one = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    with stub_find(fake_tool()):
        from unittest import mock

        with mock.patch.object(cb7.rar, "list_names_by_tool", return_value=["p10.png", "p2.png", "p1.png"]):
            entries = cb7.list_pages(cb7_path)
        check("列页按自然序（p1 p2 p10，不是字典序）",
              [e.name for e in entries] == ["p1.png", "p2.png", "p10.png"],
              str([e.name for e in entries]))
        with mock.patch.object(cb7.rar, "list_names_by_tool", return_value=["a.html"]):
            try:
                cb7.list_pages(cb7_path)
                check("没有图片时报错", False, "竟然没抛错")
            except Exception as exc:
                check("没有图片时报错", "没有能看的图片" in str(exc), str(exc))
        with mock.patch.object(cb7.rar, "read_page_by_tool", return_value=page_one):
            blob = cb7.read_page(cb7_path, "p1.png")
        check("整页字节拿到了", blob == page_one, f"{len(blob)} 字节")
        with mock.patch.object(cb7.rar, "read_page_by_tool", return_value=page_one[:6]):
            head = cb7.read_page_head(cb7_path, "p1.png", limit=6)
        check("页头按 limit 截断", head == page_one[:6], f"{len(head)} 字节")
        heads = cb7.read_page_heads(cb7_path, ["p1.png", None])
        check("批量为全空（7-Zip 没有多成员流式读）", heads == [b"", b""], repr(heads))
        try:
            # `stub_find` 换的是 `cb7.find`，但 `open_comic()` 走的是
            # `cb7.list_pages()` → `rar.list_names_by_tool()`。所以要连它一起打桩：
            # 没有真 7z.exe 的机器上，这两层就是"列页"这件事的全部。
            with mock.patch.object(cb7.rar, "list_names_by_tool",
                                   return_value=["p1.png", "p2.png", "p3.png"]):
                comic = open_comic(cb7_path)
            check("认成 cb7 漫画", comic.source == "cb7", comic.source)
            check("source_label 说 7z 漫画", comic.source_label == "7z 漫画", comic.source_label)
            check("页数来自列页", len(comic.pages) == 3, f"{len(comic.pages)} 页")
            with mock.patch.object(cb7.rar, "read_page_by_tool", return_value=page_one):
                check("read_page 走到 7z", comic.read_page(0) == page_one)
                check("read_page_head 走到 7z", comic.read_page_head(0) == page_one)
            check("read_page_heads 是空但不炸", comic.read_page_heads([0, 1]) == [b"", b""])
        except Exception as exc:
            check("cb7 漫画能开", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    print("\n=== 7. 真 ZIP 换 .cb7 扩展名：走 zip 那条路 ===")
    zip_named = os.path.join(tmp, "actually_zip.cb7")
    with zipfile.ZipFile(zip_named, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, 4):
            zf.writestr(f"p{i}.png", b"\x89PNG\r\n\x1a\n" + b"\x22" * 40)
    with stub_find(None):  # 没有工具也不影响：它根本不是 7z
        comic = open_comic(zip_named)
    check("认成普通压缩包", comic.source == "cbz", comic.source)
    check("3 页", len(comic.pages) == 3, f"{len(comic.pages)} 页")

    # ------------------------------------------------------------------
    print("\n=== 8. 设置页「重新检测」：与 CBR 共用一份缓存 ===")
    check("本机确实找不到 7-Zip（否则下面的打桩没必要）", cb7.probe() is None,
          str(cb7.probe()))
    settings = Settings()
    dialog = SettingsDialog(settings)
    pump()
    check("初始标签说没找到", "没找到" in dialog.rar_status.text(), dialog.rar_status.text())
    # 设置页按的是 `rar.find()`（两种格式共用一份缓存），不是 `cb7.find()`——
    # 第 3 段模块 docstring 写的那条设计：探测分开、持久化合并。所以这里打桩
    # `rar.find()` 才会真的让按钮动起来。
    rar_saved = cb7.rar.find
    with stub_find(fake_tool()):
        cb7.rar.find = lambda force=False: fake_tool()
        try:
            dialog.rar_recheck_button.click()
            pump()
        finally:
            cb7.rar.find = rar_saved
    check("按一下能找到（打桩的 7-Zip）", "已找到" in dialog.rar_status.text(),
          dialog.rar_status.text())
    check("标签说找到的是 7-Zip", "7-Zip" in dialog.rar_status.text(),
          dialog.rar_status.text())
    check("tooltip 给出完整路径",
          dialog.rar_status.toolTip() == r"C:\Program Files\7-Zip\7z.exe",
          dialog.rar_status.toolTip())
    written = dialog.apply_to(Settings())
    check("apply_to 写下了 7z 缓存", (written.unrar_path, written.unrar_kind) ==
          (r"C:\Program Files\7-Zip\7z.exe", "7z"),
          f"{written.unrar_path!r} {written.unrar_kind!r}")
    shot(dialog, "05_settings_recheck.png")
    dialog.deleteLater()
    pump()

    fresh = Settings()
    dialog2 = SettingsDialog(fresh)
    pump()
    untouched = dialog2.apply_to(Settings())
    check("没按过检测时 apply_to 不动这三个字段",
          (untouched.unrar_path, untouched.unrar_kind, untouched.unrar_checked) == ("", "", False),
          f"{untouched.unrar_path!r} {untouched.unrar_kind!r} {untouched.unrar_checked}")
    dialog2.deleteLater()
    pump()

    check("open_book_kind 也认得 .cb7", open_book_kind(cb7_path) == "comic")

    cb7.forget()
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
