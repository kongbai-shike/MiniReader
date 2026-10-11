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
from ..core.model import Book, BookError, Progress, human_size
from ..hotkeys import format_hotkey, register_all
from ..qt import (
    QApplication,
    QBrush,
    QColor,
    QCursor,
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
    QRect,
    QRectF,
    QSizePolicy,
    QSlider,
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
    alpha_to_percent,
    data_dir,
    default_geometry,
    percent_to_alpha,
    validate_geometry,
)
from .autoscroll import AutoScroller
from .reader import ReaderView
from .settings_dialog import SettingsDialog
from .theme import PALETTE
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

READER_MENU_QSS = """
QMenu {
    background: #2b2b2b; color: #bdbdc0;
    border: 1px solid #3a3a3a; border-radius: 8px;
    padding: 6px; margin: 0; font-size: 13px;
}
QMenu::item {
    background: transparent; color: #bdbdc0;
    border-radius: 6px; margin: 0; padding: 0 18px;
    min-height: 40px;
}
QMenu::item:selected { background: #3a3a3a; color: #bdbdc0; }
QMenu::item:disabled { background: transparent; color: #6f6f72; }
QMenu::separator { height: 1px; background: #3a3a3a; margin: 4px 8px; }
QMenu::icon { left: 0; width: 0; }
"""

HOTKEY_LABELS = {name: label for name, _field, label in HOTKEY_FIELDS}


