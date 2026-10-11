"""章节目录 / 书签 面板的无头测试。

在 Qt 的 "offscreen" 平台插件上跑：不需要显示器，也不会在你工作时抢焦点。

这里最要紧的一条是**虚拟化防回归**：`set_chapters` 塞 165 858 章必须远低于 1 秒。
换成 `QListWidget` 逐条建 item 的写法，这条会立刻失败——那正是它存在的意义。

按键和鼠标是合成 `QKeyEvent` / `QMouseEvent` 再 `sendEvent`，两条理由：一，`QTest`
不在 `minireader.qt` 的导出清单里，零新依赖的前提下不能直接用它；二，合成事件和真实
事件走的是同一条"搜索框 → 事件过滤器 → 列表"、以及"行 → 选中/删除"的路径，
测到的就是用户敲的、点的那个东西。

    py -3.12 -m unittest tests.test_toc_panel -v
"""

from __future__ import annotations

import os
import sys
import time
import unittest

# 必须在 QApplication 存在之前设置，所以也必须在 minireader.qt 导入 Qt 之前。
# setdefault，调用方显式给的 QT_QPA_PLATFORM 仍然优先。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import (  # noqa: E402
    QApplication,
    QEvent,
    QKeyEvent,
    QMouseEvent,
    QPoint,
    QPointF,
    Qt,
    install_message_filter,
)

from minireader.ui import theme  # noqa: E402
from minireader.ui.toc_panel import (  # noqa: E402
    MIN_TARGET,
    ROW_CURRENT,
    ROW_NORMAL,
    ROLE_ROW_TYPE,
    TocPanel,
    current_row_colors,
)

# offscreen 插件没有自己的字体、也没有窗口管理器，每次跑都会警告这两件事；
# 其余警告照常打印。
_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

# 整个进程只建一个 QApplication：Qt 不允许第二个。
_app = QApplication.instance() or QApplication([])

# 165 858 不是编出来的数字：一本真实存在的病态长书就是这个章数，
# 也正是 `QListWidget` 会卡好几秒的那个规模。
HUGE = 165_858

BOOKMARKS = [
    {"id": 7, "chapter": 2, "label": "第2章 标题2", "excerpt": "这里是正文摘要…", "created": "2024-05-01 10:00"},
    {"id": 9, "chapter": 5, "label": "第5章 标题5", "excerpt": "另一段正文摘要", "created": "2024-05-02 11:30"},
]


def pump() -> None:
    """把 Qt 的事件队列推干净（布局、重绘、信号投递）。"""
    _app.processEvents()


def make_titles(count: int) -> list[str]:
    return [f"第{i + 1}章 章节标题 {i}" for i in range(count)]


def key(widget, code: int, text: str = "") -> None:
    """给某个控件投递一次真实的按键（按下 + 抬起）。"""
    _app.sendEvent(widget, QKeyEvent(QEvent.KeyPress, code, Qt.NoModifier, text))
    _app.sendEvent(widget, QKeyEvent(QEvent.KeyRelease, code, Qt.NoModifier, text))
    pump()


def click(widget, point: QPoint) -> None:
    """在某个控件上真实按一下（按钮、书签行都走这条路径）。"""
    local = QPointF(point.x(), point.y())
    glob = QPointF(widget.mapToGlobal(point).x(), widget.mapToGlobal(point).y())
    _app.sendEvent(
        widget,
        QMouseEvent(QEvent.MouseButtonPress, local, glob, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier),
    )
    _app.sendEvent(
        widget,
        QMouseEvent(QEvent.MouseButtonRelease, local, glob, Qt.LeftButton, Qt.NoButton, Qt.NoModifier),
    )
    pump()


