"""Qt binding compatibility layer.

MiniReader is written against a flat-enum, Qt5/Qt6-agnostic subset so that it runs
on whichever binding is already installed:

    PySide6  (preferred, Qt 6)  <- `pip install PySide6`
    PyQt5    (Qt 5.15)          <- verified on this machine, no install needed

PyQt6 is deliberately NOT supported yet: it dropped the flat enum form
(`Qt.FramelessWindowHint`), and rewriting every flag as `Qt.WindowType.X` would
make the one file that matters least readable for no runtime gain.

Everything binding-specific lives here, so the rest of the project imports Qt
symbols from `minireader.qt` and never touches PyQt5/PySide6 directly.
"""

from __future__ import annotations

import os
import sys

BINDING: str
QT_VERSION: str
IS_QT6: bool

try:  # ---- PySide6 (Qt 6) -------------------------------------------------------
    from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    from PySide6.QtCore import Signal, Slot  # noqa: F401

    BINDING = "PySide6"
    IS_QT6 = True
except ImportError:  # pragma: no cover - depends on the machine
    try:  # ---- PyQt5 (Qt 5.15) --------------------------------------------------
        from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: F401
        from PyQt5.QtCore import pyqtSignal as Signal  # noqa: F401
        from PyQt5.QtCore import pyqtSlot as Slot  # noqa: F401

        BINDING = "PyQt5"
        IS_QT6 = False
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "MiniReader needs a Qt binding. Install one of:\n"
            "    py -3.12 -m pip install PySide6      # Qt 6, preferred\n"
            "    py -3.12 -m pip install PyQt5        # Qt 5.15, smallest\n"
            f"(original error: {exc})"
        ) from exc

QT_VERSION = QtCore.qVersion()

# QtNetwork is optional at runtime: the single-instance handshake (QLocalServer /
# QLocalSocket) is its only user, and a minimal binding install without it must still
# run. Imported through these names, the rest of the app degrades to "no single-instance
# check" rather than failing to start.
def _qtnetwork(name: str):
    from importlib import import_module

    try:
        return getattr(import_module(f"{BINDING}.QtNetwork"), name)
    except (ImportError, AttributeError):  # pragma: no cover - depends on the install
        return None


QLocalServer = _qtnetwork("QLocalServer")
QLocalSocket = _qtnetwork("QLocalSocket")

# ---------------------------------------------------------------------------
# Flat enum aliases. PySide6 and PyQt5 both accept these; PyQt6 would not.
# ---------------------------------------------------------------------------

Qt = QtCore.Qt
QPoint = QtCore.QPoint
QRect = QtCore.QRect
QTimer = QtCore.QTimer
QElapsedTimer = QtCore.QElapsedTimer
QEasingCurve = QtCore.QEasingCurve
QVariantAnimation = QtCore.QVariantAnimation
QPropertyAnimation = QtCore.QPropertyAnimation
QEvent = QtCore.QEvent
QSize = QtCore.QSize
QRectF = QtCore.QRectF
QAbstractNativeEventFilter = QtCore.QAbstractNativeEventFilter
QObject = QtCore.QObject

QFont = QtGui.QFont
QEnterEvent = QtGui.QEnterEvent
QFontMetrics = QtGui.QFontMetrics
QColor = QtGui.QColor
QTextCursor = QtGui.QTextCursor
QTextBlockFormat = QtGui.QTextBlockFormat
QTextOption = QtGui.QTextOption
QGuiApplication = QtGui.QGuiApplication
QCursor = QtGui.QCursor
QPainter = QtGui.QPainter
QPainterPath = QtGui.QPainterPath
QPixmap = QtGui.QPixmap
QIcon = QtGui.QIcon
QPen = QtGui.QPen
QBrush = QtGui.QBrush
# Needed to synthesise input in the headless tests, and by anything that forwards
# events. QPointF lives in QtCore on both bindings.
QPointF = QtCore.QPointF
QMimeData = QtCore.QMimeData
QUrl = QtCore.QUrl
QWheelEvent = QtGui.QWheelEvent
QKeyEvent = QtGui.QKeyEvent
QMouseEvent = QtGui.QMouseEvent
QContextMenuEvent = QtGui.QContextMenuEvent
QHelpEvent = QtGui.QHelpEvent

