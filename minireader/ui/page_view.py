"""The widget that paints one comic page (mode A), and will host mode C later.

`PageView` is a plain `QWidget` that paints a `QPixmap` centred in itself and
letterboxes the leftover space with the theme's background. It owns no book and
no cache - `ComicReader` hands it a pixmap and a message, and it draws whichever
it has.

Two decisions worth recording:

**Why a custom paintEvent instead of a `QLabel` with a pixmap.** A QLabel sizes
itself to its content, so a 4000px-wide page would make the window grow and the
scroll area fight it; here the widget is always exactly the viewport and the
page is scaled to fit inside it. That is also what keeps the letterbox bars the
same colour as the reading background instead of the dialog grey.

**Why the message is drawn rather than shown in a label.** A broken page in a
comic is a page you want to skip past, not a modal. Painting "第 12 页读不出来"
in the middle of the page keeps the position, keeps the page number in the
bottom bar, and lets the reader turn to the next page.

The zoom/scale arithmetic lives in `comic_cache.fit_size()`, which is pure and
unit-tested without a QApplication; this file only calls it.
"""

from __future__ import annotations

from ..qt import QColor, QFont, QPainter, QPixmap, QSize, QWidget, Qt
from .comic_cache import fit_size, scale_pixmap
from .theme import PALETTE

#: Zoom modes, in the order the title-bar button cycles them.
ZOOM_MODES: tuple[str, ...] = ("width", "height", "original")

#: Button label for each zoom mode.
ZOOM_LABELS = {"width": "↔", "height": "↕", "original": "1:1"}

#: Tooltip text for each zoom mode.
ZOOM_TIPS = {"width": "宽度适应", "height": "高度适应", "original": "原始尺寸"}


def next_zoom(current: str) -> str:
    """The zoom mode after `current`, wrapping. Unknown values restart at "width"."""
    try:
        index = ZOOM_MODES.index(current)
    except ValueError:
        return ZOOM_MODES[0]
    return ZOOM_MODES[(index + 1) % len(ZOOM_MODES)]


class PageView(QWidget):
    """Paints one page, scaled to the widget, centred, on the reading background."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._message = ""
        self._zoom = "width"
        self._fg = PALETTE["muted"]
        self.setAutoFillBackground(False)
        # The window paints its own translucent background; a child that claims
        # an opaque background would punch a grey hole in it.
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)

    # -- content --------------------------------------------------------------

    def set_page(self, pixmap: QPixmap | None, message: str = "") -> None:
        """Show `pixmap`, or `message` when the page could not be decoded."""
        self._pixmap = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self._message = "" if self._pixmap is not None else (message or "")
        self.update()

    def clear(self) -> None:
        """Blank the view - used by the placeholder and when a book closes."""
        self._pixmap = None
        self._message = ""
        self.update()

    @property
    def has_page(self) -> bool:
        return self._pixmap is not None

    @property
    def message(self) -> str:
        return self._message

    # -- zoom -----------------------------------------------------------------

    def set_zoom(self, zoom: str) -> None:
        self._zoom = zoom if zoom in ZOOM_MODES else ZOOM_MODES[0]
        self.update()

    @property
    def zoom(self) -> str:
        return self._zoom

    def page_size(self) -> QSize:
        """The size the page is currently painted at (before centring)."""
        if self._pixmap is None:
            return QSize(0, 0)
        width, height = fit_size(
            (self._pixmap.width(), self._pixmap.height()),
            (self.width(), self.height()),
            self._zoom,
        )
        return QSize(width, height)

    # -- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            # 不画底色：窗口的圆角底色（含 alpha）由顶层 paintEvent 统一画，这一层
            # 只负责画图。letterbox 的空隙因此透出窗口底色，和正文一样——如果这里
            # 填一块不透明色，alpha 调到 0 时就会看到一块黑的图片板。
            if self._pixmap is None:
                self._paint_message(painter)
                return

            width, height = fit_size(
                (self._pixmap.width(), self._pixmap.height()),
                (self.width(), self.height()),
                self._zoom,
            )
            scaled = scale_pixmap(self._pixmap, (width, height))
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        finally:
            painter.end()

    def _paint_message(self, painter: QPainter) -> None:
        """Centred grey text for a missing or broken page."""
        text = self._message or "还没有打开漫画"
        font = QFont(self.font())
        font.setPixelSize(14)
        painter.setFont(font)
        painter.setPen(QColor(self._fg))
        painter.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, text)

    def set_text_colour(self, colour: str) -> None:
        """The colour of the "this page is broken" text. Set from Settings.fg."""
        self._fg = colour or PALETTE["muted"]
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # The scale is computed per paint, so a resize only needs a repaint; the
        # call is explicit so the letterbox bars do not lag the window edge.
        self.update()
