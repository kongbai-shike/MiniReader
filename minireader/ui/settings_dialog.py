"""The settings dialog: the seven features, in one place, with live preview.

Every control here maps to a field in `storage.Settings`, and `apply_to()` is the only
writer, so the dialog cannot invent state the rest of the app does not understand.

Three deliberate choices:

* **Two settings need a restart, and the dialog says so.** `always_on_top` and
  `hide_from_taskbar` are window *flags*. Changing flags at runtime destroys and
  recreates the native window on Windows, which orphans the HWND that the global
  hotkeys are registered against (see `ui/window.py`). Rather than pretend, the dialog
  labels them and shows a restart note when they change.
* **Opacity previews live.** `on_preview` is called while the slider moves, so the
  ClearType softening that partial opacity causes on Windows is visible before saving
  rather than discovered later.
* **Focus is never removed.** Keyboard users need to see where they are; the stylesheet
  draws a lighter border (#5a5a5a) on the focused control instead of the platform default.

Accessibility note on the palette: every text colour used here is checked by
`tests/test_theme.py` at 4.5:1 or better against the surface it is drawn on.
"""

from __future__ import annotations

from dataclasses import replace

from ..hotkeys import HotkeyError, format_hotkey, parse_hotkey, spec_from_qt
from ..qt import (
    QApplication,
    QCheckBox,
    QColor,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFont,
    QFontComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    Qt,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QtWidgets,
    Signal,
)
from ..storage import HOTKEY_FIELDS, Settings, alpha_to_percent, percent_to_alpha
from .theme import AA_NORMAL, PALETTE, contrast_ratio

# 一键排版预设。名字是给读者看的，值必须都是 clamp() 允许范围内的合法值——预设本身
# 就是个方便入口，不该绕过校验，所以 apply_to() 最后照样调 s.clamp()。
READ_PRESETS: tuple[tuple[str, dict], ...] = (
    ("默认（深色）", {"font_family": "", "font_size": 25, "line_height": 210, "paragraph_indent": 2, "margin": 16, "bg": "#1b1b1b", "fg": "#d8d8da"}),
    ("护眼（米黄）", {"font_family": "", "font_size": 24, "line_height": 200, "paragraph_indent": 2, "margin": 24, "bg": "#f5efe0", "fg": "#3a352c"}),
    ("夜间（纯黑）", {"font_family": "", "font_size": 24, "line_height": 200, "paragraph_indent": 2, "margin": 20, "bg": "#000000", "fg": "#9aa0ad"}),
    ("大字（松散）", {"font_family": "", "font_size": 30, "line_height": 220, "paragraph_indent": 2, "margin": 28, "bg": "#1b1b1b", "fg": "#d8d8da"}),
)


# 左侧导航条目；顺序必须与 _build() 里构建 4 个分组页的顺序一致（第 i 条 ↔ 第 i 页）。
# 参考 Telegram Desktop 设置：左导航 + 右侧单页，一次只看一个分组。
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("正文排版", "字体、字号、行距、缩进、页边距与正文配色"),
    ("外观", "窗口与正文的不透明度，以及失去焦点时自动变淡"),
    ("窗口", "边缘吸附、移出隐身、托盘、置顶与任务栏按钮"),
    ("全局快捷键", "在任何程序里都能生效的组合键"),
)

# 主按钮（确定）的橙：QQ 迷你窗的强调色。深色文字压橙色底的对比度约 7:1，白字只有
# 3:1，所以主按钮用深字。
_PRIMARY = "#c8891a"
_PRIMARY_HOVER = "#dda032"
_PRIMARY_PRESSED = "#a96f12"


