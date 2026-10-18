"""阶段 8 核心层：CBR/RAR 漫画（`minireader.core.rar`）。

RAR 是这个项目里唯一**不能**用纯 Python 解压的漫画容器：压缩算法是专有的，
`rarfile` 只用 Python 解析**头部**，取字节要调外部程序。于是这个文件要测的东西
比 `test_comic.py` 里的 ZIP 部分多一层：**找工具**、**记住找到过**、**调子进程时
的那几个保命参数**。

四条设计要点决定了这里的测法：

1. **假 RAR 是手搓的，而且是真 RAR。** `build_rar()` 用 `struct.pack` + 变长整数
   拼出真的 RAR5 签名、MAIN 块、FILE 块和 ENDARC 块，成员用 **STORED（不压缩）**
   方式存。这不是凑合：`rarfile` 对 `RAR_M0` 成员走 `DirectReader`，纯 Python 读
   字节，**完全不碰外部程序**。于是「列页、读一页、读页头」这些正向路径在本机
   没装 WinRAR/7-Zip 的情况下也能真跑，而不是全靠打桩。手搓 RAR 的好处和阶段 6
   手搓 MOBI 一样：样本在仓库里，不依赖下载，也不依赖用户机器上装了什么。

2. **探测顺序是打桩测的，因为真机上只有一种结果。** `probe()` 的四步（PATH 里的
   unrar → PATH 里的 7z/7za → WinRAR 安装目录 → 7-Zip 安装目录）在任意一台机器
   上只能命中一步，所以顺序本身必须靠打桩 `shutil.which` / `os.path.isfile` 来证。
   打桩的粒度是**函数**而不是模块状态：`probe()` 不读也不写缓存，是纯的那一半。

3. **缓存的行为分成「成功」和「失败」两种，这是刻意的。** 成功的探测结果缓存一
   整个会话；**失败的不缓存**——用户读到「请先安装 7-Zip」、装好、然后每次仍说
   「请先安装」比多跑四次 `which` 糟得多。`test_a_failed_probe_is_not_cached`
   守的就是这条。

4. **子进程参数是安全属性，不是实现细节。** 没有 `-p-`，带密码的 RAR 会让 unrar
   等 stdin 等到天荒地老；没有 `CREATE_NO_WINDOW`，GUI 程序每翻一页闪一个黑框；
   没有 timeout，一个坏包能把窗口锁死。这三条都断言在**真正传下去的参数**上
   （打桩 `subprocess.run` 检查 `cmd` 与 `kwargs`），而不是断言源码里有那行字。

Qt-free，和 `minireader.core.*` 的其它模块一样。
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import rar  # noqa: E402
from minireader.core.archives import ArchiveEntry, ArchiveError  # noqa: E402
from minireader.core.comic import ComicBook, open_comic  # noqa: E402
from minireader.core.opener import open_book  # noqa: E402
from minireader.storage import RAR_TOOL_KINDS  # noqa: E402

#: RAR5 的签名。注意 `rarfile.is_rarfile()` 也认 RAR3 的
#: `b"Rar!\x1a\x07\x00"`，但这里只造 RAR5：它是当前 WinRAR 的默认格式，头部
#: 结构也更好手搓（全部变长整数，没有 RAR3 那种固定长度的字段）。
RAR5_ID = b"Rar!\x1a\x07\x01\x00"


# ---------------------------------------------------------------------------
# 手搓一个真的 RAR5
# ---------------------------------------------------------------------------


def _vint(value: int) -> bytes:
    """RAR5 的变长整数：每字节 7 位，高位表示还有后续。"""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _vstr(text: str) -> bytes:
    """变长整数长度 + UTF-8 字节。"""
    raw = text.encode("utf-8")
    return _vint(len(raw)) + raw


def _block(block_type: int, payload: bytes, add_size: int = 0) -> bytes:
    """一个 RAR5 块：CRC32(头) + 头长度 + 类型 + 标志 + [数据区长度] + 载荷。

    `add_size` 非零时设置 `RAR5_BLOCK_FLAG_DATA_AREA`（0x2），块后面跟着那么多
    字节的成员数据。CRC 只覆盖头部（`hdata[4:]`），这正是 `rarfile` 校验的方式。
    """
    flags = 0x2 if add_size else 0x0
    body = _vint(block_type) + _vint(flags)
    if add_size:
        body += _vint(add_size)
    body += payload
    head = _vint(len(body)) + body
    return struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF) + head


def _file_block(name: str, data: bytes, compress_flags: int = 0) -> bytes:
    """一个 FILE 块 + 它的数据。

    `file_compress_flags = 0` 让 `rarfile` 把 `compress_type` 算成 `RAR_M0`，
    于是读取走 `DirectReader`——**纯 Python，不启动任何外部程序**。这就是这个
    文件能在没装 WinRAR 的机器上测正向路径的原因。

    反过来，`compress_flags = 1 << 7` 造出**真的压缩过**的成员，`rarfile` 就必须
    起外部程序才能解。本机没有那个程序，于是它会抛 `RarCannotExec`——这正是
    兜底路径的触发条件，见 `TestTheFallbackIsReachable`。
    """
    payload = _vint(0x4)  # RAR5_FILE_FLAG_HAS_CRC32
    payload += _vint(len(data))  # file_size
    payload += _vint(0o644 << 16)  # mode（权限在低位，高位是 unix 类型）
    payload += struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
    payload += _vint(compress_flags)  # 0 → RAR_M0；1<<7 → 走外部程序
    payload += _vint(0)  # file_host_os → RAR5_OS_WINDOWS
    payload += _vstr(name)
    return _block(0x2, payload, add_size=len(data)) + data


def build_rar(
    path: Path, members: dict[str, bytes], compress_flags: int = 0
) -> Path:
    """写一个真的 RAR5 到 `path`。返回 `path`。

    `compress_flags=0`（默认）成员全是 STORED，纯 Python 可读；非零造出「需要
    外部程序」的成员，用来把兜底路径逼出来。
    """
    out = bytearray(RAR5_ID)
    out += _block(0x1, _vint(0))  # MAIN，main_flags = 0
    for name, data in members.items():
        out += _file_block(name, data, compress_flags)
    out += _block(0x5, _vint(0))  # ENDARC
    path.write_bytes(bytes(out))
    return path


def fake_tool(path: str = r"C:\no\such\UnRAR.exe", kind: str = rar.TOOL_UNRAR) -> rar.Tool:
    """一个「已找到工具」的答案，路径故意不存在。

    STORED 成员永远不需要那个程序真的存在，所以用它来测正向路径反而更强：如果
    实现偷偷启动了子进程，测试会因为「找不到程序」而炸。
    """
    return rar.Tool(path=path, kind=kind)


class RarTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-rar-"))
        rar.forget()

    def tearDown(self) -> None:
        rar.forget()

    def make_rar(self, members: dict[str, bytes], name: str = "book.cbr") -> Path:
        return build_rar(self.tmp / name, members)

    def found(self, tool: rar.Tool | None = None):
        """把 `find()` 钉成给定答案（默认「找到一个工具」）。"""
        return mock.patch.object(rar, "find", return_value=tool or fake_tool())


# ---------------------------------------------------------------------------
# 探测顺序
# ---------------------------------------------------------------------------


class TestProbeOrder(RarTestCase):
    """四步探测的顺序。每一条都打桩 `which` / `isfile`，因为真机上只有一种结果。"""

    def probe_with(self, *, on_path=(), on_disk=()) -> rar.Tool | None:
        """让 PATH 只认出 `on_path`，让磁盘上只有 `on_disk` 存在。"""
        def which(name):
            return f"/usr/bin/{name}" if name in on_path else None

        def isfile(path):
            return path in on_disk

        with mock.patch.object(rar.shutil, "which", side_effect=which):
            with mock.patch.object(rar.os.path, "isfile", side_effect=isfile):
                return rar.probe()

    def test_unrar_on_the_path_wins(self) -> None:
        found = self.probe_with(on_path=("unrar", "7z"))
        self.assertIsNotNone(found)
        self.assertEqual(found.kind, rar.TOOL_UNRAR)
        self.assertEqual(found.path, "/usr/bin/unrar")

    def test_sevenzip_on_the_path_is_the_second_choice(self) -> None:
        found = self.probe_with(on_path=("7z",))
        self.assertEqual(found.kind, rar.TOOL_SEVENZIP)
        self.assertEqual(found.path, "/usr/bin/7z")

    def test_the_standalone_sevenzip_is_accepted_too(self) -> None:
        """便携版 7-Zip 只有 `7za.exe`，没有 `7z.exe`。"""
        found = self.probe_with(on_path=("7za",))
        self.assertEqual(found.kind, rar.TOOL_SEVENZIP)
        self.assertEqual(found.path, "/usr/bin/7za")

    def test_winrar_install_directory_is_checked_when_the_path_has_nothing(self) -> None:
        """WinRAR **不**把自己加进 PATH，所以这一步是正常 Windows 安装上唯一的发现途径。"""
        candidates = rar._winrar_candidates()
        self.assertTrue(candidates, "至少要有一个候选路径，否则这条永远测不到")
        found = self.probe_with(on_disk=(candidates[0],))
        self.assertEqual(found.kind, rar.TOOL_UNRAR)
        self.assertEqual(found.path, candidates[0])

    def test_sevenzip_install_directory_is_the_last_step(self) -> None:
        candidates = rar._sevenzip_candidates()
        found = self.probe_with(on_disk=(candidates[0],))
        self.assertEqual(found.kind, rar.TOOL_SEVENZIP)
        self.assertEqual(found.path, candidates[0])

    def test_the_path_is_checked_before_the_install_directories(self) -> None:
        """两边都命中时，PATH 那个赢——它是用户 PATH 里明确放着的。"""
        on_disk = rar._winrar_candidates()[:1]
        found = self.probe_with(on_path=("7z",), on_disk=on_disk)
        self.assertEqual(found.path, "/usr/bin/7z")

    def test_winrar_is_checked_before_sevenzip(self) -> None:
        """两个安装目录都存在时，先报 WinRAR：它是参考实现。"""
        found = self.probe_with(
            on_disk=rar._sevenzip_candidates()[:1] + rar._winrar_candidates()[:1]
        )
        self.assertEqual(found.kind, rar.TOOL_UNRAR)

    def test_7z_exe_is_preferred_over_7za_exe_in_the_install_directory(self) -> None:
        candidates = rar._sevenzip_candidates()
        seven_z = [p for p in candidates if p.endswith("7z.exe")]
        seven_za = [p for p in candidates if p.endswith("7za.exe")]
        self.assertTrue(seven_z and seven_za)
        found = self.probe_with(on_disk=tuple(seven_z + seven_za))
        self.assertEqual(found.path, seven_z[0])

    def test_nothing_anywhere_gives_none(self) -> None:
        self.assertIsNone(self.probe_with())

    def test_the_winrar_candidate_is_the_command_line_tool(self) -> None:
        """`UnRAR.exe` 而不是 `WinRAR.exe`：后者是 GUI，命令行给它没用。"""
        for path in rar._winrar_candidates():
            self.assertTrue(path.endswith("UnRAR.exe"), path)
            self.assertNotIn("WinRAR.exe", path)

    def test_the_program_files_roots_are_deduplicated(self) -> None:
        """32 位 Windows 没有 ProgramW6432，而有它的机器上两个变量常常相等。"""
        env = {"ProgramW6432": r"C:\Program Files", "ProgramFiles": r"C:\Program Files",
               "ProgramFiles(x86)": r"C:\Program Files (x86)"}
        with mock.patch.dict(os.environ, env, clear=True):
            roots = rar._program_files()
        self.assertEqual(roots, (r"C:\Program Files", r"C:\Program Files (x86)"))

    def test_a_machine_without_program_files_variables_has_no_candidates(self) -> None:
        """兜底：环境变量全空时返回空元组，而不是拼出 `WinRAR\\UnRAR.exe`。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rar._program_files(), ())
            self.assertEqual(rar._winrar_candidates(), ())


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


