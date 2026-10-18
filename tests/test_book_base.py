"""阶段 1：BookBase / Reader 抽象层。

这个模块守的是"抽象层本身"的契约，不是窗口行为——窗口行为仍由
test_ui_offscreen.py 那 160 条负责，它们一条都没改，这本身就是阶段 1 的验收条件。

三类断言值得单独说明：

1. **MRO 顺序**。`Reader` 必须排在 Qt 控件**前面**（`class ReaderView(Reader,
   QTextEdit)`），这样基类里的方法才能生效；但正因为它排在前面，基类里
   任何与 Qt 重名的方法都会**盖掉** Qt 的实现。所以有一组测试专门断言
   `Reader` 不声明 `show` / `viewport` / `hide` / `close` 等 Qt 名字——
   这是阶段 1 里真实踩到过的坑：方案 §2.3 原本写的是 `Reader.show()` 和
   `Reader.viewport()`，而 `window.py:559` 要 `self.reader.viewport()`
   `.installEventFilter(self)`，一旦被盖成 `return None`，边缘缩放和
   悬停暂停自动滚动会一起失效。

2. **接口名字必须对两个绑定都空着**。用 hasattr 逐个查 QWidget/QTextEdit，
   而不是只测当前装着的 PyQt5——阶段 1 的约束是 PySide6/PyQt5 双绑定兼容。

3. **core 里不许出现 Qt**。沿用 test_store.py 的做法：读源码找禁用字符串。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core.book_base import (  # noqa: E402
    KIND_COMIC,
    KIND_TEXT,
    POSITION_LABELS,
    BookBase,
    Position,
)
from minireader.core.model import Book  # noqa: E402
from minireader.qt import (  # noqa: E402
    QApplication,
    QTextEdit,
    QWidget,
    install_message_filter,
)
from minireader.ui.reader import ReaderView  # noqa: E402
from minireader.ui.reader_base import Reader  # noqa: E402

_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

_app = QApplication.instance() or QApplication([])

# Qt 自己就有的名字。`Reader` 一个都不许声明：它排在 MRO 最前面，声明即覆盖。
_QT_OWNED = (
    "show",
    "hide",
    "viewport",
    "close",
    "update",
    "repaint",
    "resize",
    "move",
    "setParent",
    "geometry",
    "parent",
    "children",
    "raise_",
    "lower",
    "isVisible",
    "setVisible",
    "sizeHint",
    "minimumSizeHint",
    "paintEvent",
    "keyPressEvent",
    "mousePressEvent",
    "wheelEvent",
    "resizeEvent",
    "showEvent",
    "hideEvent",
    "closeEvent",
    "event",
    "eventFilter",
    "setFocus",
    "hasFocus",
    "grab",
    "activateWindow",
    "windowTitle",
    "setWindowTitle",
)

# `Reader` 承诺提供的方法名。少一个都算接口破了。
_INTERFACE = (
    "show_book",
    "next_page",
    "prev_page",
    "goto",
    "current_position",
    "total_positions",
    "progress_percent",
    "scrollbar",
    "apply_style",
    "stop",
)


def _make_book(text: str = "第一章 起\n正文甲。\n第二章 承\n正文乙。\n第三章 转\n正文丙。\n") -> Book:
    return Book.from_text(text, path=str(Path(tempfile.gettempdir()) / "book_base_probe.txt"))


class TestPosition(unittest.TestCase):
    def test_text_label_keeps_the_old_status_bar_text(self):
        # 底栏原来的文案就是 "第 x/y 章"，这条断言把它钉死。
        self.assertEqual(Position(kind=KIND_TEXT, index=2, total=12).label, "第 3/12 章")

    def test_comic_label_counts_pages(self):
        self.assertEqual(Position(kind=KIND_COMIC, index=4, total=40).label, "第 5/40 页")

    def test_span_label_is_the_two_page_form(self):
        # 双页模式：第 5-6/40 页（方案 §8.1）。
        self.assertEqual(
            Position(kind=KIND_COMIC, index=4, total=40, span=2).label, "第 5-6/40 页"
        )

    def test_span_of_one_never_prints_a_range(self):
        self.assertEqual(
            Position(kind=KIND_COMIC, index=4, total=40, span=1).label, "第 5/40 页"
        )

    def test_span_below_one_is_clamped(self):
        # span=0 会让 "第 5-4/40 页" 这种反向区间漏出去。
        self.assertEqual(
            Position(kind=KIND_COMIC, index=4, total=40, span=0).label, "第 5/40 页"
        )

    def test_percent_counts_the_current_position_as_reached(self):
        # 第 1 章 / 共 10 章 是 10%，不是 0%。既有 _update_status() 一直这么算，
        # 所以文本模式的百分比在改走 Position 之后一个数字都不会变。
        self.assertEqual(Position(kind=KIND_TEXT, index=0, total=10).percent, 10.0)
        self.assertEqual(Position(kind=KIND_TEXT, index=9, total=10).percent, 100.0)

    def test_percent_never_exceeds_one_hundred(self):
        self.assertEqual(Position(kind=KIND_TEXT, index=99, total=10).percent, 100.0)

    def test_percent_of_an_empty_book_is_zero(self):
        self.assertEqual(Position(kind=KIND_TEXT, index=0, total=0).percent, 0.0)

    def test_text_formats_like_the_progress_label_always_did(self):
        self.assertEqual(Position(kind=KIND_TEXT, index=2, total=12).text(), "25.00%")
        self.assertEqual(Position(kind=KIND_TEXT, index=0, total=3).text(), "33.33%")

    def test_unknown_kind_falls_back_to_chapters(self):
        self.assertEqual(Position(kind="???", index=0, total=4).label, "第 1/4 章")

    def test_it_is_frozen(self):
        pos = Position(kind=KIND_TEXT, index=0, total=1)
        with self.assertRaises(FrozenInstanceError):
            pos.index = 3  # type: ignore[misc]

    def test_defaults_are_the_text_reader(self):
        pos = Position()
        self.assertEqual((pos.kind, pos.index, pos.offset, pos.total, pos.span), ("text", 0, 0, 0, 1))

    def test_labels_table_is_the_two_kinds(self):
        self.assertEqual(POSITION_LABELS, {KIND_TEXT: "章", KIND_COMIC: "页"})


class TestBookBase(unittest.TestCase):
    def test_defaults_are_a_text_book_with_nothing_in_it(self):
        base = BookBase()
        self.assertEqual(base.kind, KIND_TEXT)
        self.assertEqual(base.position_count, 0)
        self.assertEqual(base.position_label, "章")
        self.assertFalse(base.is_paged)

    def test_a_half_built_subclass_still_answers(self):
        # 没有 chapter_count 也不许抛异常：底栏会在书还没装好时就读这些属性。
        class Bare(BookBase):
            pass

        bare = Bare()
        self.assertEqual(bare.position_count, 0)
        self.assertEqual(bare.position(3).label, "第 4/0 章")

    def test_position_count_reads_chapter_count_by_duck_typing(self):
        class HasChapters(BookBase):
            @property
            def chapter_count(self) -> int:
                return 7

        self.assertEqual(HasChapters().position_count, 7)

    def test_comic_kind_is_paged(self):
        class Comicish(BookBase):
            kind = KIND_COMIC

            @property
            def position_count(self) -> int:
                return 40

        comic = Comicish()
        self.assertTrue(comic.is_paged)
        self.assertEqual(comic.position_label, "页")

    def test_position_carries_the_books_own_kind_and_total(self):
        class Comicish(BookBase):
            kind = KIND_COMIC

            @property
            def position_count(self) -> int:
                return 40

        pos = Comicish().position(4, span=2)
        self.assertEqual((pos.kind, pos.index, pos.total, pos.span), (KIND_COMIC, 4, 40, 2))
        self.assertEqual(pos.label, "第 5-6/40 页")

    def test_position_clamps_negatives(self):
        pos = BookBase().position(-5, offset=-9, span=0)
        self.assertEqual((pos.index, pos.offset, pos.span), (0, 0, 1))

    def test_describe_and_to_record_have_neutral_defaults(self):
        base = BookBase()
        self.assertEqual(base.describe(), "0 章")
        self.assertEqual(base.to_record(), {})

    def test_from_path_is_not_implemented_on_the_base(self):
        with self.assertRaises(NotImplementedError):
            BookBase.from_path("whatever.txt")


class TestBookIsABookBase(unittest.TestCase):
    def test_book_inherits_book_base(self):
        self.assertTrue(issubclass(Book, BookBase))
        self.assertIsInstance(_make_book(), BookBase)

    def test_mro_is_book_then_book_base_then_object(self):
        # BookBase 是 dataclass 之外唯一的基类，且必须排在 object 前面。
        self.assertEqual([c.__name__ for c in Book.__mro__], ["Book", "BookBase", "object"])

    def test_adding_the_base_did_not_add_or_remove_a_field(self):
        # @dataclass 只收集**本类**的注解字段，基类不是 dataclass 就不会被卷进来。
        self.assertEqual(
            [f.name for f in fields(Book)],
            [
                "id",
                "title",
                "path",
                "size",
                "mtime",
                "encoding",
                "encoding_label",
                "confident",
                "text",
                "chapters",
                "rule_id",
                "added_at",
                "notes",
                "source",
                "author",
            ],
        )

    def test_book_keeps_its_own_constructors(self):
        book = _make_book()
        self.assertEqual(book.chapter_count, 3)
        self.assertEqual(book.title, "book_base_probe")

    def test_position_count_is_the_chapter_count(self):
        self.assertEqual(_make_book().position_count, 3)

    def test_book_is_not_paged(self):
        self.assertFalse(_make_book().is_paged)
        self.assertEqual(_make_book().position_label, "章")

    def test_book_position_matches_the_old_status_bar_numbers(self):
        book = _make_book()
        pos = book.position(1)
        self.assertEqual(pos.label, "第 2/3 章")
        self.assertEqual(pos.text(), "66.67%")

    def test_book_describe_and_to_record_still_override_the_base(self):
        book = _make_book()
        self.assertNotEqual(book.describe(), BookBase().describe())
        self.assertEqual(book.to_record()["source"], "txt")


class TestReaderIsAPlainClass(unittest.TestCase):
    def test_it_is_not_an_abc_or_a_protocol(self):
        # ABCMeta 和 sip.wrappertype 不兼容：真写成 ABC，下面那条 MRO 断言会
        # 直接抛 TypeError: metaclass conflict。
        import abc
        import typing

        self.assertNotIsInstance(Reader, abc.ABCMeta)
        self.assertFalse(issubclass(Reader, abc.ABC))
        self.assertNotIn(typing.Protocol, Reader.__mro__)

    def test_reader_view_puts_reader_before_qt(self):
        mro = [c.__name__ for c in ReaderView.__mro__]
        self.assertLess(mro.index("Reader"), mro.index("QTextEdit"))
        self.assertEqual(mro[:4], ["ReaderView", "Reader", "QTextEdit", "QAbstractScrollArea"])

    def test_reader_view_is_both_things(self):
        self.assertTrue(issubclass(ReaderView, Reader))
        self.assertTrue(issubclass(ReaderView, QTextEdit))

    def test_reader_declares_every_interface_name(self):
        for name in _INTERFACE:
            with self.subTest(name=name):
                self.assertIn(name, Reader.__dict__, f"Reader 少了 {name}")

    def test_reader_declares_no_qt_owned_name(self):
        # 这是阶段 1 的真坑：基类排在 MRO 最前，声明 show/viewport 就会盖掉 Qt 的，
        # 于是 self.reader.show() 报 TypeError、self.reader.viewport() 变成 None，
        # window.py:559 的 installEventFilter 和 window.py:1843 的 _edge_surfaces()
        # 会一起坏掉。
        for name in _QT_OWNED:
            with self.subTest(name=name):
                self.assertNotIn(
                    name,
                    Reader.__dict__,
                    f"Reader 声明了 Qt 自己的 {name}()，会盖掉 Qt 的实现",
                )

    def test_interface_names_are_free_on_both_widget_bases(self):
        # 双绑定兼容：名字必须在 QWidget / QTextEdit 上都是空的，而不只是当前
        # 装着的 PyQt5 上为空。
        for name in _INTERFACE + ("kind",):
            for owner in (QWidget, QTextEdit):
                with self.subTest(name=name, owner=owner.__name__):
                    self.assertFalse(
                        hasattr(owner, name),
                        f"{owner.__name__} 已经有 {name} 了，接口名要换",
                    )


class TestReaderViewForwarding(unittest.TestCase):
    def setUp(self):
        self.book = _make_book()
        self.view = ReaderView()
        self.view.resize(400, 300)

    def tearDown(self):
        self.view.deleteLater()
        _app.processEvents()

    def test_a_fresh_view_answers_before_any_book_is_set(self):
        # __init__ 一行没改，所以这些访问器必须在"还没打开书"时也安全。
        self.assertEqual(self.view.total_positions(), 0)
        self.assertIsNone(self.view._book)
        pos = self.view.current_position()
        self.assertEqual((pos.kind, pos.index, pos.total), (KIND_TEXT, 0, 0))

    def test_show_book_renders_and_remembers_the_chapter(self):
        self.view.show_book(self.book, index=1)
        _app.processEvents()
        self.assertIs(self.view._book, self.book)
        self.assertEqual(self.view._index, 1)
        self.assertEqual(self.view.toPlainText().strip(), self.book.chapter_body(1).strip())

    def test_show_book_clamps_the_index(self):
        self.view.show_book(self.book, index=99)
        _app.processEvents()
        self.assertEqual(self.view._index, 2)

    def test_show_book_forwards_char_offset_to_the_existing_set_chapter(self):
        # 阶段 2 要把 window.py:1600 的 set_chapter() 换成 show_book()，
        # char_offset 这条路不能丢——改字体之后它比像素可靠。
        seen: dict = {}
        original = self.view.set_chapter

        def spy(body, **kwargs):
            seen.update(kwargs)
            return original(body, **kwargs)

        self.view.set_chapter = spy  # type: ignore[method-assign]
        self.view.show_book(self.book, index=1, offset=7, char_offset=42)
        self.view.set_chapter = original  # type: ignore[method-assign]
        self.assertEqual(seen.get("offset"), 7)
        self.assertEqual(seen.get("char_offset"), 42)
        self.assertEqual(seen.get("at_end"), False)

    def test_show_book_defaults_char_offset_to_none(self):
        seen: dict = {}
        original = self.view.set_chapter

        def spy(body, **kwargs):
            seen.update(kwargs)
            return original(body, **kwargs)

        self.view.set_chapter = spy  # type: ignore[method-assign]
        self.view.show_book(self.book, index=0)
        self.view.set_chapter = original  # type: ignore[method-assign]
        self.assertIsNone(seen.get("char_offset"))

    def test_total_positions_is_the_chapter_count(self):
        self.view.show_book(self.book)
        _app.processEvents()
        self.assertEqual(self.view.total_positions(), 3)

    def test_current_position_reports_chapters_and_a_pixel_offset(self):
        self.view.show_book(self.book, index=2)
        _app.processEvents()
        pos = self.view.current_position()
        self.assertEqual(pos.kind, KIND_TEXT)
        self.assertEqual(pos.index, 2)
        self.assertEqual(pos.total, 3)
        self.assertEqual(pos.offset, self.view.current_offset())
        self.assertEqual(pos.label, "第 3/3 章")

    def test_progress_percent_matches_the_position(self):
        self.view.show_book(self.book, index=0)
        _app.processEvents()
        self.assertEqual(self.view.progress_percent(), self.view.current_position().percent)

    def test_goto_moves_to_another_chapter(self):
        self.view.show_book(self.book, index=0)
        _app.processEvents()
        self.view.goto(self.book.position(2))
        _app.processEvents()
        self.assertEqual(self.view._index, 2)
        self.assertEqual(self.view.current_position().label, "第 3/3 章")

    def test_goto_before_a_book_is_a_no_op(self):
        self.view.goto(Position(kind=KIND_TEXT, index=5, total=9))
        self.assertIsNone(self.view._book)

    def test_scrollbar_is_the_real_vertical_one(self):
        # AutoScroller 只认 value()/maximum()/setValue()，所以这里必须交出真的
        # QScrollBar，而不是 None 或替身。
        self.assertIs(self.view.scrollbar(), self.view.verticalScrollBar())

    def test_next_and_prev_page_emit_the_chapter_signal(self):
        seen: list[int] = []
        self.view.chapterRequested.connect(seen.append)
        self.view.next_page()
        self.view.prev_page()
        self.assertEqual(seen, [1, -1])

    def test_stop_cancels_a_running_animation(self):
        self.view.show_book(self.book, index=1)
        _app.processEvents()
        self.view.scroll_to_char(0)
        self.view.stop()
        self.assertFalse(self.view.animating)

    def test_stop_is_safe_when_nothing_is_running(self):
        self.view.stop()

    def test_viewport_is_still_qt_own(self):
        # window.py:559 用 self.reader.viewport().installEventFilter(self)，
        # window.py:1843 把它当成边缘缩放的一个判定面。被基类盖掉就全废了。
        self.assertIsNotNone(self.view.viewport())
        self.assertIs(self.view.viewport(), QTextEdit.viewport(self.view))

    def test_show_and_hide_are_still_qt_own(self):
        self.view.show()
        self.assertTrue(self.view.isVisible())
        self.view.hide()
        self.assertFalse(self.view.isVisible())


class TestCoreStaysQtFree(unittest.TestCase):
    """core/ 里不许出现 Qt——test_store.py 对 store.py 立过同样的规矩。"""

    BANNED = ("PyQt5", "PySide6", "minireader.qt", "QtCore", "QtWidgets")

    def test_book_base_source_has_no_qt(self):
        from minireader.core import book_base

        source = Path(book_base.__file__).read_text(encoding="utf-8")
        for needle in self.BANNED:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_book_base_imports_only_stdlib(self):
        # 用 ast 读真正的 import 语句，而不是断言一个不存在的 __all__。
        import ast
        import sys as _sys
        from minireader.core import book_base

        tree = ast.parse(Path(book_base.__file__).read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对 import：包内模块，下面单独看
                    roots.add("." * node.level + (node.module or ""))
                elif node.module:
                    roots.add(node.module.split(".")[0])
        stdlib = set(_sys.stdlib_module_names)
        external = {
            name
            for name in roots
            if not name.startswith(".") and name != "__future__" and name not in stdlib
        }
        self.assertEqual(external, set(), f"book_base 引了非标准库模块：{external}")
        # 包内相对 import 只允许 chapters/encoding/model 这一层之外的纯数据模块。
        self.assertNotIn(".epub", roots)

    def test_reader_base_lives_in_ui_and_core_does_not_import_it(self):
        # 方向必须是 ui -> core，不能反过来。
        from minireader.core import model

        source = Path(model.__file__).read_text(encoding="utf-8")
        self.assertNotIn("reader_base", source)

    def test_importing_book_base_does_not_pull_qt_in(self):
        import subprocess

        code = (
            "import sys;"
            "import minireader.core.book_base;"
            "bad=[m for m in sys.modules if m.startswith(('PyQt5','PySide6'))];"
            "print(len(bad))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
