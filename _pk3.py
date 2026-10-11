"""找出启动时间里我们自己那一份花在哪：逐个模块量成本。"""
import os, pathlib, subprocess, sys, tempfile

PY = sys.executable
ROOT = pathlib.Path(r"C:\Users\pc\Desktop\随手文件\MiniReader")
ENV = dict(os.environ, QT_QPA_PLATFORM="windows", PYTHONIOENCODING="utf-8")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="mr-mod-"))

CASES = [
    ("标准库 zipfile", "import zipfile", False),
    ("标准库 xml.etree.ElementTree", "import xml.etree.ElementTree", False),
    ("标准库 sqlite3", "import sqlite3", False),
    ("标准库 codecs+re+unicodedata", "import codecs, re, unicodedata", False),
    ("minireader.snap", "import minireader.snap", True),
    ("minireader.storage", "import minireader.storage", True),
    ("minireader.hotkeys", "import minireader.hotkeys", True),
    ("minireader.core.encoding", "import minireader.core.encoding", True),
    ("minireader.core.chapters", "import minireader.core.chapters", True),
    ("minireader.core.epub", "import minireader.core.epub", True),
    ("minireader.core（包 __init__）", "import minireader.core", True),
    ("minireader.core.model（会拉 core 包）", "import minireader.core.model", True),
    ("minireader.store", "import minireader.store", True),
    ("minireader.ui.theme", "import minireader.ui.theme", True),
    ("minireader.ui.window（全部 UI）", "import minireader.ui.window", True),
]

print("=== 单个模块的 import 成本（独立子进程 × 3 取中位数）===")
for label, code, needs_qt in CASES:
    lines = ["import sys, time", "sys.path.insert(0, %r)" % str(ROOT)]
    if needs_qt:
        lines.append("import PyQt5.QtWidgets")
    lines.append("t = time.perf_counter()")
    lines.append(code)
    lines.append("print('%.1f' % ((time.perf_counter() - t) * 1000))")
    src = TMP / (label.replace(".", "_").replace(" ", "_") + ".py")
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    times = []
    for _ in range(3):
        r = subprocess.run([PY, str(src)], capture_output=True, text=True, env=ENV)
        if r.returncode != 0:
            times = None
            print(f"  {label:<38} 失败: {r.stderr.strip().splitlines()[-1][:80]}")
            break
        times.append(float(r.stdout.strip().splitlines()[-1]))
    if times:
        print(f"  {label:<38} {sorted(times)[1]:6.1f} ms")

print("\n=== core 包的 __init__ 是不是把 epub（zipfile + ElementTree）拉进来了 ===")
lines = [
    "import sys, time",
    "sys.path.insert(0, %r)" % str(ROOT),
    "import PyQt5.QtWidgets",
    "t = time.perf_counter()",
    "import minireader.core.model",
    "print('import minireader.core.model = %.1f ms' % ((time.perf_counter() - t) * 1000))",
    "print('zipfile 进了 sys.modules :', 'zipfile' in sys.modules)",
    "print('ElementTree 进了 sys.modules :', 'xml.etree.ElementTree' in sys.modules)",
    "print('core.epub 进了 sys.modules :', 'minireader.core.epub' in sys.modules)",
    "print('pyexpat 进了 sys.modules :', 'pyexpat' in sys.modules)",
]
src = TMP / "lazy.py"
src.write_text("\n".join(lines) + "\n", encoding="utf-8")
r = subprocess.run([PY, str(src)], capture_output=True, text=True, env=ENV)
print(r.stdout.strip() or r.stderr.strip()[-400:])

print("\n=== 对照：完全不碰 core 包 ===")
lines[4] = "import minireader.storage"
lines[5] = "print('import minireader.storage  = %.1f ms' % ((time.perf_counter() - t) * 1000))"
src.write_text("\n".join(lines) + "\n", encoding="utf-8")
r = subprocess.run([PY, str(src)], capture_output=True, text=True, env=ENV)
print(r.stdout.strip() or r.stderr.strip()[-400:])
