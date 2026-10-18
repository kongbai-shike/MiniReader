r"""阶段 6 手动确认：MOBI/AZW3 的三条用户路径。

跑法： py -3.12 _manual\stage6_manual.py

它不碰 `minireader.ui`，全部走 `minireader.core`——因为这三条路径的**判定**都在
核心层，而手动确认要看的是"用户会看到哪句话"。

三条场景（对应 brief 里的手动确认清单）：

  1. 未装 mobi 时打开 .mobi → 「打开 MOBI/AZW3 需要先安装 mobi：pip install mobi」
  2. 装了 mobi 后打开未加密 MOBI → 正文可读
  3. 打开带 DRM 的 MOBI → 「这本书有 DRM 保护，请先用 Calibre 脱壳后再打开」

场景 1 用 `sys.path` 摘除来模拟（和 `stage6_no_mobi.py` 同一个手法：真的把库
藏起来，而不是 mock 掉 `is_available`，这样连 import 路径一起验了）。
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 手搓一个未加密的 MOBI（和 tests/test_mobi.py 里那份是同一套布局）
# ---------------------------------------------------------------------------


def build_mobi(body: bytes, *, crypto: int = 0, title: str = "手动确认用的书") -> bytes:
    """手搓一个未加密的 MOBI（和 tests/test_mobi.py 里那份是同一套布局）。

    0x28/0x2C/0x50/0x6C 四个字段必须显式写成 0xFFFFFFFF：它们分别是
    metaOrthIndex / metaInflIndex / firstnontext / firstresource。**留 0 是错的**——
    `mobi` 库看到 metaInflIndex != 0xFFFFFFFF 就以为这本书带字典索引，会去
    `dictSupport(...).getPositionMap()` 解析一段不存在的索引数据，然后死在
    `TypeError: cannot unpack non-iterable bool object`。手搓夹具的"看起来像 0 就行"
    在这里不成立，sentinel 本身就是格式的一部分。
    """
    header = bytearray(232)
    struct.pack_into(">H", header, 0x00, 1)  # compression = 未压缩
    struct.pack_into(">L", header, 0x04, len(body))
    struct.pack_into(">H", header, 0x08, 1)
    struct.pack_into(">H", header, 0x0A, 4096)
    struct.pack_into(">H", header, 0x0C, crypto)
    header[0x10:0x14] = b"MOBI"
    struct.pack_into(">L", header, 0x14, 200)
    struct.pack_into(">L", header, 0x18, 2)
    struct.pack_into(">L", header, 0x1C, 65001)
    struct.pack_into(">L", header, 0x20, 1)
    struct.pack_into(">L", header, 0x24, 6)
    struct.pack_into(">L", header, 0x28, 0xFFFFFFFF)  # metaOrthIndex
    struct.pack_into(">L", header, 0x2C, 0xFFFFFFFF)  # metaInflIndex
    struct.pack_into(">L", header, 0x50, 0xFFFFFFFF)  # firstnontext
    raw_title = title.encode("utf-8")
    struct.pack_into(">L", header, 0x54, 136)
    struct.pack_into(">L", header, 0x58, len(raw_title))
    struct.pack_into(">L", header, 0x68, 6)  # mobi version
    struct.pack_into(">L", header, 0x6C, 0xFFFFFFFF)  # firstresource
    header[136 : 136 + len(raw_title)] = raw_title

    rec0 = 78 + 2 * 8 + 2
    rec1 = rec0 + len(header)
    palm = bytearray(78)
    palm[0:32] = b"ManualBook".ljust(32, b"\x00")
    palm[0x3C:0x44] = b"BOOKMOBI"
    struct.pack_into(">H", palm, 76, 2)
    return bytes(palm) + struct.pack(">LLLL", rec0, 0, rec1, 0) + b"\x00\x00" + bytes(header) + body


BODY = (
    "<html><head><title>手动确认用的书</title></head><body>"
    "<h1>第一章 手动确认</h1><p>这是从手搓 MOBI 里读出来的正文。</p>"
    "<mbp:pagebreak/>"
    "<h1>第二章 手动确认</h1><p>第二章的正文也在这里。</p>"
    "</body></html>"
).encode("utf-8")


def hide_mobi() -> tuple[str, list[tuple[int, str]]]:
    """把装了 mobi 的 site-packages 从 sys.path 摘掉，模拟「没装」。

    返回 (路径, 被摘掉的位置)，好让 `restore_mobi()` 原样放回去——否则场景 2
    自己会跟着一起被藏起来，变成永远 SKIP 的假测试。
    """
    spec = find_spec("mobi")
    if spec is None:
        return "(本来就没装)", []
    package_dir = os.path.dirname(os.path.dirname(spec.origin or ""))
    removed: list[tuple[int, str]] = []
    for index, entry in enumerate(list(sys.path)):
        if os.path.abspath(entry) == os.path.abspath(package_dir):
            removed.append((index, sys.path.pop(index)))
            break
    for name in [n for n in sys.modules if n == "mobi" or n.startswith("mobi.")]:
        del sys.modules[name]
    return package_dir, removed


def restore_mobi(removed: list[tuple[int, str]]) -> None:
    """把 `hide_mobi()` 摘掉的路径放回原位。"""
    for index, entry in removed:
        sys.path.insert(index, entry)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="stage6_manual_"))
    plain = tmp / "plain.mobi"
    plain.write_bytes(build_mobi(BODY))
    azw3 = tmp / "plain.azw3"
    azw3.write_bytes(build_mobi(BODY))
    locked = tmp / "locked.mobi"
    locked.write_bytes(build_mobi(BODY, crypto=2))
    broken = tmp / "broken.mobi"
    broken.write_bytes(build_mobi(BODY)[:140])

    from minireader.core import mobidoc
    from minireader.core.formats import CURRENT_PHASE, is_supported
    from minireader.core.model import BookError
    from minireader.core.opener import open_book, open_book_kind

    print(f"mobi 库当前状态：is_available() = {mobidoc.is_available()}")

    print("\n[相位闸门]")
    check("CURRENT_PHASE == 6", CURRENT_PHASE == 6, f"实际 {CURRENT_PHASE}")
    check(".mobi 现在能开", is_supported("book.mobi"))
    check(".azw3 现在能开", is_supported("book.azw3"))
    check(".cbr 仍锁在阶段 8", not is_supported("book.cbr"))

    print("\n[场景 3：DRM 只提示，不解密]")
    try:
        open_book(locked)
        check("DRM 书被拒绝", False, "居然打开了")
    except BookError as exc:
        check("DRM 文案正确", str(exc) == mobidoc.DRM_NOTE, str(exc))
        check("文案点名 Calibre", "Calibre" in str(exc))
    check("is_drm() 认得出", mobidoc.is_drm(locked))
    check("is_drm() 不误报未加密的书", not mobidoc.is_drm(plain))

    print("\n[损坏 ≠ DRM]")
    try:
        open_book(broken)
        check("损坏文件被拒绝", False, "居然打开了")
    except BookError as exc:
        check("损坏文案正确", str(exc) == mobidoc.CORRUPT_NOTE, str(exc))
    check(
        "两条文案确实不同",
        mobidoc.DRM_NOTE != mobidoc.CORRUPT_NOTE,
        "DRM 与损坏各说各的",
    )

    print("\n[场景 1：没装 mobi]")
    where, removed = hide_mobi()
    check("确实把它藏起来了", not mobidoc.is_available(), f"摘掉 {where}")
    try:
        open_book(plain)
        check("提示安装", False, "居然打开了")
    except BookError as exc:
        check("提示文案正确", str(exc) == mobidoc.MOBI_NOTE, str(exc))
        check("文案里有可照抄的命令", "pip install mobi" in str(exc), str(exc))
    try:
        open_book(locked)
        check("DRM 判定排在依赖之前", False, "居然报的是安装提示")
    except BookError as exc:
        check(
            "没装 mobi 时 DRM 书仍报 DRM",
            str(exc) == mobidoc.DRM_NOTE,
            "顺序即设计：不会让人白装一个库",
        )
    restore_mobi(removed)
    check("放回去了", mobidoc.is_available(), "场景 2 才能真的跑")

    print("\n[场景 2：装了 mobi，未加密的 MOBI]")
    if not mobidoc.is_available():
        print("  SKIP：本机没有 mobi 库（pip install mobi 之后再看这一节）")
    else:
        book = open_book(plain)
        check("正文可读", "这是从手搓 MOBI 里读出来的正文。" in book.text)
        check("第二章也在", "第二章的正文也在这里。" in book.text)
        check("分了两章", book.chapter_count >= 2, f"{book.chapter_count} 章")
        check("书名来自 <title>", book.title == "手动确认用的书", book.title)
        check("自称 MOBI", book.encoding_label == "MOBI", book.describe())
        check("kind 是文字书", book.kind == "text", book.kind)
        az = open_book(azw3)
        check(".azw3 走同一条路", "这是从手搓 MOBI 里读出来的正文。" in az.text)
        check("open_book_kind 说是文字书", open_book_kind(plain) == "text")
        leftover = list(Path(tempfile.gettempdir()).glob("mobiex*"))
        check("解包临时目录没有残留", not leftover, f"残留 {leftover}" if leftover else "")

    shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{'=' * 60}")
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for _, name, detail in failed:
        print(f"  FAIL {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