# 阶段 2 起漫画阅读器要用。QImageReader 之所以重要：它能在**不解码整张图**的前提下
# 问出图片尺寸（见 方案 §2.1⑥），这是连续滚动模式里占位块撑住滚动条的唯一依据。
# QBuffer 让 QImageReader 直接读内存里的 zip 成员，不必先落盘成临时文件。
QImage = QtGui.QImage
QImageReader = QtGui.QImageReader
QTransform = QtGui.QTransform
QBuffer = QtCore.QBuffer
# QPixmap.loadFromData 与 QImageReader 都吃 QByteArray；漫画每一页的原始字节正是
# 从这里进 Qt 的（zip 成员 -> bytes -> QByteArray -> QPixmap）。
QByteArray = QtCore.QByteArray

QApplication = QtWidgets.QApplication
QWidget = QtWidgets.QWidget
QTextEdit = QtWidgets.QTextEdit
QVBoxLayout = QtWidgets.QVBoxLayout
QHBoxLayout = QtWidgets.QHBoxLayout
QGridLayout = QtWidgets.QGridLayout
QFormLayout = QtWidgets.QFormLayout
QLabel = QtWidgets.QLabel
QToolButton = QtWidgets.QToolButton
QPushButton = QtWidgets.QPushButton
QFrame = QtWidgets.QFrame
# 正文的"内容透明度"靠它实现。永远只加在正文子控件上，不加在顶层窗口上：顶层已经有
# WA_TranslucentBackground + paintEvent，再叠一层 effect 会和原生窗口合成打架。
QGraphicsOpacityEffect = QtWidgets.QGraphicsOpacityEffect
QScrollArea = QtWidgets.QScrollArea
QSizeGrip = QtWidgets.QSizeGrip
QFileDialog = QtWidgets.QFileDialog
QDialog = QtWidgets.QDialog
QDialogButtonBox = QtWidgets.QDialogButtonBox
QSlider = QtWidgets.QSlider
QSpinBox = QtWidgets.QSpinBox
QCheckBox = QtWidgets.QCheckBox
QLineEdit = QtWidgets.QLineEdit
QGroupBox = QtWidgets.QGroupBox
QMenu = QtWidgets.QMenu
QMessageBox = QtWidgets.QMessageBox
QSystemTrayIcon = QtWidgets.QSystemTrayIcon
QToolTip = QtWidgets.QToolTip
QSizePolicy = QtWidgets.QSizePolicy
# 阅读器要在文本/漫画之间切换，靠 QStackedWidget 换页而不是重建控件——重建会丢掉
# 排版状态，也会让 window.py 里 self.reader 的绑定时机变得难猜（方案 §2.5）。
QStackedWidget = QtWidgets.QStackedWidget
# 漫画模式 A/B 要驱动自己的滚动条；AutoScroller 只认 value()/maximum()/setValue()，
# 所以它需要一个真的 QScrollBar（tests/test_autoscroll.py 早就这么做了）。
QScrollBar = QtWidgets.QScrollBar

# The 章节目录 list is virtualised: a real book has a few thousand chapters and a
# pathological one had 165,858, and a QListWidget with that many items takes seconds to
# populate. A model + view only asks for the rows actually on screen.
QListView = QtWidgets.QListView
QAbstractListModel = QtCore.QAbstractListModel
QModelIndex = QtCore.QModelIndex
QStyledItemDelegate = QtWidgets.QStyledItemDelegate
QStyleOptionViewItem = QtWidgets.QStyleOptionViewItem
QStyle = QtWidgets.QStyle
QTabWidget = QtWidgets.QTabWidget
QFontComboBox = QtWidgets.QFontComboBox
QColorDialog = QtWidgets.QColorDialog
# 阶段 3：设置对话框的「漫画与 PDF」分区用下拉框选枚举值（PDF 判型、缩放、模式、綴じ方向）。
# 用 QComboBox 而不是四个复选框组：每个字段都是"从几个互斥值里挑一个"，下拉框天然
# 表达这件事，而且 currentData() 让值可以跟着行一起走，不必拿显示文本反查。
QComboBox = QtWidgets.QComboBox
QTextOption = QtGui.QTextOption