class TestCache(RarTestCase):
    def test_a_successful_probe_is_cached(self) -> None:
        tool = fake_tool()
        with mock.patch.object(rar, "probe", return_value=tool) as probe:
            self.assertEqual(rar.find(), tool)
            self.assertEqual(rar.find(), tool)
        self.assertEqual(probe.call_count, 1, "第二次 find() 不该再探测")

    def test_a_failed_probe_is_not_cached(self) -> None:
        """失败不缓存：用户装好工具之后必须能立刻成功，不该等到重启。"""
        with mock.patch.object(rar, "probe", return_value=None) as probe:
            self.assertIsNone(rar.find())
            self.assertIsNone(rar.find())
        self.assertEqual(probe.call_count, 2)

    def test_force_reprobes(self) -> None:
        tool = fake_tool()
        with mock.patch.object(rar, "probe", return_value=tool) as probe:
            rar.find()
            rar.find(force=True)
        self.assertEqual(probe.call_count, 2)

    def test_known_never_probes(self) -> None:
        """重绘、tooltip 这类不能阻塞的调用者用 `known()`。"""
        with mock.patch.object(rar, "probe") as probe:
            self.assertIsNone(rar.known())
        probe.assert_not_called()

    def test_forget_drops_the_answer(self) -> None:
        tool = fake_tool()
        with mock.patch.object(rar, "probe", return_value=tool):
            rar.find()
            self.assertEqual(rar.known(), tool)
            rar.forget()
            self.assertIsNone(rar.known())

    def test_remember_seeds_the_cache_from_a_live_path(self) -> None:
        target = self.tmp / "UnRAR.exe"
        target.write_bytes(b"MZ")
        with mock.patch.object(rar, "probe") as probe:
            remembered = rar.remember(str(target), rar.TOOL_UNRAR)
            self.assertIsNotNone(remembered)
            self.assertEqual(rar.find(), remembered)
        probe.assert_not_called()

    def test_remember_rejects_a_path_that_is_gone(self) -> None:
        """settings.json 可能来自另一台机器，或者工具后来被卸了。"""
        with mock.patch.object(rar, "probe", return_value=None) as probe:
            self.assertIsNone(rar.remember(str(self.tmp / "gone.exe"), rar.TOOL_UNRAR))
            self.assertIsNone(rar.known())
            rar.find()
        probe.assert_called_once()

    def test_remember_rejects_an_unknown_kind(self) -> None:
        """kind 决定命令行方言，认不出来就必须重新探测而不是猜。"""
        target = self.tmp / "tool.exe"
        target.write_bytes(b"MZ")
        self.assertIsNone(rar.remember(str(target), "winrar-gui"))
        self.assertIsNone(rar.known())

    def test_remember_rejects_an_empty_path(self) -> None:
        self.assertIsNone(rar.remember("", rar.TOOL_UNRAR))
        self.assertIsNone(rar.remember(None, rar.TOOL_UNRAR))  # type: ignore[arg-type]
        self.assertIsNone(rar.known())

    def test_remember_does_not_mark_a_miss_as_probed(self) -> None:
        """否则一次失败的 `remember()` 会让后面的 `find()` 信任一个空答案。"""
        self.assertIsNone(rar.remember("", ""))
        with mock.patch.object(rar, "probe", return_value=fake_tool()) as probe:
            self.assertIsNotNone(rar.find())
        probe.assert_called_once()

    def test_require_raises_the_install_message(self) -> None:
        with mock.patch.object(rar, "find", return_value=None):
            with self.assertRaises(ArchiveError) as caught:
                rar.require()
        self.assertEqual(str(caught.exception), rar.MESSAGE_NO_TOOL)

    def test_require_returns_the_tool(self) -> None:
        tool = fake_tool()
        with mock.patch.object(rar, "find", return_value=tool):
            self.assertEqual(rar.require(), tool)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


