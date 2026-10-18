"""The mini window: frameless, always on top, wheel-read, boss-key-hideable.

Window-level decisions and why:

* **Runtime `setWindowFlags()` only through one gate.** On Windows, changing window
  flags destroys and recreates the native window: geometry jumps, native state is lost.
  The 📌 button needs a live toggle anyway, so every flag change goes through
  `_apply_window_flags()`, which does `hide()` → `setWindowFlags()` → `show()` and then
  re-registers the global hotkeys: they are bound to the HWND, so a recreated window
  would otherwise leave the boss key pointing at a dead one. 隐藏任务栏 stays
  "restart to apply" in the settings dialog.
* **`Qt.Tool` for the taskbar.** A Tool window stays out of the taskbar and out of
  Alt+Tab, which is the point of a 摸鱼 reader. `win32.set_tool_window()` can flip the
  same `WS_EX_TOOLWINDOW` bit on a live HWND, and is used to *verify* the flag rather
  than to set it, because the flag route is the one already proven here.
* **Dragging and resizing go through the OS**, not through manual `move()` maths:
  `QWindow.startSystemMove()` / `startSystemResize()` hand the gesture to the window
  manager, which gives Aero Snap and correct multi-monitor behaviour for free. A
  manual fallback is kept in case they return False.
* **The window background is painted, not styled.** `WA_TranslucentBackground` is set
  once before `show()`, and `paintEvent` fills a rounded rect with `self.bg_color`
  (RGB from `Settings.bg`, alpha from `Settings.bg_alpha`). Nothing ever calls
  `setStyleSheet`/`setPalette` on this top-level window to change its background: a
  stylesheet on a translucent frameless top-level is the combination that misbehaves,
  and re-applying one on every colour change is what used to abort the app. Changing
  the background only ever calls `update()`. Whole-window fading is a *separate*
  feature and still goes through `setWindowOpacity()`.
* **Three separate opacity axes.** 窗口不透明度 (`Settings.opacity`) fades the whole
  window through `setWindowOpacity()`; 背景透明度 (`Settings.bg_alpha`) is the alpha of
  the painted background and leaves the text crisp; 内容透明度
  (`Settings.content_alpha`) is a `QGraphicsOpacityEffect` on the **reader child only**
  and fades the text while the background stays put. The effect is never put on this
  top-level window - `WA_TranslucentBackground` already owns its compositing.
* **Hiding is gated on having a way back.** The boss key refuses to hide when neither a
  global hotkey nor a tray icon can bring the window back.
* **Two different kinds of "gone".** 老板键 is `hide()` - the window really is hidden.
  鼠标移出隐身 is `setWindowOpacity(LEAVE_HIDE_OPACITY)` - nearly invisible but deliberately
  NOT 0.0: at alpha 0 Windows makes the layered window click-through, so the pointer coming
  back never generates an enterEvent and the window cannot restore itself. The tiny nonzero
  floor (0.01) is visually fully transparent but still hit-testable. So this window must
  never be made `WA_TransparentForMouseEvents` either.
* **Three bands, QQ-迷你窗 style.** 顶栏 (42 px) holds 打开/书签/自动滚动 on the left,
  the blank drag strip (with the title) in the middle, and 移出隐身开关/透明度/置顶/最小化/关闭
  on the right; 底栏 (30 px) holds 「‹ 上一页」胶囊 + 目录 + 设置 on the left, the read-only
  "第 x/y 章" + "xx.xx%" cluster centred, and the 「下一页 ›」胶囊 on the right. Both bars are
  transparent children, so the painted rounded background shows through and alpha 0 really
  does leave nothing but the text.

The window keeps a 4 px margin uncovered by children. That strip is both the
visible frame and the resize hot zone, so no overlay widgets are needed to catch
edge presses.
"""

from __future__ import annotations

import os

from .. import snap as snapmod
from .. import win32
from ..core.book_base import KIND_COMIC, KIND_TEXT
from ..core.formats import DUAL_IDENTITY_EXTS, _ext_of, dialog_filters, support_note
from ..core.images import folder_has_images
from ..core.model import Book, BookError, Progress, book_id, human_size
from ..core.opener import (
    PDF_COMIC,
    PDF_TEXT,
    open_book,
    open_book_kind,
    pdf_choice,
)
from ..hotkeys import format_hotkey, register_all
from ..qt import (
    EDGE_BOTTOM,
    EDGE_LEFT,
    EDGE_RIGHT,
    EDGE_TOP,
    QApplication,
    QBrush,
    QColor,
    QCursor,
    QDialog,
    QEasingCurve,
    QEvent,
    QFileDialog,
    QFontMetrics,
    QGuiApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QAction,
    QPainter,
    QPainterPath,
    QPoint,
    QPropertyAnimation,
    QPushButton,
    QRect,
    QRectF,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTimer,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
    Qt,
    EVENT_ACTIVATION_CHANGE,
    exec_dialog,
    event_global_pos,
    event_local_pos,
    screen_rects,
)
from ..store import BookmarkStore
from ..storage import (
    HOTKEY_FIELDS,
    Library,
    ProgressStore,
    Settings,
    _as_int,
    alpha_to_percent,
    data_dir,
    default_geometry,
    percent_to_alpha,
    validate_geometry,
)
from .autoscroll import AutoScroller
from .comic_cache import DEFAULT_CACHE_MB
from .comic_reader import PRELOAD_RADIUS, ComicReader
from .reader import ReaderView
from .settings_dialog import SettingsDialog
from . import theme
from .theme import ACTIVE_EXTRAS, PALETTE
from .toc_panel import TocPanel

FRAME = 4           # width of the uncovered border: frame + resize hot zone
RESIZE_MARGIN = 8   # how far inside the frame a press still counts as a resize
# 圆角半径。paintEvent 用 QPainterPath.addRoundedRect 画的就是这个角；窗口边角之外
# 那一小块是透明的，配合 WA_TranslucentBackground 才看得出圆角。9px 对齐 QQ 迷你窗实测。
BG_RADIUS = 9
TITLE_MAX = 22      # characters of chapter title shown in the status strip
SNAP_SETTLE_MS = 140   # wait this long after movement stops before snapping
# 书签摘录的长度。一行能显示多少字取决于字号，所以这里取一个"够认出是哪儿"的长度，
# 而不是"刚好填满一行"——多出来的会被列表行自己截断。
EXCERPT_CHARS = 60
# 按钮的点击目标下限。26px 是 QQ 迷你窗顶栏按钮的实测悬停块尺寸（26×26 圆角块），
# 顶/底栏按钮都以它为命中区下限。
TITLE_BTN_MIN = 26
NAV_BTN_MIN = 30
# 阶段 2：顶栏加高到 42px（QQ 实测 42），底栏翻页胶囊的实测命中区 87×29。
TITLE_BAR_H = 42
PAGE_BTN_MIN_W = 87
PAGE_BTN_MIN_H = 29
# tooltip 与按钮底边之间留 2px 的缝，免得气泡和图标糊在一起。
TOOLTIP_GAP_PX = 2
OPACITY_MS = 160       # dim/undim glide
# 兼容模式（translucent_bg=False）下整窗不透明度的下限，百分比。这条路径没有逐像素
# alpha，"背景透明度归零"只能靠 setWindowOpacity(0) 表达，那会把文字也一起抹掉、窗口
# 变成看不见摸不着的空壳。留 5% 至少还能看见轮廓、能拖回来。
COMPAT_MIN_OPACITY = 5
# 鼠标移出隐身前的缓冲时间（毫秒）。鼠标划过窗口边缘时会连着发 leave/enter，立刻降
# 到近全透就是一次闪烁；等一拍，enter 再把这次待执行的隐身取消掉。
LEAVE_HIDE_DELAY_MS = 100
# 鼠标移出隐身时整窗落到的不透明度。**不能写成 0.0**：setWindowOpacity 的底层是
# WS_EX_LAYERED + UpdateLayeredWindow，Windows 对 layered window 在 alpha=0 时直接让窗口
# 点击穿透——WindowFromPoint 命中的是下层窗口，指针移回时 Qt 收不到 enterEvent，窗口就
# 再也回不来（只剩托盘/老板键能救）。在 Qt.Tool + Frameless + StaysOnTop +
# WA_TranslucentBackground 的本窗组合上实测：alpha=0 必穿透，0.005/0.01/0.02/0.05 都能
# 正常命中并触发 enter。0.01 肉眼全透但保留命中测试，是隐身下限。
LEAVE_HIDE_OPACITY = 0.01

# 阶段 3：正文右键菜单的实测规格（QQ 迷你窗 04_context_menu_user.png）：
# 底 #2b2b2b、字 #bdbdc0、项高 40px、圆角 6-8px、分隔线/悬停都是 #3a3a3a，
# 纯文字（无图标、无快捷键右列），整菜单约 135×148px。
READER_MENU_MIN_W = 135
READER_MENU_ITEM_H = 40

# 👁/📌 的 tooltip 随状态实时切换（阶段 3）。文案是动作而不是状态描述：
# 开关已经按下时，提示的是"再点一下会发生什么"。
TOOLTIP_LEAVE_HIDE_ON = "关闭隐身"
TOOLTIP_LEAVE_HIDE_OFF = "开启隐身"
TOOLTIP_PIN_ON = "取消置顶"
TOOLTIP_PIN_OFF = "置顶"

# 主题的中文名（定义在 theme.py：设置对话框也要用，而它是被本模块 import 的）。

def reader_menu_qss() -> str:
    """正文右键菜单的 QSS。

    阶段 3 的实测规格是「底 #2b2b2b、字 #bdbdc0、项高 40px、圆角 6-8px」，
    这些颜色现在从 `theme.ACTIVE_EXTRAS` 取——它们不属于 11 键调色板（是"某一档
    亮度"而不是语义角色），但同样必须随主题走，否则白昼下菜单还是黑底。

    **必须是函数**：模块级常量在 import 时就把颜色插值定死了，切主题不会重算。
    """
    x = ACTIVE_EXTRAS
    return f"""
QMenu {{
    background: {x['menu_bg']}; color: {x['menu_fg']};
    border: 1px solid {x['menu_border']}; border-radius: 8px;
    padding: 6px; margin: 0; font-size: 13px;
}}
QMenu::item {{
    background: transparent; color: {x['menu_fg']};
    border-radius: 6px; margin: 0; padding: 0 18px;
    min-height: 40px;
}}
QMenu::item:selected {{ background: {x['menu_hover']}; color: {x['menu_fg']}; }}
QMenu::item:disabled {{ background: transparent; color: {x['menu_disabled']}; }}
QMenu::separator {{ height: 1px; background: {x['menu_border']}; margin: 4px 8px; }}
QMenu::icon {{ left: 0; width: 0; }}
"""


HOTKEY_LABELS = {name: label for name, _field, label in HOTKEY_FIELDS}


def _qss() -> str:
    p = PALETTE
    x = ACTIVE_EXTRAS
    # 顶层窗口的背景不在这里：它由 paintEvent 画（见 MiniWindow.paintEvent）。
    # 标题栏/状态栏一律透明，把窗口的圆角底色透出来——这样 alpha=0 时整窗只剩文字，
    # 桌面直接透过来，而不是留下两条不透明的横杠。
    return f"""
    #TitleBar, #StatusBar {{ background: transparent; }}
    #TitleLabel {{ color: {p['fg']}; font-size: 12px; }}
    #StatusLabel {{ color: {p['muted']}; font-size: 11px; }}
    #ProgressLabel {{ color: {p['faint']}; font-size: 11px; }}
    #HintLabel {{ color: {p['muted']}; font-size: 11px; }}
    #WarnLabel {{ color: {p['warn']}; font-size: 11px; }}
    QToolButton {{ color: {p['fg']}; background: transparent; border: 0; padding: 2px 7px; font-size: 12px; }}
    QToolButton:hover {{ background: {p['hover']}; border-radius: 6px; }}
    QToolButton:checked {{ background: {p['hover']}; border-radius: 6px; }}
    #StatusBar QToolButton {{
        font-size: 14px; min-height: 28px; padding: 2px 8px;
    }}
    /* 底栏两端的翻页胶囊：QQ 实测 87×29、圆角 14px、1px 描边、文字实测色。
       ID 选择器压过上面的通用钮规则（边框/字号/内边距都以这里为准），文字 13px 满足
       可读性下限；:hover 只提亮底色，不动文字色（和顶栏悬停块同一条原则）。
       注意 QSS 的 min-width 是**内容盒**：73 + 左右 padding 12 + 边框 2 = 87px 整宽，
       正好等于规格命中区宽度，不是随手写的 87（那样实际会变成 109px 把底栏顶爆）。 */
    QToolButton#PrevPageButton, QToolButton#NextPageButton {{
        color: {x['capsule_fg']}; background: transparent;
        border: 1px solid {x['capsule_border']}; border-radius: 14px;
        min-width: 73px; min-height: 27px; padding: 0 6px; font-size: 13px;
    }}
    QToolButton#PrevPageButton:hover, QToolButton#NextPageButton:hover {{
        background: {p['hover']}; border: 1px solid {x['capsule_border']}; border-radius: 14px;
    }}
    QToolButton#PrevPageButton:disabled, QToolButton#NextPageButton:disabled {{
        color: {p['faint']}; border: 1px solid {p['border']}; border-radius: 14px;
    }}
    #OpacityPopup {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 8px; }}
    #Muted {{ color: {p['muted']}; font-size: 11px; }}
    """


# QToolTip 是顶层窗口，只认 QApplication 级样式表——挂在窗口样式表上不会生效。
# 对齐 QQ 实测：圆角 3px、padding 4×8，颜色取自 theme。1px 同色描边是为了强制 Qt
# 走 QSS 渲染路径（没有这个 border，Windows 上会回落到系统原生气泡）。
# 幂等标记 `/* MiniReaderTooltip */` 由 `_install_tooltip_theme()` 负责摘挂。
TOOLTIP_MARKER = "/* MiniReaderTooltip */"


def tooltip_qss() -> str:
    """tooltip 的 QSS。**必须是函数**，理由同 `reader_menu_qss()`。"""
    x = ACTIVE_EXTRAS
    return f"""
{TOOLTIP_MARKER}
QToolTip {{
    background: {x['tooltip_bg']}; color: {x['tooltip_fg']};
    border: 1px solid {x['tooltip_border']}; border-radius: 3px; padding: 4px 8px;
}}
"""


def _install_tooltip_theme(app) -> None:
    """把 tooltip 样式挂到 QApplication 上，**先摘旧块再挂新块**。

    这里曾经是 `if "MiniReaderTooltip" not in app.styleSheet(): 追加`。那个写法在
    单主题下是对的（幂等，多窗口不会重复追加），但切主题时标记还在，新颜色永远挂
    不进去——结果是主窗变白、tooltip 仍然黑底。所以必须先按标记把旧块切掉。

    只认 QApplication 级样式表：QToolTip 是顶层窗口，挂在窗口样式表上不生效。
    """
    if app is None:
        return
    sheet = app.styleSheet()
    if TOOLTIP_MARKER in sheet:
        sheet = sheet[: sheet.index(TOOLTIP_MARKER)].rstrip()
    app.setStyleSheet(sheet + tooltip_qss())


def pdf_ask_qss() -> str:
    """PDF 询问框的 QSS：与设置对话框同一套层级（panel 底、card 面、field 控件），
    但只有一行正文和三个按钮，所以不复用 _stylesheet() 那 100 多行。

    **必须是函数**：原来这里是 f-string 常量，`{PALETTE['panel']}` 在 import 时
    就已经插值定死，切主题不会重算。
    """
    p = PALETTE
    x = ACTIVE_EXTRAS
    return f"""
QDialog {{ background: {p['panel']}; }}
QLabel#AskHeading {{ color: {p['fg']}; font-size: 13px; font-weight: bold; }}
QLabel#AskBody {{ color: {p['muted']}; font-size: 12px; }}
QPushButton {{
    background: {p['field']}; color: {p['fg']};
    border: 1px solid {p['border']}; border-radius: 4px;
    padding: 6px 16px; font-size: 13px; min-width: 80px; min-height: 32px;
}}
QPushButton:hover {{ background: {p['border']}; }}
QPushButton:pressed {{ background: {x['pressed_bg']}; }}
QPushButton:default {{ border: 1px solid {p['accent']}; }}
"""