def _qss() -> str:
    p = PALETTE
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
    /* 底栏两端的翻页胶囊：QQ 实测 87×29、圆角 14px、1px #6e6e6f 描边、文字 #a3a3a3。
       ID 选择器压过上面的通用钮规则（边框/字号/内边距都以这里为准），文字 13px 满足
       可读性下限；:hover 只提亮底色，不动文字色（和顶栏悬停块同一条原则）。
       注意 QSS 的 min-width 是**内容盒**：73 + 左右 padding 12 + 边框 2 = 87px 整宽，
       正好等于规格命中区宽度，不是随手写的 87（那样实际会变成 109px 把底栏顶爆）。 */
    QToolButton#PrevPageButton, QToolButton#NextPageButton {{
        color: #a3a3a3; background: transparent;
        border: 1px solid #6e6e6f; border-radius: 14px;
        min-width: 73px; min-height: 27px; padding: 0 6px; font-size: 13px;
    }}
    QToolButton#PrevPageButton:hover, QToolButton#NextPageButton:hover {{
        background: {p['hover']}; border: 1px solid #6e6e6f; border-radius: 14px;
    }}
    QToolButton#PrevPageButton:disabled, QToolButton#NextPageButton:disabled {{
        color: {p['faint']}; border: 1px solid {p['border']}; border-radius: 14px;
    }}
    #OpacityPopup {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 8px; }}
    #Muted {{ color: {p['muted']}; font-size: 11px; }}
    """


# QToolTip 是顶层窗口，只认 QApplication 级样式表——挂在窗口样式表上不会生效。
# 对齐 QQ 实测：底 #262626、字 #9b9b9b、圆角 3px、padding 4×8。1px 同色描边是为了
# 强制 Qt 走 QSS 渲染路径（没有这个 border，Windows 上会回落到系统原生气泡）。
TOOLTIP_QSS = """
/* MiniReaderTooltip */
QToolTip {
    background: #262626; color: #9b9b9b;
    border: 1px solid #262626; border-radius: 3px; padding: 4px 8px;
}
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

        self._build_ui()
        # 滚动器只拿正文的滚动条，不拿 ReaderView 本身：这样它和滚轮的缓动动画
        # （_animate_to / _cancel_anim）没有任何耦合，谁动滚动条谁负责，不会互相取消。
        self.autoscroller = AutoScroller(self.reader.verticalScrollBar(), parent=self)
        self.autoscroller.reachedEnd.connect(self._on_autoscroll_end)
        self.autoscroller.stopped.connect(self._sync_autoscroll_button)
        self.apply_autoscroll_settings()
        # 鼠标停在正文上时暂停：读者把鼠标移回窗口通常就是想手动滚一下或者看一眼，
        # 此时文字还在自己走会很难受。
        self.reader.viewport().installEventFilter(self)
        # 正文左/右缘 8px 被 viewport 盖着；开追踪后 eventFilter 才能在不按键悬停时
        # 把那一条上的光标换成左右缩放形状（见 _route_edge_event）。
        self.reader.viewport().setMouseTracking(True)
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

        self.reader = ReaderView(self.body)
        self.reader.chapterRequested.connect(self.step_chapter)
        self.reader.scrolled.connect(self._on_scrolled)
        # 阶段 3：正文右键菜单。坐标原样透传——菜单必须出现在右键点。
        self.reader.contextMenuRequested.connect(self.show_reader_menu)
        body_layout.addWidget(self.reader, 1)
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

        # 左：翻页胶囊（映射现有 step_chapter 逻辑——当前一"页"就是一章；正文里的
        # 滚轮翻页/右键菜单是阶段 3 的事）。87×29、圆角 14、#6e6e6f 描边、#a3a3a3 字。
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
        self._show_placeholder()

    def _apply_theme(self) -> None:
        # 只给标题栏/状态栏/按钮上色，绝不涉及窗口背景——背景归 paintEvent。
        self.setStyleSheet(_qss())
        # QToolTip 是顶层窗口，只吃 QApplication 级样式表。幂等：marker 已在就不重复挂，
        # 多个窗口/多次 _apply_theme 不会把 QSS 越拼越长。
        app = QApplication.instance()
        if app is not None and "MiniReaderTooltip" not in app.styleSheet():
            app.setStyleSheet(app.styleSheet() + TOOLTIP_QSS)

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
            edges |= int(Qt.LeftEdge)
        elif pos.x() >= w - m:
            edges |= int(Qt.RightEdge)
        if pos.y() <= m:
            edges |= int(Qt.TopEdge)
        elif pos.y() >= h - m:
            edges |= int(Qt.BottomEdge)
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
        if edges & int(Qt.LeftEdge):
            # Right edge fixed: the left edge follows the pointer, but cannot cross
            # right-min_w (which is exactly the minimum-width floor).
            left = min(start.x() + dx, right - min_w)
        elif edges & int(Qt.RightEdge):
            # Left edge fixed; the right edge follows the pointer.
            right = max(right + dx, left + min_w)
        if edges & int(Qt.TopEdge):
            top = min(start.y() + dy, bottom - min_h)
        elif edges & int(Qt.BottomEdge):
            bottom = max(bottom + dy, top + min_h)
        return QRect(left, top, right - left, bottom - top)

    @staticmethod
    def _cursor_for_edges(edges: int):
        """The resize cursor for an edges bitmask, or None for the interior."""
        left = bool(edges & int(Qt.LeftEdge))
        right = bool(edges & int(Qt.RightEdge))
        top = bool(edges & int(Qt.TopEdge))
        bottom = bool(edges & int(Qt.BottomEdge))
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
        )
        if self.settings.hide_from_taskbar:
            # A dialog parented to a Qt.Tool window still gets its own taskbar button on
            # Windows, which would defeat 隐藏任务栏 for as long as the dialog is open.
            # Setting the flag here is safe: the dialog has no HWND worth preserving yet.
            dialog.setWindowFlag(Qt.Tool, True)
        self._dialog_open = True
        self.refresh_opacity()
        try:
            accepted = exec_dialog(dialog)
        finally:
            self._dialog_open = False
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
        dialog.deleteLater()
        self.refresh_opacity()

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
        # 模式切换要重建原生窗口，放最前面：后面的 opacity/background 才会作用在正确
        # 的窗口上。
        self._sync_translucent_mode()
        self.apply_opacity(self.settings.target_opacity(active=self.isActiveWindow() or self._dialog_open))
        self.apply_background()
        self.apply_content_alpha()
        self.reader.apply_style(self.settings)
        self.apply_autoscroll_settings()
        self.apply_disguise()
        self.sync_tray()
        self.reload_hotkeys()
        self._sync_leave_hide_button()
        self.refresh_opacity()

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
            self, "打开 TXT", os.path.expanduser("~"), "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if path:
            self.load_path(path)

    def load_path(self, path: str, *, chapter: int | None = None, offset: int | None = None) -> bool:
        """Open a TXT file, resuming where this book was last left off.

        The resume lookup lives here rather than at the call sites so that every way of
        opening a book - the file dialog, drag-and-drop, the command line, the
        remembered last book - behaves the same. Passing an explicit chapter or offset
        overrides it.
        """
        try:
            book = Book.from_path(path)
        except BookError as exc:
            self.set_warning(f"打不开：{exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the window
            self.set_warning(f"打不开：{type(exc).__name__}: {exc}")
            return False
        if chapter is None and offset is None:
            saved = self.progress.get(book.id)
            if saved.chapter or saved.offset:
                chapter, offset = saved.chapter, saved.offset
        self.set_book(book, chapter=chapter, offset=offset)
        return True

    def set_book(self, book: Book, *, chapter: int | None = None, offset: int | None = None) -> None:
        self.book = book
        self.chapter_index = book.clamp(chapter if chapter is not None else 0)
        self.show_chapter(offset=offset or 0)
        self.title_label.setText(book.title)
        self.title_label.setToolTip(f"{book.title}\n{book.describe()}")
        self.set_warning("编码可能不准，可在设置里手动指定" if not book.confident else "")
        self.library.record(book.to_record())
        self.library.save()
        self.persist_progress()
        # 换书就要换目录和书签：面板可能正开着，而它上面还挂着上一本书的章节。
        self._refresh_toc()

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
        self.reader.set_chapter(
            self.book.chapter_body(self.chapter_index),
            offset=offset,
            at_end=at_end,
            char_offset=char_offset,
        )
        self._update_status()

    def step_chapter(self, delta: int) -> None:
        if self.book is None:
            return
        target = self.book.clamp(self.chapter_index + delta)
        if target == self.chapter_index:
            return
        self.chapter_index = target
        # Going back lands at the END of the previous chapter: the reader was moving
        # forward, so the far edge is the interesting one.
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
        menu.setStyleSheet(READER_MENU_QSS)

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
        """
        book = self.book
        if book is None:
            return
        try:
            reloaded = Book.from_path(book.path)
        except BookError as exc:
            self.set_warning(f"重新加载失败：{exc}")
            return
        anchor = self.reader.first_visible_char() if self.reader.char_count() else None
        self.book = reloaded
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
        """
        book = self.book
        if book is None:
            self.set_warning("先打开一本书再加书签")
            return
        chapter = book.clamp(self.chapter_index)
        offset = self.reader.first_visible_char()
        body = book.chapter_body(chapter)
        excerpt = _excerpt(body, offset)
        ch = book.chapter(chapter)
        label = (ch.title if ch else "") or f"章节 {chapter + 1}"
        try:
            self.store.add(book.id, chapter, offset, label=label, excerpt=excerpt)
        except Exception as exc:  # noqa: BLE001 - 存书签失败不该打断阅读
            self.set_warning(f"书签没存上：{exc}")
            return
        self.reload_bookmarks()
        self.set_warning("已加书签")
        if self.toc_panel.isVisible():
            self.toc_panel.set_current(chapter)

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
        if self.reader.verticalScrollBar().maximum() <= 0:
            self.autoscroller.stop()
            self._sync_autoscroll_button()
            self.set_warning("这一章不用滚动就到头了")
            return
        self.autoscroller.reset_end_latch()
        self.autoscroller.start()
        self._sync_autoscroll_button()

    def _show_placeholder(self) -> None:
        self.reader.set_chapter(
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

    # ---- status ----------------------------------------------------------

    def _update_status(self) -> None:
        book = self.book
        if book is None:
            return
        # 首末章时禁用按钮，比让用户点了没反应要好——箭头灰掉本身就是"到头了"的提示。
        self.prev_button.setEnabled(self.chapter_index > 0)
        self.next_button.setEnabled(self.chapter_index < book.chapter_count - 1)
        if self.toc_panel.isVisible():
            self.toc_panel.set_current(self.chapter_index)
        percent = (self.chapter_index + 1) * 100.0 / book.chapter_count if book.chapter_count else 0.0
        chapter = book.chapter(self.chapter_index)
        title = (chapter.title if chapter else "") or ""
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1] + "…"
        # 底栏右侧：章节号和进度各自固定一格，滚动时两个数字不会互相挤动。
        # 进度固定两位小数（QQ 实测格式「6.54%」），窄底栏里宽度也恒定不跳。
        self.chapter_label.setText(f"第 {self.chapter_index + 1}/{book.chapter_count} 章")
        self.progress_label.setText(f"{percent:.2f}%")
        # 次要信息退到 status_label：书名 · 编码 · 体积。
        details = [part for part in (title, book.encoding_label, human_size(book.size)) if part]
        self.status_label.setText(" · ".join(details))
        self.status_label.setToolTip(book.split_summary())

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
        if self._first_txt(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._first_txt(event.mimeData())
        if path:
            self.load_path(path)
            event.acceptProposedAction()
            return
        event.ignore()

    @staticmethod
    def _first_txt(mime) -> str | None:
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                local = url.toLocalFile()
                if local.lower().endswith(".txt"):
                    return local
        return None

    # ---- persistence -----------------------------------------------------

    def _on_scrolled(self) -> None:
        self._progress_timer.start()

    def current_progress(self) -> Progress:
        return Progress(chapter=self.chapter_index, offset=self.reader.current_offset())

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