class TestConstants(unittest.TestCase):
    def test_the_message_names_both_programs_and_says_what_to_do(self) -> None:
        self.assertEqual(
            rar.MESSAGE_NO_TOOL, "打开 CBR/RAR 需要 WinRAR 或 7-Zip，请先安装其中一个，然后重试"
        )

    def test_the_tool_kinds_agree_with_storage(self) -> None:
        """`storage.py` 刻意重复了这份元组（它不 import core.rar），两份不许漂。"""
        self.assertEqual(rar.TOOL_KINDS, RAR_TOOL_KINDS)

    def test_create_no_window_is_the_windows_flag(self) -> None:
        self.assertEqual(rar.CREATE_NO_WINDOW, 0x08000000)

    def test_the_timeout_is_short_enough_to_keep_the_window_alive(self) -> None:
        self.assertLessEqual(rar.EXTRACT_TIMEOUT, 60)
        self.assertGreater(rar.EXTRACT_TIMEOUT, 0)


# ---------------------------------------------------------------------------
# 子进程参数：这是安全属性，不是实现细节
# ---------------------------------------------------------------------------


class TestSubprocessArguments(RarTestCase):
    """每条都断言**真正传下去的参数**，而不是源码里有那行字。"""

    def run_with(self, *args: str, stdout: bytes = b"ok", returncode: int = 0,
                 stderr: bytes = b"") -> tuple[list[str], dict]:
        done = subprocess.CompletedProcess(args=[], returncode=returncode,
                                           stdout=stdout, stderr=stderr)
        with mock.patch.object(rar.subprocess, "run", return_value=done) as run:
            rar._run(fake_tool(), list(args))
        cmd, kwargs = run.call_args.args[0], run.call_args.kwargs
        return cmd, kwargs

    def list_cmd(self, kind: str = rar.TOOL_UNRAR) -> list[str]:
        """走真正的 `list_names_by_tool()`，看它到底拼出什么命令行。"""
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(rar.subprocess, "run", return_value=done) as run:
            rar.list_names_by_tool(fake_tool(kind=kind), self.tmp / "a.cbr")
        return run.call_args.args[0]

    def test_every_call_hides_the_console_window(self) -> None:
        """GUI 程序不加这个，每翻一页闪一个黑框。"""
        _cmd, kwargs = self.run_with("lb")
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_every_call_refuses_to_read_stdin(self) -> None:
        """`stdin=DEVNULL` 与 `-p-` 是一对：一个堵住提示，一个堵住读取。"""
        _cmd, kwargs = self.run_with("lb")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_every_call_has_a_timeout(self) -> None:
        _cmd, kwargs = self.run_with("lb")
        self.assertEqual(kwargs["timeout"], rar.EXTRACT_TIMEOUT)

    def test_unrar_listing_disables_the_password_prompt(self) -> None:
        """没有 `-p-`，带密码的 RAR 会让 unrar 等 stdin 等到超时。"""
        cmd = self.list_cmd()
        self.assertIn("-p-", cmd)
        self.assertIn("--", cmd, "`--` 之后一律当文件名，防止以 - 开头的成员被当参数")

    def test_unrar_listing_uses_the_bare_name_mode(self) -> None:
        self.assertEqual(self.list_cmd()[1], "lb")

    def test_sevenzip_listing_disables_the_password_prompt(self) -> None:
        cmd = self.list_cmd(kind=rar.TOOL_SEVENZIP)
        self.assertIn("-p-", cmd)
        self.assertIn("-slt", cmd)

    def test_unrar_extraction_disables_the_password_prompt(self) -> None:
        tool = fake_tool()
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"x", stderr=b"")
        with mock.patch.object(rar.subprocess, "run", return_value=done) as run:
            rar.read_page_by_tool(tool, self.tmp / "a.cbr", "p1.png")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[1], "p")
        self.assertIn("-p-", cmd)
        self.assertIn("-inul", cmd)
        self.assertIn("p1.png", cmd)

    def test_sevenzip_extraction_streams_to_stdout(self) -> None:
        tool = fake_tool(kind=rar.TOOL_SEVENZIP)
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"x", stderr=b"")
        with mock.patch.object(rar.subprocess, "run", return_value=done) as run:
            rar.read_page_by_tool(tool, self.tmp / "a.cbr", "p1.png")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[1], "x")
        self.assertIn("-so", cmd)
        self.assertIn("-p-", cmd)

    def test_a_timeout_says_what_it_means(self) -> None:
        with mock.patch.object(
            rar.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="unrar", timeout=1)
        ):
            with self.assertRaises(ArchiveError) as caught:
                rar._run(fake_tool(), ["lb"])
        text = str(caught.exception)
        self.assertIn("超时", text)
        self.assertIn(str(rar.EXTRACT_TIMEOUT), text)

    def test_a_missing_program_says_install_one(self) -> None:
        """工具在探测之后被删了（或者路径是假的），报的仍是那句安装话。"""
        with mock.patch.object(rar.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(ArchiveError) as caught:
                rar._run(fake_tool(), ["lb"])
        self.assertEqual(str(caught.exception), rar.MESSAGE_NO_TOOL)

    def test_a_nonzero_exit_reports_the_tool_message(self) -> None:
        with mock.patch.object(
            rar.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=2, stdout=b"", stderr=b"wrong password"
            ),
        ):
            with self.assertRaises(ArchiveError) as caught:
                rar._run(fake_tool(), ["lb"])
        self.assertIn("wrong password", str(caught.exception))


