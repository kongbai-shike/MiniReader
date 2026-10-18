"""Stage-3 ask-once probe: does an ambiguous PDF ask, and is the answer remembered?"""
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = r"C:\Users\pc\Desktop\随手文件\MiniReader"
sys.path.insert(0, ROOT)

from minireader.qt import QApplication, QEvent  # noqa: E402

_app = QApplication.instance() or QApplication([])

from minireader.ui import window as window_module  # noqa: E402
from minireader.core.model import book_id  # noqa: E402
from minireader.core.opener import PDF_COMIC, PDF_TEXT, pdf_choice  # noqa: E402
from minireader.core.pdfdoc import classify  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.window import MiniWindow  # noqa: E402


def chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_png(width, height, colour=(200, 30, 30)) -> bytes:
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def pdf_objects(path, objs, root=1):
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref = len(out)
    count = max(objs) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, count):
        if num in offsets:
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"
    out += f"trailer\n<< /Size {count} /Root {root} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(bytes(out))
    return Path(path)


def make_blank_pdf(path, n_pages=2, w=612, h=792):
    """A PDF with pages but no content at all: density 0, coverage 0 -> needs_ask."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    for i in range(n_pages):
        objs[3 + i] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] >>".encode()
    return pdf_objects(path, objs)


def make_text_pdf(path, n_pages=3, w=612, h=792, lines_per_page=45):
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    font_num = 3 + n_pages
    objs[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    for i in range(n_pages):
        body = f"BT /F1 11 Tf 14 TL 40 750 Td\n"
        for k in range(lines_per_page):
            body += f"(Page {i + 1} line {k + 1}: the quick brown fox jumps over the lazy dog.) Tj T*\n"
        body += "ET"
        stream = body.encode()
        content_num = font_num + 1 + i
        objs[content_num] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        objs[3 + i] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode()
    return pdf_objects(path, objs)


def make_sparse_pdf(path, n_pages=2, w=612, h=792, lines=2):
    """A little real text, no images: low density (<200) and low coverage (<0.7).

    So `classify()` says needs_ask - but text extraction still succeeds, which
    makes it the fixture for "asked, chose text, opened, remembered".
    """
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    font_num = 3 + n_pages
    objs[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    for i in range(n_pages):
        body = "BT /F1 11 Tf 14 TL 40 750 Td\n"
        for k in range(lines):
            body += f"(Page {i + 1} short line {k + 1}.) Tj T*\n"
        body += "ET"
        stream = body.encode()
        content_num = font_num + 1 + i
        objs[content_num] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        objs[3 + i] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode()
    return pdf_objects(path, objs)


def make_scanned_pdf(path, n_pages=3, w=612, h=792):
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    img_num = 3 + n_pages
    pixels = zlib.compress(bytes((180, 200, 220)) * (w * h), 6)
    objs[img_num] = (
        f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB "
        f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(pixels)} >>\nstream\n"
    ).encode() + pixels + b"\nendstream"
    for i in range(n_pages):
        body = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
        content_num = img_num + 1 + i
        objs[content_num] = f"<< /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream"
        objs[3 + i] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            f"/Resources << /XObject << /Im0 {img_num} 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode()
    return pdf_objects(path, objs)


tmp = Path(tempfile.mkdtemp(prefix="mr-ask-"))
os.environ["APPDATA"] = str(tmp)

blank = make_blank_pdf(tmp / "blank.pdf")
sparse = make_sparse_pdf(tmp / "sparse.pdf")
text_pdf = make_text_pdf(tmp / "novel.pdf")
scanned = make_scanned_pdf(tmp / "scan.pdf")

print("=== A. classify() verdicts ===")
for name, p in (("blank", blank), ("sparse", sparse), ("text", text_pdf), ("scan", scanned)):
    info = classify(p)
    print(f"  {name:6s} kind={info.kind:6s} density={info.density:9.1f} coverage={info.coverage:.3f} needs_ask={info.needs_ask}")

print()
print("=== B. pdf_choice() reports needs_ask ===")
for name, p in (("blank", blank), ("sparse", sparse), ("text", text_pdf), ("scan", scanned)):
    c = pdf_choice(p)
    print(f"  {name:6s} -> PdfChoice(kind={c.kind!r}, needs_ask={c.needs_ask})")
print(f"  prefer='comic' overrides measurement: {pdf_choice(blank, 'comic')}")

print()
print("=== C. an ambiguous PDF that CAN open as text asks once and remembers ===")

settings = Settings()
progress = ProgressStore.load()
library = Library.load()
win = MiniWindow(settings, progress, library)
win._quit_on_close = False
win.resize(480, 360)
win.show()
_app.processEvents()

seen = {"calls": 0, "dialog": None}


def fake_exec_text(dialog):
    """Stub exec_dialog: click '按文本' and accept."""
    seen["calls"] += 1
    seen["dialog"] = dialog
    dialog.text_button.click()
    return True


real_exec = window_module.exec_dialog
window_module.exec_dialog = fake_exec_text
try:
    ok = win.load_path(str(blank))
finally:
    window_module.exec_dialog = real_exec
_app.processEvents()

print(f"  load_path -> {ok}")
print(f"  exec_dialog called {seen['calls']} time(s)")
print(f"  dialog title = {seen['dialog'].windowTitle()!r}")
print(f"  warning = {win.warn_label.text()!r}")
print(f"  reader = {type(win.reader).__name__}, book kind = {win.book.kind if win.book else None}")
print(f"  remembered = {win.progress.get(book_id(blank)).pdf_kind!r} (expect '' - open failed)")
print(f"  book_id import check: {book_id(blank)[:24]}...")
print(f"  book.encoding_label = {win.book.encoding_label!r}")
print(f"  remembered pdf_kind = {win.progress.get(win.book.id).pdf_kind!r}")

print()
print("=== D. reopening the same file does NOT ask again ===")
win2 = MiniWindow(settings, progress, library)
win2._quit_on_close = False
win2.resize(480, 360)
win2.show()
_app.processEvents()
calls2 = {"n": 0}


def fake_exec_count(dialog):
    calls2["n"] += 1
    dialog.comic_button.click()
    return True


window_module.exec_dialog = fake_exec_count
try:
    ok2 = win2.load_path(str(blank))
finally:
    window_module.exec_dialog = real_exec
_app.processEvents()
print(f"  load_path -> {ok2}, exec_dialog called {calls2['n']} time(s)")
print(f"  book kind = {win2.book.kind} (remembered 'text')")
print(f"  remembered = {win2.progress.get(win2.book.id).pdf_kind!r}")

print()
print("=== E. choosing 漫画 writes PDF_COMIC and opens as a comic ===")
blank2 = make_blank_pdf(tmp / "blank2.pdf")
win3 = MiniWindow(settings, progress, library)
win3._quit_on_close = False
win3.resize(480, 360)
win3.show()
_app.processEvents()


def fake_exec_comic(dialog):
    dialog.comic_button.click()
    return True


window_module.exec_dialog = fake_exec_comic
try:
    ok3 = win3.load_path(str(blank2))
finally:
    window_module.exec_dialog = real_exec
_app.processEvents()
print(f"  load_path -> {ok3}, book kind = {win3.book.kind}, reader = {type(win3.reader).__name__}")
print(f"  remembered = {win3.progress.get(win3.book.id).pdf_kind!r} (expect {PDF_COMIC!r})")

print()
print("=== F. cancelling opens as text and writes NOTHING ===")
blank3 = make_blank_pdf(tmp / "blank3.pdf")
win4 = MiniWindow(settings, progress, library)
win4._quit_on_close = False
win4.resize(480, 360)
win4.show()
_app.processEvents()


def fake_exec_cancel(dialog):
    dialog.cancel_button.click()
    return False


window_module.exec_dialog = fake_exec_cancel
try:
    ok4 = win4.load_path(str(blank3))
finally:
    window_module.exec_dialog = real_exec
_app.processEvents()
print(f"  load_path -> {ok4}, book kind = {win4.book.kind} (expect text)")
print(f"  remembered = {win4.progress.get(win4.book.id).pdf_kind!r} (expect '')")

print()
print("=== G. an unambiguous PDF never asks ===")
win5 = MiniWindow(settings, progress, library)
win5._quit_on_close = False
win5.resize(480, 360)
win5.show()
_app.processEvents()
calls5 = {"n": 0}


def fake_exec_never(dialog):
    calls5["n"] += 1
    return False


window_module.exec_dialog = fake_exec_never
try:
    ok5 = win5.load_path(str(text_pdf))
    ok6 = win5.load_path(str(scanned))
finally:
    window_module.exec_dialog = real_exec
_app.processEvents()
print(f"  text PDF  -> {ok5}, kind = {win5.book.kind if win5.book else None}")
print(f"  scan PDF  -> {ok6}, kind = {win5.book.kind if win5.book else None}")
print(f"  exec_dialog called {calls5['n']} time(s) (expect 0)")

print()
print("=== H. pdf_default_kind='comic' skips the question entirely ===")
forced = Settings(pdf_default_kind="comic")
win6 = MiniWindow(forced, progress, library)
win6._quit_on_close = False
win6.resize(480, 360)
win6.show()
_app.processEvents()
calls6 = {"n": 0}


def fake_exec_forced(dialog):
    calls6["n"] += 1
    return False


blank4 = make_blank_pdf(tmp / "blank4.pdf")
window_module.exec_dialog = fake_exec_forced
try:
    ok7 = win6.load_path(str(blank4))
finally:
    window_module.exec_dialog = real_exec
_app.processEvents()
print(f"  load_path -> {ok7}, kind = {win6.book.kind} (expect comic)")
print(f"  exec_dialog called {calls6['n']} time(s) (expect 0)")

for w in (win, win2, win3, win4, win5, win6):
    try:
        w._geom_timer.stop()
        w._progress_timer.stop()
        w._snap_timer.stop()
        w._leave_hide_timer.stop()
        w._opacity_anim.stop()
        w.autoscroller.stop()
        w.close()
        w.deleteLater()
    except Exception as exc:
        print(f"  dispose warning: {exc}")
_app.processEvents()
_app.sendPostedEvents(None, QEvent.DeferredDelete)

print()
print("ALL DONE")