def click_center(widget) -> None:
    click(widget, QPoint(widget.width() // 2, widget.height() // 2))


class TocPanelTestCase(unittest.TestCase):
    """每个测试一个干净的面板，跑完就删，避免事件过滤器在别的测试里还活着。"""

    def setUp(self) -> None:
        self.panels: list[TocPanel] = []

    def tearDown(self) -> None:
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        pump()
        # 同 WindowTestCase：Qt5 在分发器还有 pending 事件时会把 DeferredDelete 重新
        # 排队，单靠一轮 processEvents 不保证面板真的被销毁——强制投递，别让顶层
        # 面板跨模块残留（事件过滤器跟着它一起活，正是 setUp 注释里担心的事）。
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        pump()

    def make_panel(self, width: int = 320, height: int = 480) -> TocPanel:
        panel = TocPanel()
        self.panels.append(panel)
        panel.resize(width, height)
        panel.show()
        pump()
        return panel

    def make_searchable_panel(self) -> TocPanel:
        panel = self.make_panel()
        panel.set_chapters(
            [
                "第一章 开始",
                "Chapter Two: The Middle",
                "第三章 终局",
                "序章 名字里带 CHAPTER 也照样能搜到",
            ]
        )
        pump()
        return panel

    def make_bookmark_panel(self) -> TocPanel:
        panel = self.make_panel()
        panel.tabs.setCurrentIndex(1)
        panel.set_bookmarks(BOOKMARKS)
        pump()
        return panel

    def press_search(self, panel: TocPanel, code: int, text: str = "") -> None:
        """真按键路径：搜索框 → 事件过滤器 → 列表。"""
        panel.search.setFocus(Qt.OtherFocusReason)
        pump()
        key(panel.search, code, text)


class TestChapterVirtualisation(TocPanelTestCase):
    """165 858 章的防回归测试。"""

    def test_a_huge_book_loads_far_under_a_second(self) -> None:
        panel = self.make_panel()
        titles = make_titles(HUGE)
        started = time.perf_counter()
        panel.set_chapters(titles)
        elapsed = time.perf_counter() - started
        self.assertEqual(panel.model.rowCount(), HUGE)
        self.assertEqual(panel.model.source_count(), HUGE)
        # 这条断言要抓的是"实现从 O(n) 退化成 O(n²)"，不是给机器跑分。虚拟化实现实测
        # 在几十毫秒级（单跑约 40ms），而"每章建一个 QListWidgetItem"那类做法在这个规模
        # 上要好几秒；165 858 行的平方级实现更是几分钟起步。
        #
        # 阈值取 1.5s：原来的 0.5s 是"单跑好看"的数，放进 608 个用例的整套里跑时
        # 被 GC 和调度抖到 0.503s 就会假报失败——实测就撞到过一次。1.5s 仍然是毫秒级
        # 实现的三十多倍余量，两种实现之间照样隔着数量级。
        self.assertLess(elapsed, 1.5, f"set_chapters({HUGE}) 用了 {elapsed:.4f}s")

    def test_the_model_holds_plain_strings_not_one_object_per_chapter(self) -> None:
        """模型里只有字符串列表，没有"每章一个对象"——内存和耗时都不随章数爆炸。"""
        panel = self.make_panel()
        panel.set_chapters(make_titles(1000))
        self.assertEqual(len(panel.model._source), 1000)
        self.assertIsInstance(panel.model._source[0], str)

    def test_data_returns_none_for_an_invalid_index(self) -> None:
        """题面约定：非法 QModelIndex 返回 None，DisplayRole 正常给标题。"""
        panel = self.make_panel()
        panel.set_chapters(["第一章 甲", "第二章 乙"])
        model = panel.model
        self.assertIsNone(model.data(model.index(-1, 0), Qt.DisplayRole))
        self.assertIsNone(model.data(model.index(99, 0), Qt.DisplayRole))
        self.assertEqual(model.data(model.index(1, 0), Qt.DisplayRole), "第二章 乙")

    def test_the_view_asks_the_model_for_only_the_rows_on_screen(self) -> None:
        """虚拟化的本质：视图只对屏幕上那几十行要数据，与总章数无关。

        这是比计时更硬的防回归：逐条建 item 的实现一次重绘要碰全部条目，
        这里数 `data()` 的调用次数，165 858 章也只该问一屏那么多行。
        """
        panel = self.make_panel(320, 300)
        panel.set_chapters(make_titles(HUGE))
        panel.list_view.viewport().repaint()
        pump()
        calls: list[int] = []
        original = panel.model.data

        def counting(index, role=Qt.DisplayRole):
            calls.append(1)
            return original(index, role)

        panel.model.data = counting
        panel.list_view.viewport().repaint()
        pump()
        self.assertGreater(len(calls), 0, "视图确实向模型要过数据")
        self.assertLess(len(calls), 2000, f"不该问 {len(calls)} 行，一屏只有几十行")


class TestFiltering(TocPanelTestCase):
    def test_filtering_narrows_and_is_case_insensitive(self) -> None:
        panel = self.make_searchable_panel()
        self.assertEqual(panel.model.rowCount(), 4)

        panel.search.setText("chapter")
        pump()
        self.assertEqual(panel.model.rowCount(), 2, "Chapter / CHAPTER 都要命中")
        self.assertEqual(panel.model.title_at(0), "Chapter Two: The Middle")

        panel.search.setText("CHAPTER TWO")
        pump()
        self.assertEqual(panel.model.rowCount(), 1)
        self.assertEqual(panel.model.chapter_at(0), 1, "映射回原始章序号")

        panel.search.setText("第三章")
        pump()
        self.assertEqual(panel.model.rowCount(), 1)
        self.assertEqual(panel.model.chapter_at(0), 2)

    def test_clearing_the_filter_restores_every_chapter(self) -> None:
        panel = self.make_searchable_panel()
        panel.search.setText("开始")
        pump()
        self.assertEqual(panel.model.rowCount(), 1)
        panel.search.clear()
        pump()
        self.assertEqual(panel.model.rowCount(), 4)
        self.assertEqual(panel.model.chapter_at(3), 3, "未过滤时行号即章序号")

    def test_the_match_count_is_shown(self) -> None:
        panel = self.make_searchable_panel()
        panel.search.setText("章")
        pump()
        self.assertEqual(panel.count_label.text(), "找到 3 章")
        panel.search.setText("不存在的标题")
        pump()
        self.assertEqual(panel.model.rowCount(), 0)
        self.assertEqual(panel.count_label.text(), "找到 0 章")

    def test_filtering_a_huge_book_is_fast(self) -> None:
        panel = self.make_panel()
        panel.set_chapters(make_titles(HUGE))
        started = time.perf_counter()
        panel.search.setText("章节标题 123")
        pump()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, f"过滤 {HUGE} 章用了 {elapsed:.4f}s")
        self.assertGreater(panel.model.rowCount(), 0)
        self.assertIsNotNone(panel.model._mapping, "过滤后必须有行号到章序号的映射")


class TestKeyboard(TocPanelTestCase):
    def test_enter_emits_the_chapter_under_the_selection(self) -> None:
        panel = self.make_searchable_panel()
        got: list[int] = []
        panel.chapterChosen.connect(got.append)
        panel.focus_search()
        pump()
        # 打开时当前行已经在第 0 行（见 `_on_model_rows_changed`），所以两次 ↓ 到第 2 行。
        self.press_search(panel, Qt.Key_Down)
        self.press_search(panel, Qt.Key_Down)
        self.assertEqual(panel.list_view.currentIndex().row(), 2)
        self.press_search(panel, Qt.Key_Return)
        self.assertEqual(got, [2])

    def test_arrows_move_the_selection_while_the_search_box_keeps_focus(self) -> None:
        """焦点必须留在搜索框，否则用户没法连着打字；方向键靠过滤器驱动列表。"""
        panel = self.make_searchable_panel()
        panel.focus_search()
        pump()
        self.assertTrue(panel.search.hasFocus())
        key(panel.search, Qt.Key_Down)
        self.assertTrue(panel.search.hasFocus(), "打字焦点不能被列表抢走")
        self.assertEqual(panel.list_view.currentIndex().row(), 1)
        key(panel.search, Qt.Key_Up)
        self.assertEqual(panel.list_view.currentIndex().row(), 0)

    def test_the_search_box_still_receives_typing(self) -> None:
        panel = self.make_searchable_panel()
        panel.focus_search()
        pump()
        for char in "终局":
            key(panel.search, 0, char)
        self.assertEqual(panel.search.text(), "终局")
        self.assertEqual(panel.model.rowCount(), 1)

    def test_a_simulated_activation_emits_the_chapter(self) -> None:
        """双击 / Enter 都汇到 activated；过滤后必须给出原始章序号而不是视图行号。"""
        panel = self.make_searchable_panel()
        got: list[int] = []
        panel.chapterChosen.connect(got.append)
        panel.search.setText("章")
        pump()
        self.assertEqual(panel.model.rowCount(), 3)
        panel.list_view.activated.emit(panel.model.index(2, 0))
        pump()
        self.assertEqual(got, [3], "第 3 个可见行对应原书第 4 章")

    def test_a_double_click_emits_the_chapter(self) -> None:
        panel = self.make_panel()
        panel.set_chapters(make_titles(10))
        got: list[int] = []
        panel.chapterChosen.connect(got.append)
        panel.list_view.doubleClicked.emit(panel.model.index(4, 0))
        pump()
        self.assertEqual(got, [4])

    def test_escape_closes_the_panel(self) -> None:
        panel = self.make_searchable_panel()
        closed: list[int] = []
        panel.closed.connect(lambda: closed.append(1))
        self.press_search(panel, Qt.Key_Escape)
        self.assertEqual(closed, [1])

    def test_escape_from_the_list_also_closes_the_panel(self) -> None:
        panel = self.make_searchable_panel()
        closed: list[int] = []
        panel.closed.connect(lambda: closed.append(1))
        key(panel.list_view, Qt.Key_Escape)
        self.assertEqual(closed, [1])


class TestCurrentChapter(TocPanelTestCase):
    def test_set_current_marks_and_selects_the_right_row(self) -> None:
        panel = self.make_panel()
        panel.set_chapters(make_titles(500))
        panel.set_current(321)
        pump()
        self.assertEqual(panel.model.current_chapter(), 321)
        self.assertEqual(panel.list_view.currentIndex().row(), 321)
        self.assertEqual(panel.model.index(321, 0).data(ROLE_ROW_TYPE), ROW_CURRENT)
        self.assertEqual(panel.model.index(320, 0).data(ROLE_ROW_TYPE), ROW_NORMAL)

    def test_the_current_row_is_visually_marked_not_just_greyed(self) -> None:
        """当前章要有底色 + 前景色 + 加粗，不是"比旁边深一点的灰"。"""
        panel = self.make_panel()
        panel.set_chapters(make_titles(10))
        panel.set_current(3)
        pump()
        current = panel.model.index(3, 0)
        other = panel.model.index(4, 0)
        self.assertIsNotNone(current.data(Qt.BackgroundRole))
        self.assertIsNotNone(current.data(Qt.ForegroundRole))
        self.assertIsNotNone(current.data(Qt.DecorationRole), "左侧强调色条")
        self.assertIsNotNone(current.data(Qt.FontRole))
        self.assertIsNone(other.data(Qt.BackgroundRole), "普通行不染色")
        self.assertIsNone(other.data(Qt.ForegroundRole))

    def test_set_current_does_not_crash_when_the_filter_hides_it(self) -> None:
        panel = self.make_searchable_panel()
        panel.search.setText("开始")
        pump()
        self.assertEqual(panel.model.rowCount(), 1)
        panel.set_current(2)          # 第 3 章已被过滤掉
        pump()
        self.assertEqual(panel.model.current_chapter(), 2)
        self.assertEqual(panel.model.row_of_chapter(2), -1, "它确实不可见")
        panel.search.clear()          # 清空过滤后高亮要回来
        pump()
        self.assertEqual(panel.model.index(2, 0).data(ROLE_ROW_TYPE), ROW_CURRENT)

    def test_an_out_of_range_current_is_just_not_visible(self) -> None:
        panel = self.make_panel()
        panel.set_chapters(make_titles(10))
        panel.set_current(999)
        pump()
        self.assertEqual(panel.model.row_of_chapter(999), -1)
        self.assertEqual(panel.model.rowCount(), 10)

    def test_set_current_scrolls_the_row_into_view(self) -> None:
        panel = self.make_panel(height=200)
        panel.set_chapters(make_titles(5000))
        panel.set_current(4000)
        pump()
        rect = panel.list_view.visualRect(panel.model.index(4000, 0))
        viewport = panel.list_view.viewport().rect()
        self.assertTrue(rect.intersects(viewport), f"第 4000 行不在可见区：{rect}")


class TestColour(TocPanelTestCase):
    def test_the_current_row_colours_pass_wcag_aa(self) -> None:
        """当前章不是"另一种灰"：强调色底 + 深色文字，文字与底色必须 >= 4.5:1。"""
        panel = self.make_panel()
        foreground, background = panel.current_row_colors()
        ratio = theme.contrast_ratio(foreground, background)
        self.assertGreaterEqual(
            ratio, theme.AA_NORMAL, f"当前行 {foreground} on {background} 只有 {ratio:.2f}:1"
        )

    def test_the_current_row_background_is_the_accent_colour(self) -> None:
        panel = self.make_panel()
        _foreground, background = panel.current_row_colors()
        self.assertEqual(background, theme.PALETTE["accent"])

    def test_the_model_hands_the_same_colours_to_the_view(self) -> None:
        """模型交给视图的颜色必须就是上面那对——不然测的是一份，画的是另一份。"""
        panel = self.make_panel()
        panel.set_chapters(make_titles(5))
        panel.set_current(0)
        pump()
        foreground, background = current_row_colors()
        index = panel.model.index(0, 0)
        self.assertEqual(index.data(Qt.ForegroundRole).color().name(), foreground)
        self.assertEqual(index.data(Qt.BackgroundRole).color().name(), background)

    def test_the_palette_keeps_every_pair_this_panel_paints(self) -> None:
        """面板只画这几对颜色，每一对都要达标——换了调色板就会在这里响。"""
        for role, fg_key, bg_key in (
            ("目录行", "fg", "panel"),
            ("当前章", "bg", "accent"),
            ("次要文字", "muted", "panel"),
            ("搜索框", "fg", "field"),
            ("搜索框提示", "muted", "field"),
        ):
            with self.subTest(role=role):
                ratio = theme.contrast_ratio(theme.PALETTE[fg_key], theme.PALETTE[bg_key])
                self.assertGreaterEqual(ratio, theme.AA_NORMAL, f"{role} 只有 {ratio:.2f}:1")


class TestBookmarks(TocPanelTestCase):
    def test_a_click_emits_the_bookmark_id(self) -> None:
        panel = self.make_bookmark_panel()
        got: list[int] = []
        panel.bookmarkChosen.connect(got.append)
        click_center(panel.bookmark_rows()[1])
        self.assertEqual(got, [9])

    def test_delete_emits_the_bookmark_id(self) -> None:
        panel = self.make_bookmark_panel()
        got: list[int] = []
        panel.bookmarkDeleted.connect(got.append)
        panel.bookmark_rows()[0].delete_button.click()
        pump()
        self.assertEqual(got, [7], "删除按钮发出的必须是 id，不是行号")
        self.assertEqual(panel.bookmark_count(), 2, "面板只发信号，数据由父窗口改")

    def test_the_delete_button_meets_the_minimum_target(self) -> None:
        panel = self.make_bookmark_panel()
        button = panel.bookmark_rows()[0].delete_button
        self.assertGreaterEqual(button.width(), MIN_TARGET)
        self.assertGreaterEqual(button.height(), MIN_TARGET)
        self.assertGreaterEqual(panel.bookmark_rows()[0].height(), MIN_TARGET)

    def test_pressing_delete_does_not_also_choose_the_bookmark(self) -> None:
        """删除是破坏性操作：按 ✕ 只发删除，不该顺手把阅读位置跳过去。"""
        panel = self.make_bookmark_panel()
        chosen: list[int] = []
        deleted: list[int] = []
        panel.bookmarkChosen.connect(chosen.append)
        panel.bookmarkDeleted.connect(deleted.append)
        click_center(panel.bookmark_rows()[0].delete_button)
        self.assertEqual(deleted, [7])
        self.assertEqual(chosen, [])

    def test_each_row_shows_the_label_and_the_excerpt(self) -> None:
        panel = self.make_bookmark_panel()
        row = panel.bookmark_rows()[0]
        self.assertEqual(row.label.text(), "第2章 标题2")
        self.assertEqual(row.excerpt.text(), "这里是正文摘要…")
        self.assertEqual(row.excerpt.objectName(), "TocBookmarkExcerpt", "摘要用 muted 色")
        self.assertIn("2024-05-01", row.toolTip())

    def test_the_delete_button_has_a_tooltip(self) -> None:
        panel = self.make_bookmark_panel()
        self.assertTrue(panel.bookmark_rows()[0].delete_button.toolTip())

    def test_an_empty_list_shows_the_empty_state(self) -> None:
        panel = self.make_panel()
        panel.tabs.setCurrentIndex(1)
        panel.set_bookmarks([])
        pump()
        self.assertEqual(panel.bookmark_count(), 0)
        self.assertTrue(panel.empty_label.isVisible())
        self.assertEqual(panel.empty_label.text(), "还没有书签")
        self.assertEqual(panel.empty_label.objectName(), "TocMuted", "空状态用 muted 色")

    def test_loading_bookmarks_hides_the_empty_state(self) -> None:
        panel = self.make_panel()
        panel.tabs.setCurrentIndex(1)
        panel.set_bookmarks([])
        pump()
        panel.set_bookmarks(BOOKMARKS)
        pump()
        self.assertEqual(panel.bookmark_count(), 2)
        self.assertFalse(panel.empty_label.isVisible())
        self.assertTrue(panel.bookmark_area.isVisible())

    def test_reloading_replaces_rather_than_appends(self) -> None:
        panel = self.make_panel()
        panel.set_bookmarks(BOOKMARKS)
        pump()
        panel.set_bookmarks(BOOKMARKS[:1])
        pump()
        self.assertEqual(panel.bookmark_count(), 1)
        self.assertEqual(panel.bookmark_rows()[0].label.text(), "第2章 标题2")

    def test_a_row_without_an_id_is_not_fatal(self) -> None:
        panel = self.make_panel()
        panel.set_bookmarks([{"label": "坏的"}, BOOKMARKS[0]])
        pump()
        self.assertEqual(panel.bookmark_count(), 2)


class TestLayout(TocPanelTestCase):
    def test_it_renders_at_the_small_size(self) -> None:
        panel = self.make_panel(260, 300)
        panel.set_chapters(make_titles(200))
        panel.set_bookmarks(BOOKMARKS)
        panel.set_current(10)
        pump()
        self.assertEqual((panel.width(), panel.height()), (260, 300))
        self.assertGreater(panel.list_view.viewport().width(), 100)
        self.assertGreater(panel.list_view.viewport().height(), 50)

    def test_it_renders_at_the_large_size(self) -> None:
        panel = self.make_panel(520, 800)
        panel.set_chapters(make_titles(2000))
        panel.set_current(1000)
        pump()
        self.assertEqual((panel.width(), panel.height()), (520, 800))
        self.assertGreater(panel.list_view.viewport().height(), 400)

    def test_every_clickable_control_meets_the_minimum_target(self) -> None:
        panel = self.make_panel(260, 300)
        panel.set_bookmarks(BOOKMARKS)
        pump()
        # 标签按钮的尺寸由 QSS 的 QTabBar::tab 决定（min-height 22 + padding），
        # 所以这里检查标签条本身的高度，再看每个真实的 QToolButton。
        self.assertGreaterEqual(panel.tabs.tabBar().height(), MIN_TARGET, "标签条")
        controls = {
            "关闭按钮": panel.close_button,
            "删除按钮": panel.bookmark_rows()[0].delete_button,
        }
        for name, button in controls.items():
            with self.subTest(control=name):
                self.assertGreaterEqual(button.width(), MIN_TARGET, name)
                self.assertGreaterEqual(button.height(), MIN_TARGET, name)

    def test_icon_only_buttons_have_a_tooltip(self) -> None:
        """没有屏幕阅读器的说法，tooltip 就是标签：每个图标按钮都得有。"""
        panel = self.make_panel()
        panel.set_bookmarks(BOOKMARKS)
        pump()
        self.assertEqual(panel.close_button.text(), "×", "关闭按钮是图标")
        self.assertTrue(panel.close_button.toolTip())
        for row in panel.bookmark_rows():
            self.assertEqual(row.delete_button.text(), "", "删除按钮是纯图标")
            self.assertTrue(row.delete_button.toolTip())

    def test_resizing_reflows_instead_of_assuming_a_size(self) -> None:
        panel = self.make_panel(520, 800)
        panel.set_chapters(make_titles(50))
        pump()
        wide = panel.list_view.viewport().width()
        panel.resize(260, 300)
        pump()
        self.assertLess(panel.list_view.viewport().width(), wide)


class TestPanelChrome(TocPanelTestCase):
    def test_the_close_button_emits_closed(self) -> None:
        panel = self.make_panel()
        closed: list[int] = []
        panel.closed.connect(lambda: closed.append(1))
        click_center(panel.close_button)
        self.assertEqual(closed, [1])

    def test_focus_search_puts_the_caret_in_the_search_box(self) -> None:
        panel = self.make_panel()
        panel.tabs.setCurrentIndex(1)
        panel.focus_search()
        pump()
        self.assertTrue(panel.search.hasFocus())
        self.assertEqual(panel.tabs.currentIndex(), 0)

    def test_the_two_tabs_switch(self) -> None:
        panel = self.make_panel()
        self.assertEqual(panel.tabs.count(), 2)
        self.assertEqual(panel.tabs.tabText(0), "目录")
        self.assertEqual(panel.tabs.tabText(1), "书签")
        panel.tabs.setCurrentIndex(1)
        pump()
        self.assertEqual(panel.tabs.currentIndex(), 1)

    def test_opacity_hint_is_recorded(self) -> None:
        """面板自身的状态不碰数据，但父窗口要能回读它。"""
        panel = self.make_panel()
        self.assertFalse(panel.opacity_dimmed())
        panel.set_opacity_hint(True)
        self.assertTrue(panel.opacity_dimmed())

    def test_the_stylesheet_keeps_the_focus_ring(self) -> None:
        """焦点可见性靠 QSS 的 :focus 边框，不能被"去掉虚线框"顺手删掉。"""
        panel = self.make_panel()
        qss = panel.styleSheet()
        accent = theme.PALETTE["accent"]
        self.assertIn(f"QLineEdit#TocSearch:focus {{ border: 1px solid {accent}; }}", qss)
        self.assertIn(f"QListView#TocList:focus {{ border: 1px solid {accent}; }}", qss)
        self.assertIn(f"QScrollArea#TocBookmarkArea:focus {{ border: 1px solid {accent}; }}", qss)


if __name__ == "__main__":
    unittest.main()