class TestSevenZipListing(unittest.TestCase):
    """`7z l -slt` 的输出是空行分隔的 `Key = Value` 记录。"""

    LISTING = "\n\n".join(
        [
            "Path = C:\\books\\a.cbr\nType = Rar\nPhysical Size = 1024\nHeaders Size = 100",
            "Path = p1.png\nSize = 10\nPacked Size = 10\nFolder = -\n",
            "Path = sub\nSize = 0\nFolder = +\n",
            "Path = p2.png\nSize = 20\nFolder = -\n",
        ]
    )

    def test_it_returns_the_member_paths(self) -> None:
        self.assertEqual(rar._parse_sevenzip_listing(self.LISTING), ["p1.png", "p2.png"])

    def test_it_skips_the_archive_record(self) -> None:
        """第一个记录描述压缩包自己，靠 `Physical Size` 认出来。"""
        self.assertNotIn("C:\\books\\a.cbr", rar._parse_sevenzip_listing(self.LISTING))

    def test_it_skips_directories(self) -> None:
        self.assertNotIn("sub", rar._parse_sevenzip_listing(self.LISTING))

    def test_empty_output_is_empty(self) -> None:
        self.assertEqual(rar._parse_sevenzip_listing(""), [])


# ---------------------------------------------------------------------------
# 正向路径：手搓的 STORED RAR 不需要任何外部程序
# ---------------------------------------------------------------------------


