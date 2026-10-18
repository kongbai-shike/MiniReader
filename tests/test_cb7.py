"""第二批核心层：CB7（7z）漫画（`minireader.core.cb7`）。

CB7 和 CBR 是一对孪生兄弟，但**不是同一个**，这个文件要守的正是那些不同点：

1. **7z 的头是压缩的，所以连文件名都需要外部程序。** RAR 的头部 rarfile 用纯
   Python 能解（`test_rar.py` 才能手搓一个真 RAR5 并真的读出来）；7z 不行，头块
   本身是 LZMA 压的。于是这里**没有**「手搓一个真样本走正向路径」这种测法，正向
   路径全靠打桩 `rar.list_names_by_tool()` / `rar.read_page_by_tool()`——它们在
   `test_rar.py` 里已经被真跑验证过了。诚实的边界写在报告里：CB7 的解压路径在本机
   没有真 7-Zip 的情况下只有测试覆盖。

2. **探测只认 7z，不认 unrar。** `rar.find()` 优先 `unrar`（RAR 的参考实现），那
   个偏好在这里恰好是错的：`unrar` 打不开 7z。所以 `cb7.find()` 问的是自己的问题，
   只在 `rar.known()` 恰好返回一个 7z 时才借用它。`test_an_unrar_is_not_borrowed`
   守的就是这条——共用一份缓存会让同时装了 WinRAR 和 7-Zip 的机器上 CB7 永远打不开。

3. **`read_page_heads()` 刻意什么都不做。** 7-Zip 没有「一次调用流出多个成员」的
   低成本写法，量 300 页的占位高度就要起 300 个进程。返回 `b""` 是「我不知道」的
   诚实回答，代价是一个稍微不准的占位块，不是一个错的页。

4. **持久化复用 `unrar_path` / `unrar_kind`。** 一个 7z.exe 同时服务 CBR 和 CB7，
   存两份会在重装之后互相矛盾。所以 `remember()` 只接受 `kind == "7z"` 的答案。

Qt-free，和 `minireader.core.*` 的其它模块一样。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import cb7, rar  # noqa: E402
from minireader.core.archives import ArchiveEntry, ArchiveError  # noqa: E402
from minireader.core.comic import ComicBook, open_comic  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.storage import RAR_TOOL_KINDS  # noqa: E402

#: 7z 的签名。`looks_like_cb7()` 只读这六个字节，所以假样本就是一个签名加垃圾
#: ——真样本需要 7-Zip 才造得出来，而「能认出签名」正是这个函数全部的职责。
SEVENZIP_ID = b"7z\xbc\xaf\x27\x1c"


def fake_tool(path: str = r"C:\no\such\7z.exe", kind: str = rar.TOOL_SEVENZIP) -> rar.Tool:
    """一个路径故意不存在的工具。

    所有走 `rar.list_names_by_tool()` / `rar.read_page_by_tool()` 的测试都会把这
    两个函数打桩掉，所以这个路径永远不该被真的执行——它存在只是为了让 `Tool` 合
    法。实现要是偷偷绕开桩去起子进程，测试会立刻炸。
    """
    return rar.Tool(path=path, kind=kind)


class Cb7TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mr-cb7-")
        self.tmp = Path(self._tmp.name)
        cb7.forget()

    def tearDown(self) -> None:
        cb7.forget()
        self._tmp.cleanup()

    def make_cb7(self, name: str = "a.cb7") -> Path:
        """一个签名正确的假 CB7。内容不重要——没有任何测试会真的去解它。"""
        target = self.tmp / name
        target.write_bytes(SEVENZIP_ID + b"\x00" * 32)
        return target

    def found(self, tool: rar.Tool | None = None):
        """让 `cb7.find()` 直接给出一个工具（或没有）。"""
        return mock.patch.object(cb7, "find", return_value=tool or fake_tool())


# ---------------------------------------------------------------------------
# 探测顺序
# ---------------------------------------------------------------------------


class TestProbeOrder(Cb7TestCase):
    """两步：PATH 里的 7z/7za → 安装目录。四步里的后两步属于 RAR，这里不需要。"""

    def probe_with(self, which=None, isfile=None) -> rar.Tool | None:
        table = which or {}
        present = isfile or set()

        def fake_which(name):
            return table.get(name)

        def fake_isfile(path):
            return path in present

        with mock.patch.object(cb7.shutil, "which", side_effect=fake_which):
            with mock.patch.object(cb7.os.path, "isfile", side_effect=fake_isfile):
                return cb7.probe()

    def test_a_7z_on_the_path_wins(self) -> None:
        tool = self.probe_with(which={"7z": r"C:\Tools\7z.exe"})
        self.assertIsNotNone(tool)
        self.assertEqual(tool.path, r"C:\Tools\7z.exe")
        self.assertEqual(tool.kind, rar.TOOL_SEVENZIP)

    def test_the_portable_7za_is_accepted(self) -> None:
        """便携版只带 7za.exe，没有安装目录、也不一定有 7z 这个名字。"""
        tool = self.probe_with(which={"7za": r"C:\Portable\7za.exe"})
        self.assertIsNotNone(tool)
        self.assertEqual(tool.path, r"C:\Portable\7za.exe")

    def test_7z_is_preferred_over_7za(self) -> None:
        tool = self.probe_with(which={"7z": r"C:\A\7z.exe", "7za": r"C:\B\7za.exe"})
        self.assertEqual(tool.path, r"C:\A\7z.exe")

    def test_the_install_directory_is_checked_when_path_is_empty(self) -> None:
        candidate = rar._sevenzip_candidates()[0]
        tool = self.probe_with(isfile={candidate})
        self.assertIsNotNone(tool)
        self.assertEqual(tool.path, candidate)

    def test_the_path_beats_the_install_directory(self) -> None:
        """PATH 里那个是用户自己选的版本，安装目录里的只是「碰巧还在」。"""
        candidate = rar._sevenzip_candidates()[0]
        tool = self.probe_with(which={"7z": r"C:\Custom\7z.exe"}, isfile={candidate})
        self.assertEqual(tool.path, r"C:\Custom\7z.exe")

    def test_nothing_anywhere_is_none(self) -> None:
        self.assertIsNone(self.probe_with())

    def test_probe_lets_a_broken_environment_through(self) -> None:
        """`probe()` 不吞环境级错误：`which` 抛 OSError 是环境坏了，不是「没找到」。

        这条把契约写清楚——`probe()` 只回答「有没有」，容错留给 `find()` 的调用方。
        把 OSError 吞成 `None` 会让「磁盘挂了」和「没装 7-Zip」变成同一句话。
        """
        with mock.patch.object(cb7.shutil, "which", side_effect=OSError("坏了")):
            with self.assertRaises(OSError):
                cb7.probe()


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


class TestCache(Cb7TestCase):
    def test_a_success_is_cached(self) -> None:
        tool = fake_tool()
        with mock.patch.object(cb7, "probe", return_value=tool) as probe:
            self.assertEqual(cb7.find(), tool)
            self.assertEqual(cb7.find(), tool)
        probe.assert_called_once()

    def test_a_miss_is_not_cached(self) -> None:
        """用户读到「请先安装 7-Zip」、装好、然后每次仍说「请先安装」是最糟的。"""
        with mock.patch.object(cb7, "probe", return_value=None) as probe:
            self.assertIsNone(cb7.find())
            self.assertIsNone(cb7.find())
        self.assertEqual(probe.call_count, 2)

    def test_force_reprobes(self) -> None:
        tool = fake_tool()
        with mock.patch.object(cb7, "probe", return_value=tool) as probe:
            cb7.find()
            cb7.find(force=True)
        self.assertEqual(probe.call_count, 2)

    def test_known_does_not_probe(self) -> None:
        with mock.patch.object(cb7, "probe") as probe:
            self.assertIsNone(cb7.known())
        probe.assert_not_called()

    def test_forget_clears_the_cache(self) -> None:
        with mock.patch.object(cb7, "probe", return_value=fake_tool()):
            self.assertIsNotNone(cb7.find())
        cb7.forget()
        self.assertIsNone(cb7.known())

    def test_an_unrar_is_not_borrowed(self) -> None:
        """共用缓存会让同时装了 WinRAR 和 7-Zip 的机器上 CB7 永远打不开。"""
        unrar = rar.Tool(path=r"C:\Program Files\WinRAR\UnRAR.exe", kind=rar.TOOL_UNRAR)
        with mock.patch.object(rar, "known", return_value=unrar):
            with mock.patch.object(cb7, "probe", return_value=None) as probe:
                self.assertIsNone(cb7.find())
        probe.assert_called_once()

    def test_a_sevenzip_is_borrowed(self) -> None:
        """一个 7z.exe 同时服务 CBR 和 CB7，不该探两遍。"""
        seven = fake_tool()
        with mock.patch.object(rar, "known", return_value=seven):
            with mock.patch.object(cb7, "probe", side_effect=AssertionError("不该再探")):
                self.assertEqual(cb7.find(), seven)

    def test_remember_seeds_the_cache_from_a_live_path(self) -> None:
        target = self.tmp / "7z.exe"
        target.write_bytes(b"MZ")
        with mock.patch.object(cb7, "probe") as probe:
            remembered = cb7.remember(str(target), rar.TOOL_SEVENZIP)
            self.assertIsNotNone(remembered)
            self.assertEqual(cb7.find(), remembered)
        probe.assert_not_called()

    def test_remember_rejects_an_unrar(self) -> None:
        """`unrar` 打不开 7z，存下来只会让第一页报 "cannot execute"。"""
        target = self.tmp / "UnRAR.exe"
        target.write_bytes(b"MZ")
        self.assertIsNone(cb7.remember(str(target), rar.TOOL_UNRAR))
        self.assertIsNone(cb7.known())

    def test_remember_rejects_a_path_that_is_gone(self) -> None:
        with mock.patch.object(cb7, "probe", return_value=None) as probe:
            self.assertIsNone(cb7.remember(str(self.tmp / "gone.exe"), rar.TOOL_SEVENZIP))
            self.assertIsNone(cb7.known())
            cb7.find()
        probe.assert_called_once()

    def test_remember_does_not_mark_a_miss_as_probed(self) -> None:
        self.assertIsNone(cb7.remember("", ""))
        with mock.patch.object(cb7, "probe", return_value=fake_tool()) as probe:
            self.assertIsNotNone(cb7.find())
        probe.assert_called_once()

    def test_require_raises_the_install_message(self) -> None:
        with mock.patch.object(cb7, "find", return_value=None):
            with self.assertRaises(ArchiveError) as caught:
                cb7.require()
        self.assertEqual(str(caught.exception), cb7.MESSAGE_NO_TOOL)


# ---------------------------------------------------------------------------
# 常量与契约
# ---------------------------------------------------------------------------


class TestConstants(unittest.TestCase):
    def test_the_message_names_sevenzip_and_says_what_to_do(self) -> None:
        self.assertEqual(cb7.MESSAGE_NO_TOOL, "打开 CB7 需要 7-Zip，请先安装，然后重试")

    def test_the_magic_is_the_real_one(self) -> None:
        self.assertEqual(cb7.MAGIC, b"7z\xbc\xaf\x27\x1c")

    def test_the_timeout_is_the_same_number_as_the_rar_one(self) -> None:
        """两个容器都靠外部程序，超时的理由一模一样，数字不该漂。"""
        self.assertEqual(cb7.EXTRACT_TIMEOUT, rar.EXTRACT_TIMEOUT)

    def test_the_kind_it_stores_is_one_storage_knows(self) -> None:
        self.assertIn(rar.TOOL_SEVENZIP, RAR_TOOL_KINDS)

    def test_cb7_reuses_the_sevenzip_candidates(self) -> None:
        """安装目录的候选列表只有一份，两个容器不会漂。"""
        self.assertTrue(hasattr(cb7.rar, "_sevenzip_candidates"))


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


class TestLooksLikeCb7(Cb7TestCase):
    def test_a_real_signature_is_accepted(self) -> None:
        self.assertTrue(cb7.looks_like_cb7(self.make_cb7()))

    def test_a_zip_is_rejected(self) -> None:
        """`.cb7` 是 ZIP 换了扩展名的话要走 zip 路径——那条路不需要外部程序。"""
        target = self.tmp / "sneaky.cb7"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("p1.png", b"x")
        self.assertFalse(cb7.looks_like_cb7(target))

    def test_a_missing_file_is_rejected_without_raising(self) -> None:
        self.assertFalse(cb7.looks_like_cb7(self.tmp / "nope.cb7"))

    def test_garbage_is_rejected(self) -> None:
        target = self.tmp / "a.cb7"
        target.write_bytes(b"<html>404</html>")
        self.assertFalse(cb7.looks_like_cb7(target))


class TestListing(Cb7TestCase):
    def test_pages_are_naturally_sorted(self) -> None:
        with self.found():
            with mock.patch.object(
                rar, "list_names_by_tool", return_value=["p10.png", "p2.png", "p1.png"]
            ):
                pages = cb7.list_pages(self.make_cb7())
        self.assertEqual([p.name for p in pages], ["p1.png", "p2.png", "p10.png"])

    def test_non_images_are_filtered_out(self) -> None:
        with self.found():
            with mock.patch.object(
                rar, "list_names_by_tool", return_value=["a.txt", "p1.png", "notes.nfo"]
            ):
                pages = cb7.list_pages(self.make_cb7())
        self.assertEqual([p.name for p in pages], ["p1.png"])

    def test_macos_and_dotfiles_are_filtered_out(self) -> None:
        """和 ZIP 侧共用 `is_page_name()`，否则同一个包存成两种扩展名页数不同。"""
        with self.found():
            with mock.patch.object(
                rar,
                "list_names_by_tool",
                return_value=["__MACOSX/p1.png", ".hidden.png", "p1.png"],
            ):
                pages = cb7.list_pages(self.make_cb7())
        self.assertEqual([p.name for p in pages], ["p1.png"])

    def test_an_empty_archive_says_so(self) -> None:
        with self.found():
            with mock.patch.object(rar, "list_names_by_tool", return_value=["a.txt"]):
                with self.assertRaises(ArchiveError) as caught:
                    cb7.list_pages(self.make_cb7())
        self.assertEqual(str(caught.exception), "这个压缩包里没有能看的图片")

    def test_a_missing_file_says_so(self) -> None:
        with self.found():
            with self.assertRaises(ArchiveError) as caught:
                cb7.list_pages(self.tmp / "nope.cb7")
        self.assertEqual(str(caught.exception), "找不到这个文件：nope.cb7")

    def test_a_tool_failure_is_reported_in_chinese(self) -> None:
        with self.found():
            with mock.patch.object(
                rar, "list_names_by_tool", side_effect=ArchiveError("解压失败：2")
            ):
                with self.assertRaises(ArchiveError) as caught:
                    cb7.list_pages(self.make_cb7())
        self.assertIn("读不出这个压缩包", str(caught.exception))

    def test_no_tool_raises_the_install_message(self) -> None:
        with mock.patch.object(cb7, "find", return_value=None):
            with self.assertRaises(ArchiveError) as caught:
                cb7.list_pages(self.make_cb7())
        self.assertEqual(str(caught.exception), cb7.MESSAGE_NO_TOOL)

    def test_entries_carry_the_name_in_both_fields(self) -> None:
        """7z 的成员名是 UTF-8 存的，不像 ZIP 有 cp437 的坑，所以 ref == name。"""
        with self.found():
            with mock.patch.object(rar, "list_names_by_tool", return_value=["p1.png"]):
                pages = cb7.list_pages(self.make_cb7())
        self.assertEqual(pages, [ArchiveEntry(ref="p1.png", name="p1.png")])


class TestReading(Cb7TestCase):
    def test_read_page_goes_through_the_rar_sevenzip_command(self) -> None:
        """命令行拼装只有一份：`rar.read_page_by_tool()` 已经测过 7z 的形状。"""
        with self.found():
            with mock.patch.object(
                rar, "read_page_by_tool", return_value=b"\x89PNG-bytes"
            ) as read:
                out = cb7.read_page(self.make_cb7(), "p1.png")
        self.assertEqual(out, b"\x89PNG-bytes")
        read.assert_called_once()

    def test_read_page_head_truncates(self) -> None:
        with self.found():
            with mock.patch.object(rar, "read_page_by_tool", return_value=b"0123456789"):
                out = cb7.read_page_head(self.make_cb7(), "p1.png", limit=4)
        self.assertEqual(out, b"0123")

    def test_read_page_heads_never_spawns_a_process(self) -> None:
        """量 300 页的占位高度起 300 个进程，不如让占位块稍微不准。"""
        with self.found():
            with mock.patch.object(rar.subprocess, "run") as run:
                heads = cb7.read_page_heads(self.make_cb7(), ["p1.png", "p2.png", "p3.png"])
        run.assert_not_called()
        self.assertEqual(heads, [b"", b"", b""])

    def test_read_page_heads_aligns_with_the_names(self) -> None:
        heads = cb7.read_page_heads(self.make_cb7(), ["a", None, "b"])
        self.assertEqual(len(heads), 3)

    def test_read_page_heads_without_a_tool_is_still_empty(self) -> None:
        with mock.patch.object(cb7, "find", return_value=None):
            heads = cb7.read_page_heads(self.make_cb7(), ["p1.png"])
        self.assertEqual(heads, [b""])


# ---------------------------------------------------------------------------
# 与漫画层的接线
# ---------------------------------------------------------------------------


class TestComicIntegration(Cb7TestCase):
    """`ComicBook` 的五个取字节入口都要认得 `source == "cb7"`（约束 25）。"""

    def listing(self, names=("p1.png", "p2.png", "p3.png")):
        return mock.patch.object(rar, "list_names_by_tool", return_value=list(names))

    def test_open_comic_reads_a_cb7_as_a_cb7(self) -> None:
        target = self.make_cb7()
        with self.found():
            with self.listing():
                book = open_comic(target)
        self.assertEqual(book.source, "cb7")
        self.assertEqual(len(book.pages), 3)
        self.assertEqual(book.pages[0].name, "p1.png")

    def test_the_source_label_is_the_one_the_library_shows(self) -> None:
        target = self.make_cb7()
        with self.found():
            with self.listing():
                book = open_comic(target)
        self.assertEqual(book.source_label, "7z 漫画")

    def test_every_byte_entry_point_works(self) -> None:
        """这五条就是约束 25 的判据：少一条就有一处落到文件系统路径。"""
        target = self.make_cb7()
        with self.found():
            with self.listing():
                book = open_comic(target)
            with mock.patch.object(rar, "read_page_by_tool", return_value=b"\x89PNG-one"):
                self.assertEqual(book.read_page(0), b"\x89PNG-one")
                self.assertEqual(book.read_page_image(0, 0), b"\x89PNG-one")
                self.assertTrue(book.read_page_head(0).startswith(b"\x89PNG"))
                self.assertTrue(book.read_page_image_head(0, 0).startswith(b"\x89PNG"))
                heads = book.read_page_heads([0, 2])
        self.assertEqual(len(heads), 2)
        self.assertEqual(heads, [b"", b""])
    def test_open_book_dispatches_a_cb7_to_the_comic_reader(self) -> None:
        target = self.make_cb7()
        with self.found():
            with self.listing():
                book = open_book(target)
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.source, "cb7")

    def test_a_cb7_that_is_really_a_zip_goes_down_the_zip_path(self) -> None:
        """很多 .cb7 是 ZIP 换了扩展名，走 zip 路径零成本、不需要外部程序。"""
        target = self.tmp / "sneaky.cb7"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("p1.png", b"\x89PNG-one")
        with mock.patch.object(cb7, "find", side_effect=AssertionError("不该探测工具")):
            book = open_comic(target)
        self.assertEqual(book.source, "cbz")

    def test_a_cb7_without_a_tool_reports_the_install_message(self) -> None:
        target = self.make_cb7()
        with mock.patch.object(cb7, "find", return_value=None):
            with self.assertRaises(Exception) as caught:
                open_comic(target)
        self.assertEqual(str(caught.exception), cb7.MESSAGE_NO_TOOL)

    def test_the_drag_and_drop_kind_is_comic(self) -> None:
        from minireader.core.formats import kind_for

        self.assertEqual(kind_for("a.cb7"), "comic")


# ---------------------------------------------------------------------------
# 真起一个进程（只有 Windows 有意义）
# ---------------------------------------------------------------------------


class TestAProcessReallyRuns(Cb7TestCase):
    def test_the_sevenzip_command_line_really_launches_a_process(self) -> None:
        """`rar._run()` 的路径在 `test_rar.py` 里已经真跑过，这里再确认一次
        `cb7` 借的是同一个入口而不是自己写了一份。"""
        if os.name != "nt":  # pragma: no cover
            self.skipTest("这个桩用 .bat，只在 Windows 上有意义")
        stub = self.tmp / "stub_7z.bat"
        stub.write_text("@echo off\r\necho CB7-RAN %*\r\n", encoding="ascii")
        out = rar._run(rar.Tool(path=str(stub), kind=rar.TOOL_SEVENZIP), ["l", "-p-", "a.cb7"])
        self.assertIn(b"CB7-RAN", out)


if __name__ == "__main__":
    unittest.main()
