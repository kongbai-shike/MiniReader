"""收尾5 手动确认：真 GUI 上开文字型 PDF / 扫描型 PDF / 手工纠正 / 模糊弹窗。

用真窗口（不是 offscreen），截图用 PrintWindow(hwnd, hdc, 2)。
输入一律走 PostMessage —— 本机有游戏窗口会抢前台焦点和滚轮。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"   # 真窗口；test_pdf.py 用的是 setdefault
ROOT = r"C:\Users\pc\Desktop\随手文件\MiniReader"
sys.path.insert(0, ROOT)

OUT = Path(r"C:\Users\pc\AppData\Local\Temp\mr_shots")
OUT.mkdir(exist_ok=True)
FIX = Path(r"C:\Users\pc\AppData\Local\Temp\mr_pdf_fix")
FIX.mkdir(exist_ok=True)

# 夹具生成器直接复用测试文件里的（不重复造）
from tests import test_pdf as fx  # noqa: E402

TEXT_PDF = fx.make_text_pdf(FIX / "text.pdf", pages=3)
SCAN_PDF = fx.make_scan_pdf(FIX / "scan.pdf", pages=3)
SPARSE_PDF = fx.make_sparse_pdf(FIX / "sparse.pdf", pages=2)
print(f"[fix] text={TEXT_PDF.stat().st_size}B scan={SCAN_PDF.stat().st_size}B "
      f"sparse={SPARSE_PDF.stat().st_size}B")

from minireader.qt import QApplication, QTimer  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402
from minireader.storage import Settings, ProgressStore, Library  # noqa: E402

app = QApplication.instance() or QApplication([])

# 沙箱 %APPDATA%，别污染真配置
sandbox = Path(r"C:\Users\pc\AppData\Local\Temp\mr_manual_sandbox")
if sandbox.exists():
    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)
sandbox.mkdir(parents=True)
os.environ["APPDATA"] = str(sandbox)

settings = Settings(wheel_animation_ms=0)
progress = ProgressStore.load()
library = Library.load()

win = MiniWindow(settings, progress, library)
win._quit_on_close = False
win.resize(900, 700)
win.show()
for _ in range(6):
    app.processEvents()
time.sleep(0.4)
for _ in range(6):
    app.processEvents()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()

PW_RENDERFULLCONTENT = 2


def shot(name: str) -> str:
    """PrintWindow(hwnd, hdc, 2) —— 不带 flag 2 抓到的是空白。"""
    hwnd = int(win.winId())
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    ok = user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)
    path = OUT / f"{name}.png"

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)

    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    rows = []
    raw = buf.raw
    for y in range(h):
        row = raw[y * w * 4:(y + 1) * w * 4]
        rows.append(b"\x00" + b"".join(
            bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4])) for x in range(w)))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)
    print(f"  [shot] {path.name} {w}x{h} printwindow={ok}")
    return str(path)


def pump(ms=350):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def open_it(path, label):
    print(f"\n--- {label}: {Path(path).name} ---")
    win.load_path(str(path))
    pump(600)


def report(tag):
    pos = win.reader.current_position()
    print(f"  reader={type(win.reader).__name__} kind={win.book.kind} "
          f"pos={pos.label} text={pos.text()}")
    print(f"  status={win.status_label.text()!r} progress={win.progress_label.text()!r}")
    print(f"  chapter_label={win.chapter_label.text()!r}")
    if win.book is not None and getattr(win.book, "pages", None):
        page = win.book.pages[min(win.chapter_index, len(win.book.pages) - 1)]
        print(f"  page_size={page.width}x{page.height} sizes_known={win.book.sizes_known()}")
    try:
        print(f"  comic_btn_visible={win.comic_mode_button.isVisible()} "
              f"zoom_btn_visible={win.comic_zoom_button.isVisible()}")
    except Exception as exc:
        print(f"  btn check failed: {exc}")
    return pos


print("\n" + "=" * 70)
print("A. 文字型 PDF → 正文正常显示")
print("=" * 70)
open_it(TEXT_PDF, "text pdf")
report("A")
shot("A_text_pdf")
body = win.reader.toPlainText() if hasattr(win.reader, "toPlainText") else ""
print(f"  body chars={len(body)} head={body[:60]!r}")
print(f"  EXPECT: reader=ReaderView kind=text, body has real words")

print("\n" + "=" * 70)
print("B. 扫描型 PDF → 能逐页翻")
print("=" * 70)
open_it(SCAN_PDF, "scan pdf")
report("B")
shot("B_scan_pdf_p1")
for i in range(2):
    win.reader.next_page()
    pump(400)
    pos = report(f"B page{i+2}")
shot("B_scan_pdf_p2")
print("  EXPECT: reader=ComicReader kind=comic, page advances, pixmap changes")

print("\n" + "=" * 70)
print("C. 手工纠正：右键菜单「按文本打开」")
print("=" * 70)
print("  (用稀疏 PDF —— 它有文字，所以按文本开得出来，能真正验证切换)")
SPARSE2 = fx.make_sparse_pdf(FIX / "sparse_c.pdf", pages=2)
open_it(SPARSE2, "sparse pdf")
report("C")
print("  先记住「按漫画」再切回「按文本」：")
win.remember_pdf_kind("comic")
pump(700)
report("C1")
print(f"  saved pdf_kind={progress.get(win.book.id).pdf_kind!r}")
win.remember_pdf_kind("text")
pump(700)
report("C2")
print(f"  saved pdf_kind={progress.get(win.book.id).pdf_kind!r}")
shot("C_forced_text")
print("  EXPECT: 漫画 ↔ 文本 双向切换都成功，Progress.pdf_kind 跟着变")

print("\n  扫描件上强行「按文本打开」必须失败且不写记忆（防锁死）：")
SCAN2 = fx.make_scan_pdf(FIX / "scan_c.pdf", pages=3)
open_it(SCAN2, "scan pdf")
report("C3")
win.remember_pdf_kind("text")
pump(800)
report("C4")
print(f"  saved pdf_kind={progress.get(win.book.id).pdf_kind!r}  (expect '')")
print(f"  warning={win.warn_label.text()!r}")
shot("C3_scan_refused_text")
print("  EXPECT: 仍是 ComicReader，pdf_kind 保持空，警告说已保持原来的打开方式")

print("\n" + "=" * 70)
print("D. 模糊 PDF 弹一次询问（stub 掉 exec_dialog，看它被调用）")
print("=" * 70)
import minireader.ui.window as W  # noqa: E402

calls = []
real_exec = W.exec_dialog


def fake_exec(dialog, *a, **kw):
    calls.append(type(dialog).__name__)
    if isinstance(dialog, W.PdfKindDialog):
        print(f"    -> 弹窗出现：title={dialog.windowTitle()!r}")
        print(f"       buttons: text={dialog.text_button.text()!r} "
              f"comic={dialog.comic_button.text()!r} "
              f"cancel={dialog.cancel_button.text()!r}")
        print(f"       text_button isDefault={dialog.text_button.isDefault()}")
        dialog._pick("comic")   # 用户选「按漫画」
        return 1
    return real_exec(dialog, *a, **kw)


W.exec_dialog = fake_exec
open_it(SPARSE_PDF, "sparse pdf (ambiguous)")
report("D")
print(f"  exec_dialog calls = {calls}")
print(f"  saved pdf_kind={progress.get(win.book.id).pdf_kind!r}")
print("  EXPECT: PdfKindDialog shown exactly once, pdf_kind='comic' remembered")

print("\n  重开同一本，不该再问：")
calls.clear()
win.load_path(str(SPARSE_PDF))
pump(600)
report("D2")
print(f"  exec_dialog calls = {calls}  (expect [])")

print("\n  取消弹窗 → 按文本打开且不写记忆：")
from minireader.core.model import book_id  # noqa: E402

rec = progress.get(book_id(str(SPARSE_PDF)))
if rec is not None:
    rec.pdf_kind = ""
    progress.put(book_id(str(SPARSE_PDF)), rec)
    progress.save()


def cancel_exec(dialog, *a, **kw):
    calls.append("cancelled")
    return 0


W.exec_dialog = cancel_exec
calls.clear()
win.load_path(str(SPARSE_PDF))
pump(700)
report("D3")
rec = progress.get(book_id(str(SPARSE_PDF)))
print(f"  saved pdf_kind={(rec.pdf_kind if rec else None)!r}  (expect '' or None)")
print("  EXPECT: reader=ReaderView (default text), nothing remembered")
shot("D_ambiguous")

W.exec_dialog = real_exec

print("\n" + "=" * 70)
print("E. 非 PDF 不受影响（TXT 行为与阶段 1 一致）")
print("=" * 70)
txt = FIX / "plain.txt"
txt.write_text("第一章 起\n" + "正文内容。" * 300 + "\n第二章 承\n" + "更多正文。" * 300,
               encoding="utf-8")
open_it(txt, "txt")
report("E")
shot("E_txt")
print("  EXPECT: reader=ReaderView, comic buttons hidden")

print("\n" + "=" * 70)
print("ALL SHOTS:", OUT)
for p in sorted(OUT.glob("*.png")):
    print(f"  {p.name} {p.stat().st_size}B")

win.close()
for _ in range(3):
    app.processEvents()
print("\nDONE")