class TestStoredArchive(RarTestCase):
    """这些是**真的**在读一个 RAR，只是成员没有压缩，所以纯 Python 就能取字节。

    打桩的只有 `find()`——它回答「用哪个工具」，而 STORED 成员根本不会用到那个
    程序（`fake_tool()` 的路径故意不存在，实现要是偷偷起子进程这里就会炸）。
    """

    MEMBERS = {"p1.png": b"\x89PNG-one", "p2.png": b"\x89PNG-two"}

    def test_looks_like_rar_says_yes(self) -> None:
        self.assertTrue(rar.looks_like_rar(self.make_rar(self.MEMBERS)))

    def test_looks_like_rar_says_no_to_a_zip(self) -> None:
        target = self.tmp / "a.cbr"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("p1.png", b"x")
        self.assertFalse(rar.looks_like_rar(target))

    def test_looks_like_rar_says_no_to_a_missing_file(self) -> None:
        """拖放和文件对话框都会问它，所以它绝不能抛。"""
        self.assertFalse(rar.looks_like_rar(self.tmp / "nope.cbr"))

    def test_looks_like_rar_says_no_to_garbage(self) -> None:
        target = self.tmp / "a.cbr"
        target.write_bytes(b"<html>404</html>")
        self.assertFalse(rar.looks_like_rar(target))

    def test_list_pages_returns_the_images_in_reading_order(self) -> None:
        target = self.make_rar({"p2.png": b"b", "p10.png": b"c", "p1.png": b"a"})
        with self.found():
            pages = rar.list_pages(target)
        # 自然排序：p1 < p2 < p10，而不是字典序 p1 < p10 < p2。
        self.assertEqual([p.name for p in pages], ["p1.png", "p2.png", "p10.png"])

    def test_list_pages_skips_non_images(self) -> None:
        target = self.make_rar({"p1.png": b"a", "readme.txt": b"hi", "Thumbs.db": b"x"})
        with self.found():
            pages = rar.list_pages(target)
        self.assertEqual([p.name for p in pages], ["p1.png"])

    def test_list_pages_skips_macosx_junk_and_dot_files(self) -> None:
        target = self.make_rar(
            {"p1.png": b"a", "__MACOSX/p0.png": b"junk", ".hidden.png": b"x"}
        )
        with self.found():
            pages = rar.list_pages(target)
        self.assertEqual([p.name for p in pages], ["p1.png"])

    def test_an_archive_without_images_says_so(self) -> None:
        target = self.make_rar({"readme.txt": b"hi"})
        with self.found():
            with self.assertRaises(ArchiveError) as caught:
                rar.list_pages(target)
        self.assertEqual(str(caught.exception), "这个压缩包里没有能看的图片")

    def test_read_page_returns_the_bytes(self) -> None:
        target = self.make_rar(self.MEMBERS)
        with self.found():
            self.assertEqual(rar.read_page(target, "p1.png"), b"\x89PNG-one")

    def test_read_page_head_returns_only_the_prefix(self) -> None:
        target = self.make_rar({"p1.png": b"\x89PNG" + b"\x00" * 5000})
        with self.found():
            head = rar.read_page_head(target, "p1.png", limit=16)
        self.assertEqual(len(head), 16)
        self.assertTrue(head.startswith(b"\x89PNG"))

    def test_read_page_heads_stays_aligned_and_swallows_failures(self) -> None:
        """一个坏成员不能连累另外 299 个的高度。"""
        target = self.make_rar(self.MEMBERS)
        with self.found():
            heads = rar.read_page_heads(target, ["p1.png", "missing.png", "p2.png"])
        self.assertEqual(len(heads), 3)
        self.assertTrue(heads[0].startswith(b"\x89PNG"))
        self.assertEqual(heads[1], b"")
        self.assertTrue(heads[2].startswith(b"\x89PNG"))

    def test_read_page_heads_tolerates_none_entries(self) -> None:
        """配对阅读会传进 `None`（孤页的第二个槽），不能因此炸。"""
        target = self.make_rar(self.MEMBERS)
        with self.found():
            heads = rar.read_page_heads(target, ["p1.png", None])  # type: ignore[list-item]
        self.assertEqual(len(heads), 2)
        self.assertEqual(heads[1], b"")

    def test_read_page_heads_without_a_tool_is_all_blank(self) -> None:
        """没工具时每页「尺寸未知」，而不是在布局中途抛异常。"""
        target = self.make_rar(self.MEMBERS)
        with mock.patch.object(rar, "find", return_value=None):
            heads = rar.read_page_heads(target, ["p1.png", "p2.png"])
        self.assertEqual(heads, [b"", b""])

    def test_a_stored_member_is_read_without_ever_launching_the_tool(self) -> None:
        """这是本文件能在没装 WinRAR 的机器上测正向路径的全部理由。

        `fake_tool()` 的路径不存在；`rarfile` 对 `RAR_M0` 成员走 `DirectReader`，
        纯 Python 读字节。要是哪天实现改成「一律走子进程」，这条会先炸。
        """
        target = self.make_rar(self.MEMBERS)
        with mock.patch.object(rar.subprocess, "run") as run:
            with self.found(fake_tool(path=str(self.tmp / "definitely-not-here.exe"))):
                self.assertEqual(rar.read_page(target, "p2.png"), b"\x89PNG-two")
        run.assert_not_called()


