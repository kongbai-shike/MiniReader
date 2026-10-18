r"""收尾 1：300 个空 PageView 到底占多少内存。

报告 §11 原来写的是"每个空控件几十字节"——那是估的，而且明显不对。
这个脚本把它量出来，并且用**能重复**的方法量。

## 为什么不能直接"造 N 个、看 RSS 涨多少"

第一版就是这么写的，结果同一个脚本三次跑出 516 / 2410 / 2273 字节/个。
原因：RSS 是进程级读数，受 Python 和 Qt 各自的分配器"竞技场"影响——
第一次分配会向系统要一整块内存，之后从池子里切；池子够不够、什么时候再要
一块，取决于之前分配过什么。在一个复用的进程里读"造 N 个前后之差"，
量到的很大一部分是**分配器的历史**，不是控件的成本。

## 用边际成本

同一个进程里量两个数量级（N1=2000、N2=8000），取

    (RSS(N2) - RSS(N1)) / (N2 - N1)

固定开销（解释器、Qt、分配器已持有的池子）在相减时抵消掉，剩下的是
**第 2001 到第 8000 个控件每个的真实边际成本**。再把这个测量放进**全新
子进程**里跑，避免上一次测量留下的池子污染。

Python 侧另用 tracemalloc 量（它只看 Python 堆，很稳），和边际 RSS 对照，
就能把"Python 包装器"和"C++ 对象"分开。

跑法： py -3.12 _manual\measure_pageview_memory.py
      加 --child 时只跑一轮并把 RSS 打成 JSON（父进程用）
"""

from __future__ import annotations

import ctypes
import gc
import json
import os
import subprocess
import sys
import tracemalloc
from ctypes import wintypes

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

N1 = 2000
N2 = 8000


