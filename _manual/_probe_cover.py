import os, sys, struct, zipfile, zlib, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from minireader.qt import QApplication
app = QApplication.instance() or QApplication([])


def png(w, h):
    raw = b"".join(b"\x00" + b"\x66" * (w * 3) for _ in range(h))

    def c(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + c(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + c(b"IDAT", zlib.compress(raw)) + c(b"IEND", b""))


tmp = tempfile.mkdtemp()
os.environ["APPDATA"] = tmp
p = os.path.join(tmp, "wide.cbz")
with zipfile.ZipFile(p, "w") as zf:
    for i in range(6):
        zf.writestr("w%d.png" % i, png(1200, 700))

from minireader.storage import Settings, Library, ProgressStore
from minireader.ui.window import MiniWindow

for mode in ("single", "spread"):
    s = Settings(wheel_animation_ms=0)
    s.comic_default_mode = mode
    s.comic_cover_alone = False
    w = MiniWindow(s, ProgressStore.load(), Library.load())
    w.resize(1200, 800)
    w.show()
    for _ in range(3):
        app.processEvents()
    w.load_path(p)
    for _ in range(3):
        app.processEvents()
    r = w._comic_reader
    pair = r._spread.pair if r.mode == "spread" else "n/a"
    print("default_mode=%-7s reader.mode=%-7s cover_setting=%s cover_alone=%s pair=%s"
          % (mode, r.mode, r._cover_setting, r.cover_alone, pair))

import shutil
shutil.rmtree(tmp, ignore_errors=True)