class TestTheFallbackIsReachable(RarTestCase):
    """兜底子进程路径必须**从公开入口走得到**，不能只是存在。

    这是本文件里最该存在的一类测试，因为第一版实现漏了它：`_run()`、
    `list_names_by_tool()`、`read_page_by_tool()` 三个函数写好了、有 13 条测试，
    但**没有任何生产代码调用它们**。`rarfile` 拒绝工具时抛 `RarCannotExec`，
    `read_page()` 把它包成「读不出这一页：Cannot find working tool」就结束了。
    13 条测试全绿，功能是死的——正是 docs/DEVELOPMENT.md 第 28 条说的那件事。

    所以这里测的不是"那几个函数能跑"，而是"**用户打开一个压缩过的 CBR 时，
    它们真的会被跑到**"。造出真的压缩成员（`compress_flags=1<<7`）就能逼出
    那个异常，不需要装任何解压程序：本机没有工具，`rarfile` 必然拒绝。
    """

    MEMBERS = {"p1.png": b"\x89PNG-real-page-bytes", "p2.png": b"\x89PNG-second"}

    def packed(self) -> Path:
        """一个成员**真的被压缩过**的 RAR——`rarfile` 必须起外部程序才能解。"""
        return build_rar(self.tmp / "packed.cbr", self.MEMBERS, compress_flags=1 << 7)

    def test_rarfile_refuses_a_compressed_member_without_a_tool(self) -> None:
        """先证明前提：不装工具时 `rarfile` 确实抛 `RarCannotExec`。

        没有这一条，下面的测试可能因为别的原因通过（比如成员其实是 STORED）。
        """
        target = self.packed()
        import rarfile

        with self.found(fake_tool(path=str(self.tmp / "definitely-not-here.exe"))):
            rf = rarfile.RarFile(str(target))
            try:
                with self.assertRaises(rarfile.RarCannotExec):
                    rf.read("p1.png")
            finally:
                rf.close()

    def test_read_page_falls_back_when_rarfile_refuses_the_tool(self) -> None:
        """公开入口 `read_page()` 必须自己接住 `RarCannotExec` 并走兜底。"""
        target = self.packed()
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self.MEMBERS["p1.png"], stderr=b""
        )
        with mock.patch.object(rar.subprocess, "run", return_value=done) as run:
            with self.found():
                data = rar.read_page(target, "p1.png")
        self.assertEqual(data, self.MEMBERS["p1.png"])
        run.assert_called_once()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[1], "p", "兜底读页用的是 unrar p")
        self.assertIn("-inul", cmd)
        self.assertIn("-p-", cmd)
        self.assertIn("p1.png", cmd)

    def test_read_page_head_falls_back_and_truncates(self) -> None:
        """`read_page_head()` 的兜底要自己截断：外部程序只能整块吐出来。"""
        target = self.packed()
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"\x89PNG" + b"\x00" * 5000, stderr=b""
        )
        with mock.patch.object(rar.subprocess, "run", return_value=done):
            with self.found():
                head = rar.read_page_head(target, "p1.png", limit=16)
        self.assertEqual(len(head), 16)
        self.assertTrue(head.startswith(b"\x89PNG"))

    def test_a_listing_that_cannot_be_parsed_falls_back_to_the_tool(self) -> None:
        """`list_pages()` 的头解析失败时，让工具列一次名字再放弃。"""
        target = self.packed()
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"p1.png\np2.png\n", stderr=b""
        )
        with mock.patch.object(rar.subprocess, "run", return_value=done):
            with self.found():
                with mock.patch.object(
                    rar, "_open", return_value=_unparsable_archive()
                ):
                    pages = rar.list_pages(target)
        self.assertEqual([p.name for p in pages], ["p1.png", "p2.png"])

    def test_a_failed_listing_fallback_reports_the_original_problem(self) -> None:
        """兜底也失败时，报的是原来那个错，不是兜底的错。"""
        target = self.packed()
        with mock.patch.object(
            rar, "list_names_by_tool", side_effect=ArchiveError("工具也列不出来")
        ):
            with self.found():
                with mock.patch.object(
                    rar, "_open", return_value=_unparsable_archive()
                ):
                    with self.assertRaises(ArchiveError) as caught:
                        rar.list_pages(target)
        self.assertIn("读不出这个压缩包", str(caught.exception))

    def test_read_page_heads_does_not_spawn_a_process_per_page(self) -> None:
        """**刻意**不兜底：预读阶段每页起一个进程会把开书变成几十秒。

        `read_page_heads()` 是给滚动模式量占位高度用的，未知尺寸只是占位矮一点，
        比开一本书起 300 个进程好得多。这条测试把那个决定钉住。
        """
        target = self.packed()
        with mock.patch.object(rar.subprocess, "run") as run:
            with self.found():
                heads = rar.read_page_heads(target, ["p1.png", "p2.png"])
        self.assertEqual(heads, [b"", b""])
        run.assert_not_called()

    def test_the_fallback_really_launches_a_process(self) -> None:
        """上面几条都打桩了 `subprocess.run`；这一条**真的起一个进程**。

        用 `.bat` 当工具：Windows 上 `subprocess` 能直接执行它，所以这一条证明的是
        子进程管道本身（`CREATE_NO_WINDOW`、`stdin=DEVNULL`、`timeout`、stdout 抓取）
        在这台机器上是通的，而不只是"参数拼对了"。它不是 unrar，解不了真 RAR，
        所以只断言"进程起来了、输出回来了"。
        """
        if os.name != "nt":  # pragma: no cover
            self.skipTest("这个桩用 .bat，只在 Windows 上有意义")
        stub = self.tmp / "stub_unrar.bat"
        stub.write_text("@echo off\r\necho FALLBACK-RAN %*\r\n", encoding="ascii")
        out = rar._run(rar.Tool(path=str(stub), kind=rar.TOOL_UNRAR), ["p", "-p-", "a.cbr"])
        self.assertIn(b"FALLBACK-RAN", out)
        self.assertIn(b"p", out)


