r"""收尾 2：用户点名的五个 Qt 行为，在两个绑定下逐条实测。

全量 discover 全绿只说明"没有崩溃"，不说明"行为一致"。这五条是阶段 4 依赖的
具体行为，任何一条在两个绑定下不同都会变成难查的 bug：

  1. `QScrollArea.setWidgetResizable` 的滚动范围是否同样"延迟到下一轮"
  2. `wheelEvent` 的投递与传播（阶段 4 缺陷 2 的关键）
  3. `QTimer` 去抖的精度
  4. `QStackedWidget` 的切换
  5. 信号连接（PySide6 的 Signal vs PyQt5 的 pyqtSignal）

脚本自己在两个绑定下各跑一遍（屏蔽 PySide6 走 PyQt5），把结果并排打印。

跑法： py -3.12 _manual\probe_binding_parity.py
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
import zipfile
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BLOCK = """
import builtins, sys
_real = builtins.__import__
def _fake(name, *a, **k):
    if name == "PySide6" or name.startswith("PySide6."):
        raise ImportError("blocked on purpose")
    return _real(name, *a, **k)
builtins.__import__ = _fake
for _m in [m for m in sys.modules if m.startswith("PySide6")]:
    del sys.modules[_m]
"""

_BODY = r'''
import os, sys, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, {root!r})

from minireader.qt import (
    BINDING, QT_VERSION, QApplication, QEvent, QPoint, QPointF, QTimer,
    QStackedWidget, QWidget, QLabel, QWheelEvent, Qt, Signal, enum_value,
)
from minireader.ui.strip_view import StripView

app = QApplication.instance() or QApplication([])
print("@@BINDING@@" + BINDING + " " + QT_VERSION)

def show(label, value):
    print("@@%s@@%s" % (label, value))

# --- 1. setWidgetResizable 的滚动范围时机 --------------------------------
strip = StripView()
strip.resize(600, 400)
strip.show()
app.processEvents()
strip.set_pages([(120, 180)] * 30)
bar = strip.verticalScrollBar()
immediate = bar.maximum()
app.processEvents()
after_one = bar.maximum()
app.processEvents()
after_two = bar.maximum()
show("range_immediate", immediate)
show("range_after1", after_one)
show("range_after2", after_two)
show("range_expected", max(0, strip.total_height() - strip.viewport().height()))

# --- 2. wheel 投递与传播 -------------------------------------------------
strip.scroll_to_page(0)
app.processEvents()
slot = strip.slot(0)
before = bar.value()
pos = QPointF(slot.width() / 2, slot.height() / 2)
ev = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                 Qt.NoModifier, Qt.NoScrollPhase, False)
app.sendEvent(slot, ev)
app.processEvents()
show("wheel_moved_by", bar.value() - before)
show("wheel_accepted", ev.isAccepted())

# 滚轮**不会**向父控件传播：发给槽位，看 StripView 自己的 wheelEvent 收到几次
calls = []
orig = strip.wheelEvent
strip.wheelEvent = lambda e: (calls.append(1), orig(e))[1]
ev2 = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                  Qt.NoModifier, Qt.NoScrollPhase, False)
app.sendEvent(slot, ev2)
app.processEvents()
show("wheel_reached_strip", len(calls))
strip.wheelEvent = orig

# --- 3. QTimer 去抖精度 --------------------------------------------------
fired = []
t = QTimer()
t.setSingleShot(True)
t.setInterval(60)
t.timeout.connect(lambda: fired.append(time.perf_counter()))
start = time.perf_counter()
t.start()
deadline = start + 0.5
while not fired and time.perf_counter() < deadline:
    app.processEvents()
    time.sleep(0.002)
if fired:
    show("timer_ms", round((fired[0] - start) * 1000))
    show("timer_fired", True)
else:
    show("timer_fired", False)

# 去抖：连发多次，只有最后一次之后才 fire
strip.scroll_to_page(0)
app.processEvents()
band_hits = []
strip.bandChanged.connect(lambda a, b: band_hits.append((a, b)))
for i in range(5):
    bar.setValue(i * 10)
    app.processEvents()
    time.sleep(0.005)
time.sleep(0.12)
app.processEvents()
show("debounce_fires", len(band_hits))

# --- 4. QStackedWidget 切换 ----------------------------------------------
stack = QStackedWidget()
a, b = QWidget(), QWidget()
a.setObjectName("A"); b.setObjectName("B")
stack.addWidget(a); stack.addWidget(b)
stack.resize(200, 100); stack.show()
app.processEvents()
show("stack_count", stack.count())
show("stack_initial", stack.currentIndex())
stack.setCurrentWidget(b)
app.processEvents()
show("stack_after_set", stack.currentIndex())
show("stack_current_is_b", stack.currentWidget() is b)

# --- 5. 信号连接 ---------------------------------------------------------
seen = []
class Emitter(QWidget):
    ping = Signal(int)
em = Emitter()
em.ping.connect(lambda v: seen.append(v))
em.ping.emit(7)
app.processEvents()
show("signal_seen", seen)
show("signal_has_connect", hasattr(em.ping, "connect"))
show("signal_has_disconnect", hasattr(em.ping, "disconnect"))

# 一个 signal 连两个槽，按连接顺序都收到
order = []
em.ping.connect(lambda v: order.append("second"))
em.ping.emit(9)
app.processEvents()
show("signal_order", order)

# --- 6. enum 转换（阶段 4 收尾新增的兼容层） ------------------------------
show("enum_LeftEdge", enum_value(Qt.LeftEdge))
show("enum_NoModifier", enum_value(Qt.NoModifier))
show("enum_plain_int", enum_value(5))
show("enum_None", enum_value(None))

strip.stop(); strip.deleteLater()
'''


def make_fixture() -> str:
    path = os.path.join(os.environ["TEMP"], "mr-parity.cbz")
    if os.path.exists(path):
        return path

    def png(w: int, h: int) -> bytes:
        raw = b"".join(b"\x00" + b"\x55" * (w * 3) for _ in range(h))

        def c(t: bytes, d: bytes) -> bytes:
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n"
                + c(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + c(b"IDAT", zlib.compress(raw)) + c(b"IEND", b""))

    with zipfile.ZipFile(path, "w") as zf:
        for i in range(30):
            zf.writestr(f"page_{i + 1:03d}.png", png(120, 180))
    return path


def run(block: bool) -> dict[str, str]:
    code = (_BLOCK if block else "") + _BODY.format(root=ROOT)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", cwd=ROOT, env=env)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        # 协议是 `@@KEY@@VALUE`（**两个** `@@`，不是四个）：`split("@@", 2)` 给
        # ['', KEY, VALUE]。上一版按四个分隔符切，于是每个键都取不到，两个绑定
        # 都报 "?"——量到的是解析器的 bug，不是绑定的行为。
        if line.startswith("@@") and line.count("@@") == 2:
            _, key, value = line.split("@@", 2)
            out[key] = value
    if not out:
        print("没有输出，stderr：")
        print((proc.stderr or "")[-2000:])
    return out


def main() -> None:
    make_fixture()
    print(f"Python {sys.version.split()[0]}")
    print("两个绑定各跑一遍同样的探针\n")

    results = {"PySide6": run(False), "PyQt5": run(True)}
    bindings = {name: data.get("BINDING", "?") for name, data in results.items()}
    for name, binding in bindings.items():
        print(f"{name}: {binding}")
    print()

    keys = [k for k in results["PySide6"] if k != "BINDING"]
    width = max(len(k) for k in keys) + 2
    print(f"{'探针'.ljust(width)}{'PySide6'.ljust(22)}{'PyQt5'.ljust(22)}一致")
    print("-" * (width + 48))
    same = True
    for key in keys:
        left = results["PySide6"].get(key, "-")
        right = results["PyQt5"].get(key, "-")
        if key == "timer_ms":
            # 定时器是"不早于"，不是"正好"。60ms 的定时器实测 64/62ms 属于正常的
            # 调度抖动，不是绑定差异——所以这里比的是"落在同一档"，不是相等。
            equal = abs(int(left) - int(right)) <= 20
        else:
            equal = left == right
        same = same and equal
        print(f"{key.ljust(width)}{left.ljust(22)}{right.ljust(22)}{'是' if equal else '**否**'}")
    print()
    print("两个绑定行为一致" if same else "有行为差异，需要逐条判断")


if __name__ == "__main__":
    main()