QKeySequence = QtGui.QKeySequence
# QShortcut moved from QtWidgets (Qt 5) to QtGui (Qt 6).
QShortcut = getattr(QtGui, "QShortcut", None) or getattr(QtWidgets, "QShortcut")
# QAction moved the same way, and menus need it.
QAction = getattr(QtGui, "QAction", None) or getattr(QtWidgets, "QAction")


# ---------------------------------------------------------------------------
# Enum values that moved between Qt 5 and Qt 6
# ---------------------------------------------------------------------------

def _enum(owner, *names):
    """First attribute that exists on `owner`, else None. Qt 5 keeps enums flat on the
    class; Qt 6 nests them (`QSystemTrayIcon.ActivationReason.Trigger`)."""
    for name in names:
        value = getattr(owner, name, None)
        if value is not None:
            return value
    return None


# QEvent::ActivationChange is 99 in both Qt 5 and Qt 6, but it is spelled
# differently, and a wrong int here would silently make 失焦变淡 never fire.
EVENT_ACTIVATION_CHANGE = int(getattr(QEvent, "ActivationChange", 99))
EVENT_ACTIVATION_CHANGE = int(EVENT_ACTIVATION_CHANGE)

TRAY_TRIGGER = _enum(QSystemTrayIcon, "Trigger")
TRAY_DOUBLE_CLICK = _enum(QSystemTrayIcon, "DoubleClick")
TRAY_CONTEXT = _enum(QSystemTrayIcon, "Context")

# QTextBlockFormat::ProportionalHeight == 1. Spelled out because PyQt5 exposes the
# bare name while Qt 6 nests it as QTextBlockFormat.LineHeightTypes.ProportionalHeight.
PROPORTIONAL_HEIGHT = getattr(QTextBlockFormat, "ProportionalHeight", 1)


# ---------------------------------------------------------------------------
# Enum -> int. The two bindings disagree, and both directions are asymmetric.
# ---------------------------------------------------------------------------
#
# Measured on this machine:
#
#   PyQt5 5.15   Qt.LeftEdge        -> int() works, .value does NOT exist
#                QTextBlockFormat.ProportionalHeight -> int() works
#   PySide6 6.12 Qt.LeftEdge        -> int() RAISES, .value == 2 works
#                QTextBlockFormat.ProportionalHeight -> int() RAISES, .value == 1
#
# So neither `int(flag)` nor `flag.value` is portable on its own. `enum_value()`
# tries both and falls back to the value itself, which also makes it safe to call
# on a plain int (several call sites pass one).
#
# These exist because Qt 6 replaced `enum` with `enum.Enum` semantics: PySide6 6.12
# no longer lets a scoped enum convert to int implicitly. That is what turned 387
# tests red the first time this project ran under PySide6 - see
# docs/DEVELOPMENT.md constraint 17.

def enum_value(flag) -> int:
    """A Qt enum (or plain int) as a Python int, on either binding.

    Use this instead of `int(flag)` for **flags and enums**. `int(Qt.LeftEdge)`
    raises TypeError on PySide6 6.12; `Qt.LeftEdge.value` raises AttributeError on
    PyQt5 5.15. Both are fine for `enum_value(Qt.LeftEdge)`.
    """
    if flag is None:
        return 0
    if isinstance(flag, int):
        return int(flag)
    value = getattr(flag, "value", None)
    if isinstance(value, int):
        return int(value)
    try:
        return int(flag)
    except (TypeError, ValueError):
        return 0


#: Qt.LeftEdge / RightEdge / TopEdge / BottomEdge as plain ints, for `edges_at()`
#: and the resize maths. Qt 6 nests these under `Qt.Edge`; the numbers are the same
#: in Qt 5 and Qt 6 (Left=1, Top=2, Right=4, Bottom=8).
EDGE_LEFT = enum_value(_enum(Qt, "LeftEdge") or 1)
EDGE_RIGHT = enum_value(_enum(Qt, "RightEdge") or 4)
EDGE_TOP = enum_value(_enum(Qt, "TopEdge") or 2)
EDGE_BOTTOM = enum_value(_enum(Qt, "BottomEdge") or 8)