class TitleBar(QWidget):
    """Drag handle. Buttons are children, so a press on a button never starts a move."""

    def __init__(self, window: "MiniWindow") -> None:
        super().__init__(window)
        self.setObjectName("TitleBar")
        self._window = window
        self.setFixedHeight(TITLE_BAR_H)
        self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._window.begin_drag(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """把标题栏每个按钮的 tooltip 从"鼠标右下角"改钉到"图标正下方"。

        QEvent.ToolTip 默认的落点是鼠标位置，快速划过时气泡会追着光标跑。这里拦截
        事件、自己调 QToolTip.showText，锚点由 tooltip_anchor() 基于按钮屏幕几何与
        可用屏幕空间算出（下方放不下就自动翻到上方）。文案/样式都不变。
        """
        if event.type() == QEvent.ToolTip and isinstance(obj, QToolButton):
            text = obj.toolTip()
            if text:
                QToolTip.showText(tooltip_anchor(obj), text, obj)
                return True
        return super().eventFilter(obj, event)


def _screen_containing(global_pos: QPoint):
    """包含某全局点的屏幕（按 availableGeometry 判定，避开任务栏）；都不沾边时回落主屏。

    不用 QGuiApplication.screenAt：它在 Qt 5 的部分小版本上不可靠，遍历屏幕几何两边
    绑定行为一致。
    """
    for screen in QGuiApplication.screens():
        if screen.availableGeometry().contains(global_pos):
            return screen
    return QGuiApplication.primaryScreen()


def _tooltip_size(text: str) -> tuple[int, int]:
    """估算 tooltip 气泡尺寸，用于判断下方放不放得下。

    用 QToolTip 自己的字体量文字，加上 TOOLTIP_QSS 的 padding（4×8）与 1px 边框。
    结果不需要像素级精确——只要翻转判断对就行，真正的定位由 QToolTip 在屏内二次钳制。
    """
    metrics = QFontMetrics(QToolTip.font())
    advance = getattr(metrics, "horizontalAdvance", None)
    text_w = int(advance(text)) if advance is not None else int(metrics.width(text))
    return text_w + 16 + 2, metrics.height() + 8 + 2


def tooltip_anchor(button: QToolButton) -> QPoint:
    """tooltip 的全局左上角：钉在按钮 bottomLeft 正下方，空间不足时翻到正上方。

    水平方向默认与按钮左边对齐（mapToGlobal(bottomLeft())），但会钳进屏幕可用区域，
    避免气泡右缘超出屏幕。整个决策只依赖可用屏幕几何，不写死方向。
    """
    tip_w, tip_h = _tooltip_size(button.toolTip())
    below = button.mapToGlobal(button.rect().bottomLeft()) + QPoint(0, TOOLTIP_GAP_PX)
    top_left = button.mapToGlobal(button.rect().topLeft())
    screen = _screen_containing(
        button.mapToGlobal(button.rect().center())
    )
    avail = screen.availableGeometry() if screen is not None else None

    x, y = below.x(), below.y()
    if avail is not None:
        # 下方放不下，且上方确实放得下时才翻转——上方也不够时宁可向下溢出一点
        # （QToolTip 自己还会再钳），不能在两边都不够时反复横跳。
        if y + tip_h > avail.bottom() + 1:
            above_y = top_left.y() - TOOLTIP_GAP_PX - tip_h
            if above_y >= avail.top():
                y = above_y
        x = max(avail.left(), min(x, avail.right() - tip_w + 1))
    return QPoint(int(x), int(y))


class ElidedLabel(QLabel):
    """窄底栏里的次要信息标签：空间不够就尾部省略，给完整文本留 tooltip。

    普通 QLabel 会按 sizeHint 撑布局，397px 宽的底栏会被"书名 · 编码 · 体积"顶爆。
    水平策略 Expanding + 最小宽 0：正常时按 sizeHint 占满所属布局的剩余空间，窄到
    放不下时可以收缩到 0 而不连累旁边的章节/进度标签；resize 时重新 elide。
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Expanding)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)

    def full_text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        self._full_text = text or ""
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        if not self._full_text:
            super().setText("")
            return
        elided = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, max(0, self.width()))
        super().setText(elided)


class OpacityPopup(QWidget):
    """🎚 的快捷浮层：背景 / 内容两根 0-100 滑块，实时预览，关掉不落盘。

    用 Qt.Popup 而不是普通子控件：点窗口外任意处它就自己消失，不用额外处理失焦关闭；
    它也是主窗口的子窗口，老板键把主窗藏起来时会跟着一起消失，不会孤零零留在屏幕上。
    """

    def __init__(self, window: "MiniWindow") -> None:
        super().__init__(window, Qt.Popup)
        self._window = window
        self.setObjectName("OpacityPopup")
        # QWidget 子类默认不画样式表里的背景，得显式打开这条开关，否则 #OpacityPopup
        # 的背景/描边/圆角一条都不会生效，浮层会是一块透明的方框。
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 250 放不下"背景不透明度 (0-100)"再加滑块和数值；300 才让滑块还有可拖的长度。
        self.setFixedWidth(300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.bg_slider, self.bg_value = self._slider_row(
            layout, "背景不透明度 (0-100)", window.settings.bg_alpha, self._on_bg
        )
        self.content_slider, self.content_value = self._slider_row(
            layout, "内容不透明度 (0-100)", window.settings.content_alpha, self._on_content
        )

        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self._sync_enabled()

    def _slider_row(self, layout, caption: str, alpha: int, slot):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        label = QLabel(caption)
        label.setObjectName("Muted")
        row_layout.addWidget(label)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        # 先设值再接信号：否则构造期的 setValue 会立刻触发一次预览。
        slider.setValue(alpha_to_percent(alpha))
        slider.valueChanged.connect(slot)
        row_layout.addWidget(slider, 1)

        value = QLabel(f"{slider.value()}%")
        value.setObjectName("Muted")
        value.setFixedWidth(34)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_layout.addWidget(value)

        layout.addWidget(row)
        return slider, value

    def _on_bg(self, percent: int) -> None:
        self.bg_value.setText(f"{percent}%")
        self._window.preview_bg_alpha(percent_to_alpha(percent))

    def _on_content(self, percent: int) -> None:
        self.content_value.setText(f"{percent}%")
        self._window.preview_content_alpha(percent_to_alpha(percent))

    def _sync_enabled(self) -> None:
        translucent = bool(self._window.settings.translucent_bg)
        self.content_slider.setEnabled(translucent)
        self.hint.setText("" if translucent else "兼容模式下内容与背景一起变淡，无法单独调节")

    def sync(self) -> None:
        """每次打开把两根滑块拨回当前设置（上一轮的预览值不留下）。"""
        for slider, value, alpha in (
            (self.bg_slider, self.bg_value, self._window.settings.bg_alpha),
            (self.content_slider, self.content_value, self._window.settings.content_alpha),
        ):
            slider.blockSignals(True)
            slider.setValue(alpha_to_percent(alpha))
            slider.blockSignals(False)
            value.setText(f"{slider.value()}%")
        self._sync_enabled()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        # 浮层不写盘：关掉就把两个维度都撤回到已落盘的 settings。
        self._window.apply_background(self._window.settings)
        self._window.apply_content_alpha(self._window.settings)
        super().hideEvent(event)


def _excerpt(body: str, offset: int, limit: int = EXCERPT_CHARS) -> str:
    """书签摘录：从锚点往后取一小段，压成一行。

    用**章内正文**而不是从界面上抓文字：界面上的文字已经被折行、还可能被省略号截断，
    而正文是稳定的。压掉换行是必须的——书签列表每行只能显示一行文字，摘录里带换行
    会把那一行撑成两三行，列表看起来就散了。
    """
    text = " ".join((body or "")[max(0, offset) :].split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


class PdfKindDialog(QDialog):
    """「这本书判不出来」的一次性询问：文本 / 漫画。

    只在 `pdfdoc.classify()` 给出 `needs_ask` 时出现——正文密度和图片覆盖率都低到
    什么都说明不了（只有封面的 PDF、空白模板、用矢量画的漫画）。别的 PDF 一律静默
    打开，问一次是例外而不是流程。

    用 QDialog 而不是 QMessageBox：两个按钮是**并列**的两个答案，不是「确定/取消」，
    而 QMessageBox 的按钮盒会按平台把其中一个渲染成主按钮。这里谁也不是主按钮。
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("这本书按哪种方式打开")
        self.setStyleSheet(pdf_ask_qss())
        self.setModal(True)
        self.setMinimumWidth(360)
        self._choice: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        heading = QLabel(f"《{title}》判不出来是文本还是漫画。")
        heading.setObjectName("AskHeading")
        heading.setWordWrap(True)
        root.addWidget(heading)

        body = QLabel(
            "这本书里既没有多少可提取的文字，也没有整页的图片。\n"
            "选「按文本」会像小说一样分页排版；选「按漫画」会把每页当图片显示。\n"
            "选过一次以后，这本书就一直按它打开，不再问。"
        )
        body.setObjectName("AskBody")
        body.setWordWrap(True)
        root.addWidget(body)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.text_button = QPushButton("按文本")
        self.comic_button = QPushButton("按漫画")
        self.cancel_button = QPushButton("取消")
        # 默认焦点给"按文本"：它也是取消时的结果，键盘用户按 Enter 得到的和按 Esc
        # 得到的一致，不会出现"回车打开方式与预期不符"。
        self.text_button.setDefault(True)
        self.text_button.clicked.connect(lambda: self._pick(PDF_TEXT))
        self.comic_button.clicked.connect(lambda: self._pick(PDF_COMIC))
        self.cancel_button.clicked.connect(self.reject)
        for button in (self.text_button, self.comic_button, self.cancel_button):
            buttons.addWidget(button)
        root.addLayout(buttons)

    def _pick(self, kind: str) -> None:
        """Remember the answer, then accept.

        The answer rides on an attribute rather than on the dialog's result code
        because `exec_dialog()` answers one question - accepted or not - and
        tests stub it at the module level. Keeping the payload out of the result
        code means the stub only has to click a button and return True.
        """
        self._choice = kind
        self.accept()

    def choice(self) -> str | None:
        """`PDF_TEXT` / `PDF_COMIC`, or None when the dialog was dismissed."""
        return self._choice


class MiniWindow(QWidget):
    """Owns the book, the reader, and every piece of persisted state."""

    def __init__(
        self,
        settings: Settings,
        progress: ProgressStore,
        library: Library,
        hotkeys=None,
        store_path: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.progress = progress
        self.library = library
        self.hotkeys = hotkeys
        self.book: Book | None = None
        self.chapter_index = 0
        self.boss_hidden = False
        self.tray = None
        self._boss_available = False
        self._boss_spec = settings.boss_key
        self._drag_origin = None
        self._quit_on_close = True
        # 只有托盘菜单的"退出"（或应用外壳的 real_quit）会把它置 True。✕ 和老板键都只
        # 是把窗口藏起来：常驻托盘的应用，关闭按钮不等于退出进程。
        self._really_quit = False
        self._settings_path = data_dir() / "settings.json"
        # Snap bookkeeping. A gesture may move, resize, or (Aero Snap) both, and only a
        # pure move should be snapped - so a resize *during an active gesture* vetoes it.
        # `_gesture_active` is what keeps that from leaking: construction calls
        # setGeometry(), which fires a resize with no gesture behind it, and without the
        # gate that stray resize would veto the user's first drag.
        self._gesture_active = False
        self._gesture_resized = False
        self._snapping = False
        self._snap_retries = 0
        # 边缘缩放手势。_resize_edges 为 0 表示没在缩放；否则是 Qt.LeftEdge/RightEdge/
        # TopEdge/BottomEdge 的位组合，告诉我们哪几条边在动。起点记全局坐标和按下瞬间
        # 的几何，每次移动都从"起点 + 总位移"重算（不累加，避免逐帧误差漂移）。
        # 刻意与 _drag_origin（移动）互斥：按在边缘只能缩放，绝不能像旧实现那样缩放
        # 失败后 fall-through 变成拖窗口。
        self._resize_edges = 0
        self._resize_start_pos = None
        self._resize_start_geom = None
        self._dialog_open = False
        # 打开中的设置对话框（没有就是 None）。顶栏 🌙 与设置页下拉是两个入口，
        # 靠它把主题状态互相灌过去，免得两处显示不一致。
        self._settings_dialog = None
        # 打开设置对话框那一刻的主题。下拉是实时预览（不落盘），取消时要切回这里。
        self._theme_before_dialog = theme.CURRENT
        # 🎚 的快捷浮层。懒创建：不点开就一直不存在，省得每次启动都多一个原生窗口。
        self._opacity_popup = None
        # 鼠标移出隐身的账本。None = 当前没处于移出隐身；非 None = 隐身前的用户设置值，
        # 鼠标移入时用它恢复。它只是**临时覆盖**，settings.opacity 一个字节都不动。
        self._opacity_before_hide = None
        # 书签库。传 None 就是默认位置（%APPDATA%\MiniReader\reading.db）；数据库建不起来
        # 时会自己降级成内存库并置 persistent=False，所以这里不需要 try。
        self.store = BookmarkStore(store_path)

        self._build_flags()

        # 窗口底色。paintEvent 每帧拿它填圆角矩形；所有改背景的操作最终都只改这一个
        # QColor 然后 update()。先给一个安全的默认值，_build_ui() 末尾再用 settings 覆盖。
        self.bg_color = QColor(PALETTE["bg"])
        self.bg_color.setAlpha(255)

        # The debounced savers are created before the UI so that anything the widgets
        # do on construction (an initial scroll emits `scrolled`) can safely reach them.
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.setInterval(500)
        self._geom_timer.timeout.connect(self.save_geometry)

        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.setInterval(900)
        self._progress_timer.timeout.connect(self.persist_progress)

        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(SNAP_SETTLE_MS)
        self._snap_timer.timeout.connect(self._maybe_snap)

        # 鼠标移出隐身的防抖。用实例 QTimer 而不是 QTimer.singleShot：后者是静态方法，
        # 派发出去就收不回来，而 enterEvent 必须能把这次待执行的降透明度取消掉。
        self._leave_hide_timer = QTimer(self)
        self._leave_hide_timer.setSingleShot(True)
        self._leave_hide_timer.setInterval(LEAVE_HIDE_DELAY_MS)
        self._leave_hide_timer.timeout.connect(self._apply_leave_hide)
        # 正文右键菜单开着时，移出隐身必须让位于菜单：菜单是顶层 popup，鼠标选菜单项
        # 本来就离开了主窗几何，若照常隐身，菜单还在、主窗没了。菜单关闭后再恢复。
        self._context_menu_open = False

        # One animation object, retargeted from wherever the window currently is.
        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.setDuration(OPACITY_MS)
        self._opacity_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._target_opacity = 100

        # `_filtered_viewport` / `_autoscroll_bar` 是 `_install_view_filter()` 和
        # `_bind_autoscroller()` 的记账：它们都靠"和上次不一样才动"来避免重复安装。
        # 必须在 `_build_ui()` **之前**就有初值——`_build_ui()` 里构造 ComicReader
        # 时会接上 `viewportChanged`，而 `apply_comic_settings()` 可能在构造期就切
        # 模式，那时 `_rebind_reader_views()` 已经会被调用。
        self._filtered_viewport = None
        self._autoscroll_bar = None
        self.autoscroller = None
        # 没有可滚动条时的替身（见 `_bind_autoscroller`）：范围 (0,0) 的真条，
        # 让 AutoScroller 永远拿得到一根能读 value() 的东西。
        self._parked_bar = QScrollBar(Qt.Vertical, self)
        self._parked_bar.setRange(0, 0)
        self._parked_bar.setVisible(False)

        self._build_ui()
        # 滚动器只拿正文的滚动条，不拿 ReaderView 本身：这样它和滚轮的缓动动画
        # （_animate_to / _cancel_anim）没有任何耦合，谁动滚动条谁负责，不会互相取消。
        self._bind_autoscroller(self.reader.scrollbar())
        # 鼠标停在正文上时暂停：读者把鼠标移回窗口通常就是想手动滚一下或者看一眼，
        # 此时文字还在自己走会很难受。边缘缩放热区也走同一个过滤器。
        self._install_view_filter(self.reader.viewport())
        self._restore_geometry()
        self.apply_disguise()
        self.apply_opacity(self.settings.target_opacity(active=True), animate=False)

    # ---- construction ----------------------------------------------------

    def _window_flags(self):
        """当前设置对应的窗口标志。

        单独抽出来是为了让 📌 能在运行时重算（见 _apply_window_flags）：窗口标志必须
        只有这一个来源，否则构造期和运行时两套算法迟早会漂开。
        """
        flags = Qt.FramelessWindowHint
        if self.settings.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        if self.settings.hide_from_taskbar:
            flags |= Qt.Tool
        return flags

    def _build_flags(self) -> None:
        self.setWindowFlags(self._window_flags())
        self.setWindowTitle("MiniReader")
        # 必须在 show() 之前设置，否则 Qt 不会给这个窗口创建带 alpha 通道的合成表面，
        # paintEvent 里画出来的半透明底色会被当成不透明黑。这个属性只按 translucent_bg
        # 的取值设一次；运行时要切换它得走 _sync_translucent_mode()（hide→改→show），
        # 因为改它和改 windowFlags 一样会让 Qt 重建原生窗口。
        self.setAttribute(Qt.WA_TranslucentBackground, bool(self.settings.translucent_bg))
        self.setMinimumSize(320, 360)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setObjectName("MiniRoot")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(FRAME, FRAME, FRAME, FRAME)
        root.setSpacing(0)

        self.title_bar = TitleBar(self)
        bar_layout = QHBoxLayout(self.title_bar)
        bar_layout.setContentsMargins(6, 0, 4, 0)
        bar_layout.setSpacing(2)

        # 左：阅读动作（打开/书签/自动滚动）。阶段 2 把翻章从顶栏挪到了底栏胶囊，
        # 顶栏左侧腾出来安置这三个高频动作，底栏才放得下"目录/设置/翻页"三组入口。
        self.open_button = QToolButton()
        self.open_button.setText("打开")
        self.open_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        self.open_button.setToolTip("打开一个本地 TXT 或 EPUB 文件")
        self.open_button.clicked.connect(self.open_dialog)
        bar_layout.addWidget(self.open_button)

        self.bookmark_button = QToolButton()
        self.bookmark_button.setText("🔖")
        self.bookmark_button.setToolTip("在这里加一个书签（Ctrl+B）")
        self.bookmark_button.clicked.connect(self.add_bookmark)
        self.bookmark_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.bookmark_button)

        self.autoscroll_button = QToolButton()
        self.autoscroll_button.setText("⏵")
        self.autoscroll_button.setCheckable(True)
        self.autoscroll_button.setToolTip("自动滚动（Ctrl+Shift+空格 开始/暂停）")
        self.autoscroll_button.clicked.connect(self.toggle_autoscroll)
        self.autoscroll_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.autoscroll_button)

        # 外观主题：点一下切到下一档（暗夜 ⇄ 白昼）。**不是开关**——两个主题没有
        # "开/关"的语义，所以 setCheckable(False)，按一下就切一次。
        # 立即生效，只走 apply_background() 那条通道，不碰 windowFlags/setPalette。
        self.theme_button = QToolButton()
        self.theme_button.setText("🌙")
        self.theme_button.setCheckable(False)
        self.theme_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        self.theme_button.clicked.connect(self.toggle_theme)
        bar_layout.addWidget(self.theme_button)

        # 漫画专属的两个循环切换钮。放标题栏而不是 30px 底栏：底栏已经有"翻页/目录/设置"
        # 三组控件，还有一条"397px 宽下三组不重叠"的既有断言，塞不下第四个。
        # 文本模式下 setVisible(False)，不参与布局——所以文本模式的标题栏和改动前逐像素
        # 一致，既有布局测试一条都不用改。文案/提示由 ComicReader 提供，两处口径唯一。
        self.comic_mode_button = QToolButton()
        self.comic_mode_button.setText("▭")
        self.comic_mode_button.setToolTip("阅读模式：单页")
        self.comic_mode_button.clicked.connect(self.cycle_comic_mode)
        self.comic_mode_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        self.comic_mode_button.setVisible(False)
        bar_layout.addWidget(self.comic_mode_button)

        self.comic_zoom_button = QToolButton()
        self.comic_zoom_button.setText("↔")
        self.comic_zoom_button.setToolTip("缩放：宽度适应")
        self.comic_zoom_button.clicked.connect(self.cycle_comic_zoom)
        self.comic_zoom_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        self.comic_zoom_button.setVisible(False)
        bar_layout.addWidget(self.comic_zoom_button)

        # 装订方向。只有双页模式才看得出左右，所以只在模式 C 显示——在单页/连续滚动里
        # 它不改变任何像素，放一个按了没反应的按钮比不放更糟。
        self.comic_binding_button = QToolButton()
        self.comic_binding_button.setText("右")
        self.comic_binding_button.setToolTip("装订方向：右綴じ（小页码在右）")
        self.comic_binding_button.clicked.connect(self.cycle_comic_binding)
        self.comic_binding_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        self.comic_binding_button.setVisible(False)
        bar_layout.addWidget(self.comic_binding_button)

        # 中：拖拽区。TitleBar 自己实现 mousePressEvent，按住空白处就能拖窗口；按钮是
        # 它的子控件，所以按在按钮上不会误触发拖动。书名显示在这条上（伪装标题也走它）。
        self.title_label = QLabel("MiniReader")
        self.title_label.setObjectName("TitleLabel")
        self.title_label.setAlignment(Qt.AlignCenter)
        bar_layout.addWidget(self.title_label, 1)

        # 右：窗口控制。顺序按"越靠右越接近退出"排。
        # 👁 是"鼠标移出隐身"的开关（checkable，按下 = 开启）。老板键不在这里：它仍然
        # 是全局热键 + 托盘，和移出隐身互不干扰（一个是 hide()，一个是 opacity=0）。
        self.boss_button = QToolButton()
        self.boss_button.setText("👁")
        self.boss_button.setCheckable(True)
        self.boss_button.setChecked(bool(self.settings.mouse_leave_hide))
        # 文案是双态的（开启隐身/关闭隐身），状态变了立刻换——见 _sync_leave_hide_button。
        self.boss_button.setToolTip(
            TOOLTIP_LEAVE_HIDE_ON if self.settings.mouse_leave_hide else TOOLTIP_LEAVE_HIDE_OFF
        )
        self.boss_button.toggled.connect(self._on_leave_hide_toggled)
        self.boss_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.boss_button)

        # 透明度快捷浮层。单击弹出两根滑块，实时预览；关掉浮层不落盘。
        self.alpha_button = QToolButton()
        self.alpha_button.setText("🎚")
        self.alpha_button.setToolTip("透明度：背景 / 内容（实时预览，关掉不保存）")
        self.alpha_button.clicked.connect(self.open_opacity_popup)
        self.alpha_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.alpha_button)

        self.pin_button = QToolButton()
        self.pin_button.setText("📌")
        self.pin_button.setCheckable(True)
        self.pin_button.setChecked(bool(self.settings.always_on_top))
        # 双态文案（置顶/取消置顶），toggle_on_top 后经 _sync_pin_button 立刻更新。
        self.pin_button.setToolTip(
            TOOLTIP_PIN_ON if self.settings.always_on_top else TOOLTIP_PIN_OFF
        )
        self.pin_button.clicked.connect(self.toggle_on_top)
        self.pin_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.pin_button)

        self.minimize_button = QToolButton()
        self.minimize_button.setText("—")
        self.minimize_button.setToolTip("最小化")
        self.minimize_button.clicked.connect(self.showMinimized)
        self.minimize_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.minimize_button)

        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭（按设置决定是退出还是隐藏到托盘）")
        self.close_button.clicked.connect(self.close)
        self.close_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        bar_layout.addWidget(self.close_button)

        # 决策 A：标题栏全部按钮的 tooltip 统一钉在图标正下方（见 TitleBar.eventFilter
        # 与 tooltip_anchor）。用 findChildren 而不是逐个列名：以后再加按钮自动生效。
        for button in self.title_bar.findChildren(QToolButton):
            button.installEventFilter(self.title_bar)

        root.addWidget(self.title_bar)

        # 目录面板和正文并排，用"显示/隐藏"切换而不是弹独立窗口：这是一个无边框、
        # 置顶的 Qt.Tool 窗口，再开一个窗口就要自己处理它的置顶、老板键和失焦变淡，
        # 而抽屉天然跟着主窗口一起隐藏，不会在老板键按下后留在屏幕上。
        self.body = QWidget()
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.toc_panel = TocPanel(self.body)
        self.toc_panel.setVisible(False)
        self.toc_panel.chapterChosen.connect(self._on_toc_chapter)
        self.toc_panel.bookmarkChosen.connect(self._on_bookmark_chosen)
        self.toc_panel.bookmarkDeleted.connect(self._on_bookmark_deleted)
        self.toc_panel.closed.connect(self.close_toc)
        body_layout.addWidget(self.toc_panel)

        # 两个 Reader 全程存活，切换只换 QStackedWidget 的索引（见 _select_reader）。
        # 不用"销毁重建"是因为重建要重新接信号、重新装事件过滤器、重新 apply_style，
        # 任何一处漏掉都是静默的行为退化；而两个控件的常驻内存代价可以忽略。
        self._reader_stack = QStackedWidget(self.body)
        self._reader_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._text_reader = ReaderView(self._reader_stack)
        self._text_reader.chapterRequested.connect(self.step_chapter)
        self._text_reader.scrolled.connect(self._on_scrolled)
        # 阶段 3：正文右键菜单。坐标原样透传——菜单必须出现在右键点。
        self._text_reader.contextMenuRequested.connect(self.show_reader_menu)

        self._comic_reader = ComicReader(self._reader_stack)
        self._comic_reader.scrolled.connect(self._on_scrolled)
        self._comic_reader.contextMenuRequested.connect(self.show_reader_menu)
        self._comic_reader.notice.connect(self.set_warning)
        # 换模式会换掉 `viewport()` 返回的控件（单页是 PageView，连续滚动是那个
        # QScrollArea），装在它上面的边缘缩放过滤器必须跟着搬。reader 负责说"换了"，
        # 窗口负责搬——漏掉这一步，模式 B 下 4px 边框的缩放热区会静默失效。
        self._comic_reader.viewportChanged.connect(self._rebind_reader_views)

        self._reader_stack.addWidget(self._text_reader)
        self._reader_stack.addWidget(self._comic_reader)
        # self.reader 始终指向"当前生效的那个"，属性名不变：窗口里 25 处调用点、
        # 以及十几条既有测试都按这个名字取正文控件。
        self.reader = self._text_reader
        body_layout.addWidget(self._reader_stack, 1)
        root.addWidget(self.body, 1)
        self._build_reader_menu()

        # 底栏（阶段 2 重构，对齐 QQ 阅读态）：
        # [‹ 上一页] [☰ 目录] [⚙ 设置]  ……  第 x/y 章 · xx.xx% · 书名…  ……  [下一页 ›]
        # 三组入口（翻页 / 目录 / 设置）全部常驻可见；打开/书签/自动滚动已移到顶栏左侧。
        self.status_bar = QWidget()
        self.status_bar.setObjectName("StatusBar")
        # 30px：可读性下限要求底栏 29-36px、按钮命中区 ≥28px，翻页胶囊高 29px 也放得下。
        self.status_bar.setFixedHeight(30)
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.setContentsMargins(8, 0, 8, 0)
        # 元素之间留白 ≥8px（可读性硬性要求），按钮自身的 padding 另算。
        status_layout.setSpacing(8)

        # 左：翻页胶囊。文本模式翻章、漫画模式翻页——同一个 step_chapter 按书的分派
        # 决定翻的是章还是页，按钮本身不需要知道手里是哪一种书。
        self.prev_button = QToolButton()
        self.prev_button.setObjectName("PrevPageButton")
        self.prev_button.setText("‹ 上一页")
        self.prev_button.setMinimumSize(PAGE_BTN_MIN_W, PAGE_BTN_MIN_H)
        self.prev_button.setToolTip("上一页（上一章）")
        self.prev_button.clicked.connect(lambda: self.step_chapter(-1))
        status_layout.addWidget(self.prev_button)

        # 目录抽屉的开关。用 ☰ 而不是文字"目录"：底栏在 397px 宽时很挤，
        # 图标 + tooltip 更省地方，字号随 #StatusBar QToolButton 是 14px（≥13 下限）。
        self.toc_button = QToolButton()
        self.toc_button.setText("☰")
        self.toc_button.setCheckable(True)
        self.toc_button.setToolTip("章节目录（Ctrl+T）")
        self.toc_button.clicked.connect(self.toggle_toc)
        self.toc_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        status_layout.addWidget(self.toc_button)

        self.settings_button = QToolButton()
        self.settings_button.setText("⚙")
        self.settings_button.setToolTip("设置：排版、不透明度、快捷键、托盘、标题伪装、边缘吸附")
        self.settings_button.clicked.connect(self.open_settings)
        self.settings_button.setMinimumSize(TITLE_BTN_MIN, TITLE_BTN_MIN)
        status_layout.addWidget(self.settings_button)

        # 两侧各一根等权弹簧：中间的信息簇因此整体居中（QQ 实测进度文本在底栏正中）。
        # 弹簧只吃"布局完剩下的"空间，不会饿死簇里的标签；簇内空间不够时只有
        # ElidedLabel 收缩省略，章节号/进度两个数字始终完整。
        status_layout.addStretch(1)
        center_layout = QHBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)

        # 中：章节与进度，只读，随滚动更新。分成两个标签（而不是原来那一条长字符串）
        # 是为了把"第 x/y 章"和"xx.xx%"这两个高频数字固定下来，书名/编码/体积退到后面。
        self.chapter_label = QLabel("")
        self.chapter_label.setObjectName("StatusLabel")
        center_layout.addWidget(self.chapter_label)

        self.progress_label = QLabel("")
        # 进度是底栏里唯一允许弱化的元素（QQ 实测 #67676a）；目录/设置等按钮不弱化。
        self.progress_label.setObjectName("ProgressLabel")
        center_layout.addWidget(self.progress_label)

        # 书名 · 编码 · 体积这类次要信息。保留 status_label 这个名字：占位文案
        # （"还没有打开文件"）和几处测试都走它，调用点不用改。竖窄窗口里空间不够时
        # ElidedLabel 自己尾部省略，完整内容在 tooltip 里。
        self.status_label = ElidedLabel("")
        self.status_label.setObjectName("StatusLabel")
        center_layout.addWidget(self.status_label, 1)

        self.warn_label = QLabel("")
        self.warn_label.setObjectName("WarnLabel")
        center_layout.addWidget(self.warn_label)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("HintLabel")
        center_layout.addWidget(self.hint_label)

        status_layout.addLayout(center_layout)
        status_layout.addStretch(1)

        # 右：翻页胶囊，与左侧对称钉在底栏端头。
        self.next_button = QToolButton()
        self.next_button.setObjectName("NextPageButton")
        self.next_button.setText("下一页 ›")
        self.next_button.setMinimumSize(PAGE_BTN_MIN_W, PAGE_BTN_MIN_H)
        self.next_button.setToolTip("下一页（下一章）")
        self.next_button.clicked.connect(lambda: self.step_chapter(1))
        status_layout.addWidget(self.next_button)

        root.addWidget(self.status_bar)

        # 边缘缩放热区比 4px 透明 FRAME 宽（edges_at 取 FRAME+RESIZE_MARGIN=12px），
        # 但边框内侧 8px 被标题栏/底栏这两个子控件盖着，Qt 默认把子控件上的鼠标事件
        # 交给子控件、不传父窗口。装同一个 eventFilter，把外环的按下/拖动/悬停路由到
        # 主窗口的缩放逻辑；不在外环的事件一律放行（标题照常拖、按钮照常点）。
        # 正文 viewport 的 filter 在构造后另装（见 __init__），这里只补三个面的鼠标
        # 追踪——不按键时也要能在边缘把光标换成缩放形状。
        self.title_bar.setMouseTracking(True)
        self.status_bar.setMouseTracking(True)
        self.title_bar.installEventFilter(self)
        self.status_bar.installEventFilter(self)

        self._apply_theme()
        self.reader.apply_style(self.settings)
        self.apply_background()
        self.apply_content_alpha()
        # 漫画侧的设置（缓存预算、预加载、默认模式与缩放）在这里落一次，让第一本
        # 漫画就用上用户的选择，而不是等打开设置对话框才生效。
        self.apply_comic_settings()
        # 🌙 的提示文案是双态的（点一下切到哪一档），首帧就要对。
        self._sync_theme_controls()
        self._show_placeholder()

    def _apply_theme(self) -> None:
        # 只给标题栏/状态栏/按钮上色，绝不涉及窗口背景——背景归 paintEvent。
        self.setStyleSheet(_qss())
        # 右键菜单是长生命周期对象，样式表只在建的时候挂一次，切主题要重挂。
        menu = getattr(self, "reader_menu", None)
        if menu is not None:
            menu.setStyleSheet(reader_menu_qss())
        # QToolTip 是顶层窗口，只吃 QApplication 级样式表。
        _install_tooltip_theme(QApplication.instance())

    def apply_theme(self, name: str, *, persist: bool = True) -> str:
        """切换窗口外观主题（dark / light）。立即生效，返回真正生效的主题名。

        **只走 apply_background() 那条通道**：本方法改的是样式表与自绘颜色，
        窗口本身（windowFlags / setPalette / WA_TranslucentBackground）一个字不碰——
        这三样在无边框 + 半透明顶层窗上都会触发原生窗口重建，是崩溃老巢。

        与透明度正交：`bg_alpha` 决定底色 alpha，`content_alpha` 决定正文 alpha，
        主题只决定 RGB。paintEvent 一个字不改。

        「体贴规则」：正文配色（Settings.bg/fg）默认**不**跟着主题走，因为那是用户
        自己的选择；只有当它还等于**原主题**的默认值时才顺手跟过去——那说明用户
        从没动过它。改过的配色一律保留（见 theme.READ_DEFAULTS）。
        """
        previous = theme.CURRENT
        resolved = theme.apply_theme(name)
        self.settings.theme = resolved

        old_bg, old_fg = theme.read_defaults(previous)
        if (self.settings.bg, self.settings.fg) == (old_bg, old_fg):
            self.settings.bg, self.settings.fg = theme.read_defaults(resolved)

        self._apply_theme()
        # 只改 self.bg_color + update()，不碰任何窗口属性。
        self.apply_background()
        # 正文选中底色与文字色随主题走；apply_style 会保留字符锚点。
        self.reader.apply_style(self.settings)
        # 目录高亮条是 paint 出来的，得让面板重画。
        self._refresh_toc()
        self._sync_theme_controls()
        if persist:
            self.settings.save(self._settings_path)
        return resolved

    def _sync_theme_controls(self) -> None:
        """把当前主题同步到顶栏 🌙 与设置页下拉（两处入口必须一致）。"""
        name = theme.CURRENT
        label = theme.THEME_LABELS.get(name, name)
        button = getattr(self, "theme_button", None)
        if button is not None:
            button.setToolTip(
                f"外观主题：{label}（点一下切到{theme.THEME_LABELS.get(theme.next_theme(name), '')}）"
            )
        # 设置对话框若正开着，把它那页下拉也刷成当前主题——两个入口必须同步。
        dialog = getattr(self, "_settings_dialog", None)
        if dialog is not None:
            dialog.sync_theme(theme.CURRENT)

    def toggle_theme(self) -> None:
        """顶栏 🌙 的槽：点一下切到下一档主题。"""
        self.apply_theme(theme.next_theme(theme.CURRENT))

    # ---- background ------------------------------------------------------

    def apply_background(self, settings=None) -> None:
        """把 Settings 里的 bg + bg_alpha 变成 paintEvent 要填的颜色。

        这是改背景的唯一入口，而且**只**改 self.bg_color 然后 update()——不碰
        setStyleSheet、不碰 setPalette、不碰 windowFlags。这三样在无边框 + 半透明的
        顶层窗口上都会触发原生窗口重建，是这类崩溃的老巢。

        兼容模式（translucent_bg=False）下 paintEvent 不画这块颜色，alpha 不参与
        渲染；照样按 settings 存进去，是为了切回半透明模式时立刻就正确。
        """
        source = settings if settings is not None else self.settings
        colour = QColor(source.bg)
        if not colour.isValid():
            colour = QColor(PALETTE["bg"])
        colour.setAlpha(max(0, min(int(source.bg_alpha), 255)))
        self.bg_color = colour
        self.update()

    def _sync_translucent_mode(self, settings=None) -> None:
        """按 translucent_bg 切换底层呈现方式。

        改 WA_TranslucentBackground 会让 Qt 重建原生窗口（和改 windowFlags 同理），
        运行中直接 setAttribute 会跳位置、丢原生状态。所以必须先 hide()、再改属性、
        再 show()，并且把几何和阅读位置原样搬回去。"阅读位置"用首个可见字符表示
        （和字号/像素无关），换回像素偏移会在窗口重建后漂掉。
        """
        source = settings if settings is not None else self.settings
        want = bool(source.translucent_bg)
        if want == bool(self.testAttribute(Qt.WA_TranslucentBackground)):
            return
        geometry = self.geometry()
        anchor = self.reader.first_visible_char() if self.reader.char_count() > 0 else 0
        was_visible = self.isVisible()
        self.hide()
        self.setAttribute(Qt.WA_TranslucentBackground, want)
        if was_visible:
            self.setGeometry(geometry)
            self.show()
            if self.reader.char_count() > 0:
                self.reader.scroll_to_char(anchor)
            # 重建后窗口不透明度会回到 1.0，按 settings 重新落一次。
            self.apply_opacity(self.settings.target_opacity(active=True), animate=False)

    def preview_bg_alpha(self, alpha: int) -> None:
        """拖动 alpha 滑块时的实时预览。

        半透明模式：改底色 alpha（只透背景，文字清晰），只 update()。
        兼容模式：没有逐像素 alpha，同一根滑块改成临时调整窗 setWindowOpacity。
        两种路径都不写回 settings——预览不落盘，取消设置时自动回到原样。
        """
        alpha = max(0, min(int(alpha), 255))
        if not self.settings.translucent_bg:
            # alpha_override 让这次调用按滑块的临时值折算，而不是已落盘的 bg_alpha。
            self.apply_opacity(
                self.settings.target_opacity(active=self.isActiveWindow() or self._dialog_open),
                animate=False,
                alpha_override=alpha,
            )
            return
        colour = QColor(self.bg_color)
        colour.setAlpha(alpha)
        self.bg_color = colour
        self.update()

    def apply_content_alpha(self, settings=None) -> None:
        """把 Settings.content_alpha 落到正文控件的 QGraphicsOpacityEffect 上。

        只作用于正文子控件，背景（paintEvent 画的底色）和窗口整体不透明度都不受影响
        ——这是和 bg_alpha 相互独立的那个维度。兼容模式下正文与背景无法分离，effect
        保持满不透明，淡出交给整窗 setWindowOpacity。
        """
        source = settings if settings is not None else self.settings
        if not source.translucent_bg:
            self.reader.apply_content_alpha(255)
            return
        self.reader.apply_content_alpha(max(0, min(int(source.content_alpha), 255)))

    def preview_content_alpha(self, alpha: int) -> None:
        """拖动内容透明度滑块的实时预览：只调正文的 effect，不写回 settings。"""
        if not self.settings.translucent_bg:
            return                          # 兼容模式下这根滑块是禁用的
        self.reader.preview_content_alpha(max(0, min(int(alpha), 255)))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        """画出窗口底色。这是顶层窗口唯一画背景的地方，按模式二选一。"""
        painter = QPainter(self)
        if not self.settings.translucent_bg:
            # 兼容模式：窗口本来就不透明（没有 WA_TranslucentBackground），圆角无从
            # 谈起，直接铺一层纯色；整窗的透明交给 setWindowOpacity。刻意不 fillPath，
            # 这条路径没有逐像素 alpha，画圆角只会把四个角也涂成窗口底色。
            painter.fillRect(
                self.rect(), QColor(self.bg_color.red(), self.bg_color.green(), self.bg_color.blue())
            )
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), BG_RADIUS, BG_RADIUS)
        painter.fillPath(path, QBrush(self.bg_color))

    # ---- geometry --------------------------------------------------------

    def _restore_geometry(self) -> None:
        screens = screen_rects() or [(0, 0, 1280, 720)]
        geo = validate_geometry(self.settings.geometry, screens)
        if geo is None:
            geo = default_geometry(screens[0])
        self.setGeometry(geo["x"], geo["y"], geo["w"], geo["h"])

    def save_geometry(self) -> None:
        rect = self.geometry()
        self.settings.copy_into_geometry(rect.x(), rect.y(), rect.width(), rect.height())
        self.settings.save(self._settings_path)

    # ---- window flags (置顶) ---------------------------------------------

    def _apply_window_flags(self) -> None:
        """改窗口标志必须重建原生窗口：hide → setWindowFlags → show。

        和 _sync_translucent_mode 同一个道理：运行中直接 setWindowFlags 会跳位置、丢
        原生状态。置顶开关走这条路。重建之后还必须重注册全局热键——热键绑在 HWND 上，
        窗口重建后旧 HWND 就死了，不重注册老板键会失效，那是唯一能把窗口找回来的东西。
        """
        geometry = self.geometry()
        anchor = self.reader.first_visible_char() if self.reader.char_count() > 0 else 0
        was_visible = self.isVisible()
        self.hide()
        self.setWindowFlags(self._window_flags())
        if was_visible:
            self.setGeometry(geometry)
            self.show()
            if self.reader.char_count() > 0:
                self.reader.scroll_to_char(anchor)
            self.apply_opacity(self.settings.target_opacity(active=True), animate=False)
        self.reload_hotkeys()

    def toggle_on_top(self) -> None:
        """📌：切换置顶，立刻生效并持久化。"""
        self.settings.always_on_top = not bool(self.settings.always_on_top)
        self.settings.save(self._settings_path)
        self._apply_window_flags()
        self._sync_pin_button()

    def _sync_pin_button(self) -> None:
        self.pin_button.setChecked(bool(self.settings.always_on_top))
        self.pin_button.setToolTip(
            TOOLTIP_PIN_ON if self.settings.always_on_top else TOOLTIP_PIN_OFF
        )

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._geom_timer.start()
        if not self._snapping and (self.settings.snap_enabled or self.settings.keep_on_screen):
            self._snap_timer.start()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._geom_timer.start()
        # A resize means the user is sizing the window, not placing it. Snapping a
        # resize would move the edge they are dragging. Only a resize inside a real
        # gesture counts - see the note on `_gesture_active` in __init__.
        if self._gesture_active:
            self._gesture_resized = True
        self._snap_timer.stop()

    # ---- edge snapping ---------------------------------------------------

    def _maybe_snap(self) -> None:
        """Settle the window once a drag has finished. Never during one.

        The OS owns the drag (`startSystemMove`), so there is no release event to hang
        this off; a quiet-period timer is the reliable signal that the gesture ended.
        The mouse-button check matters because a *pause* mid-drag is also a quiet period:
        snapping then would move the window out from under the cursor while the OS is
        still computing the drag from its own anchor, and the two would visibly fight.
        The deferral is capped so a stale button state cannot suppress snapping forever.

        The magnetic pull and the off-screen clamp are deliberately separate promises.
        Gating the clamp behind `snap_enabled` looked tidy and was wrong: switching off
        snapping would silently switch off the safety net that keeps a dragged-away
        window grabbable, and with the taskbar hidden there is no other way back.
        """
        if self.boss_hidden or self._drag_origin is not None:
            return
        if self._mouse_is_down():
            if self._snap_retries < 40:
                self._snap_retries += 1
                self._snap_timer.start()
            return
        self._snap_retries = 0
        # The gesture is over: consume its state so it cannot affect the next one.
        resized, self._gesture_resized = self._gesture_resized, False
        self._gesture_active = False

        rects = screen_rects()
        if not rects:
            return
        x, y = self.x(), self.y()
        if self.settings.snap_enabled and not resized:
            x, y = snapmod.snap_position(
                x, y, self.width(), self.height(), rects, self.settings.snap_distance
            )
        if self.settings.keep_on_screen:
            x, y = snapmod.clamp_position(x, y, self.width(), self.height(), rects)
        if (x, y) != (self.x(), self.y()):
            self._snapping = True
            try:
                self.move(x, y)
            finally:
                self._snapping = False

    @staticmethod
    def _mouse_is_down() -> bool:
        try:
            return bool(QApplication.mouseButtons() & Qt.LeftButton)
        except Exception:  # noqa: BLE001
            return False

    # ---- native drag / resize -------------------------------------------

    def edges_at(self, pos) -> int:
        """Which window edges a press at `pos` should resize. Pure, and unit-tested."""
        w, h = self.width(), self.height()
        m = RESIZE_MARGIN + FRAME
        edges = 0
        if pos.x() <= m:
            edges |= EDGE_LEFT
        elif pos.x() >= w - m:
            edges |= EDGE_RIGHT
        if pos.y() <= m:
            edges |= EDGE_TOP
        elif pos.y() >= h - m:
            edges |= EDGE_BOTTOM
        return edges

    def begin_drag(self, event) -> bool:
        """Hand the gesture to the OS, or fall back to manual movement."""
        self._gesture_active = True
        self._gesture_resized = False
        self._snap_retries = 0
        handle = self.windowHandle()
        if handle is not None and hasattr(handle, "startSystemMove"):
            try:
                if handle.startSystemMove():
                    return True
            except Exception:  # noqa: BLE001 - the platform may not implement it
                pass
        self._drag_origin = event_global_pos(event) - self.frameGeometry().topLeft()
        return False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 边缘优先：按在四条边/四个角上就是缩放，且只做缩放。旧实现先试
            # startSystemResize，失败会 fall-through 到 begin_drag——一次"缩放"被它
            # 拖成了"移动"。现在两条路在这里就彻底分开。
            edges = self.edges_at(event_local_pos(event))
            if edges:
                self.begin_resize(event, edges)
                event.accept()
                return
            self.begin_drag(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resize_edges and event.buttons() & Qt.LeftButton:
            self._handle_resize_move(event)
            event.accept()
            return
        if self._drag_origin is not None and event.buttons() & Qt.LeftButton:
            self.move(event_global_pos(event) - self._drag_origin)
            event.accept()
            return
        self._update_cursor(event_local_pos(event))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._resize_edges:
            self.end_resize(event)
            event.accept()
            return
        was_dragging = self._drag_origin is not None
        self._drag_origin = None
        # `_gesture_resized` is NOT cleared here: the snap check has to run first, and it
        # is the only reader. It consumes the flag itself.
        self._gesture_active = False
        if was_dragging:
            # The gesture is definitely over, so stop deferring on a held button.
            self._snap_retries = 0
            self._snap_timer.start()
        super().mouseReleaseEvent(event)

    # -- edge resize (manual, Qt-level) -----------------------------------

    def begin_resize(self, event, edges: int) -> None:
        """Start sizing from `edges` (a bitmask from `edges_at`).

        Pure Qt geometry, deliberately not `QWindow.startSystemResize()`: the native
        modal loop that enters cannot be driven under the offscreen QPA the test suite
        runs on (it returns without engaging), and enabling WS_THICKFRAME to feed
        WM_NCHITTEST immediately inset an already-created frameless/layered window by
        the thick-frame metrics on real Windows. Manual sizing is deterministic, is
        unit-testable on every edge, survives the topmost hide/rebuild, and keeps the
        existing snap + debounced-save state machine in charge.
        """
        self._resize_edges = int(edges)
        self._resize_start_pos = QPoint(event_global_pos(event))
        self._resize_start_geom = QRect(self.geometry())
        # Never move and size the same gesture: an edge press must not leave a stale
        # drag origin that mouseMoveEvent could also act on.
        self._drag_origin = None
        self._gesture_active = True
        self._gesture_resized = False
        self._snap_retries = 0
        self._snap_timer.stop()

    def _handle_resize_move(self, event) -> None:
        delta = event_global_pos(event) - self._resize_start_pos
        target = self.resized_geometry(
            self._resize_start_geom, self._resize_edges, delta.x(), delta.y()
        )
        if target != self.geometry():
            self.setGeometry(target)
        # setGeometry on a left/top drag also fires moveEvent, which restarts the snap
        # timer. Sizing is not placement: stop it every move so a resize never settles
        # into a snap while the button is held. resizeEvent does this too, but event
        # order (move vs resize) is not guaranteed across the two bindings.
        self._snap_timer.stop()

    def end_resize(self, event) -> None:
        """Finish a resize. Re-arms the settle timer for the keep-on-screen clamp only;
        the `_gesture_resized` veto (set in resizeEvent) makes _maybe_snap skip the
        magnetic pull. The debounced geometry saver was restarted by resizeEvent."""
        self._resize_edges = 0
        self._resize_start_pos = None
        self._resize_start_geom = None
        self._gesture_active = False
        self._snap_retries = 0
        self._snap_timer.start()

    def resized_geometry(self, start: QRect, edges: int, dx: int, dy: int) -> QRect:
        """Where the window lands after dragging `edges` by (dx, dy) from `start`.

        Anchors every side the user is *not* dragging and clamps to minimumSize, so a
        side/corner can never be pulled past the floor. Pure (no self mutation beyond
        reading the floor) and unit-tested for all eight directions.
        """
        left, top = start.x(), start.y()
        right, bottom = start.x() + start.width(), start.y() + start.height()
        min_w = max(0, int(self.minimumWidth()))
        min_h = max(0, int(self.minimumHeight()))
        if edges & EDGE_LEFT:
            # Right edge fixed: the left edge follows the pointer, but cannot cross
            # right-min_w (which is exactly the minimum-width floor).
            left = min(start.x() + dx, right - min_w)
        elif edges & EDGE_RIGHT:
            # Left edge fixed; the right edge follows the pointer.
            right = max(right + dx, left + min_w)
        if edges & EDGE_TOP:
            top = min(start.y() + dy, bottom - min_h)
        elif edges & EDGE_BOTTOM:
            bottom = max(bottom + dy, top + min_h)
        return QRect(left, top, right - left, bottom - top)

    @staticmethod
    def _cursor_for_edges(edges: int):
        """The resize cursor for an edges bitmask, or None for the interior."""
        left = bool(edges & EDGE_LEFT)
        right = bool(edges & EDGE_RIGHT)
        top = bool(edges & EDGE_TOP)
        bottom = bool(edges & EDGE_BOTTOM)
        if (left and top) or (right and bottom):
            return Qt.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.SizeBDiagCursor
        if left or right:
            return Qt.SizeHorCursor
        if top or bottom:
            return Qt.SizeVerCursor
        return None

    def _update_cursor(self, pos) -> None:
        shape = self._cursor_for_edges(self.edges_at(pos))
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    # ---- appearance ------------------------------------------------------

    def _scale_opacity(self, percent: int, alpha_override=None) -> int:
        """把"窗口不透明度"折算到当前模式实际生效的百分比。

        半透明模式恒等返回——底色的 alpha 由 paintEvent 负责，这里只管家窗口整体
        的淡入淡出（失焦变淡、手动调不透明度）。

        兼容模式没有逐像素 alpha，"背景透明度"滑块只能靠 setWindowOpacity 表达，于
        是把它当成整窗不透明度的基数，和 dim/手动不透明度相乘。同一根滑块在两种模式
        下方向一致：越靠左窗口越透（半透明模式只透背景，兼容模式整窗一起透）。
        """
        if self.settings.translucent_bg:
            return percent
        alpha = self.settings.bg_alpha if alpha_override is None else alpha_override
        base = max(0, min(int(alpha), 255)) / 255.0
        return max(0, min(100, int(round(percent * base))))

    def apply_opacity(self, percent: int, *, animate: bool = True, alpha_override=None) -> None:
        """Set the window's uniform opacity, in percent.

        Implemented with `setWindowOpacity()`, which on Windows is `WS_EX_LAYERED` +
        `LWA_ALPHA` - the same thing `win32.set_layered_alpha()` does by hand. Hit
        testing is unaffected (this is not `LWA_COLORKEY`), so the window does not
        become click-through. Qt drops the layered style again at 100%, which is what
        we want: an opaque layered window still loses ClearType for nothing.

        `alpha_override` is only used while the alpha slider is being dragged in 兼容
        模式: it substitutes the slider's in-flight value for the persisted bg_alpha so
        the preview tracks the handle instead of the value on disk.

        In 兼容模式 the result never goes below `COMPAT_MIN_OPACITY`: 0 there means an
        invisible window whose text is gone too, with only the tray icon as a way back.
        半透明模式 has no such floor - alpha 0 there is "text on the desktop", which is
        exactly what the feature is for.
        """
        percent = max(0, min(100, int(percent)))
        percent = self._scale_opacity(percent, alpha_override)
        if not self.settings.translucent_bg:
            percent = max(COMPAT_MIN_OPACITY, percent)
        value = percent / 100.0
        # Store the clamped number, not the request: `opacity_percent()` promises the
        # opacity the window is aiming at, and for an out-of-range request that is the
        # clamped value, not the nonsense one.
        self._target_opacity = percent
        # getattr: setWindowFlags/setGeometry can deliver an ActivationChange before the
        # animation object exists, and a crash during construction is not a trade-off.
        anim = getattr(self, "_opacity_anim", None)
        if not animate or OPACITY_MS <= 0 or anim is None:
            if anim is not None:
                anim.stop()
            self.setWindowOpacity(value)
            return
        anim.stop()
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(value)
        anim.start()

    def opacity_percent(self) -> int:
        """The opacity the window is currently aiming at, in percent.

        Deliberately not `windowOpacity() * 100`: during a dim/undim glide that reports
        an intermediate frame, which is useless for reporting and for tests.
        """
        return int(self._target_opacity)

    def refresh_opacity(self) -> None:
        """Recompute the dim-on-blur target and glide to it."""
        if self.boss_hidden:
            return
        # 移出隐身期间窗口停在 LEAVE_HIDE_OPACITY（近全透），而窗口失焦/激活都会走到
        # 这里；照常重算会把窗口重新点亮（鼠标还在窗口外，读者会看到它自己闪回来）。
        # 等 enterEvent 恢复。
        if self._opacity_before_hide is not None:
            return
        # While one of our own dialogs is up the main window is inactive by definition;
        # dimming it then just makes reading the dialog's own window look broken.
        active = self.isActiveWindow() or self._dialog_open
        self.apply_opacity(self.settings.target_opacity(active=active))

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        # QEvent.ActivationChange is the reliable signal for a Qt.Tool window: focus
        # events do not fire for a top-level window that is already focused.
        if int(event.type()) == int(EVENT_ACTIVATION_CHANGE):
            self.refresh_opacity()

    # ---- 鼠标移出隐身 ----------------------------------------------------

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        # 先撤销待执行的隐身：鼠标只是擦过边缘又回来了，这一拍之内不该闪。
        self._cancel_leave_hide()
        if self._opacity_before_hide is not None:
            # 直接 setWindowOpacity 落值，不走 apply_opacity：那条路会重新算 dim/激活
            # 状态，可能把用户设置的值顶掉，而这里要的就是"原样还回去"。
            self.setWindowOpacity(self._opacity_before_hide)
            self._opacity_before_hide = None
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._schedule_leave_hide()
        super().leaveEvent(event)

    def _schedule_leave_hide(self) -> None:
        """鼠标刚离开：延迟一拍再隐身，给"划过边缘"留个反悔的机会。"""
        if not self.settings.mouse_leave_hide:
            return
        # 右键菜单开着时不计时：菜单是独立 popup，选菜单项时指针必然在主窗外。
        if self._context_menu_open:
            return
        self._leave_hide_timer.start()

    def _cancel_leave_hide(self) -> None:
        if self._leave_hide_timer.isActive():
            self._leave_hide_timer.stop()

    def _apply_leave_hide(self) -> None:
        """延迟到点：整窗压到 LEAVE_HIDE_OPACITY，连文字一起近乎消失。

        下限是 0.01 而不是 0.0——原因见 LEAVE_HIDE_OPACITY 常量注释：alpha=0 会让
        Windows 把这个 layered window 变成点击穿透，移回鼠标收不到 enterEvent，窗口
        无法自恢复。0.01 视觉上等同全隐，但窗口仍然可命中。

        兼容模式同样降到 LEAVE_HIDE_OPACITY，不走 COMPAT_MIN_OPACITY——那个下限是给
        普通滑块用的，不是给隐身用的；而且这里必须用 setWindowOpacity 而不是改
        bg_color.alpha，因为要的是整窗消失（含文字），不是只透底色。
        """
        if not self.settings.mouse_leave_hide or not self.isVisible():
            return
        # 兜底守卫：即便定时器是在菜单弹出前启动的，到点时菜单若已打开也不许隐身。
        if self._context_menu_open:
            return
        if self._opacity_before_hide is None:
            # 只在第一次记：重复捕获会把已经落下去的 LEAVE_HIDE_OPACITY 存成"用户设置
            # 值"，窗口恢复时就只能亮到 1%，等于找不回来。
            self._opacity_before_hide = self.windowOpacity()
        # 失焦变淡/手动调不透明度的滑行动画还在跑，它会把目标值覆盖掉；先停。
        self._opacity_anim.stop()
        self.setWindowOpacity(LEAVE_HIDE_OPACITY)

    def _on_leave_hide_toggled(self, checked: bool) -> None:
        """👁 是即时开关：直接写 settings 并落盘（不像设置对话框要按"确定"）。"""
        self.settings.mouse_leave_hide = bool(checked)
        self.settings.save(self._settings_path)
        # tooltip 立刻跟着翻转：开关状态刚变，下一次悬停看到的必须是新动作。
        self.boss_button.setToolTip(
            TOOLTIP_LEAVE_HIDE_ON if checked else TOOLTIP_LEAVE_HIDE_OFF
        )
        if not checked:
            # 关掉开关时窗口可能正停在 LEAVE_HIDE_OPACITY（鼠标在窗口外），立刻点亮
            # 回来，否则要等下次移入才恢复。
            self._cancel_leave_hide()
            if self._opacity_before_hide is not None:
                self.setWindowOpacity(self._opacity_before_hide)
                self._opacity_before_hide = None

    def _sync_leave_hide_button(self, enabled: bool | None = None) -> None:
        """把 👁 的按下状态对齐 settings（或对齐调用方给的值）。

        blockSignals 是必须的：否则同步本身会被 toggled 当成一次用户点击，反过来又写盘。
        设置对话框那个复选框 toggled 时走这里，主窗按钮就跟着动。
        """
        value = bool(self.settings.mouse_leave_hide) if enabled is None else bool(enabled)
        button = getattr(self, "boss_button", None)
        if button is None:                      # 构造期 apply_preferences 可能早于按钮
            return
        button.blockSignals(True)
        button.setChecked(value)
        button.blockSignals(False)
        # 复选框预览（还没按"确定"）也要让 tooltip 跟着翻：它描述的是"此刻再点一下"。
        button.setToolTip(
            TOOLTIP_LEAVE_HIDE_ON if value else TOOLTIP_LEAVE_HIDE_OFF
        )

    def apply_disguise(self) -> None:
        """Apply 窗口标题伪装 to the OS title, the tray tooltip and the Win32 title.

        Three places, not one: Qt caches the title for its own APIs, the shell reads the
        HWND, and the tray tooltip is the only hoverable surface left once the taskbar
        button is gone. All three have to agree or the disguise leaks.
        """
        title = self.settings.disguise_title or "MiniReader"
        self.setWindowTitle(title)
        if self.tray is not None:
            self.tray.set_title(title)
        if win32.IS_WINDOWS:
            win32.set_window_text(win32.hwnd_of(self), title)

    def window_title(self) -> str:
        return self.settings.disguise_title or "MiniReader"

    # ---- settings --------------------------------------------------------

    def open_opacity_popup(self) -> None:
        """🎚：在按钮正下方弹出"背景 / 内容"双滑块浮层。

        浮层是 Qt.Popup，点窗口外任意处自己关，关的时候把预览撤回已落盘的值（见
        OpacityPopup.hideEvent）——所以这里只负责同步当前值再显示，不管关闭和落盘。
        """
        if self._opacity_popup is None:
            self._opacity_popup = OpacityPopup(self)
        self._opacity_popup.sync()
        self._opacity_popup.adjustSize()
        self._opacity_popup.move(self.alpha_button.mapToGlobal(self.alpha_button.rect().bottomLeft()))
        self._opacity_popup.show()

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self.settings,
            self,
            on_preview=self._preview_settings,
            on_background=self.preview_bg_alpha,
            on_content=self.preview_content_alpha,
            # 复选框一动就把 👁 的按下状态跟过去（只动按钮外观，设置仍然等"确定"才写）。
            on_leave_hide=self._sync_leave_hide_button,
            # 下拉里选主题：立刻生效（和顶栏 🌙 同一条通道），但**不落盘**——落盘仍然
            # 等"确定"。取消时下面的 else 分支会把主题回滚。
            on_theme=self._preview_theme,
        )
        if self.settings.hide_from_taskbar:
            # A dialog parented to a Qt.Tool window still gets its own taskbar button on
            # Windows, which would defeat 隐藏任务栏 for as long as the dialog is open.
            # Setting the flag here is safe: the dialog has no HWND worth preserving yet.
            dialog.setWindowFlag(Qt.Tool, True)
        self._dialog_open = True
        self._settings_dialog = dialog
        # 对话框打开前的主题。下拉里的主题是**实时预览**（不落盘），所以取消时必须
        # 从"打开前"这个值切回去，而不是从 self.settings.theme 读——那个字段在预览
        # 期间已经被写成预览值了（见 _preview_theme）。
        self._theme_before_dialog = theme.CURRENT
        self.refresh_opacity()
        try:
            accepted = exec_dialog(dialog)
        finally:
            self._dialog_open = False
            self._settings_dialog = None
        if accepted:
            self.settings = dialog.apply_to(self.settings)
            self.settings.save(self._settings_path)
            self.apply_preferences()
        else:
            # Undo any live preview: opacity first, then typography. apply_style keeps the
            # reader's character anchor, so cancelling a font-size change does not move
            # the reader either.
            self.apply_opacity(self.settings.target_opacity(active=self.isActiveWindow()))
            self.reader.apply_style(self.settings)
            self.apply_background(self.settings)
            # 内容透明度也不能留在预览值上：取消 = 两维度都回到打开对话框前的样子。
            self.apply_content_alpha(self.settings)
            # 👁 的按下状态同理：取消后必须回到磁盘上的 mouse_leave_hide。
            self._sync_leave_hide_button()
            # 主题同理：预览期间界面已经切过去了，取消要原样切回来（不落盘）。
            self.apply_theme(self._theme_before_dialog, persist=False)
        dialog.deleteLater()
        self.refresh_opacity()

    def _preview_theme(self, name: str) -> None:
        """设置页下拉的实时预览：立即切换外观，但**不**写盘。

        和 🌙 按钮共用 `apply_theme()`，所以两条入口的副作用完全一致（顶栏、底栏、
        正文选中色、目录高亮条都跟着走）。
        """
        self.apply_theme(name, persist=False)

    def _preview_settings(self, preview: Settings) -> None:
        """Live preview while the dialog is open. Nothing is written to disk."""
        self.apply_opacity(preview.target_opacity(active=True))
        # 背景色也在预览范围内：只 update()，不改顶层样式表。
        self.apply_background(preview)
        # 内容透明度同理：只调正文的 effect，背景不动。
        self.apply_content_alpha(preview)
        # 排版也要实时预览：用户在对话框里把字号从 17 调到 26，如果非要按下"确定"
        # 才看得见效果，那根滑杆就等于在盲调。
        self.reader.apply_style(preview)

    def apply_preferences(self) -> None:
        """Push the current settings into the live window. Safe to call any time."""
        # 主题排在模式切换之前：它只改样式表与自绘颜色，不重建原生窗口，所以放在最前
        # 面不会和下面的 _sync_translucent_mode 抢窗口。设置对话框按下"确定"时，主题
        # 预览值就是这里的落盘值，这一步把活动调色板与 settings.theme 对齐（两者通常
        # 已经相等，相等时 apply_theme 是幂等的）。
        if theme.CURRENT != self.settings.theme:
            self.apply_theme(self.settings.theme, persist=False)
        # 模式切换要重建原生窗口，放最前面：后面的 opacity/background 才会作用在正确
        # 的窗口上。
        self._sync_translucent_mode()
        self.apply_opacity(self.settings.target_opacity(active=self.isActiveWindow() or self._dialog_open))
        self.apply_background()
        self.apply_content_alpha()
        self.reader.apply_style(self.settings)
        self.apply_autoscroll_settings()
        self.apply_comic_settings()
        self.apply_disguise()
        self.sync_tray()
        self.reload_hotkeys()
        self._sync_leave_hide_button()
        self.refresh_opacity()

    def apply_comic_settings(self) -> None:
        """Push the comic-side settings into the comic reader.

        Separate from `apply_preferences()` so it can also run at construction,
        before a book exists. `comic_default_mode` and `comic_default_zoom` are
        applied to the reader's *state* rather than remembered as defaults to
        apply on open: the reader is a long-lived widget and its state is what
        the title-bar buttons read, so the setting and the button stay in step
        either way.

        Modes this build cannot render yet are skipped rather than pushed — see
        the comment on `mode` below.
        """
        settings = self.settings
        self._comic_reader.set_cache_mb(_as_int(settings.comic_cache_mb, DEFAULT_CACHE_MB))
        self._comic_reader.set_preload(_as_int(settings.comic_preload, PRELOAD_RADIUS))
        self._comic_reader.set_zoom(getattr(settings, "comic_default_zoom", "width"))
        # Mode C's own settings. Pushed here so the mode button and the settings
        # dialog describe the same state, whichever the user changed last.
        self._comic_reader.set_binding(getattr(settings, "comic_binding", "rtl"))
        self._comic_reader.set_gutter(_as_int(getattr(settings, "comic_gutter", 0), 0))
        self._comic_reader.set_cover_alone(bool(getattr(settings, "comic_cover_alone", True)))
        # Only push a mode this build can actually render. `set_mode()` answers an
        # unimplemented mode by emitting a "coming in stage N" notice and keeping the
        # old one — correct when the user clicks the button, but wrong at startup:
        # a saved `comic_default_mode="spread"` would pop that notice on every launch,
        # for a setting the user set long ago and cannot act on yet. Leaving the mode
        # alone lets it arrive silently when stage 5 implements it.
        #
        # `set_mode()` also refuses `spread` on a portrait window (the reader decides,
        # so every caller gets the same answer), which is exactly the behaviour wanted
        # here: a saved preference does not fight the window's shape.
        mode = getattr(settings, "comic_default_mode", "single")
        if mode in self._comic_reader.IMPLEMENTED_MODES:
            self._comic_reader.set_mode(mode)
        self._sync_comic_buttons()

    def sync_tray(self) -> None:
        """Create or destroy the tray icon so the setting takes effect without a restart.

        Unlike window flags, a tray icon can be added and removed live. If the user turns
        it off from the tray menu, the menu they came from disappears - which is exactly
        what they asked for.
        """
        from .tray import make_tray

        wants = bool(self.settings.show_tray)
        if wants and self.tray is None:
            self.attach_tray(make_tray(self, title=self.window_title()))
        elif not wants and self.tray is not None:
            self.tray.shutdown()
            self.tray = None
            self.update_boss_capability()

    def reload_hotkeys(self) -> None:
        """Re-register every bound chord from the current settings."""
        if self.hotkeys is None:
            return
        results = register_all(
            self.hotkeys,
            self.settings.hotkeys(),
            win32.hwnd_of(self),
            labels=HOTKEY_LABELS,
        )
        self.update_boss_capability(results)
        failures = [f"{HOTKEY_LABELS.get(name, name)}：{message}" for name, (ok, message) in results.items() if not ok]
        if failures:
            self.set_warning("；".join(failures))

    def attach_tray(self, tray) -> None:
        """Wire a tray icon up. The window never assumes the tray exists."""
        self.tray = tray
        if tray is None:
            self.update_boss_capability()
            return
        tray.toggleRequested.connect(self.toggle_boss)
        tray.showRequested.connect(self.present_window)
        tray.hideRequested.connect(self.hide_to_boss)
        tray.openRequested.connect(self.open_dialog)
        tray.settingsRequested.connect(self.open_settings)
        tray.quitRequested.connect(self.quit_app)
        tray.opacityRequested.connect(self.set_opacity_percent)
        tray.set_hidden_state(self.boss_hidden)
        tray.set_opacity(self.settings.opacity)
        tray.set_title(self.window_title())
        self.update_boss_capability()

    def quit_app(self) -> None:
        """Leave for real. Only the tray's 退出 entry (or the app shell) reaches here."""
        self._really_quit = True
        self.close()

    def set_opacity_percent(self, percent: int) -> None:
        """From the tray menu: a deliberate change, so it is persisted."""
        self.settings.opacity = int(percent)
        self.settings.clamp()
        self.settings.save(self._settings_path)
        if self.tray is not None:
            self.tray.set_opacity(self.settings.opacity)
        self.refresh_opacity()

    # ---- books -----------------------------------------------------------

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开文件", os.path.expanduser("~"), dialog_filters()
        )
        if path:
            self.load_path(path)

    def load_path(
        self,
        path: str,
        *,
        chapter: int | None = None,
        offset: int | None = None,
        pdf_kind: str | None = None,
    ) -> bool:
        """Open any supported file, resuming where this book was last left off.

        The resume lookup lives here rather than at the call sites so that every way of
        opening a book - the file dialog, drag-and-drop, the command line, the
        remembered last book - behaves the same. Passing an explicit chapter or offset
        overrides it.

        A dual-identity file whose content cannot be classified asks once, here, so
        that every entry point gets the same question and none of them can open such a
        book silently as the wrong thing.

        `pdf_kind` is a caller-supplied answer that skips both the measurement and the
        question - it is how the reader menu's 「按文本打开」/「按漫画打开」 take effect.
        """
        # `answer` is the user's choice for this file. It is written down only after
        # the book is actually in hand - see the comment at the write site.
        answer = pdf_kind if pdf_kind in (PDF_TEXT, PDF_COMIC) else None
        is_pdf = _ext_of(str(path)) in DUAL_IDENTITY_EXTS
        if answer is not None and is_pdf:
            # The user just picked a reader. No need to measure, and nothing to ask:
            # they have already answered.
            prefer: str | None = answer
        else:
            # An opinion about a non-PDF is not an opinion - do not store one.
            answer = None
            prefer = self._pdf_preference(path)
            if prefer is None and is_pdf:
                # Nobody has an opinion yet: measure, and ask if the measurement is
                # inconclusive. `pdf_choice()` is the only call that reports ambiguity.
                try:
                    choice = pdf_choice(path)
                except BookError as exc:
                    self.set_warning(f"打不开：{exc}")
                    return False
                except Exception as exc:  # noqa: BLE001 - a bad file must not kill the window
                    self.set_warning(f"打不开：{type(exc).__name__}: {exc}")
                    return False
                if choice.needs_ask:
                    answer = self._ask_pdf_kind(path)
                    # A dismissed dialog means "no opinion": text is what the file
                    # would have opened as anyway, and nothing is written down.
                    prefer = answer if answer is not None else PDF_TEXT
                else:
                    # Hand the measurement back to `open_book()` so it does not have to
                    # measure the same document a second time.
                    prefer = choice.kind
        try:
            book = open_book(path, pdf_kind=prefer)
        except BookError as exc:
            self.set_warning(f"打不开：{exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the window
            self.set_warning(f"打不开：{type(exc).__name__}: {exc}")
            return False
        if answer is not None:
            # Written only now, with the book in hand. Recording the answer before
            # the open would poison the file when the chosen reader cannot open it:
            # no book on screen means no reader menu, so there would be no way left
            # to change the answer back.
            self._remember_pdf_kind(book.id, answer)
        if chapter is None and offset is None:
            saved = self.progress.get(book.id)
            if book.kind == KIND_COMIC:
                # 漫画记的是页码（旧文件里没有这个键，取默认 0）。像素偏移对漫画没有
                # 意义，所以不看它——否则一本旧书残留的 offset 会让刚打开的漫画
                # 被当成"已读到某处"。
                if saved.page:
                    chapter = saved.page
            elif saved.chapter or saved.offset:
                chapter, offset = saved.chapter, saved.offset
        self.set_book(book, chapter=chapter, offset=offset)
        return True

    def _pdf_preference(self, path) -> str | None:
        """Which reader a PDF should open in, or None to let the file decide.

        Three answers, in priority order:

        1. What this book was opened as last time. A per-book answer is the whole
           point - the user corrected one file, not all PDFs.
        2. `pdf_default_kind` from the settings, when it is not "auto".
        3. None, meaning `pdfdoc.classify()` measures the document.

        Reading the progress store here is safe: `open_book()` needs the answer
        before it can build a book, and the store is already loaded.
        """
        if _ext_of(str(path)) not in DUAL_IDENTITY_EXTS:
            return None
        remembered = self.progress.get(book_id(path)).pdf_kind
        if remembered in (PDF_TEXT, PDF_COMIC):
            return remembered
        chosen = self.settings.pdf_default_kind
        return chosen if chosen in (PDF_TEXT, PDF_COMIC) else None

    def remember_pdf_kind(self, kind: str) -> None:
        """Store the user's "read this PDF as ..." choice for this book only.

        Called from the reader menu. The choice takes effect immediately, so the
        document is reopened - which for a PDF is cheap (opening the document is
        half a millisecond; nothing is parsed until a page is asked for).

        The reopen goes through `load_path(pdf_kind=...)` rather than writing the
        answer here first. That ordering is the whole point: a scanned PDF has no
        extractable text, so "open it as text" cannot succeed, and an answer written
        before the attempt would be left on disk pointing at a reader that fails.
        The next launch would then have no book on screen - and since the reader
        menu's PDF items are only reachable while a PDF is open, there would be no
        way left to undo the choice. `load_path()` writes only after the book is in
        hand, so a failed override changes nothing.
        """
        if self.book is None or _ext_of(self.book.path) not in DUAL_IDENTITY_EXTS:
            return
        if kind not in (PDF_TEXT, PDF_COMIC):
            return
        if self.progress.get(self.book.id).pdf_kind == kind:
            return
        path = self.book.path
        if not self.load_path(path, pdf_kind=kind):
            # The chosen reader could not open this file. Nothing was written, so
            # the reader on screen is still the one that works - say so, because
            # `load_path()`'s warning alone reads like the file itself is broken.
            self.set_warning(
                "这个 PDF 按文本打开取不到文字，已保持原来的打开方式"
                if kind == PDF_TEXT
                else "这个 PDF 按漫画打开失败，已保持原来的打开方式"
            )
            return
        self.set_warning("已按文本打开，下次仍这样打开" if kind == PDF_TEXT else "已按漫画打开，下次仍这样打开")

    def _ask_pdf_kind(self, path) -> str | None:
        """Ask once which reader an unclassifiable PDF should use.

        Called only when `pdfdoc.classify()` said `needs_ask` - nobody has a
        remembered answer, the settings say "auto", and measuring the document
        found neither enough text nor enough picture to decide.

        Returns the chosen kind, or None when the user dismissed the dialog -
        and the caller then opens the file as text, which is what it would have
        opened as without the question. A dismissed dialog must never be worse
        than no dialog.

        This deliberately does **not** write the answer down. Remembering is the
        caller's job, done only after the book actually opens: a PDF with no
        extractable text fails to open as text, and if the choice had already
        been stored, that file would be stuck failing forever - there is no book
        on screen, so there is no right-click menu to correct it with.
        """
        title = os.path.splitext(os.path.basename(str(path)))[0] or str(path)
        dialog = PdfKindDialog(title, parent=self)
        if self.settings.hide_from_taskbar:
            # Same reason as the settings dialog: a dialog parented to a Qt.Tool
            # window gets its own taskbar button on Windows, which would defeat
            # 隐藏任务栏 for as long as it is open.
            dialog.setWindowFlag(Qt.Tool, True)
        was_open = self._dialog_open
        self._dialog_open = True
        try:
            accepted = exec_dialog(dialog)
        finally:
            self._dialog_open = was_open
        choice = dialog.choice() if accepted else None
        return choice if choice in (PDF_TEXT, PDF_COMIC) else None

    def _remember_pdf_kind(self, book_id_value: str, kind: str) -> None:
        """Store one book's answer for the next open of that same file."""
        record = self.progress.get(book_id_value)
        record.pdf_kind = kind
        self.progress.put(book_id_value, record)
        self.progress.save()

    def set_book(self, book, *, chapter: int | None = None, offset: int | None = None) -> None:
        self.book = book
        self.chapter_index = book.clamp(chapter if chapter is not None else 0)
        # 先选 Reader 再显示：漫画要读的是页码而不是像素偏移，两件事必须发生在
        # show_chapter() 之前，否则第一帧会用错的控件渲染错的坐标。
        self._select_reader(book.kind)
        self.show_chapter(offset=offset or 0)
        self.title_label.setText(book.title)
        self.title_label.setToolTip(f"{book.title}\n{book.describe()}")
        self.set_warning("编码可能不准，可在设置里手动指定" if not book.confident else "")
        self.library.record(book.to_record())
        self.library.save()
        self.persist_progress()
        # 换书就要换目录和书签：面板可能正开着，而它上面还挂着上一本书的章节。
        self._refresh_toc()

    def _select_reader(self, kind: str) -> None:
        """把当前 Reader 切到 `kind` 对应的那个，并重新绑定依赖它的部件。

        `self.reader` 始终指向"当前生效的 Reader"，属性名和语义都没变——窗口里
        25 处调用点和十几条既有测试都按这个名字取正文控件。

        停掉旧 Reader 是有意义的：漫画页缓存在内存里按 MB 计（默认 256 MB），
        切去读小说时那些页不该继续占着。
        """
        index = 0 if kind != KIND_COMIC else 1
        if self._reader_stack.currentIndex() != index:
            self.reader.stop()
            self._reader_stack.setCurrentIndex(index)
        self.reader = self._reader_stack.widget(index)
        # 自动滚动器只认构造时注入的那根滚动条，换 Reader 不会让它改换目标。从"开着
        # 自动滚动的小说"切到漫画时必须显式停掉：否则它会继续在隐藏的正文上滚到底，
        # 然后 `_on_autoscroll_end` 把漫画一页页翻下去——用户什么都没按，页码自己在走。
        if self.reader.scrollbar() is None:
            self.autoscroller.stop()
            self._sync_autoscroll_button()
        self._rebind_reader_views()

    def _rebind_reader_views(self) -> None:
        """把"跟着当前 Reader 走"的部件重新挂到新的那个上面。

        两件事：

        1. **事件过滤器**。它同时负责"鼠标停在正文上时暂停自动滚动"和 4px 边缘缩放
           热区，不装的话漫画模式下边缘拖拽会静默失效。漫画换模式会换 `viewport()`
           返回的控件，所以这里先把旧的摘掉再装新的——同一个窗口只该有一处安装点。

        2. **自动滚动器换目标**。`AutoScroller` 构造时就吃死了注入的那根滚动条，
           而窗口启动时注入的是**文字正文**的条。模式 A 的漫画 `scrollbar()` 返回
           None，于是停掉（否则它会继续在隐藏的正文上滚到底，再经
           `_on_autoscroll_end` 把漫画一页页翻下去——用户什么都没按，页码自己在走）。
           模式 B 返回的是一条真滚动条，此时必须换过去：不换的话按钮亮成"暂停"、
           屏幕上什么都不动，而且滚的是看不见的正文。
        """
        viewport = self.reader.viewport()
        self._install_view_filter(viewport)
        self._bind_autoscroller(self.reader.scrollbar())
        self._sync_comic_buttons()

    def _install_view_filter(self, viewport) -> None:
        """把边缘缩放/悬停暂停的过滤器装到 `viewport` 上，旧的摘掉。"""
        if viewport is self._filtered_viewport:
            return
        if self._filtered_viewport is not None:
            self._filtered_viewport.removeEventFilter(self)
        self._filtered_viewport = viewport
        if viewport is not None:
            viewport.installEventFilter(self)
            # 正文左/右缘 8px 被 viewport 盖着；开追踪后 eventFilter 才能在不按键
            # 悬停时把那一条上的光标换成左右缩放形状（见 _route_edge_event）。
            viewport.setMouseTracking(True)

    def _bind_autoscroller(self, bar) -> None:
        """让自动滚动去滚 `bar`；`bar` 为 None 时改滚一根"停着的"空条。

        **为什么不是直接传 None。** `AutoScroller` 的契约是"构造时注入哪根条就滚
        哪根"，它 `start()` 的第一件事就是读 `bar.value()`。传 None 能构造成功，但
        任何一条走到 `start()` 的路径都会 `AttributeError: 'NoneType' object has no
        attribute 'value'`——那是个只在特定顺序下才炸的雷。给它一根范围是 (0,0) 的
        真条，`start()` 会立刻判定"到底了"并安全收摊（见 `AutoScroller.start()`），
        而且 `autoscroll.py` 一个字节都不用改。

        **为什么新建实例而不是给现有实例换目标。** 同上：它吃死注入的那根条正是它
        和滚轮缓动解耦的设计（`autoscroll.py` 的模块 docstring），加 setter 等于打开
        "运行中换目标"这个口子。代价是这里必须重接信号、重推设置——新实例不继承
        旧实例的状态。

        `self.autoscroller` 始终指向"当前生效的那个"，窗口里所有调用点和测试都按
        这个名字取，语义和 `self.reader` 一样。`self._autoscroll_bar` 记的是当前
        真正绑上去的那根（可能是那根停着的空条），用来判断"要不要重新绑"。
        """
        target = bar if bar is not None else self._parked_bar
        if target is self._autoscroll_bar:
            return
        if self.autoscroller is not None:
            self.autoscroller.stop()
            self.autoscroller.deleteLater()
        self._autoscroll_bar = target
        self.autoscroller = AutoScroller(target, parent=self)
        self.autoscroller.reachedEnd.connect(self._on_autoscroll_end)
        self.autoscroller.stopped.connect(self._sync_autoscroll_button)
        self.apply_autoscroll_settings()
        self._sync_autoscroll_button()

    def _sync_comic_buttons(self) -> None:
        """漫画专属控件的可见性与文案。文本模式下它们不参与布局。"""
        comic = self.book is not None and self.book.kind == KIND_COMIC
        for button in (self.comic_mode_button, self.comic_zoom_button):
            button.setVisible(comic)
        reader = self._comic_reader
        # The binding button only makes a visible difference in two-page mode, so
        # it appears with it. A button that changes nothing is worse than no
        # button: the user clicks it, sees nothing move, and stops trusting the
        # other two.
        self.comic_binding_button.setVisible(comic and reader.mode == "spread")
        if not comic:
            return
        self.comic_mode_button.setText(reader.mode_label())
        self.comic_mode_button.setToolTip(reader.mode_tip())
        self.comic_zoom_button.setText(reader.zoom_label())
        self.comic_zoom_button.setToolTip(reader.zoom_tip())
        self.comic_binding_button.setText(reader.binding_label())
        self.comic_binding_button.setToolTip(reader.binding_tip())

    def cycle_comic_mode(self) -> None:
        self._comic_reader.cycle_mode()
        self._sync_comic_buttons()
        self._update_status()

    def cycle_comic_binding(self) -> None:
        """翻一下装订方向。`cycle_binding()` 记的是"本书覆盖"，不是改设置。

        提示由 `ComicReader` 自己发（它才知道方向是从哪一级来的），窗口只负责
        刷新按钮和底栏。
        """
        self._comic_reader.cycle_binding()
        self._sync_comic_buttons()
        self._update_status()

    def cycle_comic_zoom(self) -> None:
        self._comic_reader.cycle_zoom()
        self._sync_comic_buttons()
        # 底栏次要信息里带着 `zoom_tip()`（"缩放：高度适应"），换档后不刷新就会一直
        # 显示上一档，直到用户翻页才追上——按钮变了而底栏没变，看起来像没生效。
        self._update_status()

    def show_chapter(
        self,
        *,
        offset: int = 0,
        at_end: bool = False,
        char_offset: int | None = None,
    ) -> None:
        if self.book is None:
            self._show_placeholder()
            return
        self.chapter_index = self.book.clamp(self.chapter_index)
        # 一律走 Reader 接口的 show_book()：文本 Reader 用 offset/char_offset 还原像素
        # 位置，漫画 Reader 只看 index。窗口不再自己调 set_chapter()，也不再自己取
        # chapter_body()——"怎么把一本书显示在某个位置"是 Reader 的事。
        self.reader.show_book(
            self.book,
            index=self.chapter_index,
            offset=offset,
            at_end=at_end,
            char_offset=char_offset,
        )
        self._update_status()

    def step_chapter(self, delta: int) -> None:
        """Move one position: a chapter for text, a page for modes A/B, a spread for C.

        Mode C delegates the step to the reader rather than doing the arithmetic
        here. A spread is two pages wide, so `chapter_index + delta` would move by
        one *page* - and the reader would then collapse that page back into the
        spread it already belongs to, so two presses would show the same spread
        (measured: pressing "下一页" four times from the cover reached only the
        third spread). `ComicReader.next_page()`/`prev_page()` know the spread
        arithmetic; this only has to adopt the page they landed on.
        """
        if self.book is None:
            return
        if self.book.kind == KIND_COMIC and self._comic_reader.mode == "spread":
            if delta > 0:
                self._comic_reader.next_page()
            elif delta < 0:
                self._comic_reader.prev_page()
            else:
                return
            # The reader owns the position; the window follows. `_on_scrolled`
            # normally syncs this, but a direct `next_page()` (hotkey or test)
            # bypasses the reader's `scrolled` signal, so copy it across here too
            # rather than relying on exactly one path.
            self.chapter_index = self.book.clamp(self._comic_reader.index)
            self._update_status()
            self.persist_progress()
            return
        target = self.book.clamp(self.chapter_index + delta)
        if target == self.chapter_index:
            # 已经在首/末页，这一步没动。连续滚动模式下要说出来：右键菜单的
            # 「上一页/下一页」和底栏那两个胶囊钮都走这里，而模式 B 的页码是
            # 滚出来的，用户点一下没反应会以为菜单坏了。单页模式保持沉默——
            # 那里的按钮本身就是灰的，已经说清楚了。
            #
            # 方向取反：`delta` 是"往书的哪个方向走"（+1 是下一页），而
            # `say_boundary` 收的是"卡在哪一端"（+1 是顶端），两者相反。
            # 传错的话"末页点下一页"会说成"已经是第一页了"——实测就是这么错的。
            if self.book.kind == KIND_COMIC:
                self._comic_reader.say_boundary(-delta)
            return
        self.chapter_index = target
        # Going back lands at the END of the previous chapter: the reader was moving
        # forward, so the far edge is the interesting one. A comic page has no "end",
        # and ComicReader ignores at_end, so the same call serves both.
        self.show_chapter(at_end=delta < 0)
        self.persist_progress()

    def goto_chapter(self, index: int, *, at_end: bool = False) -> None:
        if self.book is None:
            return
        self.chapter_index = self.book.clamp(index)
        self.show_chapter(at_end=at_end)
        self.persist_progress()

    # ---- 正文右键菜单（阶段 3）------------------------------------------

    def _build_reader_menu(self) -> None:
        """整个窗口生命周期复用同一个菜单：动作只建一次，启用态弹出前现算。

        纯文字三项：上一页 / 下一页 ｜ 重新加载本页。不设图标、不设快捷键——
        QAction.setShortcut 会在菜单项右侧渲染一列快捷键，规格实测没有那一列。
        """
        menu = QMenu(self)
        menu.setObjectName("ReaderContextMenu")
        menu.setMinimumWidth(READER_MENU_MIN_W)
        menu.setStyleSheet(reader_menu_qss())

        self.prev_page_action = QAction("上一页", menu)
        self.prev_page_action.triggered.connect(lambda: self.step_chapter(-1))
        menu.addAction(self.prev_page_action)

        self.next_page_action = QAction("下一页", menu)
        self.next_page_action.triggered.connect(lambda: self.step_chapter(1))
        menu.addAction(self.next_page_action)

        menu.addSeparator()

        self.reload_page_action = QAction("重新加载本页", menu)
        self.reload_page_action.triggered.connect(self.reload_current_chapter)
        menu.addAction(self.reload_page_action)

        # PDF 的两项只在 PDF 上出现：别的格式的 kind 是确定的，给它们一个
        # "按漫画打开" 只会让用户以为按了会有什么变化。菜单项自身 setVisible(False)
        # 而不是菜单重建，理由同上——动作只建一次。
        menu.addSeparator()
        self.pdf_text_action = QAction("按文本打开", menu)
        self.pdf_text_action.triggered.connect(lambda: self.remember_pdf_kind(PDF_TEXT))
        menu.addAction(self.pdf_text_action)

        self.pdf_comic_action = QAction("按漫画打开", menu)
        self.pdf_comic_action.triggered.connect(lambda: self.remember_pdf_kind(PDF_COMIC))
        menu.addAction(self.pdf_comic_action)
        self.pdf_separator = menu.actions()[-3]
        # Hidden until a PDF is open. Done here as well as in `_sync_pdf_actions`
        # because a hidden action still contributes to `sizeHint()`, and the menu
        # is measured (and shown) before any `aboutToShow` fires.
        for widget in (self.pdf_text_action, self.pdf_comic_action, self.pdf_separator):
            widget.setVisible(False)

        # aboutToShow：刷新边界启用态（第一章不能再往前），同时暂停移出隐身。
        # aboutToHide：恢复——指针若还在窗外，重新开始 100ms 防抖。
        menu.aboutToShow.connect(self._on_reader_menu_about_to_show)
        menu.aboutToHide.connect(self._on_reader_menu_about_to_hide)
        self.reader_menu = menu

    def show_reader_menu(self, global_pos: QPoint) -> None:
        """正文右键：在鼠标点弹出三项菜单（非阻塞 popup，不走 exec）。"""
        self.reader_menu.popup(global_pos)

    def _on_reader_menu_about_to_show(self) -> None:
        # 菜单打开期间移出隐身一律让位（定时中也立刻撤掉）。
        self._context_menu_open = True
        self._cancel_leave_hide()
        self._sync_pdf_actions()
        book = self.book
        if book is None:
            self.prev_page_action.setEnabled(False)
            self.next_page_action.setEnabled(False)
            self.reload_page_action.setEnabled(False)
            return
        self.reload_page_action.setEnabled(True)
        # 与底栏胶囊钮同一条边界规则（见 _update_status），避免点了没反应。
        self.prev_page_action.setEnabled(self.chapter_index > 0)
        self.next_page_action.setEnabled(self.chapter_index < book.chapter_count - 1)

    def _sync_pdf_actions(self) -> None:
        """Show the two PDF entries only for a PDF, and tick the current choice.

        Checkable actions rather than a radio group: the menu is a one-shot popup
        and never has to hold a consistent selection, so the extra state a
        QActionGroup would track has nowhere to go.
        """
        is_pdf = self.book is not None and _ext_of(self.book.path) in DUAL_IDENTITY_EXTS
        for widget in (self.pdf_text_action, self.pdf_comic_action, self.pdf_separator):
            widget.setVisible(is_pdf)
        if not is_pdf:
            return
        # The tick reflects what this document is *currently* open as, not what is
        # remembered - the two differ right after a settings change, and the menu
        # should describe the screen.
        opened_as_text = self.book.kind != KIND_COMIC
        self.pdf_text_action.setCheckable(True)
        self.pdf_comic_action.setCheckable(True)
        self.pdf_text_action.setChecked(opened_as_text)
        self.pdf_comic_action.setChecked(not opened_as_text)

    def _on_reader_menu_about_to_hide(self) -> None:
        self._context_menu_open = False
        # 关闭后恢复防抖：指针仍在窗外就重新计时；指针已经回到窗内（enter 会取消）
        # 或功能被关掉时，不做任何事。
        if self.settings.mouse_leave_hide and self.isVisible():
            if not self.geometry().contains(QCursor.pos()):
                self._leave_hide_timer.start()

    def reload_current_chapter(self) -> None:
        """「重新加载本页」：从磁盘重读当前书，留在当前章并保住视口锚点。

        重读而不是 set_chapter 重放旧文本：这个入口的意义就是文件在外部被改动
        （换了编码、补了内容）后能看到新版本。锚点取视口顶端的字符位置而不是像素，
        新版文本若变长/变短，读者仍停在同一句话附近。

        走 `open_book()` 而不是 `Book.from_path()`：后者只认文字书，对漫画会返回一个
        Book 去喂给 ComicReader，在那里 `book.pages` 上抛 AttributeError（实测崩在
        comic_reader.py 的 `_render()`）。重开一次同一个文件必须还是同一类书。
        """
        book = self.book
        if book is None:
            return
        try:
            reloaded = open_book(book.path)
        except BookError as exc:
            self.set_warning(f"重新加载失败：{exc}")
            return
        anchor = self.reader.first_visible_char() if self.reader.char_count() else None
        self.book = reloaded
        # 漫画没有「编码可能不准」这回事，而且它的位置是页码不是字符偏移。
        if reloaded.kind == KIND_COMIC:
            self._select_reader(reloaded.kind)
            self.chapter_index = reloaded.clamp(self.chapter_index)
            self.show_chapter()
        else:
            self.chapter_index = reloaded.clamp(self.chapter_index)
            self.show_chapter(char_offset=anchor)
        self.title_label.setText(reloaded.title)
        self.title_label.setToolTip(f"{reloaded.title}\n{reloaded.describe()}")
        self.set_warning("编码可能不准，可在设置里手动指定" if not reloaded.confident else "")
        self.library.record(reloaded.to_record())
        self.library.save()
        # 章数/标题可能已经变了，目录抽屉和书签列表不能还挂着旧版本。
        self._refresh_toc()
        self.persist_progress()

    # ---- 章节目录抽屉 ----------------------------------------------------

    def toggle_toc(self) -> None:
        if self.toc_panel.isVisible():
            self.close_toc()
        else:
            self.open_toc()

    def open_toc(self) -> None:
        """打开目录，并让它显示的是**当前这本书**的章节。"""
        self._refresh_toc()
        self.toc_panel.setVisible(True)
        self.toc_button.setChecked(True)
        # 目录打开时自动变淡是反效果：用户正在看它，窗口失焦就变暗会让它更难读。
        self.toc_panel.set_opacity_hint(False)
        self.toc_panel.set_current(self.chapter_index)
        self.toc_panel.focus_search()

    def close_toc(self) -> None:
        self.toc_panel.setVisible(False)
        self.toc_button.setChecked(False)

    def _refresh_toc(self) -> None:
        book = self.book
        titles = [c.title for c in book.chapters] if book is not None else []
        self.toc_panel.set_chapters(titles)
        self.toc_panel.set_current(self.chapter_index if book is not None else 0)
        self.reload_bookmarks()

    def _on_toc_chapter(self, index: int) -> None:
        self.goto_chapter(index)
        # 选中一章就收起来：抽屉盖住正文，读者点目录的目的就是去看那一章。
        self.close_toc()

    # ---- 书签 ------------------------------------------------------------

    def reload_bookmarks(self) -> None:
        book = self.book
        items = self.store.list_for(book.id) if book is not None else []
        self.toc_panel.set_bookmarks([b.to_dict() for b in items])

    def add_bookmark(self) -> None:
        """把"现在屏幕顶端那个字"存成书签。

        存的是**章内字符位置**而不是滚动条像素：读者换个字号或拉一下窗口，像素偏移
        指向的就是别处的文字了，那种书签跳过去只会让人以为是 bug。

        漫画没有字符，所以存的是页码，`offset` 恒为 0——书签表按 book.id 索引，两种书
        共用一张表不会串味。
        """
        book = self.book
        if book is None:
            self.set_warning("先打开一本书再加书签")
            return
        index = book.clamp(self.chapter_index)
        if book.kind == KIND_COMIC:
            # 漫画：一页就是一个书签，没有"页内位置"这回事。
            offset = 0
            excerpt = ""
            label = f"第 {index + 1} 页"
        else:
            offset = self.reader.first_visible_char()
            body = book.chapter_body(index)
            excerpt = _excerpt(body, offset)
            ch = book.chapter(index)
            label = (ch.title if ch else "") or f"章节 {index + 1}"
        try:
            self.store.add(book.id, index, offset, label=label, excerpt=excerpt)
        except Exception as exc:  # noqa: BLE001 - 存书签失败不该打断阅读
            self.set_warning(f"书签没存上：{exc}")
            return
        self.reload_bookmarks()
        self.set_warning("已加书签")
        if self.toc_panel.isVisible():
            self.toc_panel.set_current(index)

    def _on_bookmark_chosen(self, bookmark_id: int) -> None:
        item = self._find_bookmark(bookmark_id)
        if item is None:
            self.set_warning("这个书签已经不在了")
            self.reload_bookmarks()
            return
        self.chapter_index = item.chapter
        # char_offset 而不是像素：见 add_bookmark() 的说明。
        self.show_chapter(char_offset=item.offset)
        self.persist_progress()
        self.close_toc()

    def _on_bookmark_deleted(self, bookmark_id: int) -> None:
        if self.store.remove(bookmark_id):
            self.set_warning("已删书签")
        else:
            self.set_warning("这个书签已经不在了")
        self.reload_bookmarks()

    def _find_bookmark(self, bookmark_id: int):
        book = self.book
        if book is None:
            return None
        for item in self.store.list_for(book.id):
            if item.id == bookmark_id:
                return item
        return None

    # ---- 自动滚动 --------------------------------------------------------

    def apply_autoscroll_settings(self) -> None:
        self.autoscroller.set_speed(self.settings.autoscroll_speed)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        """两职责，彼此独立：
        1) 边缘缩放路由：标题栏/底栏/正文 viewport 盖住了 FRAME 内侧那条 8px 热区，
           把落在窗口外环的左键按下/拖动/释放和悬停改光标交给主窗口（_route_edge_event）。
        2) 鼠标进出正文时暂停/恢复自动滚动——只装在 viewport() 上，不装整个窗口。
        """
        if self._route_edge_event(obj, event):
            return True
        if obj is getattr(self.reader, "viewport", lambda: None)():
            kind = event.type()
            if kind == QEvent.Enter and self.settings.autoscroll_pause_on_hover:
                if self.autoscroller.is_running():
                    self.autoscroller.set_paused(True)
            elif kind == QEvent.Leave and self.settings.autoscroll_pause_on_hover:
                self.autoscroller.set_paused(False)
        return super().eventFilter(obj, event)

    def _edge_surfaces(self):
        """The child widgets that cover the resize hot ring outside the 4px frame."""
        return (
            getattr(self.reader, "viewport", lambda: None)(),
            self.title_bar,
            self.status_bar,
        )

    def _route_edge_event(self, obj, event) -> bool:
        """Forward edge-ring mouse events on child surfaces to the window's resize logic.

        Returns True only when the event is consumed (an edge press or a move/release
        that belongs to a resize already in progress). Everything in the interior is
        returned False untouched, so title drag, reader clicks/selection and the bottom
        buttons keep their accepted behaviour.
        """
        if obj not in self._edge_surfaces():
            return False
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            edges = self.edges_at(self.mapFromGlobal(event_global_pos(event)))
            if edges:
                self.begin_resize(event, edges)
                return True
            return False
        if kind == QEvent.MouseMove:
            if self._resize_edges and event.buttons() & Qt.LeftButton:
                # The implicit press-grab keeps delivering moves to the surface where
                # the edge press started, even though resizing is owned by the window.
                self._handle_resize_move(event)
                return True
            if not (event.buttons() & Qt.LeftButton):
                # Hover affordance only: resize shape on the ring, the surface's own
                # cursor (I-beam / SizeAll / arrow) restored once the pointer leaves it.
                shape = self._cursor_for_edges(
                    self.edges_at(self.mapFromGlobal(event_global_pos(event)))
                )
                if shape is None:
                    obj.unsetCursor()
                else:
                    obj.setCursor(shape)
            return False
        if (
            kind == QEvent.MouseButtonRelease
            and event.button() == Qt.LeftButton
            and self._resize_edges
        ):
            self.end_resize(event)
            return True
        return False

    def toggle_autoscroll(self) -> None:
        # 自动滚动器构造时就吃死了注入的那根条（见 `_rebind_reader_views`）。没有条
        # 的时候不拦住的话：按钮亮成"暂停"、屏幕上什么都不动，等它悄悄滚到隐藏正文
        # 的底部，`_on_autoscroll_end` 会把漫画翻到下一页——用户没碰键盘，页码自己变了。
        # 热键也走这里，所以闸门设在这一层而不是按钮的可见性上。
        #
        # 用 `scrollbar()`（Reader 接口）判断而不是 `verticalScrollBar()`：前者才是
        # "这个 Reader 有没有可滚动的条"的答案——单页漫画返回 None，而阶段 4 的连续
        # 滚动模式返回一条真条，于是这里不用改就自动放行。
        if self.reader.scrollbar() is None:
            self.autoscroller.stop()
            self._sync_autoscroll_button()
            if self.book is not None and self.book.kind == KIND_COMIC:
                self.set_warning("漫画是整页显示的，用不着自动滚动")
            return
        self.autoscroller.toggle()
        self._sync_autoscroll_button()

    def _sync_autoscroll_button(self) -> None:
        running = self.autoscroller.is_running()
        self.autoscroll_button.setChecked(running)
        self.autoscroll_button.setText("⏸" if running else "⏵")
        # 这里必须带上 Shift。不带 Shift 的那个组合是中文 Windows 的输入法中英文切换
        # 开关——照着提示按下去只会切换输入法，自动滚动一动不动。（这条注释本身不写出
        # 那个组合，因为 test_no_tooltip_advertises_ctrl_space 直接读源码找它。）
        hint = "Ctrl+Shift+空格"
        self.autoscroll_button.setToolTip(
            f"暂停自动滚动（{hint}）" if running else f"自动滚动（{hint} 开始/暂停）"
        )

    def _on_autoscroll_end(self) -> None:
        """滚到章末了：接下一章继续，读完了就停下并说出来。"""
        book = self.book
        # 漫画连续滚动模式下"整本 = 一话"，没有下一话可接。自动滚到底就是读完了，
        # 和用户自己滚到底说同一句话（走 `say_boundary`，文案只有一处实现）。
        # 不拦的话 `step_chapter(1)` 会在末页上原地打转，而 `_resume_autoscroll`
        # 又会把滚动器重新启动到已经到底的条上——按钮亮着、屏幕不动。
        if book is not None and book.kind == KIND_COMIC:
            self.autoscroller.stop()
            self._sync_autoscroll_button()
            self._comic_reader.say_boundary(-1)
            return
        if book is None or self.chapter_index >= book.chapter_count - 1:
            self.autoscroller.stop()
            self._sync_autoscroll_button()
            self.set_warning("已经是最后一章了")
            return
        self.step_chapter(1)
        # 新章节是空的文档，必须等它排版完成才能继续滚，否则 maximum 还是 0，
        # 滚动器会立刻又判定"到底了"而空转。
        QTimer.singleShot(0, self._resume_autoscroll)

    def _resume_autoscroll(self) -> None:
        # 新章节如果整章装得下一屏（maximum == 0，比如很短的一章或是空章），再调
        # start() 会立刻又判定"到底了"，于是又跳下一章——连着一串短章节就会变成几百次
        # 事件循环的空转。这里停下来直说，比一章一章地闪过去有用。
        #
        # 走 `scrollbar()`（Reader 接口）而不是 `verticalScrollBar()`（Qt 的）：
        # 后者在漫画上被有意保留给 Qt 自己，前者才是"这个 Reader 有没有可滚动的条"
        # 的答案——单页漫画返回 None，于是自动滚动在漫画上自然停下。
        bar = self.reader.scrollbar()
        if bar is None or bar.maximum() <= 0:
            self.autoscroller.stop()
            self._sync_autoscroll_button()
            # 漫画整页显示，压根没有可滚动的高度——说"这一章"就露馅了（漫画没有章，
            # 只有页）。两种书的说法分开，用户才知道自己按的那个键为什么不生效。
            if self.book is not None and self.book.kind == KIND_COMIC:
                self.set_warning("漫画是整页显示的，用不着自动滚动")
            else:
                self.set_warning("这一章不用滚动就到头了")
            return
        self.autoscroller.reset_end_latch()
        self.autoscroller.start()
        self._sync_autoscroll_button()

    def _show_placeholder(self) -> None:
        # 占位文案走当前 Reader 的接口。文本模式给出阅读提示，漫画模式画一句
        # "还没有打开漫画"——两种空状态本来就该长得不一样。
        if self.book is not None and self.book.kind == KIND_COMIC:
            self.reader.show_book(None)
        else:
            self._text_reader.show_book(None)
            self._text_reader.set_chapter(
                "把 TXT 文件拖到窗口里，或者点左下角「打开」。\n\n"
                "· 滚轮上下滚动，滚到章末再滚一下就是下一章\n"
                "· 老板键把窗口整个藏起来，再按一次回来\n"
                "· 托盘图标右键可以调不透明度、打开设置、退出\n"
                "· 窗口位置和阅读进度会自动记住",
                offset=0,
            )
        self.title_label.setText("MiniReader")
        self.chapter_label.setText("")
        self.progress_label.setText("")
        self.status_label.setText("还没有打开文件")
        self._sync_comic_buttons()

    # ---- status ----------------------------------------------------------

    def _update_status(self) -> None:
        book = self.book
        if book is None:
            return
        # 首末章/首末页时禁用按钮，比让用户点了没反应要好——箭头灰掉本身就是"到头了"
        # 的提示。总数一律走 BookBase.position_count：文本是章数，漫画是页数。
        total = book.position_count
        self.prev_button.setEnabled(self.chapter_index > 0)
        self.next_button.setEnabled(self.chapter_index < total - 1)
        if self.toc_panel.isVisible():
            self.toc_panel.set_current(self.chapter_index)
        # 底栏右侧：位置和进度各自固定一格，滚动时两个数字不会互相挤动。
        # 位置文本由 Position.label() 拼——"第 x/y 章"和"第 x/y 页"因此只有一处
        # 实现，控件名（StatusLabel / ProgressLabel）和文本格式都和改动前一致。
        position = self.reader.current_position()
        self.chapter_label.setText(position.label)
        self.progress_label.setText(position.text())
        # 次要信息退到 status_label：文本是「章节标题 · 编码 · 体积」，
        # 漫画是「页码名 · 类型 · 体积 · 页尺寸」。
        details = self._status_details(position)
        self.status_label.setText(" · ".join(details))
        self.status_label.setToolTip(self._status_tip(book))

    def _status_details(self, position) -> list[str]:
        """底栏次要信息。两种书各自的字段不同，所以分开算。"""
        book = self.book
        if book.kind == KIND_COMIC:
            title = self._page_title()
            page = book.page(self.chapter_index)
            size = ""
            if page is not None and page.size_known:
                size = f"{page.width}×{page.height}"
            return [part for part in (title, size, self._comic_reader.zoom_tip(), book.source_label) if part]
        chapter = book.chapter(self.chapter_index)
        title = (chapter.title if chapter else "") or ""
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1] + "…"
        return [part for part in (title, book.encoding_label, human_size(book.size)) if part]

    def _status_tip(self, book) -> str:
        if book.kind == KIND_COMIC:
            return "\n".join(part for part in (book.title, book.describe(), *book.notes) if part)
        return book.split_summary()

    def _page_title(self) -> str:
        """The current page's file name, for the bottom bar. Truncated like a chapter."""
        page = self.book.page(self.chapter_index) if self.book is not None else None
        if page is None:
            return ""
        name = page.name or ""
        if len(name) > TITLE_MAX:
            name = name[: TITLE_MAX - 1] + "…"
        return name

    def set_warning(self, text: str) -> None:
        self.warn_label.setText(text or "")

    def set_boss_hint(self, available: bool, spec: str = "") -> None:
        """The hint is the user's only reassurance that the boss key exists."""
        self._boss_available = bool(available)
        self._boss_spec = spec
        self.update_boss_capability()

    def update_boss_capability(self, results: dict | None = None) -> None:
        """Say what the boss key can actually do, given hotkeys and the tray.

        When `results` is passed it is authoritative - including by its *absence* of an
        entry, because an unbound boss key must revoke a previously granted "available",
        not leave the stale one in place.
        """
        if results is not None:
            ok, message = results.get("boss_key", (False, "未绑定"))
            self._boss_available = bool(ok)
            if ok:
                self.hint_label.setText(f"{format_hotkey(self._boss_spec)} 隐身")
                self.hint_label.setToolTip("按这个组合键把窗口整个藏起来，再按一次回来")
            elif self.tray is not None:
                self.hint_label.setText("老板键不可用 · 托盘可找回")
                self.hint_label.setToolTip(f"{message}；仍可隐藏，因为托盘图标能叫回来")
            else:
                self.hint_label.setText("老板键不可用")
                self.hint_label.setToolTip(f"{message}；不会隐藏，免得窗口找不回来")
            return
        if self._boss_available:
            self.hint_label.setText(f"{format_hotkey(self._boss_spec)} 隐身")
            self.hint_label.setToolTip("按这个组合键把窗口整个藏起来，再按一次回来")
        elif self.tray is not None:
            self.hint_label.setText("托盘可找回")
            self.hint_label.setToolTip("全局热键不可用，但托盘图标能显示/隐藏窗口")
        else:
            self.hint_label.setText("老板键不可用")
            self.hint_label.setToolTip("全局热键注册失败，老板键已停用（免得窗口找不回来）")

    # ---- tray presentation ----------------------------------------------

    def _restore_opacity_if_hidden(self) -> None:
        """Undo one mouse-leave hide: restore the opacity saved before the drop.

        The saved value is whatever the window had BEFORE the hide (captured in
        `_apply_leave_hide`), never the LEAVE_HIDE_OPACITY floor itself - so this path
        does not care what the floor numeric value is.

        Order matters here: `_opacity_before_hide` must be cleared BEFORE
        `refresh_opacity()` runs, or refresh's "currently mouse-leaf-hidden" early return
        swallows the recomputation and the window stays transparent. Also only touches
        the temporary overlay - `settings.opacity` is never changed by this path.
        """
        if self._opacity_before_hide is not None:
            self.setWindowOpacity(self._opacity_before_hide)
            self._opacity_before_hide = None
            self._cancel_leave_hide()
            self.refresh_opacity()

    def present_window(self) -> None:
        """Bring the window back for the tray icon or a second app instance.

        This is the one coordination point between the tray and mouse-leave hiding:
        cancel the pending hide first, then undo the near-invisible overlay, then
        unminimize, raise and activate. A window hidden by the boss key and a window
        merely faded out by the mouse leaving both end up visible at the user's own
        opacity.
        """
        self._cancel_leave_hide()
        self._restore_opacity_if_hidden()
        self.boss_hidden = False
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        if self.tray is not None:
            self.tray.set_hidden_state(False)
        self.refresh_opacity()

    # ---- boss key --------------------------------------------------------

    def can_hide(self) -> bool:
        """Hide only when something can bring the window back.

        Either the global hotkey is live, or there is a tray icon to click. Without
        both, a hidden window is gone for good, so the action is refused.
        """
        return bool(self._boss_available or self.tray is not None)

    def toggle_boss(self) -> None:
        if self.boss_hidden:
            self.show_from_boss()
        elif self.can_hide():
            self.hide_to_boss()
        else:
            self.set_warning("热键和托盘都不可用，已阻止隐藏（否则窗口找不回来）")

    def hide_to_boss(self) -> None:
        self.boss_hidden = True
        # 待执行的降透明度要撤销：窗口马上就 hide() 了，再降一次 0 只会留下脏状态。
        self._cancel_leave_hide()
        # Deliberately never persisted: a window that starts hidden on next launch is
        # unrecoverable if the tray is disabled and the hotkey fails.
        self.hide()
        if self.tray is not None:
            self.tray.set_hidden_state(True)

    def show_from_boss(self) -> None:
        self.boss_hidden = False
        # hide() 期间如果漏进来一个 leaveEvent，计时器会在窗口重新出现后到点、把它
        # 又藏起来；显示之前先撤掉待执行的隐身。
        self._cancel_leave_hide()
        self.show()
        self.raise_()
        self.activateWindow()
        if self.tray is not None:
            self.tray.set_hidden_state(False)
        # 老板键是真隐藏，不是移出隐身：清掉移出隐身的临时值，免得把
        # LEAVE_HIDE_OPACITY 当成"用户设置的值"恢复回来。
        self._opacity_before_hide = None
        self.refresh_opacity()

    # ---- drag and drop ---------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._first_supported(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._first_supported(event.mimeData())
        if path:
            self.load_path(path)
            event.acceptProposedAction()
            return
        # 拖进来一个我们不认识的文件：给一句话，而不是让拖放悄无声息地失败。
        # dragEnterEvent 已经 ignore 过，所以系统光标本来就显示"禁止"，这里只是
        # 补上原因——用户看到红圈时最想知道的就是"为什么"。
        note = self._first_drop_note(event.mimeData())
        if note:
            self.set_warning(note)
        event.ignore()

    @staticmethod
    def _local_urls(mime) -> list[str]:
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    @classmethod
    def _first_supported(cls, mime) -> str | None:
        """拖放载荷里第一个我们能打开的文件（或文件夹）。

        判据是格式表，不再硬编码 `.txt`。文件夹单独一条分支：格式表按扩展名判断，
        而文件夹没有扩展名，问它只会得到"不支持的文件类型"——所以文件夹问的是
        "里面有没有图片"（图片文件夹是一本漫画）。表里列着但还没实现的格式
        （阶段 3 的 PDF 等）返回 None，它们会走 `_first_drop_note()` 拿到
        "要等阶段 N"的说明，而不是被当成乱码文件。
        """
        for local in cls._local_urls(mime):
            if os.path.isdir(local):
                if folder_has_images(local):
                    return local
                continue
            if not support_note(local):
                return local
        return None

    @classmethod
    def _first_drop_note(cls, mime) -> str:
        """Why nothing in the payload could be opened.

        只在整批都打不开时才被调用，所以按载荷顺序报第一个原因就够了：用户拖进来的
        通常就是一个文件，报"PDF 要等阶段 3"比报"没有能打开的文件"有用得多。
        """
        for local in cls._local_urls(mime):
            if os.path.isdir(local):
                if not folder_has_images(local):
                    return "这个文件夹里没有能看的图片"
                continue
            note = support_note(local)
            if note:
                return note
        return "拖进来的东西里没有能打开的文件"

    #: 旧名字，阶段 1 的测试直接调它；语义与 `_first_supported` 相同。
    _first_txt = _first_supported

    # ---- persistence -----------------------------------------------------

    def _on_scrolled(self) -> None:
        # 漫画的"滚动"就是翻页，位置真的变了：窗口这一层的 `chapter_index` 必须跟着
        # 走，否则底栏还停在旧页码、首末页按钮的灰/亮还是旧的，900ms 后写进
        # progress.json 的也是旧页——实测滚轮翻到第 2 页后 `current_progress()` 仍报
        # `page=0`，重开又从第 1 页开始。
        #
        # 只有漫画需要同步：文本的 `scrolled` 是**章内像素滚动**，章号没变，底栏那格
        # 本来就只在换章时刷新（阶段 1 验收过的行为），那条路一个字节都不动。
        if self.book is not None and self.book.kind == KIND_COMIC:
            self.chapter_index = self.book.clamp(self._comic_reader.index)
            self._update_status()
        self._progress_timer.start()

    def current_progress(self) -> Progress:
        """当前位置，文本用像素偏移、漫画用页码。

        `chapter` 两种书都存（它就是"第几个位置"），`page` 只对漫画有意义。
        这样旧 progress.json 原样读得进来，新文件被旧版本读到时也只是忽略多出来的键。

        `pdf_kind` 要**原样带过去**。这个对象是 `persist_progress()` 写进 store 的
        全部内容，而 `set_book()` 每次都会调它——如果这里新建一个干净的 Progress，
        用户刚在右键菜单里做的「按漫画打开」会在同一个调用栈里被自己的进度保存抹掉，
        表现就是"选了也没用，下次还问"。
        """
        if self.book is not None and self.book.kind == KIND_COMIC:
            return Progress(
                chapter=self.chapter_index,
                offset=0,
                page=self.chapter_index,
                mode=self._comic_reader.mode,
                pdf_kind=self._remembered_pdf_kind(),
            )
        return Progress(
            chapter=self.chapter_index,
            offset=self.reader.current_offset(),
            pdf_kind=self._remembered_pdf_kind(),
        )

    def _remembered_pdf_kind(self) -> str:
        """This book's stored "read it as ..." answer, or "" when there is none.

        Only dual-identity files can have one, so everything else short-circuits
        before touching the store - `persist_progress()` runs on every page turn
        and must not do a dictionary lookup for a TXT.
        """
        if self.book is None or _ext_of(self.book.path) not in DUAL_IDENTITY_EXTS:
            return ""
        return self.progress.get(self.book.id).pdf_kind

    def persist_progress(self) -> None:
        """Remember where the reader is, keyed by book id.

        Also records the book as "the last one", and saves the settings file when that
        changes. It has to save: `last_book` is read back from disk on the next launch,
        and until this was fixed it only ever reached disk as a side effect of the
        window being moved or resized - so a reader who opened a book and read it
        without touching the window frame was not offered it again next time.
        """
        if self.book is None:
            return
        # An explicit save supersedes the pending debounced one.
        self._progress_timer.stop()
        self.progress.put(self.book.id, self.current_progress())
        self.progress.save()
        if self.settings.last_book != self.book.path:
            self.settings.last_book = self.book.path
            self.settings.save(self._settings_path)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 常驻托盘的应用：✕ 不是退出。只要还有办法把窗口叫回来（托盘图标或老板键），
        # 关窗口就只是藏起来，进程继续活着；真正的退出只有托盘菜单的"退出"
        # （quit_app 置 _really_quit）。什么叫都没有时（比如无托盘的 offscreen/最小化
        # 环境）才真关——否则会留下一个看不见也找不回的进程。
        if not self._really_quit and self.can_hide():
            event.ignore()
            self._cancel_leave_hide()
            if not self.boss_hidden:
                self.hide_to_boss()
            return
        self.persist_progress()
        self.save_geometry()
        # 关窗口必须停掉自动滚动定时器：它每 40ms 动一次滚动条，留着就是个关了窗还在跑的
        # 循环，还会在解释器退出时炸出 "QScrollBar already deleted" 之类的错误。
        self.autoscroller.stop()
        self.store.close()
        if self.hotkeys is not None:
            self.hotkeys.unregister_all()
        if self.tray is not None:
            self.tray.shutdown()
        super().closeEvent(event)
        if self._quit_on_close:
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)
