r"""探针：Qt 会不会把子控件未接受的滚轮事件向父控件传播？

这决定了模式 B 怎么接线。真实场景里鼠标停在 PageView 槽位上，而槽位不处理滚轮，
所以"滚轮能不能滚动整条漫画"完全取决于这个传播行为。

三层嵌套：parent <- child <- grandchild。给最里层发滚轮，看谁收到。
三种投递方式都试：sendEvent / postEvent+processEvents / 手工冒泡模拟。

跑法： py -3.12 _manual\qt_wheel_propagation_probe.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import (  # noqa: E402
    QApplication,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QWheelEvent,
    QWidget,
    Qt,
)

_app = QApplication.instance() or QApplication([])


class Watcher(QObject):
    """记录自己看到的事件，并且**不**接受它（模拟 PageView 的默认行为）。"""

    def __init__(self, name: str, accept: bool) -> None:
        super().__init__()
        self.name = name
        self.accept = accept
        self.seen: list[str] = []

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Wheel:
            self.seen.append(f"filter-on-{self.name}")
        return False

    def wheelEvent(self, event):  # noqa: N802
        self.seen.append(f"wheelEvent-of-{self.name}")
        if self.accept:
            event.accept()
        else:
            event.ignore()


def build(accept_inner: bool):
    parent = QWidget()
    parent.setObjectName("parent")
    child = QWidget(parent)
    child.setObjectName("child")
    grand = QWidget(child)
    grand.setObjectName("grandchild")
    for w in (parent, child, grand):
        w.resize(300, 300)

    watchers = {}
    for name, w in (("parent", parent), ("child", child), ("grandchild", grand)):
        wat = Watcher(name, accept_inner if name == "grandchild" else False)
        w.installEventFilter(wat)
        watchers[name] = wat
    # 也给控件本身一个 wheelEvent（用子类不方便，改用事件过滤器已经覆盖）
    parent.show()
    _app.processEvents()
    return parent, grand, watchers


def make_event() -> QWheelEvent:
    pos = QPointF(10.0, 10.0)
    return QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                       Qt.NoModifier, Qt.NoScrollPhase, False)


def main() -> None:
    for accept_inner in (False, True):
        print(f"\n{'=' * 68}")
        print(f"最里层 wheelEvent 是否 accept：{accept_inner}")
        print(f"{'=' * 68}")
        for how in ("sendEvent", "postEvent", "手工冒泡模拟"):
            parent, grand, watchers = build(accept_inner)
            for wat in watchers.values():
                wat.seen.clear()

            ev = make_event()
            if how == "sendEvent":
                _app.sendEvent(grand, ev)
            elif how == "postEvent":
                _app.postEvent(grand, ev)
                _app.processEvents()
            else:
                # 模拟 Qt 的传播：谁不接受就交给父控件
                target = grand
                while target is not None:
                    _app.sendEvent(target, make_event())
                    break  # 只发一次，用来对比"只发给最里层"和"逐层发"
                target = grand
                while target is not None:
                    e = make_event()
                    _app.sendEvent(target, e)
                    if e.isAccepted():
                        break
                    target = target.parentWidget()

            hits = []
            for name in ("grandchild", "child", "parent"):
                hits.extend(watchers[name].seen)
            print(f"  {how:<12} 收到顺序: {hits or '（谁都没收到）'}")
            parent.deleteLater()


if __name__ == "__main__":
    main()
