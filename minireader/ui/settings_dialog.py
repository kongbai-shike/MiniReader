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

from ..hotkeys import (
    DEBOUNCE_MS,
    Conflict,
    HotkeyError,
    find_conflicts,
    format_hotkey,
    parse_hotkey,
    parse_to_vks,
    spec_from_qt,
)
from ..core import rar
from ..qt import (
    QApplication,
    QCheckBox,
    QColor,
    QColorDialog,
    QComboBox,
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
    QPushButton,
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
    available_screen_geometry,
    exec_dialog,
)
from ..storage import (
    HOTKEY_FIELDS,
    WAKE_FIELDS,
    Settings,
    alpha_to_percent,
    percent_to_alpha,
)
from .theme import (
    AA_NORMAL,
    ACTIVE_EXTRAS,
    PALETTE,
    THEME_LABELS,
    THEME_NAMES,
    contrast_ratio,
)

# 一键排版预设。名字是给读者看的，值必须都是 clamp() 允许范围内的合法值——预设本身
# 就是个方便入口，不该绕过校验，所以 apply_to() 最后照样调 s.clamp()。
READ_PRESETS: tuple[tuple[str, dict], ...] = (
    ("默认（深色）", {"font_family": "", "font_size": 25, "line_height": 210, "paragraph_indent": 2, "margin": 16, "bg": "#1b1b1b", "fg": "#d8d8da"}),
    ("护眼（米黄）", {"font_family": "", "font_size": 24, "line_height": 200, "paragraph_indent": 2, "margin": 24, "bg": "#f5efe0", "fg": "#3a352c"}),
    ("夜间（纯黑）", {"font_family": "", "font_size": 24, "line_height": 200, "paragraph_indent": 2, "margin": 20, "bg": "#000000", "fg": "#9aa0ad"}),
    ("大字（松散）", {"font_family": "", "font_size": 30, "line_height": 220, "paragraph_indent": 2, "margin": 28, "bg": "#1b1b1b", "fg": "#d8d8da"}),
)


# 左侧导航条目；顺序必须与 _build() 里构建分组页的顺序一致（第 i 条 ↔ 第 i 页）。
# 参考 Telegram Desktop 设置：左导航 + 右侧单页，一次只看一个分组。
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("正文排版", "字体、字号、行距、缩进、页边距与正文配色"),
    ("漫画与 PDF", "PDF 判型、漫画默认缩放与模式、页缓存与预加载"),
    ("外观", "窗口与正文的不透明度，以及失去焦点时自动变淡"),
    ("窗口", "边缘吸附、移出隐身、托盘、置顶与任务栏按钮"),
    ("全局快捷键", "在任何程序里都能生效的组合键"),
)

# 漫画与 PDF 那一页的下拉选项：值必须与 storage.py 的枚举校验集合一致，
# 否则用户选中的值会被 clamp() 悄悄打回默认。显示名是给读者看的中文。
COMIC_MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("single", "单页"),
    ("scroll", "连续滚动"),
    ("spread", "双页"),
)
COMIC_ZOOM_CHOICES: tuple[tuple[str, str], ...] = (
    ("width", "宽度适应"),
    ("height", "高度适应"),
    ("original", "原始尺寸"),
)
PDF_KIND_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "自动判断"),
    ("text", "按文本打开"),
    ("comic", "按漫画打开"),
)
COMIC_BINDING_CHOICES: tuple[tuple[str, str], ...] = (
    ("rtl", "右綴じ（从右往左）"),
    ("ltr", "左綴じ（从左往右）"),
)

# 唤醒键的"纯修饰键累积"顺序。隐私模式的唤醒键**可以只由修饰键组成**（默认就是
# Ctrl+Shift）：它是 GetAsyncKeyState 轮询出来的，没有 RegisterHotKey 那条"必须再带一
# 个普通键"的限制。顺序固定死，免得同一组键被写成两种文本。
_WAKE_ORDER: tuple[tuple[str, str], ...] = (
    ("Key_Control", "Ctrl"),
    ("Key_Alt", "Alt"),
    ("Key_Shift", "Shift"),
    ("Key_Meta", "Win"),
)

# 哪些字段是唤醒键：决定编辑器用"纯修饰键累积"模式、也决定冲突小窗口里的提示。
_WAKE_NAMES: frozenset[str] = frozenset(name for name, _label in WAKE_FIELDS)

# 主按钮（确定）的橙：QQ 迷你窗的强调色。深色文字压橙色底的对比度约 7:1，白字只有
# 3:1，所以主按钮用深字。**白昼主题沿用同一个橙**：橙底压深字的比值只跟这对颜色本身
# 有关，跟窗口是深是浅无关，所以不必也不该为白昼再配一个橙（白底上再放浅橙会糊）。
_PRIMARY = "#c8891a"
_PRIMARY_HOVER = "#dda032"
_PRIMARY_PRESSED = "#a96f12"
# 橙底上的文字色。抽成常量而不是在 QSS 里写字面量：样式表源码里一个 6 位 hex 都不许
# 出现（有测试钉这条），否则白昼下很容易漏改出"白底白字"。
_PRIMARY_FG = "#1b1b1b"


