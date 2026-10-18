"""阶段 2 核心层：格式表 / 压缩包 / 图片文件夹 / ComicBook / 打开裁决。

这个文件守的是**核心层**的契约，全部 Qt-free——`minireader.core.*` 里不许出现
Qt 是阶段 1 就立下的规矩（test_store.py 对 store.py、test_book_base.py 对
book_base.py 各立过一次），阶段 2 新增的五个模块一并纳入。所以这里不 import
`minireader.qt`，也不需要 QApplication：`list_archive_pages()` 只按扩展名挑成员、
从不解码，造测试压缩包时塞什么字节都行。

四类断言值得单独说明：

1. **相位闸门是机制，不是巧合**。`tests/test_ui_offscreen.py:371`（断言某个格式
   被拖放拒绝）和 `:393`（`[a.mobi, book.txt, book.txt]` 里挑 book.txt）原本用的是
   `.pdf`，阶段 3 把 PDF 解锁后两条都改成了仍锁着的 `.mobi`——**断言的意思一个字没变，
   换的只是"当前还打不开的格式"这个样本**。靠的就是 `Format.ready`
   （`phase <= CURRENT_PHASE`）。`test_every_format_follows_the_phase_gate` 把这条
   机制钉死：任何一格格式的 `is_supported()` 必须等于它自己的
   `phase <= CURRENT_PHASE`，将来加格式时忘了填 phase 会在这里炸，而不是在用户的拖放里。
   同理，下面凡是拿 `.pdf` 当"打不开的格式"的地方，阶段 3 之后都换成了 `.mobi`；
   **阶段 6 又把 `.mobi` 解锁了，那些地方进一步换成了 `.cbr`（阶段 8）**。
   样本一路往后挪，断言的意思始终是「表里认得、这一版打不开」——这就是「跟随相位」。
   **阶段 8 把 `.cbr`/`.rar` 也解锁了，格式表里再没有锁着的行**：这些地方不再换样本，
   而是把断言翻了一面（"一个都不锁"、"没有格式会报相位"），报相位那条代码路径改用
   打桩的 `Format` 守着，见 `test_support_note_would_name_a_phase_if_one_were_locked`。

2. **中文压缩包成员名**。zipfile 对没有 bit 11 的名字按 cp437 解码，于是 GBK 的
   `测试图片名啊.jpg` 变成 `▓Γ╩╘═╝╞¼├√░í.jpg`。`ArchiveEntry` 因此有**两个**
   名字：`ref`（zipfile 认的原始名，读取必须用它）和 `name`（修复后的显示名，
   排序和显示用它）。只留一个名字的话，"列页对了但翻页报错"是必然结果。
   造这种压缩包的手法见 `make_cp437_zip()`。

3. **排序用修复后的名字**。`natural_key()` 把数字段按整数比，否则
   `page_10` 会排在 `page_2` 前面——漫画最常见的翻页顺序错误。

4. **错误文案是用户可见的**。每条 `ArchiveError` / `BookError` 都断言了文案，
   因为它们会原样出现在底栏的警告位（`window.set_warning("打不开：" + exc)`）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core.archives import (  # noqa: E402
    HEAD_BYTES,
    ArchiveEntry,
    ArchiveError,
    list_archive_pages,
    looks_like_archive,
    natural_key,
    read_member,
    read_member_head,
    repair_name,
)
from minireader.core.book_base import KIND_COMIC, KIND_TEXT  # noqa: E402
from minireader.core.comic import ComicBook, ComicPage, open_comic  # noqa: E402
from minireader.core import formats as formats_module  # noqa: E402
from minireader.core import opener as opener_module  # noqa: E402
from minireader.core import rar as rar_module  # noqa: E402
from minireader.core.formats import (  # noqa: E402
    CURRENT_PHASE,
    FORMATS,
    IMAGE_EXTS,
    Format,
    dialog_filters,
    format_for,
    is_image,
    is_supported,
    kind_for,
    support_note,
)
from minireader.core.images import (  # noqa: E402
    MAX_DEPTH,
    folder_has_images,
    is_image_file,
    list_folder_pages,
    list_single_page,
    read_file,
    read_file_head,
)
from minireader.core.model import BookError  # noqa: E402
from minireader.core.opener import open_book, open_book_kind  # noqa: E402

#: 一个成员名的正文无关紧要——核心层从不解码。用文本而不是真图片字节，是为了让
#: `make_cp437_zip()` 的原地改名不可能误伤到压缩数据。
FAKE_PNG = b"this is not really a png, but core never decodes it\n"


def mojibake(text: str, encoding: str = "gbk") -> str:
    """`text` 以 `encoding` 编码后按 cp437 解码——即 zipfile 看到的那个乱码名。"""
    return text.encode(encoding).decode("cp437")


def make_cp437_zip(path: Path, names: list[str], payload: bytes = FAKE_PNG) -> Path:
    """造一个成员名是 cp437 原始字节、且**没有** bit 11 的压缩包。

    `zipfile` 只要看到非 ASCII 名字就会自动置 bit 11（"这个名字是 UTF-8"），
    而我们要复现的恰恰是没置位的包——所以先写等长的 ASCII 占位名，再把名字字节
    原地补回去。名字在本地头和中央目录里各出现一次，长度完全相同，所以偏移量不变，
    CRC 也不覆盖名字，改完仍是一个合法压缩包。

    断言 `count == 2` 是为了确认补丁落在该落的地方，而不是碰巧命中了压缩数据。
    """
    real = [name.encode("cp437") for name in names]
    stubs = [bytes([0x41 + i % 26]) * len(name) for i, name in enumerate(real)]
    with zipfile.ZipFile(path, "w") as zf:
        for stub in stubs:
            zf.writestr(stub.decode("ascii"), payload)
    raw = path.read_bytes()
    for stub, name in zip(stubs, real):
        assert len(stub) == len(name)
        assert raw.count(stub) == 2, f"占位名 {stub!r} 出现了 {raw.count(stub)} 次，应为 2"
        raw = raw.replace(stub, name)
    path.write_bytes(raw)
    return path


class ComicTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-comic-"))

    def make_zip(self, name: str, members: dict[str, bytes], **kw) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, payload in members.items():
                zf.writestr(member, payload)
        return path

    def make_folder(self, name: str, files: dict[str, bytes]) -> Path:
        folder = self.tmp / name
        for relative, payload in files.items():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        folder.mkdir(parents=True, exist_ok=True)
        return folder


# ---- 格式表 ---------------------------------------------------------------


class TestFormatTable(unittest.TestCase):
    def test_the_current_phase_opens_text_and_zip_comics(self) -> None:
        for path in ("a.txt", "a.epub", "a.cbz", "a.zip", "a.jpg", "a.PNG"):
            with self.subTest(path=path):
                self.assertTrue(is_supported(path))

    def test_phase_three_also_opens_pdf(self) -> None:
        # 阶段 3 把 .pdf 从"锁着"变成"能开"。它是双重身份：判型由内容决定，
        # 但格式表这一层给的是默认猜测，仍然是 KIND_COMIC（见 opener.py 的 docstring）。
        self.assertTrue(is_supported("a.pdf"))
        self.assertEqual(support_note("a.pdf"), "")

    def test_no_format_is_locked_any_more(self) -> None:
        # 阶段 6 解锁 .mobi/.azw3 时，这里换成了阶段 8 的 .cbr/.rar；阶段 8 又把
        # 那两个解锁了，于是「表里认得、这一版打不开」这个样本用完了——格式表里
        # 每一行全部 ready（第二批又加了 FB2/DOCX/HTML/CB7 四行，同样全 ready）。
        # 断言的意思因此翻了一面：从「还有锁着的」变成「一个都不锁」。
        # .exe 和无扩展名仍然拒绝，但它们从来不在表里，与相位无关。
        for fmt in FORMATS:
            for ext in fmt.exts:
                with self.subTest(ext=ext):
                    self.assertTrue(is_supported("x" + ext), fmt.label)
        for path in ("a.exe", "noext"):
            with self.subTest(path=path):
                self.assertFalse(is_supported(path))

    def test_every_format_follows_the_phase_gate(self) -> None:
        """`is_supported` 必须**只**由 `phase <= CURRENT_PHASE` 决定。

        这条机制让阶段 6/8 的格式可以提前写进表里，而窗口行为、文件对话框、
        拖放全部自动保持一致。阶段 3 推进相位时它自动跟随，一条都不用改——
        这正是它值得存在的原因。
        """
        for fmt in FORMATS:
            for ext in fmt.exts:
                with self.subTest(ext=ext):
                    self.assertEqual(is_supported("x" + ext), fmt.phase <= CURRENT_PHASE)
                    self.assertEqual(fmt.ready, fmt.phase <= CURRENT_PHASE)

    def test_no_extension_is_claimed_by_two_formats(self) -> None:
        seen: dict[str, str] = {}
        for fmt in FORMATS:
            for ext in fmt.exts:
                with self.subTest(ext=ext):
                    self.assertNotIn(ext, seen, f"{ext} 同时属于 {seen.get(ext)} 和 {fmt.label}")
                    seen[ext] = fmt.label

    def test_extensions_are_lowercase_and_dotted(self) -> None:
        for ext in [ext for fmt in FORMATS for ext in fmt.exts] + list(IMAGE_EXTS):
            with self.subTest(ext=ext):
                self.assertTrue(ext.startswith("."), ext)
                self.assertEqual(ext, ext.lower(), ext)

    def test_lookup_is_case_insensitive(self) -> None:
        self.assertIs(format_for("a.CBZ"), format_for("a.cbz"))
        self.assertEqual(kind_for("BOOK.ZIP"), KIND_COMIC)
        self.assertTrue(is_image("PAGE.JPEG"))

    def test_unknown_extension_has_no_format(self) -> None:
        self.assertIsNone(format_for("a.exe"))
        self.assertIsNone(format_for("noext"))
        # None, not "" - "no row in the table" and "a row that says text" are
        # different answers, and the empty string is not one of them.
        self.assertIsNone(kind_for("a.exe"))
        self.assertIsNone(kind_for("noext"))

    def test_kind_matches_the_table(self) -> None:
        self.assertEqual(kind_for("a.txt"), KIND_TEXT)
        self.assertEqual(kind_for("a.epub"), KIND_TEXT)
        self.assertEqual(kind_for("a.cbz"), KIND_COMIC)
        # PDF 是双重身份：表里给的是默认猜测（漫画），真正开出来是哪种由内容决定。
        self.assertEqual(kind_for("a.pdf"), KIND_COMIC, "表里的默认猜测是漫画")

    def test_support_note_is_empty_exactly_when_supported(self) -> None:
        self.assertEqual(support_note("a.txt"), "")
        self.assertEqual(support_note("a.cbz"), "")
        self.assertTrue(support_note("a.exe"))
        self.assertTrue(support_note("noext"))

    def test_support_note_names_the_phase(self) -> None:
        # 阶段 8 之后 .cbr 也解锁了，格式表里再没有「相位没到」的行，所以这条
        # 断言翻了一面：以前它证明"锁着的格式会报相位"，现在证明"没有格式会报相位"。
        # 报相位的代码路径仍然在（`support_note()` 的 `fmt.ready` 分支），
        # 它的判据改由 `test_support_note_would_name_a_phase_if_one_were_locked`
        # 用打桩的 Format 守着——见下面那一条。
        self.assertEqual(support_note("a.cbr"), "", "阶段 8 起 RAR 漫画已解锁")
        self.assertEqual(support_note("a.mobi"), "", "阶段 6 起 Kindle 书已解锁")

    def test_support_note_for_an_unknown_type_quotes_the_extension(self) -> None:
        self.assertEqual(support_note("a.exe"), "不支持的文件类型：.exe")
        self.assertEqual(support_note("noext"), "不支持的文件类型：无扩展名")

    def test_dialog_filters_cover_every_ready_format(self) -> None:
        text = dialog_filters()
        for fmt in FORMATS:
            if not fmt.ready:
                continue
            for ext in fmt.exts:
                with self.subTest(ext=ext):
                    self.assertIn("*" + ext, text)
        for ext in IMAGE_EXTS:
            with self.subTest(ext=ext):
                self.assertIn("*" + ext, text)

    def test_dialog_filters_are_well_formed(self) -> None:
        text = dialog_filters()
        parts = text.split(";;")
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(parts[-1].endswith("(*.*)"), parts[-1])
        for part in parts:
            with self.subTest(part=part):
                self.assertIn("(", part)
                self.assertTrue(part.endswith(")"), part)
                self.assertNotIn("(.", part, "扩展名必须带 * 前缀，否则对话框列不出文件")
        self.assertNotIn(";;;;", text)

    def test_support_note_would_name_a_phase_if_one_were_locked(self) -> None:
        """相位文案本身还在，只是现在没有格式会触发它。

        阶段 8 之后六行格式全部 ready，`support_note()` 的 `if not fmt.ready` 分支
        没有任何真实输入能走到。为了不把「删断言」当成「测试全绿」，这里直接打桩
        `FORMATS`，造一行 phase 比 CURRENT_PHASE 大的格式，断言文案仍然是
        「<标签>要等阶段 N，现在还不能打开」。将来加格式忘了填 phase，这条会先炸。
        """
        locked = Format(KIND_COMIC, (".xyz",), "未来漫画", CURRENT_PHASE + 1)
        # 打桩 `format_for()` 而不是往 `FORMATS` 里塞一行：格式表是模块级一次性
        # 构建的（`_BY_EXT`），改它要同时改两处、还要记得还原；而 `support_note()`
        # 与 `is_supported()` 都只经由 `format_for()` 看表，一处打桩就够。
        with unittest.mock.patch.object(formats_module, "format_for", return_value=locked):
            self.assertEqual(
                support_note("a.xyz"), f"未来漫画要等阶段 {CURRENT_PHASE + 1}，现在还不能打开"
            )
            self.assertFalse(is_supported("a.xyz"))

    def test_dialog_filters_offer_what_this_build_can_open(self) -> None:
        text = dialog_filters()
        for ext in (
            ".txt",
            ".epub",
            ".cbz",
            ".zip",
            ".pdf",
            ".mobi",
            ".azw3",
            ".cbr",
            ".rar",
            ".fb2",
            ".docx",
            ".html",
            ".htm",
            ".cb7",
        ):
            with self.subTest(ext=ext):
                self.assertIn("*" + ext, text)

    def test_dialog_filters_offer_every_format_because_none_are_locked(self) -> None:
        # 阶段 6 之前这条断言的是「锁着的别出现在过滤器里」，样本是 .cbr/.rar。
        # 阶段 8 把 .cbr/.rar 解锁之后，格式表里没有锁着的行了，于是它翻了一面：
        # 过滤器必须**恰好**包含表里每一行——多一个少一个都是 bug。
        text = dialog_filters()
        for fmt in FORMATS:
            for ext in fmt.exts:
                with self.subTest(ext=ext):
                    self.assertIn("*" + ext, text)


# ---- 自然排序 -------------------------------------------------------------


class TestNaturalKey(unittest.TestCase):
    def test_numbers_sort_numerically(self) -> None:
        names = ["第10话.jpg", "第2话.jpg", "第1话.jpg", "p1_10.jpg", "p1_2.jpg"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["p1_2.jpg", "p1_10.jpg", "第1话.jpg", "第2话.jpg", "第10话.jpg"],
        )

    def test_the_full_order_is_stable_and_documented(self) -> None:
        """实测顺序：字母开头的排在数字开头的前面，数字段按整数比。

        实测值（不是推导值）——`1.jpg` 排在最后是因为 `natural_key` 用
        `(0, 文本)` / `(1, 数字)` 给每段打标签，数字段整体大于文本段。
        """
        names = ["第10话.jpg", "第2话.jpg", "第1话.jpg", "p1_10.jpg", "p1_2.jpg",
                 "A.jpg", "a.jpg", "1.jpg"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["A.jpg", "a.jpg", "p1_2.jpg", "p1_10.jpg",
             "第1话.jpg", "第2话.jpg", "第10话.jpg", "1.jpg"],
        )

    def test_text_segments_ignore_case_but_break_ties_deterministically(self) -> None:
        self.assertEqual(sorted(["a.jpg", "A.jpg"], key=natural_key), ["A.jpg", "a.jpg"])

    def test_mixed_int_and_str_never_compares_across_types(self) -> None:
        """第一版这里踩过：直接把数字段当 int、文本段当 str 塞进同一个元组，
        排序到 `a.jpg` 和 `1.jpg` 相遇时就 `TypeError: '<' not supported`。
        打标签对就是为了让两种段永远不直接相比。"""
        sorted(["1.jpg", "a.jpg", "10.jpg", "b2.jpg"], key=natural_key)  # 不抛即通过

    def test_it_is_a_usable_sort_key(self) -> None:
        self.assertIsInstance(natural_key("page_001.jpg"), tuple)


# ---- 压缩包成员名 ---------------------------------------------------------


class TestArchiveNames(ComicTestCase):
    def test_a_utf8_flagged_name_is_left_alone(self) -> None:
        path = self.make_zip("utf8.cbz", {"测试图片.jpg": FAKE_PNG})
        with zipfile.ZipFile(path) as zf:
            info = zf.infolist()[0]
        self.assertTrue(info.flag_bits & 0x800, "zipfile 应给非 ASCII 名自动置 bit 11")
        self.assertEqual(repair_name(info), "测试图片.jpg")

    def test_a_cp437_name_is_repaired_through_gbk(self) -> None:
        raw = mojibake("测试图片名啊")
        self.assertEqual(raw, "▓Γ╩╘═╝╞¼├√░í")
        path = make_cp437_zip(self.tmp / "gbk.cbz", [raw + ".jpg"])
        with zipfile.ZipFile(path) as zf:
            info = zf.infolist()[0]
        self.assertFalse(info.flag_bits & 0x800, "这个包不该有 bit 11")
        self.assertEqual(info.filename, raw + ".jpg")
        self.assertEqual(repair_name(info), "测试图片名啊.jpg")

    def test_an_ascii_name_survives_the_repair_untouched(self) -> None:
        path = make_cp437_zip(self.tmp / "ascii.cbz", ["page_001.jpg"])
        with zipfile.ZipFile(path) as zf:
            info = zf.infolist()[0]
        self.assertEqual(repair_name(info), "page_001.jpg")

    def test_big5_names_are_repaired_too(self) -> None:
        # 港台漫画常见 big5。修复顺序是 gbk 先试，big5 兜底。
        raw = mojibake("漫畫第一話", encoding="big5")
        path = make_cp437_zip(self.tmp / "big5.cbz", [raw + ".jpg"])
        with zipfile.ZipFile(path) as zf:
            info = zf.infolist()[0]
        repaired = repair_name(info)
        self.assertTrue(
            any("\u4e00" <= ch <= "\u9fff" for ch in repaired),
            f"应修成中日韩文字，实得 {repaired!r}",
        )


# ---- 列出与读取压缩包 -----------------------------------------------------


class TestArchiveListing(ComicTestCase):
    def test_entries_keep_both_names(self) -> None:
        raw = mojibake("测试图片名啊")
        path = make_cp437_zip(self.tmp / "both.cbz", [raw + ".jpg"])
        entries = list_archive_pages(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].ref, raw + ".jpg", "ref 必须是 zipfile 认的原始名")
        self.assertEqual(entries[0].name, "测试图片名啊.jpg", "name 是给人看的")

    def test_the_repaired_name_is_what_gets_sorted(self) -> None:
        path = self.make_zip(
            "sorted.cbz",
            {"page_10.jpg": FAKE_PNG, "page_2.jpg": FAKE_PNG, "page_1.jpg": FAKE_PNG},
        )
        self.assertEqual(
            [e.name for e in list_archive_pages(path)],
            ["page_1.jpg", "page_2.jpg", "page_10.jpg"],
        )

    def test_entries_are_archive_entry_values(self) -> None:
        path = self.make_zip("one.cbz", {"a.jpg": FAKE_PNG})
        entry = list_archive_pages(path)[0]
        self.assertIsInstance(entry, ArchiveEntry)
        self.assertEqual((entry.ref, entry.name), ("a.jpg", "a.jpg"))

    def test_non_images_are_filtered_out(self) -> None:
        path = self.make_zip(
            "mixed.cbz",
            {
                "page_1.jpg": FAKE_PNG,
                "page_2.png": FAKE_PNG,
                "ComicInfo.xml": b"<xml/>",
                "readme.txt": b"hello",
                "cover.PNG": FAKE_PNG,
            },
        )
        self.assertEqual(
            [e.name for e in list_archive_pages(path)],
            ["cover.PNG", "page_1.jpg", "page_2.png"],
        )

    def test_hidden_and_macos_metadata_are_filtered_out(self) -> None:
        path = self.make_zip(
            "junk.cbz",
            {
                ".DS_Store": b"\x00",
                "__MACOSX/._page_1.jpg": b"\x00",
                "inner/__MACOSX/page_1.jpg": b"\x00",
                "_page_1.jpg": FAKE_PNG,
                ".hidden.jpg": FAKE_PNG,
                "page_2.jpg": FAKE_PNG,
            },
        )
        self.assertEqual([e.name for e in list_archive_pages(path)], ["_page_1.jpg", "page_2.jpg"])

    def test_subfolders_are_kept_and_ordered_with_the_rest(self) -> None:
        path = self.make_zip(
            "nested.cbz",
            {"ch01/page_1.jpg": FAKE_PNG, "ch01/page_2.jpg": FAKE_PNG, "ch02/page_1.jpg": FAKE_PNG},
        )
        self.assertEqual(
            [e.name for e in list_archive_pages(path)],
            ["ch01/page_1.jpg", "ch01/page_2.jpg", "ch02/page_1.jpg"],
        )

    def test_all_image_extensions_are_recognised(self) -> None:
        members = {f"page{i}{ext}": FAKE_PNG for i, ext in enumerate(IMAGE_EXTS)}
        path = self.make_zip("every.cbz", members)
        self.assertEqual(len(list_archive_pages(path)), len(IMAGE_EXTS))


class TestArchiveReading(ComicTestCase):
    def test_read_member_returns_the_exact_bytes(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        path = self.make_zip("read.cbz", {"a.jpg": payload})
        self.assertEqual(read_member(path, "a.jpg"), payload)

    def test_read_member_must_be_given_the_reference_not_the_display_name(self) -> None:
        """这正是 `ArchiveEntry` 要有两个名字的原因：拿修复后的名字去读会 KeyError。"""
        raw = mojibake("测试图片名啊")
        path = make_cp437_zip(self.tmp / "ref.cbz", [raw + ".jpg"])
        entry = list_archive_pages(path)[0]
        self.assertEqual(read_member(path, entry.ref), FAKE_PNG)
        with self.assertRaises(ArchiveError) as caught:
            read_member(path, entry.name)
        self.assertEqual(str(caught.exception), f"压缩包里找不到这一页：{entry.name}")

    def test_read_member_head_is_a_prefix(self) -> None:
        payload = bytes(range(256)) * 400  # 102 400 B，大于 HEAD_BYTES
        path = self.make_zip("head.cbz", {"a.jpg": payload})
        head = read_member_head(path, "a.jpg")
        self.assertEqual(len(head), HEAD_BYTES)
        self.assertEqual(head, payload[:HEAD_BYTES])

    def test_read_member_head_takes_a_custom_limit(self) -> None:
        path = self.make_zip("small.cbz", {"a.jpg": b"0123456789"})
        self.assertEqual(read_member_head(path, "a.jpg", 4), b"0123")

    def test_read_member_head_of_a_short_member_returns_all_of_it(self) -> None:
        path = self.make_zip("tiny.cbz", {"a.jpg": b"abc"})
        self.assertEqual(read_member_head(path, "a.jpg"), b"abc")


class TestArchiveErrors(ComicTestCase):
    def test_a_missing_file_says_so(self) -> None:
        with self.assertRaises(ArchiveError) as caught:
            list_archive_pages(self.tmp / "nope.cbz")
        self.assertEqual(str(caught.exception), "找不到这个文件：nope.cbz")

    def test_a_non_zip_says_so(self) -> None:
        bad = self.tmp / "bad.cbz"
        bad.write_bytes(b"<html>404 not found</html>")
        with self.assertRaises(ArchiveError) as caught:
            list_archive_pages(bad)
        self.assertEqual(str(caught.exception), "这个压缩包已损坏或不是 ZIP/CBZ 格式")

    def test_an_archive_without_images_says_so(self) -> None:
        path = self.make_zip("noimg.cbz", {"readme.txt": b"hello"})
        with self.assertRaises(ArchiveError) as caught:
            list_archive_pages(path)
        self.assertEqual(str(caught.exception), "这个压缩包里没有能看的图片")

    def test_an_empty_archive_says_so(self) -> None:
        path = self.make_zip("empty.cbz", {})
        with self.assertRaises(ArchiveError) as caught:
            list_archive_pages(path)
        self.assertEqual(str(caught.exception), "这个压缩包里没有能看的图片")

    def test_looks_like_archive_never_raises(self) -> None:
        for target in (self.tmp / "ghost.cbz", self.tmp, self.tmp / "x.txt"):
            with self.subTest(target=str(target)):
                self.assertFalse(looks_like_archive(target))
        text = self.tmp / "x.txt"
        text.write_bytes(b"plain text")
        self.assertFalse(looks_like_archive(text))

    def test_looks_like_archive_accepts_a_real_zip(self) -> None:
        path = self.make_zip("real.cbz", {"a.jpg": FAKE_PNG})
        self.assertTrue(looks_like_archive(path))


# ---- 图片文件夹 -----------------------------------------------------------


class TestFolderPages(ComicTestCase):
    def test_a_flat_folder_is_listed_in_natural_order(self) -> None:
        folder = self.make_folder(
            "flat", {"page_10.jpg": b"a", "page_2.jpg": b"b", "page_1.jpg": b"c"}
        )
        self.assertEqual(
            [os.path.basename(p) for p in list_folder_pages(folder)],
            ["page_1.jpg", "page_2.jpg", "page_10.jpg"],
        )

    def test_non_images_are_ignored(self) -> None:
        folder = self.make_folder(
            "mixed", {"page_1.jpg": b"a", "notes.txt": b"b", "Thumbs.db": b"c"}
        )
        self.assertEqual([os.path.basename(p) for p in list_folder_pages(folder)], ["page_1.jpg"])

    def test_subfolders_are_used_when_the_top_level_has_no_images(self) -> None:
        folder = self.make_folder(
            "chapters",
            {
                "第2话/page_1.jpg": b"a",
                "第1话/page_1.jpg": b"b",
                "第1话/page_2.jpg": b"c",
            },
        )
        self.assertEqual(
            [p.replace(str(folder), "").replace("\\", "/").lstrip("/") for p in list_folder_pages(folder)],
            ["第1话/page_1.jpg", "第1话/page_2.jpg", "第2话/page_1.jpg"],
        )

    def test_a_folder_with_both_treats_the_top_level_as_the_book(self) -> None:
        """文档里记了这条决定：散落的封面图不该让整本书的顺序被悄悄改写。"""
        folder = self.make_folder(
            "both", {"cover.jpg": b"a", "ch01/page_1.jpg": b"b", "ch01/page_2.jpg": b"c"}
        )
        self.assertEqual([os.path.basename(p) for p in list_folder_pages(folder)], ["cover.jpg"])

    def test_walking_stops_at_max_depth(self) -> None:
        deep = self.make_folder("deep", {"/".join(["a"] * (MAX_DEPTH + 1) + ["p.jpg"]): b"x"})
        with self.assertRaises(ArchiveError) as caught:
            list_folder_pages(deep)
        self.assertEqual(str(caught.exception), "这个文件夹里没有能看的图片")

    def test_an_empty_folder_says_so(self) -> None:
        folder = self.make_folder("empty", {"readme.txt": b"hello"})
        with self.assertRaises(ArchiveError) as caught:
            list_folder_pages(folder)
        self.assertEqual(str(caught.exception), "这个文件夹里没有能看的图片")

    def test_a_missing_folder_says_so(self) -> None:
        with self.assertRaises(ArchiveError) as caught:
            list_folder_pages(self.tmp / "ghost")
        self.assertEqual(str(caught.exception), "找不到这个文件夹：ghost")

    def test_paths_come_back_absolute(self) -> None:
        folder = self.make_folder("abs", {"page_1.jpg": b"a"})
        for page in list_folder_pages(folder):
            with self.subTest(page=page):
                self.assertTrue(os.path.isabs(page))


class TestFolderHasImages(ComicTestCase):
    """拖放要在**接受之前**问这一句，否则用户看到的是"接受拖放 + 报错弹窗"。"""

    def test_flat_folder_with_images(self) -> None:
        self.assertTrue(folder_has_images(self.make_folder("f", {"a.jpg": b"x"})))

    def test_folder_with_images_only_in_subfolders(self) -> None:
        self.assertTrue(folder_has_images(self.make_folder("f", {"ch/a.jpg": b"x"})))

    def test_folder_without_any_image(self) -> None:
        self.assertFalse(folder_has_images(self.make_folder("f", {"a.txt": b"x"})))

    def test_empty_folder(self) -> None:
        self.assertFalse(folder_has_images(self.make_folder("f", {})))

    def test_a_file_is_not_a_folder(self) -> None:
        target = self.tmp / "a.jpg"
        target.write_bytes(b"x")
        self.assertFalse(folder_has_images(target))

    def test_a_missing_path(self) -> None:
        self.assertFalse(folder_has_images(self.tmp / "ghost"))


class TestSingleImage(ComicTestCase):
    def test_a_single_image_is_a_one_page_book(self) -> None:
        target = self.tmp / "cover.jpg"
        target.write_bytes(b"x")
        self.assertEqual(list_single_page(target), [str(target)])

    def test_every_image_extension_works(self) -> None:
        for ext in IMAGE_EXTS:
            target = self.tmp / ("cover" + ext)
            target.write_bytes(b"x")
            with self.subTest(ext=ext):
                self.assertEqual(len(list_single_page(target)), 1)

    def test_a_non_image_is_refused(self) -> None:
        target = self.tmp / "notes.txt"
        target.write_bytes(b"x")
        with self.assertRaises(ArchiveError) as caught:
            list_single_page(target)
        self.assertEqual(str(caught.exception), "这不是一个能看的图片文件")

    def test_a_missing_file_says_so(self) -> None:
        with self.assertRaises(ArchiveError) as caught:
            list_single_page(self.tmp / "ghost.jpg")
        self.assertEqual(str(caught.exception), "找不到这个文件：ghost.jpg")


class TestImageFileTest(unittest.TestCase):
    def test_extensions_and_dotfiles(self) -> None:
        self.assertTrue(is_image_file("a.jpg"))
        self.assertTrue(is_image_file("dir/a.JPEG"))
        self.assertFalse(is_image_file(".hidden.jpg"))
        self.assertFalse(is_image_file("noext"))
        self.assertFalse(is_image_file("a.txt"))
        self.assertFalse(is_image_file(""))


class TestFileReading(ComicTestCase):
    def test_read_file_returns_everything(self) -> None:
        target = self.tmp / "a.jpg"
        target.write_bytes(b"0123456789")
        self.assertEqual(read_file(target), b"0123456789")

    def test_read_file_head_takes_a_prefix(self) -> None:
        target = self.tmp / "a.jpg"
        target.write_bytes(b"0123456789")
        self.assertEqual(read_file_head(target, 4), b"0123")

    def test_a_missing_file_reports_a_read_error(self) -> None:
        with self.assertRaises(ArchiveError) as caught:
            read_file(self.tmp / "ghost.jpg")
        self.assertTrue(str(caught.exception).startswith("读不出这一页："))


# ---- ComicBook ------------------------------------------------------------


class TestComicBookFromArchive(ComicTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.make_zip(
            "book.cbz",
            {f"page_{i:03d}.jpg": FAKE_PNG for i in range(1, 6)},
        )
        self.book = open_comic(self.path)

    def test_identity(self) -> None:
        self.assertEqual(self.book.kind, KIND_COMIC)
        self.assertEqual(self.book.source, "cbz")
        self.assertEqual(self.book.source_label, "漫画压缩包")
        self.assertEqual(self.book.title, "book")
        self.assertEqual(self.book.path, str(self.path))
        self.assertEqual(self.book.size, self.path.stat().st_size)

    def test_pages_are_listed_in_order(self) -> None:
        self.assertEqual(self.book.position_count, 5)
        self.assertEqual(
            [p.name for p in self.book.pages],
            [f"page_{i:03d}.jpg" for i in range(1, 6)],
        )
        self.assertEqual([p.index for p in self.book.pages], [0, 1, 2, 3, 4])

    def test_position_label_is_pages(self) -> None:
        self.assertEqual(self.book.position_label, "页")
        self.assertEqual(self.book.position(0).label, "第 1/5 页")
        self.assertEqual(self.book.position(4).label, "第 5/5 页")

    def test_clamp(self) -> None:
        self.assertEqual(self.book.clamp(-5), 0)
        self.assertEqual(self.book.clamp(0), 0)
        self.assertEqual(self.book.clamp(4), 4)
        self.assertEqual(self.book.clamp(99), 4)

    def test_is_paged(self) -> None:
        self.assertTrue(self.book.is_paged)

    def test_page_lookup(self) -> None:
        self.assertIsInstance(self.book.page(0), ComicPage)
        self.assertEqual(self.book.page(0).name, "page_001.jpg")
        self.assertIsNone(self.book.page(5))
        self.assertIsNone(self.book.page(-1))

    def test_read_page(self) -> None:
        self.assertEqual(self.book.read_page(0), FAKE_PNG)
        self.assertEqual(self.book.read_page(4), FAKE_PNG)

    def test_read_page_out_of_range(self) -> None:
        for index in (5, -1, 99):
            with self.subTest(index=index):
                with self.assertRaises(ArchiveError) as caught:
                    self.book.read_page(index)
                self.assertEqual(str(caught.exception), "这一页不存在")

    def test_read_page_head(self) -> None:
        self.assertEqual(self.book.read_page_head(0, 4), FAKE_PNG[:4])

    def test_sizes_start_unknown_and_can_be_recorded(self) -> None:
        self.assertFalse(self.book.sizes_known())
        self.assertEqual((self.book.page(0).width, self.book.page(0).height), (0, 0))
        self.assertFalse(self.book.page(0).size_known)

        self.assertTrue(self.book.set_page_size(0, 800, 1200))
        self.assertEqual((self.book.page(0).width, self.book.page(0).height), (800, 1200))
        self.assertTrue(self.book.page(0).size_known)
        self.assertFalse(self.book.sizes_known(), "只有第一页已知")

    def test_set_page_size_reports_no_change(self) -> None:
        self.assertTrue(self.book.set_page_size(0, 800, 1200))
        self.assertFalse(self.book.set_page_size(0, 800, 1200), "值没变应返回 False")
        self.assertFalse(self.book.set_page_size(99, 1, 1), "越界应返回 False")

    def test_describe_names_the_source_and_page_count(self) -> None:
        text = self.book.describe()
        self.assertIn("漫画压缩包", text)
        self.assertIn("5 页", text)

    def test_to_record_matches_the_library_schema(self) -> None:
        record = self.book.to_record()
        self.assertEqual(record["rule"], "comic")
        self.assertEqual(record["chapters"], 5)
        self.assertEqual(record["encoding"], "漫画压缩包")
        self.assertEqual(record["title"], "book")
        # 库里那一列叫 source，存的是**书的种类**（comic），不是漫画的来源
        # （cbz / folder / image）——后者在 `ComicBook.source` 字段里。
        self.assertEqual(record["source"], KIND_COMIC)
        self.assertEqual(record["id"], self.book.id)

    def test_id_is_stable_across_opens(self) -> None:
        self.assertEqual(open_comic(self.path).id, self.book.id)

    def test_no_notes_for_an_ordinary_archive(self) -> None:
        self.assertEqual(self.book.notes, [])

    def test_text_shaped_answers_are_honest_no_ops(self) -> None:
        """窗口的非章节导航路径（编码提示、重新加载、书签摘要、--check 字数）
        会碰这些名字。给它们诚实的答案，那些路径就会正确地什么都不做，而不是
        在 window.py 里撒 `if kind == comic`。"""
        self.assertEqual(self.book.encoding_label, "漫画")
        self.assertIs(self.book.confident, True)
        self.assertEqual(self.book.chapters, [])
        self.assertEqual(self.book.chapter_count, 5)
        self.assertIsNone(self.book.chapter(0))
        self.assertEqual(self.book.chapter_body(0), "")
        self.assertEqual(self.book.split_summary(), "共 5 页")
        self.assertEqual(self.book.text, "")

    def test_class_vars_are_not_dataclass_fields(self) -> None:
        from dataclasses import fields

        names = {f.name for f in fields(ComicBook)}
        self.assertNotIn("encoding_label", names)
        self.assertNotIn("confident", names)
        # `kind` *is* a field here, unlike on Book: ComicBook annotates it with a
        # default, which is what makes `ComicBook(kind=...)` legal. Book leaves
        # the annotation off and inherits BookBase's plain class attribute.
        self.assertIn("kind", names)
        self.assertEqual(ComicBook().kind, KIND_COMIC)

    def test_from_path_forwards_to_open_comic(self) -> None:
        book = ComicBook.from_path(self.path)
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.position_count, 5)


class TestComicBookFromFolder(ComicTestCase):
    def test_folder_source(self) -> None:
        folder = self.make_folder("卷1", {"page_1.jpg": b"a", "page_2.jpg": b"b"})
        book = open_comic(folder)
        self.assertEqual(book.source, "folder")
        self.assertEqual(book.source_label, "图片文件夹")
        self.assertEqual(book.title, "卷1", "文件夹用目录名做标题")
        self.assertEqual(book.position_count, 2)
        self.assertEqual(book.read_page(0), b"a")

    def test_a_one_page_folder_says_so(self) -> None:
        folder = self.make_folder("单页", {"only.jpg": b"a"})
        book = open_comic(folder)
        self.assertEqual(book.position_count, 1)
        self.assertIn("这个文件夹里只有一张图片", book.notes)

    def test_a_multi_page_folder_has_no_note(self) -> None:
        folder = self.make_folder("多页", {"a.jpg": b"a", "b.jpg": b"b"})
        self.assertEqual(open_comic(folder).notes, [])


class TestComicBookFromImage(ComicTestCase):
    def test_single_image_source(self) -> None:
        target = self.tmp / "封面.png"
        target.write_bytes(b"x")
        book = open_comic(target)
        self.assertEqual(book.source, "image")
        self.assertEqual(book.source_label, "单张图片")
        self.assertEqual(book.title, "封面", "单图去掉扩展名做标题")
        self.assertEqual(book.position_count, 1)
        self.assertIn("这是一张单独的图片", book.notes)

    def test_read_page_reads_the_file(self) -> None:
        target = self.tmp / "a.jpg"
        target.write_bytes(b"payload")
        self.assertEqual(open_comic(target).read_page(0), b"payload")


class TestComicBookErrors(ComicTestCase):
    def test_a_missing_path(self) -> None:
        with self.assertRaises(BookError) as caught:
            open_comic(self.tmp / "ghost.cbz")
        self.assertEqual(str(caught.exception), "找不到这个文件：ghost.cbz")

    def test_a_file_that_is_neither_archive_nor_image(self) -> None:
        target = self.tmp / "notes.txt"
        target.write_bytes(b"hello")
        with self.assertRaises(BookError) as caught:
            open_comic(target)
        self.assertEqual(str(caught.exception), "这个文件既不是压缩包，也不是图片")

    def test_an_empty_folder(self) -> None:
        folder = self.make_folder("empty", {"a.txt": b"x"})
        with self.assertRaises(BookError) as caught:
            open_comic(folder)
        self.assertEqual(str(caught.exception), "这个文件夹里没有能看的图片")

    def test_an_archive_without_images(self) -> None:
        path = self.make_zip("noimg.cbz", {"readme.txt": b"x"})
        with self.assertRaises(BookError) as caught:
            open_comic(path)
        # The archive layer's own wording, not open_comic's fallback: an empty
        # listing here means "every member was filtered out", and saying which
        # container was empty is more useful than the generic sentence.
        self.assertEqual(str(caught.exception), "这个压缩包里没有能看的图片")


# ---- 打开裁决 -------------------------------------------------------------


class TestOpener(ComicTestCase):
    def test_a_txt_opens_as_a_text_book(self) -> None:
        target = self.tmp / "book.txt"
        target.write_bytes("第1章 起\n正文。\n第2章 承\n正文。\n".encode("utf-8"))
        book = open_book(target)
        self.assertEqual(book.kind, KIND_TEXT)
        self.assertEqual(book.chapter_count, 2)

    def test_a_cbz_opens_as_a_comic(self) -> None:
        path = self.make_zip("a.cbz", {"p1.jpg": FAKE_PNG, "p2.jpg": FAKE_PNG})
        book = open_book(path)
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.position_count, 2)

    def test_a_zip_opens_as_a_comic(self) -> None:
        path = self.make_zip("a.zip", {"p1.jpg": FAKE_PNG})
        self.assertIsInstance(open_book(path), ComicBook)

    def test_a_folder_opens_as_a_comic(self) -> None:
        folder = self.make_folder("卷1", {"p1.jpg": b"a"})
        self.assertIsInstance(open_book(folder), ComicBook)

    def test_a_single_image_opens_as_a_comic(self) -> None:
        target = self.tmp / "p1.jpg"
        target.write_bytes(b"a")
        book = open_book(target)
        self.assertIsInstance(book, ComicBook)
        self.assertEqual(book.position_count, 1)

    def test_an_empty_path(self) -> None:
        with self.assertRaises(BookError) as caught:
            open_book("")
        self.assertEqual(str(caught.exception), "没有选择文件")

    def test_a_locked_format_reports_its_phase(self) -> None:
        # .pdf 在阶段 3 解锁、.mobi 在阶段 6 解锁、.cbr 在阶段 8 解锁，所以这里再也
        # 找不到「表里认得但打不开」的真实样本了。断言的意思没变——相位没到的格式
        # 报的是相位而不是「不是有效文件」——只是样本改成了打桩的一行格式。
        locked = Format(KIND_COMIC, (".xyz",), "未来漫画", CURRENT_PHASE + 1)
        # 打桩的是 `formats.format_for` 而不是 `opener.format_for`：`open_book()`
        # 调的是从 formats 导入的 `support_note()`，后者在 formats 模块里按自己的
        # 全局名查表。打桩 opener 那一份只会影响 opener 自己的 kind 判断，报不出相位。
        with unittest.mock.patch.object(formats_module, "format_for", return_value=locked):
            with self.assertRaises(BookError) as caught:
                open_book(self.tmp / "a.xyz")
        self.assertEqual(
            str(caught.exception), f"未来漫画要等阶段 {CURRENT_PHASE + 1}，现在还不能打开"
        )

    def test_a_cbr_without_a_tool_says_what_to_install(self) -> None:
        """阶段 8 解锁了 .cbr，但这台机器上没有任何解压工具时仍要报得清楚。

        `.cbr` 现在走的是「格式受支持 → 打开时探测工具」这条路，所以错误从
        「要等阶段 8」变成了「装 WinRAR 或 7-Zip」——两者都是相位/环境的差异，
        不是文件本身有问题。用打桩把探测结果钉成「没有工具」，这条才与本机装没装
        WinRAR 无关。
        """
        target = self.tmp / "a.cbr"
        target.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 32)
        with unittest.mock.patch.object(rar_module, "find", return_value=None):
            with self.assertRaises(BookError) as caught:
                open_book(target)
        self.assertEqual(str(caught.exception), rar_module.MESSAGE_NO_TOOL)

    def test_an_unknown_extension_is_refused(self) -> None:
        target = self.tmp / "a.exe"
        target.write_bytes(b"x")
        with self.assertRaises(BookError) as caught:
            open_book(target)
        self.assertEqual(str(caught.exception), "不支持的文件类型：.exe")

    def test_a_folder_is_checked_before_the_extension_table(self) -> None:
        """文件夹没有扩展名，问格式表只会得到"不支持的文件类型"。"""
        folder = self.make_folder("漫画.第1卷", {"p1.jpg": b"a"})
        self.assertIsInstance(open_book(folder), ComicBook)


class TestOpenBookKind(ComicTestCase):
    def test_kind_agrees_with_open_book(self) -> None:
        txt = self.tmp / "a.txt"
        txt.write_bytes("第1章 起\n正文。\n".encode("utf-8"))
        cbz = self.make_zip("a.cbz", {"p1.jpg": FAKE_PNG})
        folder = self.make_folder("卷1", {"p1.jpg": b"a"})
        image = self.tmp / "p1.jpg"
        image.write_bytes(b"a")
        for path in (txt, cbz, folder, image):
            with self.subTest(path=str(path)):
                self.assertEqual(open_book_kind(path), open_book(path).kind)

    def test_an_empty_path_has_no_kind(self) -> None:
        self.assertEqual(open_book_kind(""), "")

    def test_a_locked_or_unknown_format_has_no_kind(self) -> None:
        # 阶段 8 之后 .cbr 有 kind 了（"comic"），所以「锁着的样本」只能靠打桩。
        # .exe 和无扩展名仍然没有 kind——它们从来不在格式表里，与相位无关。
        locked = Format(KIND_COMIC, (".xyz",), "未来漫画", CURRENT_PHASE + 1)
        with unittest.mock.patch.object(opener_module, "format_for", return_value=locked):
            self.assertEqual(open_book_kind(self.tmp / "a.xyz"), "")
        self.assertEqual(open_book_kind(self.tmp / "a.exe"), "")
        self.assertEqual(open_book_kind(self.tmp / "noext"), "")

    def test_a_cbr_has_a_kind_now(self) -> None:
        """阶段 8 解锁 `.cbr`/`.rar`：格式表这一层给的 kind 是漫画。

        `open_book_kind()` 只看扩展名不碰磁盘（约束 27），所以一个不存在的
        `.cbr` 也有 kind；真正打开时才会去探测解压工具。
        """
        self.assertEqual(open_book_kind(self.tmp / "a.cbr"), KIND_COMIC)
        self.assertEqual(open_book_kind(self.tmp / "a.rar"), KIND_COMIC)

    def test_a_pdf_has_a_kind_now(self) -> None:
        """阶段 3 之后 `.pdf` 是受支持的格式，只是它的 kind 由内容决定。

        `open_book_kind()` 报的是格式表里的默认猜测；真正打开时 `open_book()`
        会读文档判型，两者可以不一致（判成文字型的 PDF 开出来就是 KIND_TEXT）。
        """
        self.assertEqual(open_book_kind(self.tmp / "a.pdf"), KIND_COMIC)


# ---- core 层保持 Qt-free --------------------------------------------------


class TestCoreStaysQtFree(unittest.TestCase):
    """核心模块一个都不许引 Qt。第二批的四个模块也要在这里登记。"""

    BANNED = ("PyQt5", "PySide6", "minireader.qt", "QtCore", "QtWidgets", "QtGui")

    MODULES = (
        "archives",
        "formats",
        "images",
        "comic",
        "opener",
        "book_base",
        "rar",
        "cb7",
        "fb2",
        "docx",
        "htmldoc",
    )

    def test_no_qt_in_source(self) -> None:
        import importlib

        for name in self.MODULES:
            module = importlib.import_module(f"minireader.core.{name}")
            source = Path(module.__file__).read_text(encoding="utf-8")
            for needle in self.BANNED:
                with self.subTest(module=name, needle=needle):
                    self.assertNotIn(needle, source)

    def test_importing_the_comic_stack_does_not_pull_qt_in(self) -> None:
        import subprocess

        code = (
            "import sys;"
            "from minireader.core import ComicBook, open_book, open_comic;"
            "bad=[m for m in sys.modules if m.startswith(('PyQt5','PySide6'))];"
            "print(len(bad))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "0")


class TestCoreLazyExports(unittest.TestCase):
    """`core/__init__.py` 用 PEP 562 惰性导出：epub 会拉起 zipfile + pyexpat（约 45ms），
    而多数会话只读 TXT。"""

    def test_lazy_names_are_importable_from_the_package(self) -> None:
        from minireader.core import (  # noqa: F401
            ArchiveError,
            ComicBook,
            ComicPage,
            open_book,
            open_book_kind,
            open_comic,
        )

        self.assertIs(ComicBook, __import__("minireader.core.comic", fromlist=["ComicBook"]).ComicBook)

    def test_dir_lists_the_lazy_names(self) -> None:
        import minireader.core as core

        listing = dir(core)
        for name in ("ComicBook", "open_book", "open_comic", "parse_epub", "EpubContent"):
            with self.subTest(name=name):
                self.assertIn(name, listing)

    def test_all_lists_the_lazy_names(self) -> None:
        import minireader.core as core

        for name in ("ComicBook", "ComicPage", "ArchiveError", "open_book", "open_comic"):
            with self.subTest(name=name):
                self.assertIn(name, core.__all__)

    def test_an_unknown_attribute_still_raises(self) -> None:
        import minireader.core as core

        with self.assertRaises(AttributeError):
            core.definitely_not_a_real_name  # noqa: B018

    def test_the_lazy_names_are_real_attributes(self) -> None:
        import minireader.core as core

        self.assertTrue(hasattr(core, "ComicBook"))
        self.assertTrue(hasattr(core, "open_book"))


class TestNoDanglingQBuffer(unittest.TestCase):
    """`QBuffer(QByteArray(data))` 会悬垂指针——这条守的是**写法**，不是行为。

    为什么需要它，而不只是靠 `test_comic_ui.py` 那个 400 轮的运行时测试：

    * 那个测试只覆盖 `probe_size()` 这一条路径，而且**崩溃是概率性的**——单次调用
      实测 8 遍里只崩 1 遍。新增一处同样写法的代码不会让它变红。
    * 这条扫的是整个 `minireader/` 的源码，任何**新增**的 `QBuffer(QByteArray(...))`
      都会在这里当场失败，不需要真的触发一次访问违例。
    * 它是 Qt-free 的：只读文件、走 `ast`，不 import `minireader.qt`，
      所以能留在本文件里而不破坏"core 无 Qt"的契约。

    根因：`QBuffer` 的 C++ 构造函数收 `QByteArray *`——一个**借用**指针，SIP 不接管
    所有权。嵌套写法让临时 `QByteArray` 在语句结束就被回收，buffer 继续指向已释放
    内存，`QImageReader` 去读它就是 `0xC0000005`。正确写法见
    `minireader/ui/comic_cache.py` 的 `probe_size()`：`QByteArray` 先落到具名局部变量。
    """

    def test_no_qbuffer_wraps_a_temporary_qbytearray(self) -> None:
        import ast

        package = Path(ROOT) / "minireader"
        sources = sorted(package.rglob("*.py"))
        self.assertGreater(len(sources), 10, "没扫到源码，路径可能写错了")

        offenders: list[str] = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Name) and func.id == "QBuffer"):
                    continue
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Name)
                        and arg.func.id == "QByteArray"
                    ):
                        rel = path.relative_to(ROOT)
                        offenders.append(f"{rel}:{node.lineno} QBuffer(QByteArray(...))")

        self.assertEqual(
            offenders,
            [],
            "QBuffer 借的是指针，被包的 QByteArray 必须先落到具名局部变量；"
            "嵌套写法会让它指向已释放内存（非确定性崩溃）。见 docs/DEVELOPMENT.md 第 5 条。",
        )

    def test_the_guard_would_actually_catch_it(self) -> None:
        """给上一条测试本身做个反面校验：确认它认得出那个写法。

        否则 `offenders` 恒为空也可能是因为 AST 匹配写错了，而不是因为源码干净。
        """
        import ast

        bad = "buffer = QBuffer(QByteArray(head))"
        tree = ast.parse(bad)
        found = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "QBuffer"
                and any(
                    isinstance(a, ast.Call)
                    and isinstance(a.func, ast.Name)
                    and a.func.id == "QByteArray"
                    for a in node.args
                )
            ):
                found = True
        self.assertTrue(found, "AST 匹配逻辑坏了，上一条测试是假绿")

        good = "payload = QByteArray(head)\nbuffer = QBuffer(payload)"
        tree = ast.parse(good)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "QBuffer":
                    self.assertFalse(
                        any(isinstance(a, ast.Call) for a in node.args),
                        "正确写法被误报了",
                    )


if __name__ == "__main__":
    unittest.main()