def _stylesheet() -> str:
    """对话框整体美化规格（对齐 QQ 迷你窗的深色层级）：

    * 三层表面：对话框底 panel（#1b1b1b）< 分组框 card（#212121）< 控件 field（#2d2d2d）。
    * 字号下限：说明小字 12px，标签/按钮/输入 13px，分组标题 14px 加粗——没有更小的字。
    * 禁用态不靠透明度假装：滑块手柄/已勾选指示换成实色灰，文字用 #6f6f72，仍然可读。
    """
    p = PALETTE
    return f"""
    QDialog {{ background: {p['panel']}; }}
    QLabel {{ color: {p['fg']}; font-size: 13px; }}
    QLabel#Muted {{ color: {p['muted']}; font-size: 12px; }}
    QLabel#Warn {{ color: {p['warn']}; font-size: 12px; }}
    QLabel:disabled {{ color: #6f6f72; }}
    QGroupBox {{
        background: {p['card']}; color: {p['fg']};
        font-size: 14px; font-weight: bold;
        border: 1px solid {p['border']}; border-radius: 8px;
        margin-top: 14px; padding: 12px;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}
    QLineEdit, QSpinBox, QComboBox {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        border-radius: 4px; padding: 4px 8px; min-height: 28px; font-size: 13px;
        selection-background-color: {p['accent']};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid #5a5a5a; }}
    QLineEdit#HotkeyBad {{ border: 1px solid {p['warn']}; }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background: {p['border']}; width: 18px; border: none;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #4a4a4a; }}
    QComboBox::drop-down {{ background: {p['border']}; width: 22px; border: none; }}
    QComboBox QAbstractItemView {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        selection-background-color: {p['border']}; font-size: 13px;
    }}
    QCheckBox {{ color: {p['fg']}; font-size: 13px; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; background: {p['field']};
        border: 1px solid {p['border']}; border-radius: 3px;
    }}
    QCheckBox::indicator:hover {{ border-color: #5a5a5a; }}
    QCheckBox::indicator:checked {{ background: {_PRIMARY}; border-color: {_PRIMARY}; }}
    QCheckBox::indicator:disabled {{ background: #262626; border-color: #333333; }}
    QCheckBox:disabled {{ color: #6f6f72; }}
    QSlider::groove:horizontal {{
        height: 4px; background: {p['border']}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {p['fg']}; width: 14px; margin: -5px 0; border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{ background: #ffffff; }}
    QSlider::groove:horizontal:disabled {{ background: #2a2a2a; }}
    QSlider::sub-page:horizontal:disabled {{ background: #3d4a63; }}
    QSlider::handle:horizontal:disabled {{ background: #6a6a6a; }}
    QToolButton {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        border-radius: 4px; padding: 5px 12px; font-size: 13px; min-height: 28px;
    }}
    QToolButton:hover {{ background: {p['border']}; }}
    QToolButton:pressed {{ background: #454545; }}
    QToolButton:focus {{ border: 1px solid #5a5a5a; }}
    QPushButton {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        border-radius: 4px; padding: 6px 16px; font-size: 13px;
        min-width: 80px; min-height: 32px;
    }}
    QPushButton:hover {{ background: {p['border']}; }}
    QPushButton:pressed {{ background: #454545; }}
    QPushButton:focus {{ border: 1px solid #5a5a5a; }}
    QPushButton:disabled {{ background: #262626; color: #6f6f72; }}
    QPushButton#PrimaryButton {{
        background: {_PRIMARY}; color: #1b1b1b; border: none; font-weight: bold;
    }}
    QPushButton#PrimaryButton:hover {{ background: {_PRIMARY_HOVER}; }}
    QPushButton#PrimaryButton:pressed {{ background: {_PRIMARY_PRESSED}; }}
    QPushButton#PrimaryButton:disabled {{ background: #6a5626; color: #3a3a3a; }}
    QMenu {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {p['border']}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 2px 2px 0; }}
    QScrollBar::handle:vertical {{
        background: {p['border']}; border-radius: 4px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: #4a4a4a; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0; border: none; background: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

    /* ---- Telegram 式左导航 --------------------------------------------
       导航列用 card 底，右侧内容保持 panel 底；选中项是 field 底色的圆角
       pill + 左侧 3px accent 竖条。文字始终用 fg（悬停底=hover/field，均已过
       theme 的 4.5:1 审计），不引入任何新文字色。 */
    QListWidget#SettingsNav {{
        background: {p['card']}; color: {p['fg']}; font-size: 13px;
        border: none; border-right: 1px solid {p['border']}; outline: none;
    }}
    QListWidget#SettingsNav::item {{
        color: {p['fg']}; background: transparent;
        border: none; border-left: 3px solid transparent;
        border-radius: 6px; margin: 2px 8px; padding: 9px 8px 9px 9px;
    }}
    QListWidget#SettingsNav::item:hover {{ background: {p['hover']}; }}
    QListWidget#SettingsNav::item:selected {{
        background: {p['field']}; border-left: 3px solid {p['accent']};
    }}
    QListWidget#SettingsNav::item:selected:!active {{
        /* 键盘焦点在右侧控件上时选中态也不能丢。 */
        background: {p['field']}; border-left: 3px solid {p['accent']};
    }}
    QLabel#PageTitle {{ color: {p['fg']}; font-size: 15px; font-weight: bold; }}
    QStackedWidget {{ background: transparent; }}
    QWidget#SettingsFooter {{
        background: {p['panel']}; border-top: 1px solid {p['border']};
    }}
    """


class HotkeyEdit(QLineEdit):
    """Captures a single key chord and shows it as a MiniReader spec.

    Validating on capture rather than on save is the point: the user finds out
    immediately that a bare letter is not allowed (`RegisterHotKey` would steal it from
    every other program) instead of at the moment they press 确定.
    """

    # Emitted on every capture or clear. `editingFinished` is not enough: this widget is
    # read-only, and whether that signal fires on focus-out is not something to rely on.
    changed = Signal()

    def __init__(self, spec: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("点击这里再按组合键（Backspace 清除）")
        self._problem = ""
        self.set_spec(spec, notify=False)

    def spec(self) -> str:
        return self.text().strip() if not self._problem else ""

    def problem(self) -> str:
        return self._problem

    def set_spec(self, spec: str, *, notify: bool = True) -> None:
        spec = (spec or "").strip()
        self._problem = ""
        self.setObjectName("")
        self.setToolTip("")
        if spec:
            try:
                parse_hotkey(spec)
            except HotkeyError as exc:
                self._problem = str(exc)
                self.setToolTip(str(exc))
                self.setObjectName("HotkeyBad")
            super().setText(format_hotkey(spec))
        else:
            super().setText("")
        self._restyle()
        if notify:
            self.changed.emit()

    def _restyle(self) -> None:
        # Qt only re-evaluates the stylesheet when it is told the object name changed.
        self.style().unpolish(self)
        self.style().polish(self)

    def _reject(self, problem: str) -> None:
        self._problem = problem
        self.setObjectName("HotkeyBad")
        self.setToolTip(problem)
        self._restyle()
        self.changed.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        # Esc clears the binding instead of closing the dialog: losing the whole form
        # because you wanted to unbind one chord would be infuriating.
        if key in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Delete):
            self.set_spec("")
            event.accept()
            return
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return  # a modifier on its own is not a chord yet
        spec = spec_from_qt(key, event.modifiers())
        if spec is None:
            self._reject("这个按键不能用作全局热键")
            event.accept()
            return
        self.set_spec(spec)
        event.accept()


