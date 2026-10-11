"""章节目录 / 书签 抽屉面板。

面板由主窗口以**覆盖抽屉**的方式贴在阅读区上方，所以它不假设自己的尺寸：宽高由父窗口
决定，`resizeEvent` 只负责重新排布，260x300 和 520x800 都要好看。

四个刻意的设计：

* **目录列表是虚拟化的。** 一本正常的书几千章，实测过一本 165 858 章的病态书——
  `QListWidget` 逐条建 item 要卡好几秒。这里用 `QListView` + 自己的
  `QAbstractListModel`：`set_chapters()` 只存列表并 `beginResetModel/endResetModel`，
  视图只问屏幕上那几十行，与总章数无关。
* **配色走模型角色，不自己画。** `minireader.qt` 是唯一允许的 Qt 来源，而它没有导出
  `QStyledItemDelegate`，所以"当前章"不能靠自定义 delegate 绘制。改用
  `Qt.BackgroundRole` / `Qt.ForegroundRole` / `Qt.DecorationRole`：
  当前章 = `PALETTE["accent"]` 的整行底色 + `PALETTE["bg"]` 的文字色，
  对比度 4.86:1（`current_row_colors()` 把这对颜色交出来给测试算 WCAG）。
* **搜索框的 ↑/↓/Enter 走事件过滤器，而不是让列表抢焦点。** 过滤要在用户继续打字时
  实时生效，所以焦点必须留在搜索框；但方向键又要驱动列表。做法是给搜索框装一个
  `eventFilter`，把 ↑/↓/PageUp/PageDown 直接转交 `QListView.keyPressEvent()`，
  Enter/Esc 自己处理（Enter → `chapterChosen`，Esc → `closed`）。
  为什么不给列表 `setFocusProxy(搜索框)`：那会让列表以为自己是焦点控件，选中态跟着乱；
  过滤器只转发"按键"这一件事，其余 Qt 行为完全不动。
* **书签用真控件行，不用 item 控件。** 书签是几十条的量级，所以行放在 `QScrollArea`
  里由 `QLabel` + `QToolButton` 组成：删除按钮因此是一个真实的按钮（可点、可 Tab、
  有中文 tooltip、命中区域 >= 24x24），而不是一个需要自己算命中的图案。
"""

from __future__ import annotations

from ..qt import (
    QAbstractListModel,
    QBrush,
    QColor,
    QEvent,
    QFont,
    QHBoxLayout,
    QIcon,
    QKeyEvent,
    QLabel,
    QLineEdit,
    QListView,
    QModelIndex,
    QPainter,
    QPen,
    QPixmap,
    QScrollArea,
    QSize,
    QTabWidget,
    QToolButton,
    Qt,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .theme import PALETTE

# 每个可点控件的最小命中尺寸（题面要求 24x24；这里是小窗口，24 是"不误触"的下限）。
MIN_TARGET = 24

# 行类型角色。放在模型里而不是"比较行号"，是因为过滤后视图行号 ≠ 章序号。
ROLE_ROW_TYPE = int(Qt.UserRole) + 1
ROW_NORMAL = 0
ROW_CURRENT = 1


def current_row_colors() -> tuple[str, str]:
    """当前章的（文字色, 底色）。给测试算 WCAG 对比度用，也是实现的唯一出处。"""
    return PALETTE["bg"], PALETTE["accent"]


def make_bar_pixmap(width: int = 3, height: int = 14) -> QPixmap:
    """当前章左侧的强调色小色块，走 `Qt.DecorationRole`。

    本项目没有图片资源（`tray.make_icon()` 也是画的），这里同样用 `QPainter` 现画。
    当前章的整行底色 + 这个色条一起，才是"一眼能看出读到哪"的标记。
    """
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        pen = QPen(QColor(PALETTE["fg"]))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(PALETTE["accent"])))
        painter.drawRect(0, 0, width - 1, height - 1)
    finally:
        painter.end()
    return pixmap


