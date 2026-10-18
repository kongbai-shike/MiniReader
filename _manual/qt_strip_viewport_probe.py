r"""探针：模式 B 的 viewport() 该返回 QScrollArea 还是它的内部 viewport？

window.py 把同一个 eventFilter 装在 `reader.viewport()` 上，它管两件事：
  1) 边缘缩放的 8px 热区——靠 **鼠标按下/移动** 事件（_route_edge_event）
  2) 鼠标进出正文时暂停自动滚动——靠 **Enter/Leave**

Enter/Leave 会向上传播到祖先（已实测），但**鼠标事件不会**：它只投递给指针下
最深的那一层控件。所以"返回 QScrollArea 就够了"只对第 2 件事成立。

这个探针直接量：在槽位上按下左键，装在 `_strip` 上的过滤器和装在
`_strip.viewport()` 上的过滤器，谁收到了事件。

跑法： py -3.12 _manual\qt_strip_viewport_probe.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import (  # noqa: E402
    BINDING,
    QApplication,
    QEvent,
    QObject,
    QPoint,
    Qt,
)
from minireader.ui.strip_view import StripView  # noqa: E402

_app = QApplication.instance() or QApplication([])


class Logger(QObject):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.hits: list[str] = []

    def eventFilter(self, obj, event):  # noqa: N802
        kind = event.type()
        if kind in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.Enter, QEvent.Leave):
            names = {
                QEvent.MouseButtonPress: "MouseButtonPress",
                QEvent.MouseMove: "MouseMove",
                QEvent.Enter: "Enter",
                QEvent.Leave: "Leave",
            }
            self.hits.append(f"{names[kind]} on {obj.objectName() or type(obj).__name__}")
        return False


def main() -> None:
    strip = StripView()
    strip.setObjectName("strip")
    strip.resize(600, 400)
    strip.set_pages([(120, 180)] * 10)
    strip.show()
    _app.processEvents()

    inner = strip.viewport()
    inner.setObjectName("strip_inner_viewport")
    slot = strip.slot(0)
    print(f"strip           = {strip.objectName()}")
    print(f"strip.viewport()= {inner.objectName()}  ({inner.width()}x{inner.height()})")
    print(f"slot 0          = {slot.objectName() or type(slot).__name__} "
          f"({slot.width()}x{slot.height()})")
    print()

    on_strip = Logger("on strip")
    on_inner = Logger("on inner")
    strip.installEventFilter(on_strip)
    inner.installEventFilter(on_inner)

    def report(title: str) -> None:
        print(f"--- {title}")
        print(f"    装在 strip 上的过滤器收到: {on_strip.hits or '（无）'}")
        print(f"    装在 inner 上的过滤器收到: {on_inner.hits or '（无）'}")
        on_strip.hits.clear()
        on_inner.hits.clear()

    # 1) 鼠标按下：落在槽位上，看谁收到
    # QtTest 从当前生效的绑定取，不写死 PyQt5（见 stage4_smoke.py 的同款注释）。
    QTest = __import__(f"{BINDING}.QtTest", fromlist=["QTest"]).QTest

    QTest.mousePress(slot, Qt.LeftButton, Qt.NoModifier, QPoint(30, 30))
    _app.processEvents()
    report("在槽位上按下左键")

    # 2) 鼠标移动
    QTest.mouseMove(slot, QPoint(40, 40))
    _app.processEvents()
    report("在槽位上移动鼠标")

    # 3) 直接在 inner viewport 上按下（空白区）
    QTest.mousePress(inner, Qt.LeftButton, Qt.NoModifier, QPoint(500, 380))
    _app.processEvents()
    report("在 inner viewport 空白处按下左键")

    print()
    print("结论判据：")
    print("  · 若 '在槽位上按下左键' 时只有 inner 收到，则 viewport() 必须返回")
    print("    strip.viewport()，否则模式 B 的边缘缩放热区会静默失效。")
    print("  · 这也正是文字 Reader 的现状：它返回 QTextEdit.viewport()（内层）。")

    strip.stop()
    strip.deleteLater()


if __name__ == "__main__":
    main()
