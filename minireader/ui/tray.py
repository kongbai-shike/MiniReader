"""Tray icon: the window's escape hatch when the window itself is hidden.

This is the piece that changes the boss-key safety rule. Without a tray icon, hiding a
window whose global hotkey failed to register is unrecoverable, so the MVP refused to
hide at all. With a tray icon there is always a visible way back, so hiding is allowed
even when every hotkey is dead - `MiniWindow.toggle_boss()` gates on "hotkey OR tray".

Two Qt details that bite:

* **The menu must stay referenced.** `QSystemTrayIcon.setContextMenu()` does not take
  ownership on the Python side, and a `QMenu` that only exists as a local is collected,
  leaving a tray icon that does nothing when right-clicked.
* **Only some QPA platforms have a tray at all.** `isSystemTrayAvailable()` is False on
  the offscreen plugin, so the app must run headless without a tray - which is also why
  the tray is optional in the settings.

The icon prefers the shipped `assets/minireader.ico` (also used as the exe icon, so the
tray matches the taskbar) through `app_paths.resource_path`, which resolves correctly
both in a source checkout and under PyInstaller's `sys._MEIPASS`. If the file is
missing, a QPainter-drawn glyph is the fallback - no binary asset, still valid.
"""

from __future__ import annotations

import os

from ..app_paths import resource_path
from ..qt import (
    QColor,
    QIcon,
    QMenu,
    QPainter,
    QPen,
    QPixmap,
    QSize,
    QSystemTrayIcon,
    Qt,
    Signal,
    TRAY_DOUBLE_CLICK,
    TRAY_TRIGGER,
)
from .theme import PALETTE

OPACITY_PRESETS = (100, 90, 80, 70, 60, 50, 40, 30)