def _unparsable_archive():
    """一个 `_open()` 的替身：`infolist()` 抛异常，`close()` 正常。

    用来模拟「头部解析不了」——真造一个坏头部会让 `RarFile()` 构造函数自己就炸，
    那样测到的就不是 `list_pages()` 里的兜底分支了。
    """

    class _Fake:
        def infolist(self):
            raise RuntimeError("头部解析不了")

        def close(self):
            pass

    return _Fake()


class TestBrokenArchives(RarTestCase):
    def test_a_file_that_is_not_a_rar_says_so(self) -> None:
        target = self.tmp / "a.cbr"
        target.write_bytes(b"<html>404</html>")
        with self.found():
            with self.assertRaises(ArchiveError) as caught:
                rar.list_pages(target)
        self.assertEqual(str(caught.exception), "这个压缩包已损坏或不是 RAR 格式")

    def test_a_missing_file_says_so(self) -> None:
        with self.found():
            with self.assertRaises(ArchiveError) as caught:
                rar.list_pages(self.tmp / "nope.cbr")
        self.assertEqual(str(caught.exception), "找不到这个文件：nope.cbr")

    def test_a_truncated_rar_does_not_raise_something_foreign(self) -> None:
        """头都不完整时 `rarfile` 给的是它自己的异常树，必须翻译成中文。"""
        target = self.tmp / "a.cbr"
        target.write_bytes(RAR5_ID + b"\x00\x01\x02")
        with self.found():
            with self.assertRaises(ArchiveError):
                rar.list_pages(target)