class SettingsDialog(QDialog):
    """Modal-ish editor for `Settings`. `apply_to()` is the only writer."""

    # 记住本进程内上次查看的页（不写进 Settings.json：那是偏好持久化，页签只是
    # 会话状态）。成本为零，跨"关掉再打开设置"保留，重启应用自然归零。
    _last_nav_row = 0

    def __init__(
        self,
        settings: Settings,
        parent=None,
        on_preview=None,
        on_background=None,
        on_content=None,
        on_leave_hide=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("MiniReader 设置")
        self.setStyleSheet(_stylesheet())
        self._settings = settings
        self._on_preview = on_preview
        # 拖动 alpha 滑块的专用回调。它和 on_preview 分开，是因为 alpha 只影响窗口
        # 底色：让它走 on_preview 会把整窗重排（字号锚点、重折行）也带上，手感会飘。
        # 窗口那端把它接到一个只做 setAlpha + update() 的方法上。
        self._on_background = on_background
        # 内容透明度同理，但它落到正文控件的 QGraphicsOpacityEffect 上：背景一点不动。
        # 兼容模式下这根滑块是禁用的，窗口那端也会忽略这个回调。
        self._on_content = on_content
        # "鼠标移出隐身"复选框一动就调它，把主窗顶栏 👁 的按下状态跟过去。这里只同步
        # 按钮外观，真正的设置仍然等按"确定"才由 apply_to() 写。
        self._on_leave_hide = on_leave_hide
        # 只有用户**主动**挑过字体才把族名存进配置，否则 ""（跟随系统）会被 Qt 报告的
        # 具体族名永久顶掉。_build() 里设置初始字体时这个标志必须已经是假。
        self._font_touched = False
        self._original_restart_flags = (
            bool(settings.always_on_top),
            bool(settings.hide_from_taskbar),
        )
        self._build(settings)
        self._fit_to_content_and_screen()

    def _fit_to_content_and_screen(self) -> None:
        """Size to the CURRENT page's content, and shrink only if the screen cannot
        show it. Each page scrolls independently.

        The same two measured traps as the old single-scroll layout still apply:

        1. A scroll area's sizeHint is small, so adjustSize() alone opens the dialog
           short and scrolls even on a 1080p screen with room to spare. So the current
           page's area is first allowed the full natural height of its content,
           measured, then released.
        2. `resize()` does not stick: the layout re-activates on show and grows the
           dialog back. The working lever is the scroll area's own *maximum* height -
           it changes what the layout actually asks for - plus a hard maximum on the
           dialog as a backstop.

        The buttons live in the footer, outside every page, so they stay reachable.
        """
        area = self._scroll
        body = area.widget()

        # Reserve the width of the WIDEST page before anything measures.
        # QStackedLayout does not let hidden pages bid for size, so without
        # this the dialog opens narrow and jumps wider the first time a wide
        # page is visited. Hints are only trustworthy after polish, which the
        # first fit (called from _build) now triggers for every group.
        widest = 0
        for other in self._page_areas:
            other.widget().ensurePolished()
            width_needed = other.widget().minimumSizeHint().width()
            widest = max(widest, width_needed)
            # Never shrink: keeps the value stable if a hint measures smaller.
            other.setMinimumWidth(max(other.minimumWidth(), width_needed))
        self.setMinimumWidth(max(560, 150 + 40 + widest))

        area.setMinimumHeight(body.sizeHint().height())
        self.adjustSize()
        # Everything that is not the current page's viewport: the nav rail, the page
        # header, the footer and the frame. Measured rather than assumed.
        chrome = self.height() - area.height()
        area.setMinimumHeight(0)
        # Hidden pages must not bid for height while the current one is measured.
        for other in self._page_areas:
            other.setMinimumHeight(0)

        available = None
        screen = QApplication.desktop() if hasattr(QApplication, "desktop") else None
        if screen is not None:
            try:
                available = screen.availableGeometry()
            except Exception:
                available = None
        if available is None:
            holder = self.screen() if hasattr(self, "screen") else None
            if holder is not None:
                available = holder.availableGeometry()
        if available is None:
            return

        # Content height plus the non-scrolling chrome: what the dialog would like to be.
        limit = max(320, available.height() - 90)
        self.setMaximumHeight(max(200, limit))
        # Two convergence passes. On a page switch the newly shown group was
        # previously laid out at the WIDER width of a hidden page, so its first
        # sizeHint underestimates how tall its word-wrapped notes are at the
        # real (narrower) viewport. After one resize the hint is measured at the
        # true width; re-applying with that value converges on the exact
        # height, keeping the scroll bar for genuinely overflowing pages.
        for _ in range(2):
            preferred = chrome + body.sizeHint().height()
            target = min(preferred, limit)
            self.adjustSize()
            area.setMaximumHeight(max(120, target - chrome))
            # The stack's vertical size policy is Ignored (otherwise it bids the
            # tallest page's height - see the comment in _build), which means
            # the body's item minimum is 0 and the area's maximumHeight alone
            # cannot pull the dialog taller. Set the stack's MINIMUM height to
            # what the current page needs (itself a no-op against the dialog's
            # screen maximum, so small screens still get the scroll bar).
            footer_h = self._footer.sizeHint().height()
            self._stack.setMinimumHeight(max(120, target - footer_h))
            # adjustSize() settles smaller than `target` because a QScrollArea's
            # own sizeHint is conservative; the maximum keeps the layout from
            # growing it back, so take the height we actually computed and avoid
            # scrolling we do not need.
            self.resize(self.width(), min(target, self.maximumHeight()))

    # ---- construction ----------------------------------------------------

    def _build(self, s: Settings) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left nav rail + right paged content fill the row; the footer sits below it.
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._nav = QtWidgets.QListWidget()
        self._nav.setObjectName("SettingsNav")
        self._nav.setFocusPolicy(Qt.StrongFocus)   # up/down keys move between pages
        self._nav.setUniformItemSizes(True)
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._nav.setFixedWidth(150)

        self._stack = QtWidgets.QStackedWidget()
        # Height policy has to be Ignored: a QStackedWidget's QWidgetItem resolves its
        # minimum through closestAcceptableSize(), which walks every page and ends up
        # demanding the TALLEST page's preferred height (~419 px). Expanding does not
        # fix it (measured: item minimum stays 419). Ignored lets the stack shrink to 0
        # and lets the per-page scroll areas' maximumHeight be the real height lever;
        # horizontal stays Expanding so the visible page fills the rail.
        self._stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self._pages: list[QWidget] = []
        self._page_areas: list[QScrollArea] = []
        # The four _*_group() builders are reused verbatim - not rewritten. The only
        # change is structural: instead of stacking all four groups into one scroll
        # body, each group hangs on its own page. Order must match NAV_ITEMS exactly.
        groups = (
            self._typography_group(s),
            self._appearance_group(s),
            self._window_group(s),
            self._hotkeys_group(s),
        )
        for (title, subtitle), group in zip(NAV_ITEMS, groups):
            self._nav.addItem(title)
            page, area = self._make_page(title, subtitle, group)
            self._pages.append(page)
            self._page_areas.append(area)
            self._stack.addWidget(page)

        body_layout.addWidget(self._nav)
        body_layout.addWidget(self._stack, 1)
        root.addWidget(body, 1)

        # ---- persistent footer: notes + 确定/取消/恢复默认 -----------------
        footer = QWidget()
        footer.setObjectName("SettingsFooter")
        self._footer = footer
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 14)
        footer_layout.setSpacing(6)

        self.restart_note = QLabel("")
        self.restart_note.setObjectName("Warn")
        footer_layout.addWidget(self.restart_note)

        # 底部小字：说明两种模式到底差在哪，以及"背景不透明度"滑块在两种模式下的统一
        # 语义。复选框旁边那句只说"什么时候该开"，这里回答"开了会有什么不同"。
        mode_note = QLabel(
            "半透明模式：背景透明、文字清晰，支持圆角。\n"
            "兼容模式：整窗（含文字）一起变淡，用于透明异常时。\n"
            "两种模式下“背景不透明度”滑块方向一致：越靠左越透。"
        )
        mode_note.setObjectName("Muted")
        mode_note.setWordWrap(True)
        footer_layout.addWidget(mode_note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults
        )
        self._ok_button = buttons.button(QDialogButtonBox.Ok)
        # 主按钮走橙色（QSS 的 #PrimaryButton 规则），QDialogButtonBox 里只有这一个
        # 确认动作，视觉权重就该和取消/恢复默认不一样。
        self._ok_button.setObjectName("PrimaryButton")
        reset = buttons.button(QDialogButtonBox.RestoreDefaults)
        if reset is not None:
            reset.setText("恢复默认")
            reset.clicked.connect(self._restore_defaults)
        self._ok_button.setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.addStretch(1)
        buttons_row.addWidget(buttons)
        footer_layout.addLayout(buttons_row)
        root.addWidget(footer)

        self.setMinimumWidth(560)

        # Selection exclusivity and up/down keyboard navigation both come from the
        # list's own single-selection model; code only answers this one change signal.
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        row = max(0, min(self._nav.count() - 1, int(type(self)._last_nav_row)))
        # setCurrentRow fires currentRowChanged -> _on_nav_changed picks the page,
        # aliases _scroll to that page's area and re-fits the dialog.
        self._nav.setCurrentRow(row)
        # Validate once up front so the OK button starts in the right state.
        self._on_hotkeys_changed()

    def _make_page(self, title: str, subtitle: str, group: QGroupBox):
        """One stacked page: big title + subtitle on top, the existing group box in an
        independent scroll area below."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 12)
        layout.setSpacing(8)

        heading = QLabel(title)
        heading.setObjectName("PageTitle")
        sub = QLabel(subtitle)
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(sub)

        area = QScrollArea()
        area.setWidget(group)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(area, 1)
        return page, area

    def _on_nav_changed(self, row: int) -> None:
        """The single entry point for page switching: nav row -> stacked page. Also keeps
        `_scroll` aliased to the current page's area (old callers/tests read it) and
        remembers the row for the next dialog opened in this process."""
        if not 0 <= row < len(self._pages):
            return
        self._stack.setCurrentWidget(self._pages[row])
        self._scroll = self._page_areas[row]
        type(self)._last_nav_row = row
        self._fit_to_content_and_screen()

    def _appearance_group(self, s: Settings) -> QGroupBox:
        box = QGroupBox("外观")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        self.opacity_slider, self.opacity_value = _slider(30, 100, s.opacity, "%")
        self.opacity_slider.valueChanged.connect(self._on_opacity)
        form.addRow("窗口不透明度", _row(self.opacity_slider, self.opacity_value))

        self.dim_check = QCheckBox("窗口失去焦点时自动变淡")
        self.dim_check.setChecked(bool(s.dim_on_blur))
        self.dim_check.toggled.connect(self._on_dim_toggled)
        form.addRow("", self.dim_check)

        self.dim_slider, self.dim_value = _slider(0, 100, s.dim_opacity, "%")
        self.dim_slider.valueChanged.connect(self._on_opacity)
        form.addRow("变淡后的不透明度", _row(self.dim_slider, self.dim_value))

        # 窗口底色的 alpha。UI 用 0-100（读者看得懂），落盘仍是 0-255 字节值，靠
        # alpha_to_percent / percent_to_alpha 换算。和上面的"窗口不透明度"是两回事：
        # 不透明度是 setWindowOpacity（整窗连字一起淡），这个是 paintEvent 底座色的
        # alpha（只有背景变透明，字保持清晰）。拖到 0 就是"只剩文字浮在桌面上"。
        self.bg_alpha_slider, self.bg_alpha_value = _slider(
            0, 100, alpha_to_percent(s.bg_alpha), "%"
        )
        self.bg_alpha_slider.valueChanged.connect(self._on_bg_alpha)
        form.addRow("背景不透明度 (0-100)", _row(self.bg_alpha_slider, self.bg_alpha_value))

        # 内容透明度：只透正文文字/正文控件，背景不动。它和背景透明度是两个**独立**的
        # 维度，各自持久化。兼容模式下只有一个整窗不透明度，做不到分离，所以这根滑块
        # 会被禁用（见 _sync_content_enabled），不会假装能分。
        self.content_slider, self.content_value = _slider(
            0, 100, alpha_to_percent(s.content_alpha), "%"
        )
        self.content_slider.valueChanged.connect(self._on_content_alpha)
        form.addRow("内容不透明度 (0-100)", _row(self.content_slider, self.content_value))

        self.content_note = QLabel("")
        self.content_note.setObjectName("Muted")
        self.content_note.setWordWrap(True)
        form.addRow("", self.content_note)

        # 兼容模式 = 关掉逐像素半透明（translucent_bg=False）。勾上之后不再用
        # WA_TranslucentBackground + paintEvent 画圆角底色，改用整窗 setWindowOpacity，
        # 上面那根"背景透明度"滑块随之改为控制整窗不透明度。切换要重建原生窗口，所以
        # 按下"确定"才生效（窗口会闪一下，位置和阅读进度保持不变）。
        self.compat_check = QCheckBox("兼容模式（确定后生效；如透明异常可开启）")
        self.compat_check.setChecked(not s.translucent_bg)
        self.compat_check.toggled.connect(self._on_compat_toggled)
        form.addRow("", self.compat_check)

        note = QLabel(
            "透明度 0 时窗口背景全透，仅文字可见；按老板键可隐藏/唤回，托盘图标也能把窗口叫回来。"
            "低于 100% 时 Windows 会关闭 ClearType 次像素抗锯齿，文字边缘略发虚——"
            "这是分层窗口（WS_EX_LAYERED）的固有行为，不是渲染 bug。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        form.addRow("", note)

        self._sync_dim_enabled()
        self._sync_content_enabled()
        return box

    def _typography_group(self, s: Settings) -> QGroupBox:
        box = QGroupBox("正文排版")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        # "" 表示跟随系统。用 QFontComboBox 而不是自己列字体名：系统里装了什么字体只有
        # Qt 知道，硬编码一份列表在别人机器上必然对不上。
        self.font_combo = QFontComboBox()
        self.font_combo.setEditable(False)
        self.font_combo.setCurrentFont(QFont(s.font_family) if s.font_family else QFont())
        # 先连 touched 再连 _on_typography：QFontComboBox 在填充列表时也会发
        # currentFontChanged，靠这个标志区分"用户挑的"和"Qt 自己设的"，否则
        # font_family 会被写成一个具体族名，"跟随系统"就永远回不去了。
        self.font_combo.currentFontChanged.connect(self._mark_font_touched)
        self.font_combo.currentFontChanged.connect(self._on_typography)
        self.font_reset = QToolButton()
        self.font_reset.setText("跟随系统")
        self.font_reset.setToolTip("恢复成系统默认的中文字体")
        self.font_reset.clicked.connect(self._reset_font_family)
        form.addRow("字体", _row(self.font_combo, self.font_reset))

        self.size_slider, self.size_value = _slider(10, 48, s.font_size, " px")
        form.addRow("字号", _row(self.size_slider, self.size_value))

        self.line_slider, self.line_value = _slider(100, 320, s.line_height, "%")
        form.addRow("行距", _row(self.line_slider, self.line_value))

        self.indent_slider, self.indent_value = _slider(0, 8, s.paragraph_indent, " 字符")
        form.addRow("首行缩进", _row(self.indent_slider, self.indent_value))

        self.margin_slider, self.margin_value = _slider(0, 80, s.margin, " px")
        form.addRow("页边距", _row(self.margin_slider, self.margin_value))

        # 只开放正文的底色和字色，不开放窗口边框/标题栏：那两处要和 PALETTE 的
        # border/panel 联动，让用户单独改会把整窗对比度改坏。
        self._bg = s.bg
        self._fg = s.fg
        self.bg_swatch, self.bg_button = self._colour_row("bg", "选择正文背景色")
        self.fg_swatch, self.fg_button = self._colour_row("fg", "选择正文文字颜色")
        form.addRow("正文背景", _row(self.bg_swatch, self.bg_button))
        form.addRow("正文颜色", _row(self.fg_swatch, self.fg_button))
        self._paint_swatches()

        for slider in (self.size_slider, self.line_slider, self.indent_slider, self.margin_slider):
            slider.valueChanged.connect(self._on_typography)

        presets = QToolButton()
        presets.setText("一键预设")
        presets.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(presets)
        for name, values in READ_PRESETS:
            action = menu.addAction(name)
            action.triggered.connect(lambda _checked=False, v=values: self._apply_preset(v))
        presets.setMenu(menu)
        self.read_note = QLabel("")
        self.read_note.setObjectName("Muted")
        self.read_note.setWordWrap(True)
        form.addRow("", _row(presets, self.read_note))
        self._check_contrast()

        return box

    def _colour_row(self, field: str, title: str):
        swatch = QLabel()
        swatch.setObjectName("Swatch")
        swatch.setFixedSize(40, 24)
        button = QToolButton()
        button.setText("选择颜色…")
        button.clicked.connect(lambda: self._pick_colour(field, title))
        return swatch, button

    def _mark_font_touched(self, _font=None) -> None:
        self._font_touched = True

    def _reset_font_family(self) -> None:
        # setCurrentFont 会发 currentFontChanged，所以这里不必再手动调一次
        # _on_typography；但 touched 标志必须**保持为真**，否则用户按了"跟随系统"
        # 又会被当成从没挑过，两者结果一样，但语义会变得依赖调用顺序。
        self._font_touched = True
        self.font_combo.setCurrentFont(QFont())

    def _paint_swatches(self) -> None:
        border = PALETTE["border"]
        self.bg_swatch.setStyleSheet(f"background: {self._bg}; border: 1px solid {border}; border-radius: 4px;")
        self.fg_swatch.setStyleSheet(f"background: {self._fg}; border: 1px solid {border}; border-radius: 4px;")
        self.bg_swatch.setToolTip(self._bg)
        self.fg_swatch.setToolTip(self._fg)

    def _pick_colour(self, field: str, title: str) -> None:
        current = self._bg if field == "bg" else self._fg
        chosen = QColorDialog.getColor(QColor(current), self, title)
        if not chosen.isValid():
            return
        if field == "bg":
            self._bg = chosen.name()
        else:
            self._fg = chosen.name()
        self._paint_swatches()
        self._on_typography()

    def _apply_preset(self, values: dict) -> None:
        if "font_family" in values:
            self._font_touched = True
            self.font_combo.setCurrentFont(QFont(values["font_family"]) if values["font_family"] else QFont())
        for key, widget in (
            ("font_size", self.size_slider),
            ("line_height", self.line_slider),
            ("paragraph_indent", self.indent_slider),
            ("margin", self.margin_slider),
        ):
            if key in values:
                widget.setValue(int(values[key]))
        if "bg" in values:
            self._bg = values["bg"]
        if "fg" in values:
            self._fg = values["fg"]
        self._paint_swatches()
        self._on_typography()

    def _check_contrast(self) -> None:
        """正文字色和背景色的对比度必须达标，否则读久了眼睛会疼。

        这里只**警告**，不阻止确定：用户可能就是想要低对比（比如夜间加一点暖色）。
        但不说出来就是失职——3:1 以下的中文正文在笔记本屏幕上基本没法读。
        """
        try:
            ratio = contrast_ratio(self._bg, self._fg)
        except ValueError:
            self.read_note.setText("颜色值看不懂，已忽略")
            return
        if ratio >= AA_NORMAL:
            self.read_note.setText(f"对比度 {ratio:.1f}:1，达标")
        else:
            self.read_note.setText(
                f"对比度只有 {ratio:.1f}:1（建议 ≥ {AA_NORMAL:g}:1），正文会偏难读"
            )

    def _on_typography(self) -> None:
        """把控件值实时写进预览，让主窗口立刻按新排版重排。"""
        self.size_value.setText(f"{self.size_slider.value()} px")
        self.line_value.setText(f"{self.line_slider.value()}%")
        self.indent_value.setText(f"{self.indent_slider.value()} 字符")
        self.margin_value.setText(f"{self.margin_slider.value()} px")
        self._check_contrast()
        self._preview()

    def _current_font_family(self) -> str:
        if not self._font_touched:
            return ""
        family = self.font_combo.currentFont().family() or ""
        # 用户挑中的正好就是系统默认字体时，归一化成 ""，让配置里只有一种表示法。
        return "" if family == QFont().family() else family

    def _window_group(self, s: Settings) -> QGroupBox:
        box = QGroupBox("窗口")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        snap_row = QWidget()
        snap_layout = QHBoxLayout(snap_row)
        snap_layout.setContentsMargins(0, 0, 0, 0)
        snap_layout.setSpacing(8)
        self.snap_check = QCheckBox("拖动后吸附到屏幕边缘")
        self.snap_check.setChecked(bool(s.snap_enabled))
        self.snap_check.toggled.connect(lambda on: self.snap_spin.setEnabled(bool(on)))
        snap_layout.addWidget(self.snap_check)
        self.snap_spin = QSpinBox()
        self.snap_spin.setRange(0, 60)
        self.snap_spin.setValue(int(s.snap_distance))
        self.snap_spin.setSuffix(" px")
        self.snap_spin.setEnabled(bool(s.snap_enabled))
        snap_layout.addWidget(self.snap_spin)
        snap_layout.addStretch(1)
        form.addRow("边缘吸附", snap_row)

        self.onscreen_check = QCheckBox("不允许拖出屏幕可视范围")
        self.onscreen_check.setChecked(bool(s.keep_on_screen))
        form.addRow("", self.onscreen_check)

        self.title_edit = QLineEdit(s.disguise_title)
        self.title_edit.setPlaceholderText("留空 = 用真实标题")
        self.title_edit.setMaxLength(120)
        form.addRow("窗口标题伪装", self.title_edit)

        # 鼠标移出隐身。整窗 setWindowOpacity(0)（连文字一起消失），移回窗口区域即恢复；
        # 和老板键的 hide() 是两回事，两者互不干扰。顶栏 👁 是同一个开关。
        self.leave_hide_check = QCheckBox("鼠标移出窗口时自动隐身")
        self.leave_hide_check.setChecked(bool(s.mouse_leave_hide))
        self.leave_hide_check.toggled.connect(self._on_leave_hide_toggled)
        form.addRow("", self.leave_hide_check)

        leave_hide_note = QLabel(
            "开启后，鼠标移出窗口会立刻隐藏窗口（不透明度降到 0）；鼠标移回窗口区域即恢复。"
            "老板键仍然有效。"
        )
        leave_hide_note.setObjectName("Muted")
        leave_hide_note.setWordWrap(True)
        form.addRow("", leave_hide_note)

        self.tray_check = QCheckBox("显示托盘图标")
        self.tray_check.setChecked(bool(s.show_tray))
        form.addRow("", self.tray_check)

        # 「点 ✕ 隐藏到托盘」复选框已移除：入口统一到 main.py 常驻模型后，✕ 一律
        # 收起窗口，退出只走托盘菜单，不再有可配置分支。Settings.close_to_tray
        # 字段保留只为兼容旧 JSON，对话框不再读写它。

        self.on_top_check = QCheckBox("窗口置顶")
        self.on_top_check.setChecked(bool(s.always_on_top))
        self.on_top_check.toggled.connect(self._on_restart_flag_toggled)
        form.addRow("", self.on_top_check)

        self.taskbar_check = QCheckBox("隐藏任务栏按钮（也不出现在 Alt+Tab）")
        self.taskbar_check.setChecked(bool(s.hide_from_taskbar))
        self.taskbar_check.toggled.connect(self._on_restart_flag_toggled)
        form.addRow("", self.taskbar_check)

        return box

    def _hotkeys_group(self, s: Settings) -> QGroupBox:
        box = QGroupBox("全局快捷键")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.hotkey_edits: dict[str, HotkeyEdit] = {}
        for row, (name, _field, label) in enumerate(HOTKEY_FIELDS):
            edit = HotkeyEdit(getattr(s, name))
            self.hotkey_edits[name] = edit
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            edit.changed.connect(self._on_hotkeys_changed)
        grid.setColumnStretch(1, 1)

        hint = QLabel(
            "点进输入框后直接按组合键。至少要带一个 Ctrl / Alt / Shift / Win，"
            "单独一个按键会抢走全系统的输入（F1–F24 例外）。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        grid.addWidget(hint, len(HOTKEY_FIELDS), 0, 1, 2)

        self.hotkey_note = QLabel("")
        self.hotkey_note.setObjectName("Warn")
        grid.addWidget(self.hotkey_note, len(HOTKEY_FIELDS) + 1, 0, 1, 2)
        return box

    # ---- live preview ----------------------------------------------------

    def _preview(self) -> None:
        """把当前控件值推给主窗口做实时预览。apply_to 是唯一的写入口，所以预览走的路
        和"确定"完全一样——不会出现预览好看、保存后不一样的情况。"""
        if self._on_preview is None:
            return
        preview = Settings(**self._settings.__dict__)
        self.apply_to(preview)
        self._on_preview(preview)

    def _on_opacity(self) -> None:
        self.opacity_value.setText(f"{self.opacity_slider.value()}%")
        self.dim_value.setText(f"{self.dim_slider.value()}%")
        self._preview()

    def _on_bg_alpha(self) -> None:
        """拖动"背景透明度"：只改窗口底色，不触发整窗重排/存盘。"""
        percent = int(self.bg_alpha_slider.value())
        self.bg_alpha_value.setText(f"{percent}%")
        if self._on_background is not None:
            self._on_background(percent_to_alpha(percent))

    def _on_content_alpha(self) -> None:
        """拖动"内容透明度"：只改正文的 effect，背景和整窗都不动。"""
        percent = int(self.content_slider.value())
        self.content_value.setText(f"{percent}%")
        if self._on_content is not None:
            self._on_content(percent_to_alpha(percent))

    def _on_dim_toggled(self, _checked: bool) -> None:
        self._sync_dim_enabled()
        self._on_opacity()

    def _on_leave_hide_toggled(self, checked: bool) -> None:
        """把主窗顶栏 👁 的按下状态跟着这个复选框走（只同步外观，不写设置）。"""
        if self._on_leave_hide is not None:
            self._on_leave_hide(bool(checked))

    def _on_compat_toggled(self, _checked: bool = False) -> None:
        self._sync_content_enabled()

    def _sync_dim_enabled(self) -> None:
        self.dim_slider.setEnabled(self.dim_check.isChecked())

    def _sync_content_enabled(self) -> None:
        """兼容模式下禁用"内容透明度"：那里只有整窗不透明度，做不到只透正文。

        禁用而不是隐藏：滑块留在原地，读者能看出"这个设置存在，只是当前模式用不了"，
        比凭空消失一根滑块更好理解。
        """
        translucent = not bool(self.compat_check.isChecked())
        self.content_slider.setEnabled(translucent)
        self.content_note.setText(
            "" if translucent else "兼容模式下内容与背景一起变淡，无法单独调节"
        )

    def _on_restart_flag_toggled(self, _checked: bool = False) -> None:
        changed = (
            self.on_top_check.isChecked(),
            self.taskbar_check.isChecked(),
        ) != self._original_restart_flags
        self.restart_note.setText("置顶和任务栏按钮是窗口标志，改动要重启 MiniReader 才生效。" if changed else "")

    def _on_hotkeys_changed(self) -> None:
        seen: dict[str, str] = {}
        problems = []
        for name, _field, label in HOTKEY_FIELDS:
            edit = self.hotkey_edits[name]
            if edit.problem():
                problems.append(f"{label}：{edit.problem()}")
                continue
            spec = edit.spec()
            if not spec:
                continue
            chord = format_hotkey(spec)
            if chord in seen:
                problems.append(f"{label} 和 {seen[chord]} 用了同一个组合键")
            else:
                seen[chord] = label
        self.hotkey_note.setText("；".join(problems))
        # Blocking OK rather than silently dropping a bad binding on save: clearing a
        # field the user did not mean to clear is worse than making them look at it.
        ok = getattr(self, "_ok_button", None)
        if ok is not None:
            ok.setEnabled(not problems)

    # ---- results ---------------------------------------------------------

    def apply_to(self, s: Settings) -> Settings:
        """Write every control into `s` and clamp. Never touches geometry or last_book."""
        s.font_family = self._current_font_family()
        s.font_size = int(self.size_slider.value())
        s.line_height = int(self.line_slider.value())
        s.paragraph_indent = int(self.indent_slider.value())
        s.margin = int(self.margin_slider.value())
        s.bg = self._bg
        s.fg = self._fg
        # 两根滑块都是 0-100 的界面值，落盘要换回 0-255 字节。两者独立持久化：改背景
        # 透明度不会碰到 content_alpha，反过来也一样。
        s.bg_alpha = percent_to_alpha(self.bg_alpha_slider.value())
        s.content_alpha = percent_to_alpha(self.content_slider.value())
        # 复选框是"兼容模式"，和字段语义相反：勾上 = 关半透明。
        s.translucent_bg = not bool(self.compat_check.isChecked())
        s.opacity = int(self.opacity_slider.value())
        s.dim_on_blur = bool(self.dim_check.isChecked())
        s.dim_opacity = int(self.dim_slider.value())
        s.snap_enabled = bool(self.snap_check.isChecked())
        s.snap_distance = int(self.snap_spin.value())
        s.keep_on_screen = bool(self.onscreen_check.isChecked())
        s.disguise_title = self.title_edit.text()
        s.show_tray = bool(self.tray_check.isChecked())
        s.mouse_leave_hide = bool(self.leave_hide_check.isChecked())
        s.always_on_top = bool(self.on_top_check.isChecked())
        s.hide_from_taskbar = bool(self.taskbar_check.isChecked())
        for name, _field, _label in HOTKEY_FIELDS:
            setattr(s, name, self.hotkey_edits[name].spec())
        s.clamp()
        return s

    def settings(self) -> Settings:
        """A copy of the settings with this dialog's values applied.

        Geometry and last_book ride along untouched - they are session state, not
        preferences, and 恢复默认 must not move the window or forget the book.
        """
        return self.apply_to(replace(self._settings))

    def _restore_defaults(self) -> None:
        """Reset the *preferences* to factory values, leaving session state alone."""
        fresh = Settings()
        self._font_touched = True        # 恢复默认的"字体"就是""（跟随系统），算已选择
        self.font_combo.setCurrentFont(QFont())
        self.size_slider.setValue(fresh.font_size)
        self.line_slider.setValue(fresh.line_height)
        self.indent_slider.setValue(fresh.paragraph_indent)
        self.margin_slider.setValue(fresh.margin)
        self._bg, self._fg = fresh.bg, fresh.fg
        self._paint_swatches()
        self._check_contrast()
        self.bg_alpha_slider.setValue(alpha_to_percent(fresh.bg_alpha))
        self.content_slider.setValue(alpha_to_percent(fresh.content_alpha))
        self.compat_check.setChecked(not fresh.translucent_bg)
        self._sync_content_enabled()
        self.opacity_slider.setValue(fresh.opacity)
        self.dim_check.setChecked(fresh.dim_on_blur)
        self.dim_slider.setValue(fresh.dim_opacity)
        self.snap_check.setChecked(fresh.snap_enabled)
        self.snap_spin.setValue(fresh.snap_distance)
        self.onscreen_check.setChecked(fresh.keep_on_screen)
        self.title_edit.setText(fresh.disguise_title)
        self.tray_check.setChecked(fresh.show_tray)
        self.leave_hide_check.setChecked(fresh.mouse_leave_hide)
        self.on_top_check.setChecked(fresh.always_on_top)
        self.taskbar_check.setChecked(fresh.hide_from_taskbar)
        for name, _field, _label in HOTKEY_FIELDS:
            self.hotkey_edits[name].set_spec(getattr(fresh, name))
        self._on_hotkeys_changed()
        self._sync_dim_enabled()
        self._on_restart_flag_toggled()
        self._on_opacity()


def _slider(low: int, high: int, value: int, suffix: str):
    slider = QSlider(Qt.Horizontal)
    slider.setRange(low, high)
    slider.setValue(max(low, min(int(value), high)))
    slider.setPageStep(5)
    label = QLabel(f"{slider.value()}{suffix}")
    label.setMinimumWidth(38)
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return slider, label


def _row(*widgets) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    return holder