def make_icon(size: int = 64) -> QIcon:
    """A "page with text lines" glyph, valid on light and dark taskbars.

    Every size is drawn rather than scaled so the 16 px tray icon keeps crisp lines.
    """
    icon = QIcon()
    for dimension in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(dimension, dimension)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            # Mid-luminance fill: keeps the glyph visible against both a light and a
            # dark taskbar, unlike a white or a black one.
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(PALETTE["accent"]))
            radius = dimension * 0.22
            inset = dimension * 0.07
            painter.drawRoundedRect(
                int(inset),
                int(inset),
                int(dimension - 2 * inset),
                int(dimension - 2 * inset),
                radius,
                radius,
            )
            # Text lines, in the dark surface colour so they read as text on paper.
            pen = QPen(QColor(PALETTE["bg"]))
            pen.setWidthF(max(1.0, dimension * 0.075))
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            widths = (0.52, 0.44, 0.52, 0.30)
            left, right = 0.26, 0.74
            for index, fraction in enumerate(widths):
                y = dimension * (0.30 + index * 0.145)
                x0 = dimension * left
                x1 = x0 + dimension * (right - left) * (fraction / 0.52)
                painter.drawLine(int(x0), int(y), int(x1), int(y))
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def load_tray_icon() -> QIcon:
    """The shipped .ico when it exists, else the drawn glyph.

    Two candidate names: packaging uses `--add-data "icon.ico;."` (bundle root), while
    the source checkout keeps the asset at `assets/minireader.ico`. `isNull()` is the
    real check - `QIcon(path)` on an unsupported file constructs fine and only fails
    when it is asked to render.
    """
    for relative in ("assets/minireader.ico", "icon.ico"):
        path = resource_path(relative)
        if os.path.isfile(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return make_icon()


class TrayIcon(QSystemTrayIcon):
    """Tray icon plus its menu. Emits intents; `MiniWindow` decides what they mean.

    Show and hide are **separate** intents (`showRequested` / `hideRequested`): clicking
    the tray icon always *restores*, never hides - a user who clicks an invisible app's
    icon is trying to get it back, and a click that hides again is a trap. The menu
    carries both directions explicitly.
    """

    # Kept for compatibility with test doubles; the menu and the icon itself now speak
    # show/hide, and `toggleRequested` is no longer emitted by this class.
    toggleRequested = Signal()
    showRequested = Signal()
    hideRequested = Signal()
    openRequested = Signal()
    settingsRequested = Signal()
    quitRequested = Signal()
    opacityRequested = Signal(int)

    def __init__(self, parent=None, *, title: str = "MiniReader") -> None:
        super().__init__(load_tray_icon(), parent)
        self._hidden = False
        self._opacity = 100
        # Held on the instance: see the module docstring.
        self._menu = QMenu()
        self.setContextMenu(self._menu)
        self._show_action = self._menu.addAction("显示窗口", self.showRequested.emit)
        self._hide_action = self._menu.addAction("隐藏窗口", self.hideRequested.emit)
        self._menu.addSeparator()
        self._menu.addAction("打开 TXT…", self.openRequested.emit)
        self._opacity_menu = self._menu.addMenu("不透明度")
        for preset in OPACITY_PRESETS:
            self._opacity_menu.addAction(f"{preset}%", lambda p=preset: self.opacityRequested.emit(p))
        self._menu.addSeparator()
        self._menu.addAction("设置…", self.settingsRequested.emit)
        self._menu.addSeparator()
        self._menu.addAction("退出 MiniReader", self.quitRequested.emit)

        self.activated.connect(self._on_activated)
        self.set_title(title)
        self.set_hidden_state(False)

    # ---- state -----------------------------------------------------------

    def set_title(self, title: str) -> None:
        """The tooltip. Follows the disguise title, so hovering cannot give it away."""
        self.setToolTip(title or "MiniReader")

    def set_hidden_state(self, hidden: bool) -> None:
        """Keep the two explicit actions in step with what is actually on screen.

        While the window is hidden, 隐藏窗口 is pointless (and vice versa) - disable
        the no-op rather than let the menu offer something that does nothing.
        """
        self._hidden = bool(hidden)
        self._show_action.setEnabled(self._hidden)
        self._hide_action.setEnabled(not self._hidden)

    def set_opacity(self, percent: int) -> None:
        self._opacity = int(percent)

    def _on_activated(self, reason) -> None:
        """Single click AND double click both restore - the usual tray expectation."""
        try:
            if reason in (TRAY_TRIGGER, TRAY_DOUBLE_CLICK):
                self.showRequested.emit()
        except Exception:  # noqa: BLE001 - a tray signal must never raise
            pass

    # ---- lifecycle -------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """Whether this platform can host a tray icon.

        The `QApplication.instance()` guard is load-bearing, not defensive style.
        Calling `QSystemTrayIcon.isSystemTrayAvailable()` with no QApplication alive
        **segfaults** on PyQt5 5.15 / Qt 5.15.2 (measured: exit code 0xC0000005) rather
        than raising - Qt reaches for platform integration that does not exist yet, and
        there is no Python-level exception to catch. Any caller that might run before
        the application object exists - a self-check, a test module, an import side
        effect - would take the whole process down.
        """
        from ..qt import QApplication

        try:
            if QApplication.instance() is None:
                return False
            return bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:  # noqa: BLE001
            return False

    def icon_size(self) -> QSize:
        return QSize(16, 16)

    def shutdown(self) -> None:
        """Hide before the app exits, or Windows leaves a dead icon in the tray."""
        try:
            self.hide()
            self.setContextMenu(None)
        except Exception:  # noqa: BLE001
            pass


def make_tray(parent=None, *, title: str = "MiniReader") -> TrayIcon | None:
    """A tray icon, or None when this platform cannot host one.

    Must be called after the QApplication exists - `is_available()` returns False
    before that rather than crashing, so this is safe but pointless too early.
    """
    if not TrayIcon.is_available():
        return None
    try:
        tray = TrayIcon(parent, title=title)
        tray.show()
        return tray
    except Exception:  # noqa: BLE001 - no tray is a normal outcome, not an error
        return None