# ---------------------------------------------------------------------------
# 与漫画层的接线
# ---------------------------------------------------------------------------


class TestComicIntegration(RarTestCase):
    """`ComicBook` 的五个取字节入口都要认得 `source == "rar"`（约束 25）。"""

    MEMBERS = {"p1.png": b"\x89PNG-one", "p2.png": b"\x89PNG-two", "p3.png": b"\x89PNG-three"}

    def test_open_comic_reads_a_cbr_as_a_rar(self) -> None:
        target = self.make_rar(self.MEMBERS)
        with self.found():
            book = open_comic(target)
        self.assertEqual(book.source, "rar")
        self.assertEqual(len(book.pages), 3)
        self.assertEqual(book.pages[0].name, "p1.png")

    def test_the_source_label_is_the_one_the_library_shows(self) -> None:
        target = self.make_rar(self.MEMBERS)
        with self.found():
            book = open_comic(target)
        self.assertEqual(book.source_label, "RAR 漫画")

    def test_every_byte_entry_point_works(self) -> None:
        """这五条就是约束 25 的判据：少一条就有一处落到文件系统路径。"""
        target = self.make_rar(self.MEMBERS)
        with self.found():
            book = open_comic(target)
            self.assertEqual(book.read_page(0), b"\x89PNG-one")
            self.assertEqual(book.read_page_image(0, 0), b"\x89PNG-one")
            self.assertTrue(book.read_page_head(0).startswith(b"\x89PNG"))
            self.assertTrue(book.read_page_image_head(0, 0).startswith(b"\x89PNG"))
            heads = book.read_page_heads([0, 2])
        self.assertEqual(len(heads), 2)
        self.assertTrue(all(h.startswith(b"\x89PNG") for h in heads))

    def test_open_book_dispatches_a_cbr_to_the_comic_reader(self) -> None:
        target = self.make_rar(self.MEMBERS)
        with self.found():
            book = open_book(target)
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.source, "rar")

    def test_a_cbr_that_is_really_a_zip_goes_down_the_zip_path(self) -> None:
        """很多 .cbr 是 ZIP 换了扩展名，走 zip 路径零成本、不需要外部程序。"""
        target = self.tmp / "sneaky.cbr"
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("p1.png", b"\x89PNG-one")
        with mock.patch.object(rar, "find", side_effect=AssertionError("不该探测工具")):
            book = open_comic(target)
        self.assertEqual(book.source, "cbz")

    def test_a_cbr_without_a_tool_reports_the_install_message(self) -> None:
        target = self.make_rar(self.MEMBERS)
        with mock.patch.object(rar, "find", return_value=None):
            with self.assertRaises(Exception) as caught:
                open_comic(target)
        self.assertEqual(str(caught.exception), rar.MESSAGE_NO_TOOL)

    def test_entries_carry_the_name_in_both_fields(self) -> None:
        """RAR 的成员名是 UTF-8 存的，不像 ZIP 有 cp437 的坑，所以 ref == name。"""
        target = self.make_rar({"第01话_001.png": b"a"})
        with self.found():
            pages = rar.list_pages(target)
        self.assertEqual(pages, [ArchiveEntry(ref="第01话_001.png", name="第01话_001.png")])


if __name__ == "__main__":
    unittest.main()