# ---------------------------------------------------------------------------
# Small API shims
# ---------------------------------------------------------------------------

def exec_app(app: "QApplication") -> int:
    """Qt 5 exposes exec_(), Qt 6 dropped the underscore."""
    runner = getattr(app, "exec_", None) or getattr(app, "exec")
    return int(runner())


def exec_dialog(dialog) -> bool:
    """Show a modal dialog. True when accepted. Same exec_/exec split as `exec_app`."""
    runner = getattr(dialog, "exec_", None) or getattr(dialog, "exec")
    return int(runner()) == int(QDialog.Accepted)


def available_screen_geometry(widget=None):
    """The usable area of the relevant screen, or None when there is no screen.

    `QApplication.desktop()` is **gone in Qt 6** - PySide6 raises AttributeError on
    it. The Qt 5.14+ replacement is `QGuiApplication.primaryScreen()`, and for a
    widget the better answer is `widget.screen()` (the screen it is actually on).
    Tried in that order, so a widget straddling two monitors reports its own.

    Returns None rather than raising: the offscreen test plugin has a screen, but a
    headless run need not, and every caller already has a "no screen, do nothing"
    path.
    """
    holder = None
    if widget is not None:
        getter = getattr(widget, "screen", None)
        if callable(getter):
            try:
                holder = getter()
            except Exception:  # noqa: BLE001 - a widget mid-destruction can raise
                holder = None
    if holder is None:
        app = QGuiApplication.instance()
        getter = getattr(app, "primaryScreen", None) if app is not None else None
        if callable(getter):
            holder = getter()
    if holder is None:
        # Qt 5 fallback, kept for a PyQt5 build whose QGuiApplication has no screen.
        desktop = getattr(QApplication, "desktop", None)
        if callable(desktop):
            try:
                holder = desktop()
            except Exception:  # noqa: BLE001
                holder = None
    if holder is None:
        return None
    try:
        return holder.availableGeometry()
    except Exception:  # noqa: BLE001
        return None


def event_global_pos(event) -> QPoint:
    """QMouseEvent.globalPos() -> QPoint (Qt 5) vs globalPosition() -> QPointF (Qt 6)."""
    getter = getattr(event, "globalPosition", None)
    if getter is not None:
        return getter().toPoint()
    return event.globalPos()


def event_local_pos(event) -> QPoint:
    getter = getattr(event, "position", None)
    if getter is not None:
        return getter().toPoint()
    return event.pos()


def enable_high_dpi() -> None:
    """Qt 5 needs these attributes before QApplication exists. Qt 6 always scales."""
    for name in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):
        attr = getattr(Qt, name, None)
        if attr is not None:
            try:
                QApplication.setAttribute(attr, True)
            except Exception:  # noqa: BLE001 - a removed attribute must not be fatal
                pass


def screen_rects() -> list[tuple[int, int, int, int]]:
    """[(x, y, w, h)] of every screen's *available* area, for geometry validation."""
    rects = []
    for screen in QGuiApplication.screens():
        g = screen.availableGeometry()
        rects.append((g.x(), g.y(), g.width(), g.height()))
    return rects


def native_platform_available() -> bool:
    """Offscreen/minimal QPA cannot host a real HWND, so global hotkeys are unavailable."""
    return QGuiApplication.platformName() not in ("offscreen", "minimal", "")


def install_message_filter(noise: tuple[str, ...]) -> None:
    """Silence Qt warnings whose text contains any of `noise`.

    Used by the headless tests: the offscreen plugin has no fonts and no window
    manager, so it emits the same two warnings on every run. Anything not matching is
    printed as usual, so a genuine Qt complaint is never hidden.
    """

    def handler(_mode, _context, message: str) -> None:
        if not any(needle in message for needle in noise):
            print(message, file=sys.stderr)

    try:
        QtCore.qInstallMessageHandler(handler)
    except Exception:  # noqa: BLE001 - diagnostics must never break a run
        pass


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exec_hint() -> str:
    """The exact command a user should type to run this checkout."""
    py = os.path.basename(sys.executable) or "python"
    if is_frozen():
        return "MiniReader.exe"
    return f'{py} -m minireader'