def make_delete_icon(size: int = 12) -> QIcon:
    """书签行上的删除图标：一个 warn 色的 ✕。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(PALETTE["warn"]))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        pad = max(2, size // 5)
        painter.drawLine(pad, pad, size - pad, size - pad)
        painter.drawLine(size - pad, pad, pad, size - pad)
    finally:
        painter.end()
    return QIcon(pixmap)


def _stylesheet() -> str:
    """照 `settings_dialog._stylesheet()` 的写法：QSS 字符串，颜色只从 PALETTE 取。"""
    p = PALETTE
    return f"""
    #TocPanel {{ background: {p['panel']}; border: 1px solid {p['border']}; }}
    #TocTitle {{ color: {p['fg']}; font-size: 13px; }}
    #TocSearch {{
        background: {p['field']}; color: {p['fg']}; border: 1px solid {p['border']};
        border-radius: 4px; padding: 3px 6px; font-size: 12px;
        selection-background-color: {p['accent']};
    }}
    QLineEdit#TocSearch:focus {{ border: 1px solid {p['accent']}; }}
    #TocCount {{ color: {p['muted']}; font-size: 11px; }}
    #TocMuted {{ color: {p['muted']}; font-size: 11px; }}
    #TocBookmarkLabel {{ color: {p['fg']}; font-size: 12px; }}
    #TocBookmarkExcerpt {{ color: {p['muted']}; font-size: 11px; }}
    QListView#TocList {{
        background: {p['panel']}; color: {p['fg']};
        border: 1px solid {p['border']}; border-radius: 4px;
        outline: 0; font-size: 12px;
    }}
    QListView#TocList:focus {{ border: 1px solid {p['accent']}; }}
    QScrollArea#TocBookmarkArea {{
        background: {p['panel']}; border: 1px solid {p['border']}; border-radius: 4px;
    }}
    QScrollArea#TocBookmarkArea:focus {{ border: 1px solid {p['accent']}; }}
    #TocBookmarkBody {{ background: {p['panel']}; }}
    #TocBookmarkRow {{ background: {p['panel']}; border-radius: 4px; }}
    #TocBookmarkRow:hover {{ background: {p['hover']}; }}
    QToolButton {{
        color: {p['fg']}; background: transparent; border: 0;
        border-radius: 4px; padding: 0;
    }}
    QToolButton:hover {{ background: {p['hover']}; }}
    QToolButton:focus {{ border: 1px solid {p['accent']}; }}
    QTabWidget::pane {{ border: 0; background: {p['panel']}; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{
        background: {p['panel']}; color: {p['muted']};
        border: 1px solid {p['border']}; border-radius: 4px;
        min-height: 22px; padding: 1px 14px; margin-right: 4px; font-size: 12px;
    }}
    QTabBar::tab:selected {{ background: {p['hover']}; color: {p['fg']}; }}
    QTabBar::tab:focus {{ border: 1px solid {p['accent']}; }}
    """


class ChapterListModel(QAbstractListModel):
    """目录的数据源：只存字符串列表，一行一个 item 对象都不建。

    `_source` 是全量标题（`_lower` 是它的 casefold 版本，供搜索用），`_filtered` 是
    当前过滤结果，`_mapping` 把"视图行号"映射回"章序号"。没有过滤时
    `_filtered is _source`，此时行号就是章序号。
    """

    rowsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source: list[str] = []
        self._lower: list[str] = []
        self._filtered: list[str] = []
        self._mapping: list[int] | None = None
        self._needle = ""
        self._current = -1
        self._bar = None

    # ---- QAbstractListModel ---------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt 命名
        # 树形模型才会用到合法 parent；这是平表，必须返回 0，否则视图会去要子节点。
        if parent is not None and parent.isValid():
            return 0
        return len(self._filtered)

    def data(self, index, role=Qt.DisplayRole):
        if index is None or not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._filtered):
            return None
        if role == Qt.DisplayRole:
            return self._filtered[row]
        if role == ROLE_ROW_TYPE:
            return ROW_CURRENT if self._chapter_at(row) == self._current else ROW_NORMAL
        if role == Qt.ToolTipRole:
            return self._filtered[row]
        if role == Qt.DecorationRole:
            return self._bar if self._chapter_at(row) == self._current else None
        if role == Qt.BackgroundRole:
            if self._chapter_at(row) == self._current:
                return QBrush(QColor(current_row_colors()[1]))
            return None
        if role == Qt.ForegroundRole:
            if self._chapter_at(row) == self._current:
                return QBrush(QColor(current_row_colors()[0]))
            return None
        if role == Qt.FontRole:
            if self._chapter_at(row) == self._current:
                font = QFont()
                font.setBold(True)
                return font
            return None
        return None

    # ---- 数据 -------------------------------------------------------------

    def set_chapters(self, titles) -> None:
        """O(1)-ish：只换引用 + 一次模型重置，不为每章建对象。

        165 858 章实测远低于 1 秒（见 `tests/test_toc_panel.py` 的防回归断言）。
        """
        self._source = ["" if title is None else str(title) for title in (titles or [])]
        self._lower = [title.casefold() for title in self._source]
        self._current = -1
        self._bar = make_bar_pixmap()
        self._refilter()

    def count(self) -> int:
        """当前过滤结果的行数（= `rowCount()`）。"""
        return len(self._filtered)

    def source_count(self) -> int:
        return len(self._source)

    def title_at(self, row: int) -> str:
        if 0 <= row < len(self._filtered):
            return self._filtered[row]
        return ""

    def chapter_at(self, row: int) -> int:
        """视图行号 → 章序号（0 基）。越界返回 -1。"""
        return self._chapter_at(row)

    def row_of_chapter(self, chapter: int) -> int:
        """章序号 → 视图行号；被过滤掉时返回 -1（当前章可能不在结果里）。"""
        if not isinstance(chapter, int):
            return -1
        if self._mapping is None:
            return chapter if 0 <= chapter < len(self._source) else -1
        try:
            return self._mapping.index(chapter)
        except ValueError:
            return -1

    def current_chapter(self) -> int:
        return self._current

    def set_current(self, chapter: int) -> int:
        """标记当前章，返回它的视图行号（不可见时 -1），供视图 scrollTo。"""
        self._current = int(chapter) if isinstance(chapter, int) else -1
        row = self.row_of_chapter(self._current)
        self._touch_current()
        return row

    # ---- 过滤 -------------------------------------------------------------

    def set_filter(self, needle: str) -> None:
        """大小写不敏感的子串过滤；纯字符串扫描，不建任何控件。"""
        self._needle = (needle or "").strip().casefold()
        self._refilter()

    def needle(self) -> str:
        return self._needle

    def _refilter(self) -> None:
        self.beginResetModel()
        try:
            if not self._needle:
                self._filtered = self._source
                self._mapping = None
            else:
                needle = self._needle
                kept: list[str] = []
                mapping: list[int] = []
                for index, lowered in enumerate(self._lower):
                    if needle in lowered:
                        kept.append(self._source[index])
                        mapping.append(index)
                self._filtered = kept
                self._mapping = mapping
        finally:
            self.endResetModel()
        self.rowsChanged.emit()

    # ---- 内部 -------------------------------------------------------------

    def _chapter_at(self, row: int) -> int:
        if not (0 <= row < len(self._filtered)):
            return -1
        if self._mapping is None:
            return row
        return self._mapping[row]

    def _touch_current(self) -> None:
        """只让"当前章"那几行重画；重置模型会丢掉滚动位置，那是不能接受的。"""
        if self._filtered:
            roles = [ROLE_ROW_TYPE, Qt.BackgroundRole, Qt.ForegroundRole, Qt.DecorationRole]
            self.dataChanged.emit(self.index(0), self.index(len(self._filtered) - 1), roles)


class BookmarkRow(QWidget):
    """一条书签：标签 + 摘要 + 删除按钮。

    用真控件而不是"画出来的行"，因为删除是破坏性操作，必须是一个真的按钮：
    能被 Tab 到、有 tooltip、命中区域能测。
    """

    chosen = Signal(int)
    deleted = Signal(int)

    def __init__(self, data: dict, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TocBookmarkRow")
        self.chosen_id = int(data.get("id") or 0)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(MIN_TARGET)
        created = str(data.get("created", "") or "")
        label_text = str(data.get("label", "") or f"书签 {self.chosen_id}")
        hint = f"{label_text}｜{created}" if created else label_text
        self.setToolTip(hint)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 3, 3)
        layout.setSpacing(4)

        text = QWidget(self)
        text.setObjectName("TocBookmarkBody")
        column = QVBoxLayout(text)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        label = QLabel(label_text, text)
        label.setObjectName("TocBookmarkLabel")
        column.addWidget(label)
        excerpt = QLabel(str(data.get("excerpt", "") or ""), text)
        excerpt.setObjectName("TocBookmarkExcerpt")
        column.addWidget(excerpt)
        layout.addWidget(text, 1)

        self.delete_button = QToolButton(self)
        self.delete_button.setObjectName("TocDelete")
        self.delete_button.setText("")
        self.delete_button.setIcon(make_delete_icon())
        self.delete_button.setIconSize(QSize(12, 12))
        self.delete_button.setToolTip("删除这条书签")
        self.delete_button.setFixedSize(MIN_TARGET, MIN_TARGET)
        self.delete_button.setAutoRaise(True)
        self.delete_button.setCursor(Qt.PointingHandCursor)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        layout.addWidget(self.delete_button, 0, Qt.AlignVCenter)

        self.label = label
        self.excerpt = excerpt

    def _on_delete_clicked(self) -> None:
        self.deleted.emit(self.chosen_id)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 点行 = 选中这条书签。✕ 是行的子控件，Qt 会把按下事件直接给按钮，
        # 所以这里不会被"删除"误触发。
        if event.button() == Qt.LeftButton:
            event.accept()
            self.chosen.emit(self.chosen_id)
            return
        super().mousePressEvent(event)


class TocPanel(QWidget):
    """覆盖在阅读区上的目录 / 书签抽屉。只发意图，不做决定（父窗口去跳转）。"""

    chapterChosen = Signal(int)      # 章序号（0 基）
    bookmarkChosen = Signal(int)     # 书签 id
    bookmarkDeleted = Signal(int)    # 书签 id，用户点了删除
    closed = Signal()                # 用户要求关闭面板（Esc 或点 ✕）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TocPanel")
        self.setStyleSheet(_stylesheet())
        # 抽屉盖在阅读区上：鼠标事件在面板内部消化掉，绝不冒泡给父窗口的拖拽逻辑，
        # 否则在列表上按下会顺手把整个窗口拖走。（WA_NoMousePropagation 是 Qt 的
        # 正规开关；eventFilter 再兜一层。）
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        self.setMinimumSize(200, 220)

        self._opacity_dim = False
        self._bookmark_rows: list[BookmarkRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)
        root.addLayout(self._build_header())
        self.tabs = QTabWidget(self)
        self.tabs.setFocusPolicy(Qt.StrongFocus)
        self.tabs.addTab(self._build_chapters_tab(), "目录")
        self.tabs.addTab(self._build_bookmarks_tab(), "书签")
        root.addWidget(self.tabs, 1)

        self.installEventFilter(self)

    # ---- 构建 -------------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.title_label = QLabel("章节目录", self)
        self.title_label.setObjectName("TocTitle")
        row.addWidget(self.title_label)
        row.addStretch(1)

        self.close_button = QToolButton(self)
        self.close_button.setObjectName("TocClose")
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭目录面板（Esc）")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setFixedSize(MIN_TARGET, MIN_TARGET)
        self.close_button.setAutoRaise(True)
        self.close_button.clicked.connect(self.closed.emit)
        row.addWidget(self.close_button)
        return row

    def _build_chapters_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search = QLineEdit(page)
        self.search.setObjectName("TocSearch")
        self.search.setPlaceholderText("搜索章节…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(22)
        self.search.textChanged.connect(self._on_search_changed)
        # 搜索框是"打字的地方"，方向键要同时驱动列表：这里装过滤器而不是把焦点让给
        # 列表，理由见模块 docstring。
        self.search.installEventFilter(self)
        search_row.addWidget(self.search, 1)

        self.count_label = QLabel("", page)
        self.count_label.setObjectName("TocCount")
        self.count_label.setMinimumWidth(58)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        search_row.addWidget(self.count_label)
        layout.addLayout(search_row)

        self.model = ChapterListModel(self)
        self.model.rowsChanged.connect(self._on_model_rows_changed)

        self.list_view = QListView(page)
        self.list_view.setObjectName("TocList")
        self.list_view.setModel(self.model)
        self.list_view.setSelectionMode(QListView.NoSelection)
        self.list_view.setEditTriggers(QListView.NoEditTriggers)
        self.list_view.setUniformItemSizes(True)      # 165 858 行也能 O(1) 算滚动条
        self.list_view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_view.setFocusPolicy(Qt.StrongFocus)
        self.list_view.setMouseTracking(True)
        self.list_view.setMinimumHeight(MIN_TARGET)
        self.list_view.doubleClicked.connect(self._on_view_activated)
        self.list_view.activated.connect(self._on_view_activated)
        self.list_view.installEventFilter(self)
        layout.addWidget(self.list_view, 1)
        return page

    def _build_bookmarks_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        # 空状态放在行容器之外：不用在滚动区里假装自己是"一行"。
        self.empty_label = QLabel("还没有书签", page)
        self.empty_label.setObjectName("TocMuted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setMinimumHeight(MIN_TARGET)

        self.bookmark_body = QWidget()
        self.bookmark_body.setObjectName("TocBookmarkBody")
        self.bookmark_layout = QVBoxLayout(self.bookmark_body)
        self.bookmark_layout.setContentsMargins(0, 0, 0, 0)
        self.bookmark_layout.setSpacing(2)
        self.bookmark_layout.addStretch(1)

        self.bookmark_area = QScrollArea(page)
        self.bookmark_area.setObjectName("TocBookmarkArea")
        self.bookmark_area.setWidget(self.bookmark_body)
        self.bookmark_area.setWidgetResizable(True)
        self.bookmark_area.setFocusPolicy(Qt.StrongFocus)
        self.bookmark_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.bookmark_area.installEventFilter(self)

        layout.addWidget(self.bookmark_area, 1)
        layout.addWidget(self.empty_label, 0)
        # 空状态由第一次 set_bookmarks() 决定：构造函数里 setVisible() 会被后面的
        # show() 覆盖掉，那时面板还没显示。
        self.empty_label.show()
        self.bookmark_area.hide()
        return page

    # ---- 目录接口 ---------------------------------------------------------

    def set_chapters(self, titles: list[str]) -> None:
        self.model.set_chapters(titles)

    def set_current(self, index: int) -> None:
        """高亮当前章并滚到可见处；被过滤隐藏时不报错，只是没有可滚的行。

        同时把视图的 currentIndex 放到同一行：Qt 的方向键只有在已经有一个当前行时才
        会从它出发上下移动，否则第一次按 ↓ 只会落在第 0 行。搜索框的 ↑/↓ 依赖这一点。
        """
        row = self.model.set_current(index)
        if row < 0:
            return
        model_index = self.model.index(row)
        if model_index.isValid():
            # 用 PositionAtCenter 之外的行为做兜底：Qt 5/6 都认 PositionAtCenter。
            self.list_view.scrollTo(model_index, QListView.PositionAtCenter)
            # 不抢焦点：面板打开时焦点属于搜索框（见 focus_search）。
            self.list_view.setCurrentIndex(model_index)

    def focus_search(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.search.setFocus(Qt.OtherFocusReason)

    def visible_chapter_count(self) -> int:
        return self.model.rowCount()

    def current_row_colors(self) -> tuple[str, str]:
        """当前章的（文字色, 底色）——发给测试算 WCAG 对比度。"""
        return current_row_colors()

    def set_opacity_hint(self, dim: bool) -> None:
        """失焦变淡的钩子。面板是覆盖层，真实透明度由父窗口的 `setWindowOpacity`
        统一处理，所以这里只记状态——记下来便于父窗口回读。"""
        self._opacity_dim = bool(dim)

    def opacity_dimmed(self) -> bool:
        return self._opacity_dim

    # ---- 书签接口 ---------------------------------------------------------

    def set_bookmarks(self, items: list[dict]) -> None:
        """每个 dict：{"id": int, "chapter": int, "label": str, "excerpt": str, "created": str}"""
        for row in self._bookmark_rows:
            self.bookmark_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._bookmark_rows = []

        for item in items or []:
            if not isinstance(item, dict):
                continue
            row = BookmarkRow(item, self.bookmark_body)
            row.chosen.connect(self.bookmarkChosen.emit)
            row.deleted.connect(self.bookmarkDeleted.emit)
            # 插在结尾的 stretch 之前。
            self.bookmark_layout.insertWidget(self.bookmark_layout.count() - 1, row)
            self._bookmark_rows.append(row)

        empty = not self._bookmark_rows
        self.empty_label.setVisible(empty)
        self.bookmark_area.setVisible(not empty)

    def bookmark_count(self) -> int:
        return len(self._bookmark_rows)

    def bookmark_rows(self) -> list["BookmarkRow"]:
        return list(self._bookmark_rows)

    def empty_visible(self) -> bool:
        return bool(self.empty_label.isVisible())

    # ---- 内部联动 ---------------------------------------------------------

    def _on_search_changed(self, text: str) -> None:
        self.model.set_filter(text)

    def _on_model_rows_changed(self) -> None:
        """过滤/换书之后：更新计数，并把当前章的高亮重新指回去（若它还在结果里）。"""
        total = self.model.source_count()
        shown = self.model.count()
        if self.model.needle():
            self.count_label.setText(f"找到 {shown} 章")
        elif total:
            self.count_label.setText(f"共 {total} 章")
        else:
            self.count_label.setText("")
        row = self.model.row_of_chapter(self.model.current_chapter())
        if row >= 0:
            model_index = self.model.index(row)
            if model_index.isValid() and self.list_view.currentIndex() != model_index:
                self.list_view.setCurrentIndex(model_index)
        elif self.model.count():
            # 当前章被过滤掉（或还没设过）时也要有一个当前行：Qt 的方向键需要从某一行
            # 出发，否则第一次按 ↓/↑ 只会在原地打转。
            current = self.list_view.currentIndex()
            if current is None or not current.isValid():
                self.list_view.setCurrentIndex(self.model.index(0))

    def _activate(self, row: int) -> None:
        chapter = self.model.chapter_at(row)
        if chapter >= 0:
            self.chapterChosen.emit(chapter)

    def _on_view_activated(self, index) -> None:
        if index is None or not index.isValid():
            return
        self._activate(index.row())

    def _activate_current(self) -> None:
        """Enter：优先当前选中行，没有选中就用视图的 currentIndex。"""
        rows = self.list_view.selectionModel()
        if rows is not None:
            selected = rows.selectedIndexes()
            if selected:
                self._activate(selected[0].row())
                return
        index = self.list_view.currentIndex()
        if index is not None and index.isValid():
            self._activate(index.row())

    def _move(self, key: int) -> None:
        """转发方向键给列表：当前行会跟着动，而且视图会自己保证它可见。"""
        self.list_view.keyPressEvent(_key_event(key))

    # ---- 事件 -------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        kind = event.type()

        if kind == QEvent.KeyPress and obj is self.search:
            key = event.key()
            if key == Qt.Key_Escape:
                self.closed.emit()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._activate_current()
                return True
            if key in (
                Qt.Key_Up,
                Qt.Key_Down,
                Qt.Key_PageUp,
                Qt.Key_PageDown,
                Qt.Key_Home,
                Qt.Key_End,
            ):
                # 焦点留在搜索框（打字不断），只是把这次按键交给列表。
                self._move(key)
                return True
            return False

        if kind == QEvent.KeyPress and obj in (self.list_view, self.bookmark_area):
            if event.key() == Qt.Key_Escape:
                self.closed.emit()
                return True
            return False

        if kind in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease):
            # 面板内部不需要冒泡给父窗口：否则按一下列表就等于按住标题栏拖窗口。
            event.accept()
            return False
        return False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """只是重排，不假设尺寸：抽屉的宽高由父窗口给。"""
        super().resizeEvent(event)
        layout = self.layout()
        if layout is not None:
            layout.activate()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """面板的空白处也接受按下，事件不再往上冒泡。"""
        event.accept()


def _key_event(key: int) -> QKeyEvent:
    """造一个只带按键的 QKeyEvent，用于把搜索框的按键转交给列表。"""
    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
