"""打包基线：分阶段 import（每个阶段一个干净子进程）+ 端到端启动 + 内存。"""
import os, pathlib, subprocess, sys, tempfile

PY = sys.executable
ROOT = pathlib.Path(r"C:\Users\pc\Desktop\随手文件\MiniReader")
ENV = dict(os.environ, QT_QPA_PLATFORM="windows", PYTHONIOENCODING="utf-8")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="mr-stage-"))

STAGES = [
    ("解释器 + ctypes", "import ctypes"),
    ("+ PyQt5.QtCore", "import PyQt5.QtCore"),
    ("+ PyQt5.QtGui", "import PyQt5.QtGui"),
    ("+ PyQt5.QtWidgets", "import PyQt5.QtWidgets"),
    ("+ 本项目 qt/storage/hotkeys/core", "import minireader.qt, minireader.storage, minireader.hotkeys, minireader.core"),
    ("+ ui.window（含全部 UI 模块）", "import minireader.ui.window"),
    ("+ QApplication 实例化", "from PyQt5.QtWidgets import QApplication\nQApplication([])"),
]

print("=== 分阶段 import 成本（每阶段独立子进程，取 3 次中位数）===")
print("    （数值 = 该阶段代码耗时，不含解释器自身启动）")
rows = []
for label, code in STAGES:
    src = TMP / (label.replace("/", "_").replace(" ", "_").replace("+", "") + ".py")
    src.write_text(
        "import sys, time\n"
        "sys.path.insert(0, %r)\n" % str(ROOT)
        + "t = time.perf_counter()\n"
        + code + "\n"
        + "print('%.1f' % ((time.perf_counter() - t) * 1000))\n",
        encoding="utf-8",
    )
    times = []
    for _ in range(3):
        r = subprocess.run([PY, str(src)], capture_output=True, text=True, env=ENV)
        if r.returncode != 0:
            times = None
            err = r.stderr.strip().splitlines()[-1][:100]
            break
        times.append(float(r.stdout.strip().splitlines()[-1]))
    if times:
        med = sorted(times)[1]
        rows.append((label, med))
        print(f"  {label:<34} {med:7.1f} ms")

print("\n  累计（从最小到最大）：")
acc = 0.0
for label, med in rows:
    acc += med
    print(f"    {acc:7.1f} ms  ← {label}")

print("\n=== 端到端：进程启动 → 窗口显示（523 章真书）===")
for i in range(3):
    r = subprocess.run([PY, str(ROOT / "_pk_driver.py")], capture_output=True, text=True, env=ENV)
    print(f"  --- 第 {i+1} 次 ---")
    for line in r.stdout.strip().splitlines():
        print("   ", line)
    if r.returncode != 0:
        print("    stderr:", r.stderr.strip()[-400:])
