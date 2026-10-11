"""书签库：清洗、排序、按书隔离、持久化，以及"坏了一切也照常降级"。

主题和 test_storage.py 一样是"绝不崩"，但这里的失败面更宽：书签库是程序自己
写的文件，可能被人手改过、被别的程序占住、被倒了一半电。每一条降级路径都对应
一个真实场景，而不是为了凑覆盖率。

所有用例都继承 `SandboxedAppData`（从 test_storage 借来的沙盒），所以默认路径
也只会落在临时目录里——真 %APPDATA%\\MiniReader 一个字节都不该动。

    py -3.12 -m unittest tests.test_store -v
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader import store as store_module  # noqa: E402
from minireader.storage import data_dir  # noqa: E402
from minireader.store import DB_NAME, Bookmark, BookmarkStore  # noqa: E402
from tests.test_storage import SandboxedAppData  # noqa: E402


# open_store() 的"没给路径"哨兵：None 是"去用默认位置"这个真实的测试意图。
_USE_DEFAULT_PATH = object()


def _make_bookmark(**overrides) -> Bookmark:
    base = dict(
        id=3,
        book_id="mr-abc",
        chapter=1,
        offset=10,
        label="第一章 开端",
        excerpt="这是正文的一句话。",
        created=1_700_000_000.5,
    )
    base.update(overrides)
    return Bookmark(**base)


class StoreTestCase(SandboxedAppData):
    """沙盒 + 一定会收尾的连接池。

    连接不关会占住 Windows 上的库文件（临时目录因此删不掉），所以每个用例结束
    时统一 close()。库文件本身留在临时目录里不删：和 test_storage.py 一样，
    清理失败不值得让一个已经通过的用例变红。
    """

    def setUp(self) -> None:
        super().setUp()
        self._stores: list[BookmarkStore] = []

    def tearDown(self) -> None:
        for store in self._stores:
            store.close()
        super().tearDown()

    def open_store(self, db_path=_USE_DEFAULT_PATH) -> BookmarkStore:
        """`open_store()` 用一个显式的临时文件；`open_store(None)` 才是"用默认位置"。

        两个意思必须分开：默认位置那条路要单独验，而绝大多数用例想要的是一个
        确定、干净的文件名。
        """
        store = BookmarkStore(self.db_path if db_path is _USE_DEFAULT_PATH else db_path)
        self._stores.append(store)
        return store

    @property
    def db_path(self) -> Path:
        return self.tmp / "reading.db"

    @property
    def default_db_path(self) -> Path:
        return self.tmp / "MiniReader" / DB_NAME

    def raw(self, path: Path) -> sqlite3.Connection:
        """绕过 store 直接看库文件，用来验证"到底写下去了什么"。"""
        return sqlite3.connect(path)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class TestBookmarkToDict(unittest.TestCase):
    """`to_dict()` 是给 toc_panel.set_bookmarks() 的唯一接口，键名一改界面就哑。"""

    def test_keys_are_exactly_the_seven_in_the_contract(self) -> None:
        self.assertEqual(
            set(_make_bookmark().to_dict()),
            {"id", "book_id", "chapter", "offset", "label", "excerpt", "created"},
        )

    def test_the_panel_only_reads_five_of_them(self) -> None:
        """面板要的五个键必须都在；多出来的两个（book_id/offset）是给跳转用的。"""
        self.assertLessEqual(
            {"id", "chapter", "label", "excerpt", "created"},
            set(_make_bookmark().to_dict()),
        )

    def test_every_value_comes_straight_from_the_dataclass(self) -> None:
        bookmark = _make_bookmark(id=9, chapter=7, offset=1234, label="第七章")
        data = bookmark.to_dict()
        for name in ("id", "book_id", "chapter", "offset", "label", "excerpt", "created"):
            with self.subTest(field=name):
                self.assertEqual(data[name], getattr(bookmark, name))

    def test_created_stays_a_number(self) -> None:
        """格式化是界面的事：这里转成日期字符串，排序和"多久以前"就没法算了。"""
        self.assertIsInstance(_make_bookmark().to_dict()["created"], float)

    def test_the_dict_is_json_serialisable(self) -> None:
        """面板之外还有别的地方要把它塞进 json（日志、导出），别塞进怪对象。"""
        blob = json.dumps(_make_bookmark().to_dict(), ensure_ascii=False)
        self.assertIn("第一章 开端", blob)


class TestTheStoreIsQtFree(unittest.TestCase):
    """数据层必须能脱离 Qt 跑——做不到这一点，上面所有用例都得先起一个
    QApplication，而 CI 上没有屏幕。"""

    def test_the_source_imports_no_qt_binding(self) -> None:
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        for banned in ("PyQt5", "PySide6", "minireader.qt", "QtCore", "QtWidgets"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_reimporting_does_not_pull_qt_in(self) -> None:
        """相比"看 sys.modules"，这样写在整套测试里都成立（别的用例可能已经
        把 Qt 导进来了，那不该算这个模块的账）。"""
        def qt_modules() -> set[str]:
            return {
                name
                for name in sys.modules
                if name.startswith(("PyQt5", "PySide6", "minireader.qt"))
            }

        before = qt_modules()
        importlib.reload(store_module)
        self.assertEqual(qt_modules(), before)


# ---------------------------------------------------------------------------
# 清洗与取值
# ---------------------------------------------------------------------------

class TestSanitising(StoreTestCase):
    """书签行只有一个行高，标签里一个换行就把它撑破——所以清洗要发生在写库之前。"""

    def test_a_newline_in_the_label_becomes_a_space(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "第一行\n第二行")
        self.assertEqual(store.list_for("A")[0].label, "第一行 第二行")

    def test_carriage_return_and_tab_are_folded_too(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "甲\r\n乙\t丙")
        label = store.list_for("A")[0].label
        for bad in ("\r", "\n", "\t"):
            with self.subTest(char=repr(bad)):
                self.assertNotIn(bad, label)
        self.assertEqual(label, "甲 乙 丙")

    def test_nul_and_escape_are_dropped_not_replaced(self) -> None:
        """控制字符是"删掉"而不是"换成空格"：换成空格会把 a\\x00b 写成 "a b"，
        凭空多出一个词间空格。"""
        store = self.open_store()
        store.add("A", 0, 0, "a\x00b\x1b\x07c")
        self.assertEqual(store.list_for("A")[0].label, "abc")

    def test_a_nul_label_can_actually_be_written(self) -> None:
        """不清洗的话 \\x00 会在绑定参数时抛出来，这里证明整条路是通的。"""
        store = self.open_store()
        ident = store.add("A", 0, 0, "有\x00零字节")
        self.assertGreater(ident, 0)
        self.assertEqual(store.list_for("A")[0].label, "有零字节")

    def test_the_label_is_capped_at_120(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "字" * 5000)
        self.assertEqual(len(store.list_for("A")[0].label), 120)

    def test_the_excerpt_is_capped_at_200(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "标签", "字" * 5000)
        self.assertEqual(len(store.list_for("A")[0].excerpt), 200)

    def test_a_label_of_exactly_the_limit_survives_whole(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "字" * 120)
        store.add("A", 0, 1, "字" * 121)
        labels = [b.label for b in store.list_for("A")]
        self.assertEqual([len(text) for text in labels], [120, 120])

    def test_a_label_that_is_not_a_string_becomes_empty(self) -> None:
        """str(None) == "None" 会真的存进库里，用户就会看到一条叫 None 的书签。"""
        store = self.open_store()
        for junk in (None, 17, 3.5, ["甲"], {"a": 1}, True):
            with self.subTest(junk=repr(junk)):
                store.add("A", 0, 0, junk, junk)
        self.assertTrue(all(b.label == "" and b.excerpt == "" for b in store.list_for("A")))

    def test_a_whitespace_only_label_becomes_empty(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, " \t\n\u3000 ")
        self.assertEqual(store.list_for("A")[0].label, "")

    def test_a_single_internal_space_survives(self) -> None:
        """"第 3 章"里的空格是正文的一部分，折空白不能把它吃掉。"""
        store = self.open_store()
        store.add("A", 0, 0, "第 3 章 开端")
        self.assertEqual(store.list_for("A")[0].label, "第 3 章 开端")

    def test_a_non_breaking_space_is_collapsed_to_a_plain_one(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "甲\u00a0\u00a0乙")
        self.assertEqual(store.list_for("A")[0].label, "甲 乙")

    def test_leading_and_trailing_whitespace_is_stripped(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "   第七章 结束   ")
        self.assertEqual(store.list_for("A")[0].label, "第七章 结束")


class TestCoercion(StoreTestCase):
    """章号和位置可能来自界面，也可能来自被人手改过的库文件。"""

    def test_garbage_chapter_and_offset_do_not_raise(self) -> None:
        store = self.open_store()
        for junk in ("第三章", None, [], {}, 3.9, True, object()):
            with self.subTest(junk=repr(junk)):
                store.add("A", junk, junk, "垃圾输入")
        rows = store.list_for("A")
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(b.chapter >= 0 and b.offset >= 0 for b in rows))

    def test_negative_chapter_and_offset_are_clamped_to_zero(self) -> None:
        """负数章号会让"跳转"跑到列表外面去，所以在入库前就夹掉。"""
        store = self.open_store()
        store.add("A", -5, -100, "负的")
        bookmark = store.list_for("A")[0]
        self.assertEqual((bookmark.chapter, bookmark.offset), (0, 0))

    def test_a_numeric_string_is_accepted(self) -> None:
        store = self.open_store()
        store.add("A", "12", "34", "字符串数字")
        bookmark = store.list_for("A")[0]
        self.assertEqual((bookmark.chapter, bookmark.offset), (12, 34))

    def test_a_float_is_truncated_rather_than_rejected(self) -> None:
        store = self.open_store()
        store.add("A", 2.9, 99.9, "小数")
        bookmark = store.list_for("A")[0]
        self.assertEqual((bookmark.chapter, bookmark.offset), (2, 99))

    def test_a_bool_is_treated_as_damage_not_as_one(self) -> None:
        """True 是 int 的子类，读成 1 会把数据损坏伪装成合法输入。"""
        store = self.open_store()
        store.add("A", True, False, "布尔")
        self.assertEqual(store.list_for("A")[0].chapter, 0)

    def test_a_garbage_book_id_does_not_raise(self) -> None:
        store = self.open_store()
        for junk in (None, 17, []):
            with self.subTest(junk=repr(junk)):
                store.add(junk, 0, 0, "书名是垃圾")
        self.assertEqual(store.count(), 3)


# ---------------------------------------------------------------------------
# 落盘、排序
# ---------------------------------------------------------------------------

class TestPersistenceAndOrdering(StoreTestCase):
    def test_bookmarks_survive_reopening(self) -> None:
        """持久化的真凭实据：真的文件、真的关掉、真的重开。"""
        first = self.open_store()
        first.add("mr-A", 0, 0, "第一条")
        first.add("mr-A", 5, 20, "第二条")
        self.assertTrue(first.persistent)
        first.close()

        self.assertTrue(self.db_path.exists(), "文件必须真的落在沙盒里")
        again = self.open_store()
        labels = [b.label for b in again.list_for("mr-A")]
        self.assertEqual(labels, ["第一条", "第二条"])
        self.assertEqual(again.count(), 2)

    def test_the_file_really_is_a_sqlite_database(self) -> None:
        """不是"某种自造格式"：任何 SQLite 工具都能打开它，这也是选它的理由。"""
        store = self.open_store()
        store.add("mr-A", 0, 0, "一条")
        store.close()
        self.assertEqual(self.db_path.read_bytes()[:16], b"SQLite format 3\x00")

    def test_ordering_is_chapter_then_offset_then_id(self) -> None:
        """故意乱序插入：列表顺序由数据库决定，不由插入顺序决定。"""
        store = self.open_store()
        store.add("A", 3, 0, "第四章开头")
        store.add("A", 0, 50, "第一章第五十字")
        store.add("A", 0, 10, "第一章第十字")
        store.add("A", 3, 0, "第四章开头的第二条")
        self.assertEqual(
            [(b.chapter, b.offset, b.label) for b in store.list_for("A")],
            [
                (0, 10, "第一章第十字"),
                (0, 50, "第一章第五十字"),
                (3, 0, "第四章开头"),
                (3, 0, "第四章开头的第二条"),
            ],
        )

    def test_the_order_is_the_same_after_reopening(self) -> None:
        """每次打开都重排的话，用户会以为书签自己在动。"""
        store = self.open_store()
        for chapter, offset in ((2, 0), (0, 30), (1, 5), (0, 3)):
            store.add("A", chapter, offset, f"{chapter}-{offset}")
        before = [(b.chapter, b.offset, b.id) for b in store.list_for("A")]
        store.close()
        after = [(b.chapter, b.offset, b.id) for b in self.open_store().list_for("A")]
        self.assertEqual(before, after)

    def test_ids_are_unique_and_increasing(self) -> None:
        store = self.open_store()
        ids = [store.add("A", 0, i, f"第 {i} 条") for i in range(5)]
        self.assertEqual(sorted(set(ids)), ids, "id 必须唯一且递增（界面靠它定位）")

    def test_created_is_a_real_timestamp(self) -> None:
        """created 是 time.time()：没有 sleep，只夹一个前后取的时间窗。"""
        low = time.time()
        store = self.open_store()
        store.add("A", 0, 0, "现在")
        high = time.time()
        self.assertTrue(low <= store.list_for("A")[0].created <= high)


# ---------------------------------------------------------------------------
# 按书隔离
# ---------------------------------------------------------------------------

class TestPerBookScoping(StoreTestCase):
    def test_another_books_bookmarks_are_not_returned(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "甲书的")
        store.add("B", 0, 0, "乙书的")
        self.assertEqual([b.label for b in store.list_for("A")], ["甲书的"])
        self.assertEqual([b.label for b in store.list_for("B")], ["乙书的"])

    def test_an_unknown_book_has_no_bookmarks(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "甲书的")
        self.assertEqual(store.list_for("从来没打开过的书"), [])

    def test_count_without_a_book_id_counts_every_book(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "一")
        store.add("A", 1, 0, "二")
        store.add("B", 0, 0, "三")
        self.assertEqual(store.count(), 3)

    def test_count_with_a_book_id_counts_only_that_book(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "一")
        store.add("A", 1, 0, "二")
        store.add("B", 0, 0, "三")
        self.assertEqual(store.count("A"), 2)
        self.assertEqual(store.count("B"), 1)
        self.assertEqual(store.count("C"), 0)

    def test_clear_book_returns_how_many_rows_went(self) -> None:
        store = self.open_store()
        for i in range(4):
            store.add("A", 0, i, f"第 {i} 条")
        self.assertEqual(store.clear_book("A"), 4)
        self.assertEqual(store.list_for("A"), [])

    def test_clear_book_leaves_other_books_alone(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "甲书的")
        store.add("B", 0, 0, "乙书的")
        store.clear_book("A")
        self.assertEqual([b.label for b in store.list_for("B")], ["乙书的"])
        self.assertEqual(store.count("B"), 1)

    def test_clear_book_on_an_unknown_book_is_zero(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "甲书的")
        self.assertEqual(store.clear_book("没这本书"), 0)
        self.assertEqual(store.count("A"), 1)

    def test_remove_returns_true_then_false(self) -> None:
        """界面靠这个返回值区分「已删除」和「这个书签已经不在了」。"""
        store = self.open_store()
        ident = store.add("A", 0, 0, "删我")
        self.assertTrue(store.remove(ident))
        self.assertFalse(store.remove(ident), "第二次删除必须承认没删到东西")
        self.assertEqual(store.count("A"), 0)

    def test_remove_leaves_the_other_rows(self) -> None:
        store = self.open_store()
        first = store.add("A", 0, 0, "留着")
        second = store.add("A", 1, 0, "删掉")
        self.assertTrue(store.remove(second))
        self.assertEqual([b.id for b in store.list_for("A")], [first])

    def test_remove_of_an_unknown_id_is_false(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "一条")
        self.assertFalse(store.remove(9999))

    def test_remove_rejects_a_garbage_id_without_raising(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "一条")
        for junk in (None, "不是数字", []):
            with self.subTest(junk=repr(junk)):
                self.assertFalse(store.remove(junk))
        self.assertEqual(store.count("A"), 1)


# ---------------------------------------------------------------------------
# 降级：绝不崩
# ---------------------------------------------------------------------------

class TestDegradation(StoreTestCase):
    """书签是锦上添花的功能，它自己坏了不该让阅读器打不开。"""

    def test_a_path_whose_parent_is_a_file_degrades(self) -> None:
        blocker = self.tmp / "这不是目录"
        blocker.write_text("占位", encoding="utf-8")
        store = self.open_store(blocker / DB_NAME)
        self.assertFalse(store.persistent)
        self.assertTrue(store.note)

    def test_a_directory_path_degrades(self) -> None:
        directory = self.tmp / "一个目录"
        directory.mkdir()
        store = self.open_store(directory)
        self.assertFalse(store.persistent)

    def test_a_corrupt_file_degrades_and_is_left_untouched(self) -> None:
        """半倒电留下的坏文件：能用内存库，但绝不能顺手把用户的文件删了。"""
        bad = self.tmp / "corrupt.db"
        original = "这不是一个 sqlite 文件".encode("utf-8") * 20
        bad.write_bytes(original)
        store = self.open_store(bad)
        self.assertFalse(store.persistent)
        store.add("A", 0, 0, "内存里的一条")
        self.assertEqual(bad.read_bytes(), original, "坏文件必须原地不动，用户可能还要救它")

    def test_a_degraded_store_still_does_everything(self) -> None:
        directory = self.tmp / "一个目录"
        directory.mkdir()
        store = self.open_store(directory)
        self.assertFalse(store.persistent)
        ident = store.add("A", 2, 5, "降级也要能用")
        self.assertGreater(ident, 0)
        self.assertEqual([b.label for b in store.list_for("A")], ["降级也要能用"])
        self.assertEqual(store.count("A"), 1)
        self.assertTrue(store.remove(ident))
        self.assertEqual(store.clear_book("A"), 0)

    def test_the_note_is_a_chinese_sentence_the_ui_can_show(self) -> None:
        directory = self.tmp / "一个目录"
        directory.mkdir()
        store = self.open_store(directory)
        self.assertIn("书签", store.note)
        self.assertIn("内存", store.note)
        self.assertIn("丢失", store.note, "必须说清楚后果，用户才知道要不要处理")

    def test_a_healthy_store_has_no_note(self) -> None:
        store = self.open_store()
        self.assertTrue(store.persistent)
        self.assertEqual(store.note, "")

    def test_a_memory_store_is_persistent_but_touches_no_file(self) -> None:
        """:memory: 是"能用的库"，只是不落盘——所以 persistent 是 True，
        而磁盘上不该出现任何文件。这个选择在模块文档里写明了。"""
        store = self.open_store(":memory:")
        self.assertTrue(store.persistent)
        self.assertEqual(store.note, "")
        store.add("A", 0, 0, "只活在内存里")
        self.assertEqual(store.count("A"), 1)
        self.assertFalse(self.db_path.exists())
        self.assertFalse((self.tmp / "MiniReader").exists())

    def test_a_reopened_memory_store_is_empty(self) -> None:
        """内存库的语义要写清楚，免得有人以为 :memory: 也能当缓存。"""
        store = self.open_store(":memory:")
        store.add("A", 0, 0, "只活在内存里")
        store.close()
        self.assertEqual(self.open_store(":memory:").count(), 0)

    def test_the_default_path_is_beside_the_json_settings(self) -> None:
        """默认库走 storage.data_dir()，和 settings.json 同一个目录：删了一个
        文件就少一份数据，用户一眼能看明白。"""
        store = self.open_store(None)
        self.assertTrue(store.persistent)
        self.assertEqual(data_dir(), self.default_db_path.parent)
        self.assertTrue(self.default_db_path.exists())
        self.assertTrue(str(self.default_db_path).startswith(str(self.tmp)), "落在沙盒里")

    def test_an_empty_path_means_the_default(self) -> None:
        """Path("") 是 "."，照字面当路径会变成"这是个目录"，于是每次启动都悄悄
        降级。空串更合理的解释是"没说，用默认的"。"""
        store = self.open_store("")
        self.assertTrue(store.persistent)
        self.assertTrue(self.default_db_path.exists())

    def test_a_db_path_that_is_not_even_a_path_degrades(self) -> None:
        """传进来一个数字或列表是调用方的 bug，但仍然不该在启动路径上抛——
        连降级说明都写不出来才是真的糟。"""
        for junk in (42, ["不是路径"], {"a": 1}):
            with self.subTest(junk=repr(junk)):
                store = self.open_store(junk)
                self.assertFalse(store.persistent)
                self.assertIn("内存", store.note)
                self.assertGreater(store.add("A", 0, 0, "内存里"), 0)

    def test_a_write_failure_mid_session_degrades(self) -> None:
        """只读目录、磁盘写满、文件被换走都发生在第一条 INSERT 上，那时表早就
        建好了，所以降级不能只在打开时跑一次。Windows 上造不出"能建表不能写"
        的文件，所以直接把连接弄坏来模拟。"""
        store = self.open_store()
        store.add("A", 0, 0, "第一条")
        self.assertTrue(store.persistent)
        store._conn.close()  # 外力弄坏了连接：文件被删、盘掉线、别的程序锁住
        ident = store.add("A", 0, 1, "第二条")
        self.assertFalse(store.persistent)
        self.assertIn("内存", store.note)
        self.assertGreater(ident, 0, "降级之后写入仍然要有结果，不能只返回失败")
        self.assertEqual([b.label for b in store.list_for("A")], ["第二条"])


# ---------------------------------------------------------------------------
# 关闭
# ---------------------------------------------------------------------------

class TestClosedStore(StoreTestCase):
    """退出时窗口销毁和库关闭没有固定先后，所以关掉之后所有方法都必须安全。"""

    def test_every_method_is_a_safe_noop_after_close(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "一条")
        store.close()
        self.assertEqual(store.list_for("A"), [])
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.add("A", 0, 1, "又一条"), 0)
        self.assertFalse(store.remove(1))
        self.assertEqual(store.clear_book("A"), 0)

    def test_close_is_idempotent(self) -> None:
        store = self.open_store()
        store.close()
        store.close()
        self.assertEqual(store.list_for("A"), [])

    def test_closing_flushes_so_a_new_store_sees_the_rows(self) -> None:
        store = self.open_store()
        store.add("A", 0, 0, "关之前写的")
        store.close()
        self.assertEqual([b.label for b in self.open_store().list_for("A")], ["关之前写的"])

    def test_closing_a_degraded_store_is_safe(self) -> None:
        directory = self.tmp / "一个目录"
        directory.mkdir()
        store = self.open_store(directory)
        store.close()
        store.close()
        self.assertFalse(store.persistent)
        self.assertEqual(store.list_for("A"), [])


# ---------------------------------------------------------------------------
# 已经存在的库文件
# ---------------------------------------------------------------------------

class TestExistingDatabase(StoreTestCase):
    """老库只能被读和追加，永远不能被 DROP/ALTER——那是用户的书签。"""

    def test_reopening_keeps_rows_and_continues_the_id_sequence(self) -> None:
        first = self.open_store()
        old_id = first.add("A", 0, 0, "旧的一条")
        first.close()

        again = self.open_store()
        new_id = again.add("A", 0, 1, "新的一条")
        self.assertEqual([b.label for b in again.list_for("A")], ["旧的一条", "新的一条"])
        self.assertGreater(new_id, old_id, "id 必须接着往下走，不能从头发号")

    def test_created_is_stored_as_a_real(self) -> None:
        """Schema 要求 REAL：整数时间戳以后要做小数比较时会很别扭。"""
        store = self.open_store()
        store.add("A", 0, 0, "一条")
        store.close()
        conn = self.raw(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT typeof(created) FROM bookmarks").fetchone()[0], "real")
        finally:
            conn.close()

    def test_the_index_exists_and_is_not_duplicated_by_reopening(self) -> None:
        for _ in range(3):
            store = self.open_store()
            store.add("A", 0, 0, "一条")
            store.close()
        conn = self.raw(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_bookmarks_book_pos",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1, "CREATE INDEX IF NOT EXISTS 应该只建一次")

    def test_the_index_covers_book_id_chapter_offset_in_order(self) -> None:
        """索引的列顺序要和 list_for 的排序键一致，否则查询用不上它。"""
        self.open_store().close()
        conn = self.raw(self.db_path)
        try:
            columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_bookmarks_book_pos)")]
        finally:
            conn.close()
        self.assertEqual(columns, ["book_id", "chapter", "offset"])

    def test_an_extra_column_from_a_future_version_survives(self) -> None:
        """未来版本加了一列时，老代码读它不该报错，也不该把那一列删掉。"""
        path = self.tmp / "future.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE bookmarks (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id  TEXT    NOT NULL,
                chapter  INTEGER NOT NULL,
                "offset" INTEGER NOT NULL,
                label    TEXT    NOT NULL DEFAULT '',
                excerpt  TEXT    NOT NULL DEFAULT '',
                created  REAL    NOT NULL,
                source   TEXT    DEFAULT '旧版本'
            )
            """
        )
        conn.commit()
        conn.close()

        store = self.open_store(path)
        self.assertTrue(store.persistent)
        store.add("A", 1, 2, "加了列的库照样能写")
        self.assertEqual([b.label for b in store.list_for("A")], ["加了列的库照样能写"])
        conn = self.raw(path)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(bookmarks)")]
        finally:
            conn.close()
        self.assertIn("source", columns, "不能动用户库里已有的列")

    def test_an_incompatible_table_degrades_without_being_altered(self) -> None:
        """文件名撞上别的程序（或另一个版本的 Schema）时退回内存库，但绝不
        DROP/ALTER 对方的表——那可能是别人攒了很久的数据。"""
        path = self.tmp / "别人.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE bookmarks (foo TEXT)")
        conn.execute("INSERT INTO bookmarks (foo) VALUES ('别人的数据')")
        conn.commit()
        conn.close()

        store = self.open_store(path)
        self.assertFalse(store.persistent)
        self.assertIn("内存", store.note)
        store.add("A", 0, 0, "只看内存")
        self.assertEqual(store.count("A"), 1)

        conn = self.raw(path)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(bookmarks)")]
            rows = conn.execute("SELECT foo FROM bookmarks").fetchall()
        finally:
            conn.close()
        self.assertEqual(columns, ["foo"], "别人的表结构不能被改")
        self.assertEqual(rows, [("别人的数据",)], "别人的数据不能被删")

    def test_a_hand_edited_row_does_not_break_the_list(self) -> None:
        """SQLite 的列类型是"亲和"不是"约束"：手改过的行里 chapter 可能是文本、
        created 可能是"昨天"。一条这样的行不该让整个书签面板空掉。"""
        store = self.open_store()
        store.add("A", 0, 0, "好的一条")
        store.close()

        conn = self.raw(self.db_path)
        conn.execute(
            'INSERT INTO bookmarks (book_id, chapter, "offset", label, excerpt, created)'
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("A", "第三章", -5, "手改的\n一条", "摘要", "昨天"),
        )
        conn.commit()
        conn.close()

        rows = self.open_store().list_for("A")
        self.assertEqual(len(rows), 2)
        hand_edited = next(b for b in rows if b.label != "好的一条")
        self.assertEqual(hand_edited.chapter, 0, "垃圾章号降级成 0，不是异常")
        self.assertEqual(hand_edited.offset, 0)
        self.assertEqual(hand_edited.created, 0.0)
        self.assertEqual(hand_edited.label, "手改的 一条", "读出来也要再清洗一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