# ---------------------------------------------------------------------------
# 进程 RSS（Windows）
# ---------------------------------------------------------------------------

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_bytes() -> int:
    """当前进程的工作集（RSS），字节。

    `argtypes`/`restype` 必须显式声明。不声明的话，64 位下
    `GetCurrentProcess()` 返回的 HANDLE 会被 ctypes 压成 32 位，调用直接失败
    （实测返回 0）——第一版就是这么错的。
    """
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    func = ctypes.windll.psapi.GetProcessMemoryInfo
    func.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    func.restype = wintypes.BOOL
    if not func(ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo 失败")
    return int(counters.WorkingSetSize)


def measure(count: int, make) -> int:
    """造 `count` 个控件，返回 RSS 读数。

    **控件必须有父控件。** 无父的 `QWidget()` 是顶层窗口，Qt 会给它建原生窗口
    表面——第一版就是这么量的，量出 ~5000 字节/个，而那是"顶层窗口"的成本，
    不是"页面槽位"的成本。真实的槽位是 `PageView(self._content)`，有父、不是
    窗口，所以这里也挂在一个容器下面。
    """
    gc.collect()
    _app.processEvents()
    holder = QWidget()
    widgets = [make(holder) for _ in range(count)]
    _app.processEvents()
    value = rss_bytes()
    for widget in widgets:
        widget.deleteLater()
    holder.deleteLater()
    del widgets, holder
    gc.collect()
    _app.processEvents()
    return value


def python_bytes_per_widget(make, count: int) -> float:
    """Python 堆增量 / count。C++ 侧不计入——这正是要和 RSS 对比的原因。"""
    gc.collect()
    holder = QWidget()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    widgets = [make(holder) for _ in range(count)]
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    total = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    for widget in widgets:
        widget.deleteLater()
    holder.deleteLater()
    del widgets, holder
    gc.collect()
    return total / count


# ---------------------------------------------------------------------------
# 子进程模式：只量一轮，把结果打成 JSON
# ---------------------------------------------------------------------------

def child_main() -> None:
    """在一个**全新进程**里造 `--count N` 个控件，量一次 RSS，然后退出。

    关键是这里**不释放、不第二轮**。上一版在同一个进程里量 N1 再量 N2，
    中间把 N1 释放掉——而释放掉的内存不会还给操作系统，第二轮的读数里
    混着第一轮复用过的池子，算出来的边际成本偏高（实测 ~5000 而不是 ~700）。
    每个数量一个全新进程，就没有"复用"这件事。
    """
    from minireader.qt import BINDING, QT_VERSION
    from minireader.ui.page_view import PageView

    count = 0
    if "--count" in sys.argv:
        count = int(sys.argv[sys.argv.index("--count") + 1])
    factory = PageView if "--pageview" in sys.argv else QWidget

    gc.collect()
    _app.processEvents()
    before = rss_bytes()
    holder = QWidget()
    widgets = [factory(holder) for _ in range(count)]
    _app.processEvents()
    after = rss_bytes()
    print("@@JSON@@" + json.dumps({
        "binding": BINDING, "qt": QT_VERSION,
        "count": count, "delta": after - before,
    }))
    del widgets, holder


def parent_main() -> None:
    from minireader.qt import BINDING, QT_VERSION
    from minireader.ui.page_view import PageView

    print(f"Python {sys.version.split()[0]}")
    print(f"绑定 {BINDING} / Qt {QT_VERSION}")
    print("每个数量一个全新子进程（不释放、不第二轮），取多轮最小值\n")

    def run(count: int, pageview: bool) -> int:
        """跑一个全新子进程，返回 RSS 增量（字节）。"""
        cmd = [sys.executable, os.path.abspath(__file__), "--child", "--count", str(count)]
        if pageview:
            cmd.append("--pageview")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("@@JSON@@")), None)
        if line is None:
            raise RuntimeError(f"子进程没给出结果：{proc.stdout[-500:] or proc.stderr[-500:]}")
        return json.loads(line[len("@@JSON@@"):])["delta"]

    def marginal(pageview: bool, rounds: int = 3) -> tuple[float, list[int]]:
        """(N2-N1) 之间的边际成本，字节/个。"""
        samples = []
        for _ in range(rounds):
            d1 = run(N1, pageview)
            d2 = run(N2, pageview)
            samples.append(round((d2 - d1) / (N2 - N1)))
        return min(samples), samples

    print(f"N1={N1}, N2={N2}，边际 = (delta(N2)-delta(N1))/{N2 - N1}\n")
    results = {}
    for pageview, label in ((False, "裸 QWidget（有父、非窗口）"),
                            (True, "空 PageView（有父、非窗口）")):
        best, samples = marginal(pageview)
        results[label] = best
        py = python_bytes_per_widget(PageView if pageview else QWidget, 4000)
        print(f"--- {label} ---")
        print(f"  边际 RSS：{best:.0f} 字节/个（三轮 {samples}）")
        print(f"  Python 堆：{py:.0f} 字节/个（tracemalloc，4000 个）")
        print(f"  C++ 侧  ：约 {best - py:.0f} 字节/个")

    print()
    per = results["空 PageView（有父、非窗口）"]
    print(f"**300 个空 PageView 的实测总代价：{per * 300 / 1024:.0f} KB**"
          f"（{per:.0f} 字节/个）")
    pixmap = 120 * 180 * 4
    print(f"对照：一张 120×180 RGB 页解码后约 {pixmap / 1024:.0f} KB；"
          f"300 个空控件 ≈ {per * 300 / pixmap:.1f} 张已解码页")
    print(f"      默认缓存预算 256 MB，等于约 {256 * 1048576 / pixmap:.0f} 张这样的页")
    print(f"      真实漫画页（如 1224×1584）一张约 {1224 * 1584 * 4 / 1048576:.1f} MB，"
          f"300 个空控件 ≈ {per * 300 / (1224 * 1584 * 4):.2f} 张")


if __name__ == "__main__":
    from minireader.qt import QApplication, QWidget  # noqa: E402

    _app = QApplication.instance() or QApplication([])
    if "--child" in sys.argv:
        child_main()
    else:
        parent_main()
