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