def _stylesheet() -> str:
    """对话框整体美化规格（对齐 QQ 迷你窗的深色层级）：

    * 三层表面：对话框底 panel < 分组框 card < 控件 field（两套主题各自的 11 键表）。
    * 字号下限：说明小字 12px，标签/按钮/输入 13px，分组标题 14px 加粗——没有更小的字。
    * 禁用态不靠透明度假装：滑块手柄/已勾选指示换成实色灰，文字用当前主题的
      disabled_fg，仍然可读。

    **颜色一个字面量都不许写死**：除了 `_PRIMARY*` 这几个常量（它们与主题无关，且是被
    插值进来的），其余全部来自 `PALETTE` 或 `ACTIVE_EXTRAS`。写死一个 hex 在白昼主题
    下就是白底白字，而且只有真桌面才撞得见——`tests/test_theme.py` 用正则钉这条。
    """
    p = PALETTE
    x = ACTIVE_EXTRAS
    return f"""
    QDialog {{ background: {p['panel']}; }}
    QLabel {{ color: {p['fg']}; font-size: 13px; }}
    QLabel#Muted {{ color: {p['muted']}; font-size: 12px; }}
    QLabel#Warn {{ color: {p['warn']}; font-size: 12px; }}
    QLabel#SavedNote {{ color: {x['saved_fg']}; font-size: 12px; font-weight: bold; }}
    QLabel#SubHeading {{ color: {p['fg']}; font-size: 12px; font-weight: bold; }}
    QLabel:disabled {{ color: {x['disabled_fg']}; }}
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
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {x['focus_border']}; }}
    QLineEdit#HotkeyBad {{ border: 1px solid {p['warn']}; }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background: {p['border']}; width: 18px; border: none;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {x['handle_hover']}; }}
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
    QCheckBox::indicator:hover {{ border-color: {x['focus_border']}; }}
    QCheckBox::indicator:checked {{ background: {_PRIMARY}; border-color: {_PRIMARY}; }}
    QCheckBox::indicator:disabled {{ background: {x['disabled_bg']}; border-color: {x['disabled_border']}; }}
    QCheckBox:disabled {{ color: {x['disabled_fg']}; }}
    QSlider::groove:horizontal {{
        height: 4px; background: {p['border']}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {p['fg']}; width: 14px; margin: -5px 0; border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{ background: {x['slider_handle_hover']}; }}
    QSlider::groove:horizontal:disabled {{ background: {x['slider_groove_disabled']}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {x['slider_subpage_disabled']}; }}
    QSlider::handle:horizontal:disabled {{ background: {x['slider_handle_disabled']}; }}
    QToolButton {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        border-radius: 4px; padding: 5px 12px; font-size: 13px; min-height: 28px;
    }}
    QToolButton:hover {{ background: {p['border']}; }}
    QToolButton:pressed {{ background: {x['pressed_bg']}; }}
    QToolButton:focus {{ border: 1px solid {x['focus_border']}; }}
    QPushButton {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        border-radius: 4px; padding: 6px 16px; font-size: 13px;
        min-width: 80px; min-height: 32px;
    }}
    QPushButton:hover {{ background: {p['border']}; }}
    QPushButton:pressed {{ background: {x['pressed_bg']}; }}
    QPushButton:focus {{ border: 1px solid {x['focus_border']}; }}
    QPushButton:disabled {{ background: {x['disabled_bg']}; color: {x['disabled_fg']}; }}
    QPushButton#PrimaryButton {{
        background: {_PRIMARY}; color: {_PRIMARY_FG}; border: none; font-weight: bold;
    }}
    QPushButton#PrimaryButton:hover {{ background: {_PRIMARY_HOVER}; }}
    QPushButton#PrimaryButton:pressed {{ background: {_PRIMARY_PRESSED}; }}
    QPushButton#PrimaryButton:disabled {{ background: {x['primary_disabled_bg']}; color: {x['primary_disabled_fg']}; }}
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
    QScrollBar::handle:vertical:hover {{ background: {x['handle_hover']}; }}
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


def _modifier_name(key) -> str:
    """修饰键的显示名，不是修饰键就返回 ""。

    `getattr` 带默认值：PySide6 与 PyQt5 的 Qt.Key_* 名字并不完全一样，而"认不出
    这个键"只该让唤醒键编辑框退回普通组合键路径，不该抛异常。
    """
    for name, label in _WAKE_ORDER:
        if key == getattr(Qt, name, None):
            return label
    return ""


class HotkeyEdit(QLineEdit):
    """Captures a single key chord and shows it as a MiniReader spec.

    Validating on capture rather than on save is the point: the user finds out
    immediately that a bare letter is not allowed (`RegisterHotKey` would steal it from
    every other program) instead of at the moment they press 确定.

    `wake=True` 是隐私模式唤醒键专用的模式：它用 `parse_to_vks` 校验，允许"只有修
    饰键"（默认 Ctrl+Shift）。全局热键没有这个自由——RegisterHotKey 必须要一个普通
    键——但唤醒键是轮询的，所以这里可以，也必须能录进去。
    """

    # Emitted on every capture or clear. `editingFinished` is not enough: this widget is
    # read-only, and whether that signal fires on focus-out is not something to rely on.
    changed = Signal()

    def __init__(self, spec: str = "", parent=None, *, wake: bool = False) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText(
            "点击这里，按住唤醒键（如 Ctrl+Shift）；也可录一个普通组合键"
            if wake
            else "点击这里再按组合键（Backspace 清除）"
        )
        self._problem = ""
        # 唤醒键要能把"按住 Ctrl 再按 Shift"累积成 Ctrl+Shift：纯修饰键不产生任何
        # 普通按键事件，靠 `_held` 记住这一轮按下了哪些。
        self._wake = bool(wake)
        self._held: set[str] = set()
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
                if self._wake:
                    parse_to_vks(spec)
                else:
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
            self._held.clear()
            self.set_spec("")
            event.accept()
            return
        if self._wake:
            name = _modifier_name(key)
            if name:
                # 纯修饰键：累积起来，顺序按 _WAKE_ORDER 归一，免得 Ctrl+Shift 与
                # Shift+Ctrl 变成两种文本（它们对 GetAsyncKeyState 是同一组 VK）。
                self._held.add(name)
                self.set_spec(
                    "+".join(
                        label for _attr, label in _WAKE_ORDER if label in self._held
                    )
                )
                event.accept()
                return
        elif key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return  # a modifier on its own is not a chord yet
        spec = spec_from_qt(key, event.modifiers())
        if spec is None:
            self._reject("这个按键不能用作全局热键")
            event.accept()
            return
        self.set_spec(spec)
        event.accept()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if self._wake:
            name = _modifier_name(event.key())
            if name:
                # 松开只结束这一轮累积，显示值保持不动：否则手指一松，刚录好的
                # Ctrl+Shift 就从框里消失，用户根本看不出自己设了什么。
                self._held.discard(name)
                event.accept()
                return
        super().keyReleaseEvent(event)


class ConflictDialog(QDialog):
    """热键冲突的"回滚 + 就地改"小窗口。

    只做一件事：把本次冲突摆到用户面前，并允许当场改。它的"确定"**只关自己**（锁定
    决策 5）：不回设置页重新检测，也不保存。用户回到设置页再点一次"确定"，那才叫第二
    次检测——这样"改一个又撞上"不会变成连环弹窗。

    `rows` 是 `[(name, label, spec)]`，`conflicts` 是 `[Conflict]`：冲突涉及的行标
    红、其余行照常显示，好让用户看到全局再动手。
    """

    def __init__(self, parent, rows, conflicts, on_change=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("热键冲突")
        self.setStyleSheet(_stylesheet())
        self.setMinimumWidth(520)
        self._on_change = on_change
        self.edits: dict[str, HotkeyEdit] = {}

        conflicted: set[str] = set()
        for conflict in conflicts:
            conflicted.update(conflict.names)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        title = QLabel("这些热键互相冲突，本次改动已经全部回滚：")
        title.setObjectName("Warn")
        title.setWordWrap(True)
        layout.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        for row, (name, label, spec) in enumerate(rows):
            tag = QLabel(label)
            if name in conflicted:
                # 标"冲突"而不是 HotkeyBad：那个红框的含义是"这个写法不合法"，
                # 而冲突的键单看都是合法的。
                tag.setObjectName("Warn")
            grid.addWidget(tag, row, 0)
            edit = HotkeyEdit(spec, wake=name in _WAKE_NAMES)
            # 绑死当前的 name：闭包晚绑定会让所有行都指向最后一行的编辑框。
            edit.changed.connect(lambda name=name: self._edited(name))
            self.edits[name] = edit
            grid.addWidget(edit, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        for conflict in conflicts:
            message = QLabel(conflict.message)
            message.setObjectName("Warn")
            message.setWordWrap(True)
            layout.addWidget(message)

        hint = QLabel(
            "改完点「确定」回到设置页，再点一次「确定」才会重新检测并保存。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok, self)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setObjectName("PrimaryButton")
        ok.setText("确定")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _edited(self, name: str) -> None:
        """把这一行改后的写法同步回设置页，这样回到设置页看到的就是最新的。"""
        if self._on_change is not None:
            self._on_change(name, self.edits[name].spec())


class SettingsDialog(QDialog):
    """Modal-ish editor for `Settings`. `apply_to()` is the only writer."""

    # 按下"确定"并且热键检测全部通过时发出（锁定决策 6：这个对话框不关）。主窗把它接
    # 到"立刻写回 + 落盘"，所以用户看到的绿字"键位已保存"是真的已经存了，不是画饼。
    saved = Signal()

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
        on_theme=None,
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
        # 主题下拉的实时预览。和顶栏 🌙 共用主窗的 apply_theme()，所以两处入口的副作用
        # 完全一致；同样**不落盘**，取消时主窗会把主题切回去（见 MiniWindow.open_settings）。
        # 名字刻意不叫 `_on_theme`——那是下面的槽方法名，同名会把回调覆盖掉。
        self._on_theme_cb = on_theme
        # 只有用户**主动**挑过字体才把族名存进配置，否则 ""（跟随系统）会被 Qt 报告的
        # 具体族名永久顶掉。_build() 里设置初始字体时这个标志必须已经是假。
        self._font_touched = False
        # 上一次"重新检测"的结果。None 表示这个对话框里还没按过——此时 apply_to()
        # 不该动配置里的 unrar_* 三个字段，否则打开一次设置页就会把上次探测的结果
        # 抹成空（用户可能只是来改字号的）。
        self._rar_probe = None
        self._original_restart_flags = (
            bool(settings.always_on_top),
            bool(settings.hide_from_taskbar),
        )
        # 热键编辑器按字段名索引，**在所有页构建之前**就建好：防偷窥快捷键的编辑器
        # 长在「窗口」页（它属于那个复选框的小组），其余长在「全局快捷键」页，而
        # 校验/写回/恢复默认只认这一张表。
        self.hotkey_edits: dict[str, HotkeyEdit] = {}
        # 这次对话框里是否已经成功保存过一次。主窗靠它决定"关窗算不算确定"；对话框
        # 自己在 reject() 里也用它，免得刚存完就按 ✕ 又把主窗的预览撤回去。
        self._saved_ok = False
        # 上次"确定"时的热键快照。冲突检测只针对**本次改动的**键，靠它算出改动集合。
        self._hotkey_baseline: dict[str, str] = {}
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
        # `QApplication.desktop()` 在 Qt 6 里没有了（PySide6 直接 AttributeError），
        # 取屏的三种写法收在 `available_screen_geometry()` 里，两个绑定都能用。
        available = available_screen_geometry(self)
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
        # The group builders are reused verbatim - not rewritten. The only change is
        # structural: instead of stacking all of them into one scroll body, each group
        # hangs on its own page. Order must match NAV_ITEMS exactly.
        groups = (
            self._typography_group(s),
            self._comic_group(s),
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

        # 绿字"键位已保存"。它在按"确定"且检测通过之后才出现，因为那个按钮**不关窗**
        # （锁定决策 6），没有这句反馈用户会以为点了没反应。
        self.save_note = QLabel("")
        self.save_note.setObjectName("SavedNote")
        self.save_note.setVisible(False)
        footer_layout.addWidget(self.save_note)

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
        # 顺序也要修一次：防偷窥的编辑器在「窗口」页先建（那一页先构建），而
        # hotkey_edits 的键序应当跟 HOTKEY_FIELDS 一致——否则遍历这张表的代码
        # （校验、测试里的 next(iter(...))）就会先拿到最后一个字段。
        self.hotkey_edits = {
            name: self.hotkey_edits[name] for name, _field, _label in HOTKEY_FIELDS
        }
        # 打开时的热键快照：冲突检测只关心"这次改过的键"，靠它算差集。
        self._remember_hotkeys()
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

        # 主题放这一页的第一行：它是"整窗长什么样"的总开关，比下面的透明度更上位。
        # 它和「正文排版」页的正文配色是**两个**维度：主题只管窗口外观（顶栏/底栏/
        # 目录/托盘/菜单/tooltip），正文底色字色归正文排版页，所以下面那行说明必须
        # 写清楚，否则用户会以为切了主题正文也会跟着变色。
        self.theme_combo = QComboBox()
        for _name in THEME_NAMES:
            self.theme_combo.addItem(THEME_LABELS.get(_name, _name), _name)
        _select_data(self.theme_combo, s.theme)
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        self.theme_note = QLabel(
            "只改窗口外观。正文的字体、字号与配色在「正文排版」页，两者互不影响。"
        )
        self.theme_note.setObjectName("Muted")
        self.theme_note.setWordWrap(True)
        form.addRow("主题", _row(self.theme_combo, self.theme_note))

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

    def _comic_group(self, s: Settings) -> QGroupBox:
        """漫画与 PDF 的那一页。

        PDF 的判型和漫画的显示方式是同一件事的两半：两者都决定"这一页该怎么画"，
        而且共用同一个页缓存预算。放在一起，用户调 PDF 的时候顺手就看得到漫画的档位。
        """
        box = QGroupBox("漫画与 PDF")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        # PDF 判型。默认"自动判断"是按内容量出来的（见 core/pdfdoc.py）：
        # 字符密度 >= 200 判文本，密度极低且图片盖住 70% 以上判扫描。
        self.pdf_kind_combo = QComboBox()
        for value, label in PDF_KIND_CHOICES:
            self.pdf_kind_combo.addItem(label, value)
        _select_data(self.pdf_kind_combo, getattr(s, "pdf_default_kind", "auto"))
        form.addRow("PDF 默认按", self.pdf_kind_combo)

        pdf_note = QLabel(
            "自动判断：正文文字多的 PDF 当书读，整页扫描的当漫画翻。"
            "判错了可以在正文右键菜单里改成另一种，那本书会记住。"
        )
        pdf_note.setObjectName("Muted")
        pdf_note.setWordWrap(True)
        form.addRow("", pdf_note)

        self.comic_zoom_combo = QComboBox()
        for value, label in COMIC_ZOOM_CHOICES:
            self.comic_zoom_combo.addItem(label, value)
        _select_data(self.comic_zoom_combo, getattr(s, "comic_default_zoom", "width"))
        form.addRow("打开漫画时", self.comic_zoom_combo)

        self.comic_mode_combo = QComboBox()
        for value, label in COMIC_MODE_CHOICES:
            self.comic_mode_combo.addItem(label, value)
        _select_data(self.comic_mode_combo, getattr(s, "comic_default_mode", "single"))
        form.addRow("阅读模式", self.comic_mode_combo)

        mode_note = QLabel("双页模式要求窗口够宽；窗口太窄时会自动退回单页。")
        mode_note.setObjectName("Muted")
        mode_note.setWordWrap(True)
        form.addRow("", mode_note)

        # 缓存预算。按解码后的像素算（宽 × 高 × 4 字节），所以一张 1224×1584 的
        # PDF 页约占 7.4 MB。上限 1024 与 storage.clamp() 的范围一致。
        self.cache_slider, self.cache_value = _slider(
            64, 1024, int(getattr(s, "comic_cache_mb", 256)), " MB"
        )
        self.cache_slider.setPageStep(64)
        self.cache_slider.valueChanged.connect(self._on_cache_mb)
        form.addRow("页缓存上限", _row(self.cache_slider, self.cache_value))

        cache_note = QLabel(
            "已经解码的页放在内存里，翻回上一页就不用重新解码。"
            "调大更顺滑、更占内存；调小到 64 MB 时大约只能记住八张整页。"
        )
        cache_note.setObjectName("Muted")
        cache_note.setWordWrap(True)
        form.addRow("", cache_note)

        self.preload_spin = QSpinBox()
        self.preload_spin.setRange(0, 10)
        self.preload_spin.setValue(int(getattr(s, "comic_preload", 3)))
        self.preload_spin.setSuffix(" 页")
        form.addRow("预读前后", self.preload_spin)

        self.cover_alone_check = QCheckBox("双页模式下封面单独显示")
        self.cover_alone_check.setChecked(bool(getattr(s, "comic_cover_alone", True)))
        form.addRow("", self.cover_alone_check)

        cover_note = QLabel(
            "开启时：封面单独占一屏，之后才是两页一组。"
            "若第 1 页文件名含 cover / 封面 / 000，或前三页都是竖版，"
            "会自动按封面处理，不受这个开关影响。"
        )
        cover_note.setObjectName("Muted")
        cover_note.setWordWrap(True)
        form.addRow("", cover_note)

        self.gutter_spin = QSpinBox()
        self.gutter_spin.setRange(0, 32)
        self.gutter_spin.setValue(int(getattr(s, "comic_gutter", 0)))
        self.gutter_spin.setSuffix(" px")
        form.addRow("双页间距", self.gutter_spin)

        gutter_note = QLabel(
            "两页之间的留白。默认 0——扫描的漫画本身带边距；"
            "只有被裁到边缘的扫描件才需要加大。"
        )
        gutter_note.setObjectName("Muted")
        gutter_note.setWordWrap(True)
        form.addRow("", gutter_note)

        self.binding_combo = QComboBox()
        for value, label in COMIC_BINDING_CHOICES:
            self.binding_combo.addItem(label, value)
        _select_data(self.binding_combo, getattr(s, "comic_binding", "rtl"))
        form.addRow("装订方向", self.binding_combo)

        # CBR/RAR 需要一个外部解压程序（WinRAR 的 UnRAR.exe 或 7-Zip 的 7z.exe）。
        # 它不是本项目的依赖，所以这一行显示"找到了什么"，而不是让用户手填路径——
        # 猜路径是 rar.py 的事，用户只要能看到结果和重新检测就够了。
        self.rar_status = QLabel()
        self.rar_status.setObjectName("Muted")
        self.rar_status.setWordWrap(True)
        self.rar_recheck_button = QPushButton("重新检测")
        self.rar_recheck_button.setObjectName("SmallButton")
        self.rar_recheck_button.clicked.connect(self._on_recheck_rar)
        form.addRow("CBR/RAR 解压", _row(self.rar_status, self.rar_recheck_button))
        self._show_rar_status(getattr(s, "unrar_path", ""), getattr(s, "unrar_kind", ""))

        rar_note = QLabel(
            "CBR/RAR 要用 WinRAR 或 7-Zip 解压，本程序不附带这两个。"
            "装了其中一个之后点「重新检测」，或者重开这本书。"
        )
        rar_note.setObjectName("Muted")
        rar_note.setWordWrap(True)
        form.addRow("", rar_note)

        return box

    def _show_rar_status(self, path: str, kind: str) -> None:
        """One line describing the cached tool, or that there is none."""
        if path:
            name = {"unrar": "WinRAR", "7z": "7-Zip"}.get(kind, "解压程序")
            self.rar_status.setText(f"已找到：{name}")
            self.rar_status.setToolTip(path)
        else:
            self.rar_status.setText("没找到，打开 CBR/RAR 时会提示")
            self.rar_status.setToolTip("")

    def _on_recheck_rar(self) -> None:
        """Probe now, without waiting for a save, and show what was found.

        Deliberately writes nothing to `Settings`: the dialog's contract is that
        `apply_to()` is the only writer, and a probe result that is thrown away by
        "取消" would be a second, hidden source of truth. The button therefore only
        updates the label; pressing 确定 is what persists it (via `apply_to`, which
        re-reads the same module cache).
        """
        found = rar.find(force=True)
        self._rar_probe = found
        self._show_rar_status(found.path if found else "", found.kind if found else "")
        if found is None:
            self.rar_status.setText("还是没找到，确认装好之后再试一次")

    def _on_cache_mb(self, value: int) -> None:
        self.cache_value.setText(f"{int(value)} MB")

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
        self.tray_check.toggled.connect(self._on_recovery_toggled)
        form.addRow("", self.tray_check)

        form.addRow(self._stealth_block(s))
        # 红字在块外：块整组禁用时它仍然亮着，因为它是"为什么禁用"的说明。
        form.addRow("", self.stealth_note)

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

    def _stealth_block(self, s: Settings) -> QWidget:
        """隐私模式：藏在"鼠标移出隐身"下面，因为它们是同一个问题的两档答案。

        故意**不是一个 QGroupBox**：这一页的版式约定是"一页一张卡片"（见
        `test_each_page_holds_exactly_its_own_group`），嵌套的分组框会破坏它。用一个小
        标题 + 缩进的表单块表达"从属关系"，视觉上就是分组。

        这里**只有两个键是可配的**：模式本身是会话状态，不落盘，所以没有复选框——
        一个"保存为开"的开关会在下次启动时把一个窗口藏起来，而在快捷键注册失败、托
        盘又被关掉的那台机器上，那个窗口就再也回不来了。

        两个键分工不同，别把它们的校验混在一起：
        * 「快捷键」是**出口**，要 RegisterHotKey，所以必须带一个普通键。
        * 「唤醒键」是**入口**，只轮询 GetAsyncKeyState，不注册，所以可以只是修饰键。
        """
        block = QWidget()
        outer = QVBoxLayout(block)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(4)
        heading = QLabel("隐私模式")
        heading.setObjectName("SubHeading")
        outer.addWidget(heading)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(6)

        self.stealth_edit = HotkeyEdit(s.stealth_mode_key)
        self.hotkey_edits["stealth_mode_key"] = self.stealth_edit
        self.stealth_edit.changed.connect(self._on_hotkeys_changed)
        form.addRow("快捷键", self.stealth_edit)

        self.wake_edit = HotkeyEdit(s.stealth_wake_key, wake=True)
        self.wake_edit.changed.connect(self._on_hotkeys_changed)
        form.addRow("唤醒键", self.wake_edit)

        wake_what = QLabel(
            "唤醒键要一直**按住**才算数：按住它、再把鼠标移进隐藏前的窗口范围，窗口才"
            "会显现；松开后鼠标还在窗口里就继续可读，一旦移出立即隐形，再移进来不会自"
            "己出现（必须重新按住唤醒键再移入）。它不注册全局热键，只在本机轮询，所以"
            "可以只写修饰键（默认 Ctrl+Shift）。留空等于放弃唤醒：隐私模式还能进，但只"
            "剩托盘和老板键能把窗口找回来。"
        )
        wake_what.setObjectName("Muted")
        wake_what.setWordWrap(True)
        form.addRow("", wake_what)

        what = QLabel(
            "按此快捷键可以完全隐藏窗口（真隐藏，不是把透明度降到 0），适合有他人在场"
            "时使用。"
        )
        what.setObjectName("Muted")
        what.setWordWrap(True)
        form.addRow("", what)

        # 用户第一反应是「Ctrl+Shift+L 按下去和老板键一模一样」——因为**确实一样**：
        # 两条路径都走 hide()，安全性没有差别。唯一的差别是托盘那点标记，而 Windows 11
        # 默认把新图标塞进 ^ 溢出区，红点常常根本看不见。这里把话说在前面，免得用户
        # 以为是键没生效（本机实测：两次隐藏后窗口可见性、标题、任务栏标志完全相同，
        # 只有托盘图标像素哈希与 tooltip 变了）。
        self.stealth_boss_note = QLabel(
            "它和老板键是同一档隐藏（都是把窗口真收起来），区别只在「你怎么知道自己在"
            "哪一档」：隐私模式开着时托盘图标右上角多一个红点，鼠标停在上面会提示怎么"
            "解锁。老板键也能解隐私模式；反过来，老板键藏着时按隐私模式快捷键会把它顶"
            "掉、直接进隐私模式。"
        )
        self.stealth_boss_note.setObjectName("Muted")
        self.stealth_boss_note.setWordWrap(True)
        form.addRow("", self.stealth_boss_note)

        # 用户会被"按了没反应"骗一次：Windows 对一次按键投递两条热键消息，MiniReader
        # 把 60 ms 内的重复触发合并成一次，所以同一拍内连按两下看起来像没生效。
        # 数字直接取 DEBOUNCE_MS，避免文档与实现对不上。
        self.stealth_interval_note = QLabel(
            f"连按两次请间隔 0.1 秒以上：系统对一次按键会投递两条热键消息，"
            f"MiniReader 会把 {DEBOUNCE_MS} 毫秒内的重复触发合并成一次，"
            f"所以按得太快时第二下会被当成同一次按键。"
        )
        self.stealth_interval_note.setObjectName("Muted")
        self.stealth_interval_note.setWordWrap(True)
        form.addRow("", self.stealth_interval_note)

        warn = QLabel(
            "【重要提示】隐私模式是临时状态，不会持久化：重启 MiniReader 后默认关闭。"
            "窗口隐藏后，托盘菜单的「显示窗口」也可以恢复。"
        )
        warn.setObjectName("Muted")
        warn.setWordWrap(True)
        form.addRow("", warn)

        # 联动提示（红色）。两种情况：没有任何一种"回家路"，或者和老板键撞了键。
        # 它**故意留在被禁用的那一块之外**（由 _window_group 单独 addRow）：整组变灰
        # 时 QLabel 会跟着变灰，而这条红字正是变灰的**理由**，灰掉就没人看见理由了。
        self.stealth_note = QLabel("")
        self.stealth_note.setObjectName("Warn")
        self.stealth_note.setWordWrap(True)
        outer.addLayout(form)

        # 存一份引用：_on_recovery_toggled / _on_hotkeys_changed 要按当前状态整块启停。
        self.stealth_widget = block
        return block

    def _hotkeys_group(self, s: Settings) -> QGroupBox:
        box = QGroupBox("全局快捷键")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        row = 0
        for name, _field, label in HOTKEY_FIELDS:
            if name in self.hotkey_edits:
                # 防偷窥快捷键已经在「窗口」页那个小组里画过了：同一个设置在两页各
                # 出现一次，改一处另一处不会跟着动，用户会以为其中一个没生效。
                continue
            edit = HotkeyEdit(getattr(s, name))
            self.hotkey_edits[name] = edit
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            edit.changed.connect(self._on_hotkeys_changed)
            row += 1
        grid.setColumnStretch(1, 1)

        hint = QLabel(
            "点进输入框后直接按组合键。至少要带一个 Ctrl / Alt / Shift / Win，"
            "单独一个按键会抢走全系统的输入（F1–F24 例外）。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        grid.addWidget(hint, row, 0, 1, 2)

        self.hotkey_note = QLabel("")
        self.hotkey_note.setObjectName("Warn")
        grid.addWidget(self.hotkey_note, row + 1, 0, 1, 2)
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

    def _on_theme(self, _index: int) -> None:
        """下拉里挑了主题：立刻预览（不落盘），落盘仍然等"确定"。

        切主题只走主窗的 apply_theme()，和顶栏 🌙 是同一条通道——两处入口的副作用
        因此完全一致（顶栏/底栏/正文选中色/目录高亮条/右键菜单/tooltip 都跟着变）。
        """
        name = _combo_data(self.theme_combo)
        if self._on_theme_cb is not None:
            self._on_theme_cb(name)

    def sync_theme(self, name: str) -> None:
        """把当前主题回灌到下拉（顶栏 🌙 改了主题时由主窗调用）。

        只动控件、**不**触发预览回调：主题已经是那个了，再切一次是白跑一趟，而且会让
        "取消"的回滚逻辑看到一次无意义的状态变化。
        """
        combo = getattr(self, "theme_combo", None)
        if combo is None:
            return
        was = combo.blockSignals(True)
        _select_data(combo, name)
        combo.blockSignals(was)

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
        # 唤醒键不在 HOTKEY_FIELDS 里（它不注册，见 storage.WAKE_FIELDS），但"写错了"
        # 一样得挡住「确定」：写错的唤醒键在运行时会被当成空串，隐私模式就永远唤不醒。
        wake = getattr(self, "wake_edit", None)
        if wake is not None and wake.problem():
            problems.append(f"{WAKE_FIELDS[0][1]}：{wake.problem()}")
        self.hotkey_note.setText("；".join(problems))
        # 隐私模式那一组有自己的红字：它关心的是"藏了还能不能回来"，不是"有没有重复"。
        self._update_stealth_note()
        # Blocking OK rather than silently dropping a bad binding on save: clearing a
        # field the user did not mean to clear is worse than making them look at it.
        ok = getattr(self, "_ok_button", None)
        if ok is not None:
            ok.setEnabled(not problems)
        # 任何一次改动都让"键位已保存"作废：绿字还挂着、框里的值却已经不是那份存档，
        # 是最容易骗到人的状态。
        self._saved_ok = False
        self._set_save_note("")

    def _on_recovery_toggled(self, _checked: bool = False) -> None:
        """托盘复选框动了：防偷窥那组的"至少一种恢复方式"结论要跟着重算。"""
        self._update_stealth_note()

    def _update_stealth_note(self) -> None:
        """防偷窥小组的联动状态：启停整组 + 红字说明理由。

        规则：托盘、防偷窥快捷键、老板键，三者有其一就够——都是能看见、能按的回家路。
        一条都没有时整组禁用，因为运行时也会拒绝隐藏（MiniWindow.can_stealth_hide），
        让界面和实际行为一致，而不是让用户以为配好了。

        整组禁用不会把人困住：托盘复选框在**组外**，把它打开就能回到可用状态。
        """
        edit = self.hotkey_edits.get("stealth_mode_key")
        if edit is None or not hasattr(self, "stealth_widget"):
            return
        stealth = edit.spec()
        boss = self.hotkey_edits["boss_key"].spec()
        tray = self.tray_check.isChecked()
        problems = []
        if not tray and not stealth and not boss:
            problems.append("至少需要一种恢复方式：打开托盘图标，或给隐私模式绑定快捷键")
        if stealth and boss and format_hotkey(stealth) == format_hotkey(boss):
            problems.append("隐私模式快捷键与老板键冲突：按下去分不清是要哪一种隐藏")
        wake = getattr(self, "wake_edit", None)
        if wake is not None and not wake.spec():
            # 不拦保存（托盘和老板键仍能把窗口找回来），但必须说清楚：这不是"没有
            # 副作用"的空配置。红字在整块之外，块被禁用时也看得见。
            problems.append(
                "未设置唤醒键：隐私模式能进但无法唤醒，托盘和老板键仍可恢复窗口"
            )
        self.stealth_note.setText("；".join(problems))
        # 没有话要说时它不该占着一行高度（否则小组和「窗口置顶」之间空出一块）。
        self.stealth_note.setVisible(bool(problems))
        self.stealth_widget.setEnabled(bool(tray or stealth or boss))

    # ---- 热键冲突检测（只在内部去重，不试系统占用） --------------------

    def _hotkey_labels(self) -> dict[str, str]:
        """字段名 -> 给人看的名字。两张表合起来：唤醒键不在 HOTKEY_FIELDS 里。"""
        labels = {name: label for name, _field, label in HOTKEY_FIELDS}
        labels.update({name: label for name, label in WAKE_FIELDS})
        return labels

    def _current_hotkeys(self) -> dict[str, str]:
        """框里当前的全部热键写法，含唤醒键。"""
        specs = {
            name: self.hotkey_edits[name].spec()
            for name, _field, _label in HOTKEY_FIELDS
        }
        wake = getattr(self, "wake_edit", None)
        for name, _label in WAKE_FIELDS:
            specs[name] = wake.spec() if wake is not None else ""
        return specs

    def _conflict_rows(self) -> list[tuple[str, str, str]]:
        """冲突小窗口要列出的每一行，顺序与设置页一致：先 HOTKEY_FIELDS 再唤醒键。"""
        labels = self._hotkey_labels()
        return [
            (name, labels[name], spec)
            for name, spec in self._current_hotkeys().items()
        ]

    def _remember_hotkeys(self) -> None:
        """把当前写法记成"已保存"的基准，供下一次算改动集合。"""
        self._hotkey_baseline = self._current_hotkeys()

    def _changed_hotkeys(self) -> dict[str, str]:
        """相对上次保存，这次真正动过的键。冲突检测只关心它们。"""
        current = self._current_hotkeys()
        return {
            name: spec
            for name, spec in current.items()
            if self._hotkey_baseline.get(name) != spec
        }

    def _roll_back_hotkeys(self) -> None:
        """把**所有**热键退回上次保存的写法，再重新校验一遍。

        全退回比"只退改过的"简单且等价：没改过的本来就等于基准值。锁定决策 5 要求
        "本次被修改的键全部回滚"，这里保守到把没改的也一起对齐。
        """
        for name, _field, _label in HOTKEY_FIELDS:
            self.hotkey_edits[name].set_spec(self._hotkey_baseline.get(name, ""), notify=False)
        wake = getattr(self, "wake_edit", None)
        if wake is not None:
            for name, _label in WAKE_FIELDS:
                wake.set_spec(self._hotkey_baseline.get(name, ""), notify=False)
        self._on_hotkeys_changed()

    def _set_save_note(self, text: str) -> None:
        """绿字"键位已保存"。空串等于撤掉（连行高一起撤）。"""
        note = getattr(self, "save_note", None)
        if note is None:
            return
        note.setText(text)
        note.setVisible(bool(text))

    def _apply_conflict_edit(self, name: str, spec: str) -> None:
        """冲突小窗口里改了一行：立刻同步回设置页那个编辑器。

        同步用 notify=True：设置页的校验、确定按钮状态、红字都得跟着重算，否则回到
        设置页看到的还是旧结论。
        """
        edit = self.hotkey_edits.get(name)
        if edit is not None:
            edit.set_spec(spec)
            return
        wake = getattr(self, "wake_edit", None)
        if wake is not None and name in _WAKE_NAMES:
            wake.set_spec(spec)

    def accept(self) -> None:  # noqa: N802
        """「确定」：内部去重 -> 冲突就回滚并弹小窗口，通过就保存但**不关窗**。

        锁定决策 5/6：
        * 冲突：本次改过的键全部退回，弹出 ConflictDialog；它的确定只关闭，不检测。
        * 通过：立刻发出 saved（主窗接到写回+落盘），底部绿字"键位已保存"，对话框
          继续开着，用户还能接着改别的页。
        """
        # 校验没通过时按钮是灰的，但回车和"默认按钮"仍可能走到这里：写错的键在运行时
        # 会被当成空串悄悄丢掉，比什么都不做更坏（用户以为存上了）。
        if not self._ok_button.isEnabled():
            return
        changed = self._changed_hotkeys()
        conflicts = find_conflicts(
            changed, self._current_hotkeys(), self._hotkey_labels()
        )
        if conflicts:
            self._roll_back_hotkeys()
            self._saved_ok = False
            self._set_save_note("")
            dialog = ConflictDialog(
                self,
                self._conflict_rows(),
                conflicts,
                on_change=self._apply_conflict_edit,
            )
            try:
                exec_dialog(dialog)
            finally:
                dialog.deleteLater()
            return
        self._remember_hotkeys()
        self._saved_ok = True
        self._set_save_note("键位已保存")
        self.saved.emit()

    def reject(self) -> None:  # noqa: N802
        """取消 / 右上角 ✕。

        已经成功保存过的话，关窗按"确定"算：主窗在 saved 时就已经把设置写回去并落盘
        了，这里再走"取消"那条会把主题/透明度预览撤回，看起来像保存没生效。
        """
        if self._saved_ok:
            super().accept()
            return
        super().reject()

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
        # 主题也是"窗口外观"这一族的：从下拉取值（只可能落在 THEME_NAMES 内），
        # clamp() 那边照样兜底校验一次。
        s.theme = _combo_data(self.theme_combo)
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
        # 唤醒键不是全局热键，所以不在 HOTKEY_FIELDS 里；但它同样要落盘。
        wake = getattr(self, "wake_edit", None)
        if wake is not None:
            s.stealth_wake_key = wake.spec()
        # 漫画与 PDF。四个枚举都从下拉框里取值，取值只可能落在合法集合内——
        # clamp() 那边的校验因此是兜底而不是主路径。
        s.pdf_default_kind = _combo_data(self.pdf_kind_combo)
        s.comic_default_zoom = _combo_data(self.comic_zoom_combo)
        s.comic_default_mode = _combo_data(self.comic_mode_combo)
        s.comic_binding = _combo_data(self.binding_combo)
        s.comic_cache_mb = int(self.cache_slider.value())
        s.comic_preload = int(self.preload_spin.value())
        s.comic_cover_alone = bool(self.cover_alone_check.isChecked())
        s.comic_gutter = int(self.gutter_spin.value())
        # CBR/RAR 的解压程序缓存。只有在这个对话框里按过"重新检测"才写：没按过时
        # `_rar_probe` 是 None，此时保留磁盘上原来的答案，免得用户只是来改字号、
        # 点一下确定，就把上次探测的结果抹掉。
        if self._rar_probe is not None:
            s.unrar_path = self._rar_probe.path if self._rar_probe else ""
            s.unrar_kind = self._rar_probe.kind if self._rar_probe else ""
            s.unrar_checked = True
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
        # 主题也回到出厂值。这里**不**触发预览回调：和"恢复默认"的其它控件一样，界面
        # 要等按下"确定"才真变（主窗 apply_preferences 会带过去）。所以走 sync_theme()
        # ——它 blockSignals 之后再改下拉；直接 _select_data 会甩出一次
        # currentIndexChanged，把"恢复默认"变成一次立即预览。
        self.sync_theme(fresh.theme)
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
        wake = getattr(self, "wake_edit", None)
        if wake is not None:
            wake.set_spec(fresh.stealth_wake_key)
        self._on_hotkeys_changed()
        _select_data(self.pdf_kind_combo, fresh.pdf_default_kind)
        _select_data(self.comic_zoom_combo, fresh.comic_default_zoom)
        _select_data(self.comic_mode_combo, fresh.comic_default_mode)
        _select_data(self.binding_combo, fresh.comic_binding)
        self.cache_slider.setValue(fresh.comic_cache_mb)
        self.preload_spin.setValue(fresh.comic_preload)
        self.cover_alone_check.setChecked(fresh.comic_cover_alone)
        self.gutter_spin.setValue(fresh.comic_gutter)
        # 解压程序不是偏好而是这台机器的事实：恢复默认不假装它没装过。标签回到
        # 磁盘上那个答案，`_rar_probe` 清空，于是确定时不会写 unrar_* 三个字段。
        self._rar_probe = None
        self._show_rar_status(
            getattr(self._settings, "unrar_path", ""), getattr(self._settings, "unrar_kind", "")
        )
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


def _select_data(combo, value: str) -> None:
    """Point a combo at the row whose data is `value`.

    Falls back to row 0 rather than leaving the combo unselected: an unknown value
    in a hand-edited settings.json would otherwise show a blank dropdown, and
    `apply_to()` reads the combo back - so a blank would silently become whatever
    the first row is anyway. Doing it here at least shows the user that answer.
    """
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _combo_data(combo) -> str:
    data = combo.currentData()
    return str(data) if data is not None else ""


def _row(*widgets) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    return holder
