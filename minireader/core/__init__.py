"""Pure reading logic. Nothing in here may import Qt - see the tests."""

import importlib

from .book_base import (
    KIND_COMIC,
    KIND_TEXT,
    POSITION_LABELS,
    BookBase,
    Position,
)
from .chapters import (
    CHAPTER_RULES,
    DEFAULT_CHAPTER_RE,
    Chapter,
    ChapterRule,
    SplitResult,
    chapters_from_marks,
    clean_title,
    detect_chapter_re,
    marks_from,
    score_pattern,
    split_chapters,
    split_chapters_auto,
)
from .encoding import Decoded, DecodeError, decode_txt, detect_encoding, reencode_options
from .formats import FORMATS, Format, dialog_filters, format_for, kind_for, support_note
from .model import Book, BookError, Progress, book_id, human_size

# epub 的名字故意不在这里 import。
#
# epub.py 要 import zipfile 和 xml.etree.ElementTree（后者拉起 pyexpat），三个加起来
# 实测约 45 ms；而绝大多数会话读的是 TXT。放在模块级 import，等于每开一次程序都为
# 「说不定要读 EPUB」先付一次钱。改成 PEP 562 的 __getattr__ 之后，这份钱只在真的
# 打开 EPUB 时才付，`from minireader.core import parse_epub` 这个写法照旧能用。
#
# 注意 model.py 里那两处 `from .epub import ...` 本来就在函数体内（只在判定/解析
# EPUB 时才走到），所以这里是整条启动路径上唯一的一处 eager import。
_LAZY_EPUB_NAMES = frozenset(
    {
        "EpubContent",
        "EpubError",
        "FixedContent",
        "FixedPage",
        "Segment",
        "epub_layout",
        "looks_like_epub",
        "parse_epub",
        "parse_fixed",
    }
)

# 漫画的名字同理，而且理由更强：comic.py 拉起 archives.py（zipfile），
# 而「只读 TXT」的会话一次都不会用到它们。格式表（formats.py）不在此列——
# 它只 import dataclasses，拖放判定每次都要查，延迟反而添乱。
_LAZY_COMIC_NAMES = frozenset(
    {
        "ComicBook",
        "ComicPage",
        "ArchiveError",
        "open_comic",
        "open_epub_fixed",
        "open_pdf",
    }
)

# PDF 的名字同理，理由还多一层：pdfdoc.py 本身很轻（只 import os/struct/dataclasses），
# 但它真正干活时要 import pypdfium2，而那是个可选的第三方依赖。放在模块级 import 会
# 让「没装 pypdfium2」的用户在启动时就炸——所以这里连 pdfdoc 本身都延迟。
_LAZY_PDF_NAMES = frozenset(
    {
        "PDFIUM_NOTE",
        "PdfInfo",
        "PdfSegment",
        "TEXT_DENSITY",
        "SCAN_COVERAGE",
        "RENDER_SCALE",
        "classify",
        "extract_text",
        "is_available",
        "page_count",
        "page_sizes",
        "render_page_bmp",
        "write_bmp",
    }
)

# MOBI 的名字同理，理由和 PDF 那组完全一样：mobidoc.py 自己很轻（只 import
# os/re/struct），但真正干活时要 import mobi，而那是个 GPL-3.0 的可选依赖，
# 默认根本没装。放在模块级 import 会让「没装 mobi」的用户在启动时就炸。
#
# 注意这里**故意没有** `is_available`：pdfdoc 已经导出了同名函数，两个都放进
# _LAZY_MODULES 会互相覆盖，而且谁赢取决于先 import 哪个子模块（__getattr__ 最后
# 会把该子模块的同名属性写回 globals）。`core.is_available` 现在仍然唯一地指向
# pdfdoc，MOBI 的用 `core.mobidoc.is_available()`——包级名字有歧义时就不给包级别名，
# 而不是让它随机。
_LAZY_MOBI_NAMES = frozenset(
    {
        "CORRUPT_NOTE",
        "DRM_NOTE",
        "MOBI_NOTE",
        "PRINT_REPLICA_NOTE",
        "MobiInfo",
        "is_drm",
        "looks_like_mobi",
        "open_mobi",
        "sniff",
    }
)

#: 名字 -> 它住在哪个子模块。查表比 if/elif 链清楚，也方便 __dir__ 用。
_LAZY_MODULES = {
    **{name: "epub" for name in _LAZY_EPUB_NAMES},
    "epub": "epub",
    **{name: "comic" for name in _LAZY_COMIC_NAMES},
    "comic": "comic",
    **{name: "pdfdoc" for name in _LAZY_PDF_NAMES},
    "pdfdoc": "pdfdoc",
    **{name: "mobidoc" for name in _LAZY_MOBI_NAMES},
    "mobidoc": "mobidoc",
    # opener 的名字单独列出：它同时暴露「打开」和「PDF 该用哪个阅读器」两类，
    # 前者按路径派发，后者是给窗口用的常量。
    "open_book": "opener",
    "open_book_kind": "opener",
    "opener": "opener",
    "PDF_AUTO": "opener",
    "PDF_TEXT": "opener",
    "PDF_COMIC": "opener",
    "PdfChoice": "opener",
    "pdf_choice": "opener",
}


def __getattr__(name: str):
    """按需给包补上 epub / comic 的名字（PEP 562）。

    这里必须用 importlib.import_module，不能图省事写 `from . import epub`：
    后者走 importlib 的 _handle_fromlist，而 _handle_fromlist 会先 hasattr 一下
    本包有没有 epub 这个名字——于是又回到 __getattr__，无限递归到 RecursionError。
    import_module 直接按全名取子模块，绕开了这一步。
    """
    module_name = _LAZY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = importlib.import_module(f"{__name__}.{module_name}")
    # 拿到之后就把名字塞进 globals：后续访问不再走 __getattr__，
    # 也顺便让 `from minireader.core import parse_epub` 第二次是纯字典查找。
    globals()[module_name] = module
    for _name, _owner in _LAZY_MODULES.items():
        if _owner == module_name:
            globals()[_name] = getattr(module, _name, module)
    return globals()[name]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_MODULES))

__all__ = [
    "CHAPTER_RULES",
    "CORRUPT_NOTE",
    "DEFAULT_CHAPTER_RE",
    "DRM_NOTE",
    "FORMATS",
    "KIND_COMIC",
    "KIND_TEXT",
    "MOBI_NOTE",
    "PDFIUM_NOTE",
    "PDF_AUTO",
    "PDF_COMIC",
    "PDF_TEXT",
    "POSITION_LABELS",
    "PRINT_REPLICA_NOTE",
    "RENDER_SCALE",
    "SCAN_COVERAGE",
    "TEXT_DENSITY",
    "ArchiveError",
    "Book",
    "BookBase",
    "BookError",
    "Chapter",
    "ChapterRule",
    "ComicBook",
    "ComicPage",
    "DecodeError",
    "Decoded",
    "EpubContent",
    "EpubError",
    "FixedContent",
    "FixedPage",
    "Format",
    "MobiInfo",
    "PdfInfo",
    "PdfSegment",
    "PdfChoice",
    "Position",
    "Progress",
    "Segment",
    "SplitResult",
    "book_id",
    "chapters_from_marks",
    "classify",
    "clean_title",
    "decode_txt",
    "detect_chapter_re",
    "detect_encoding",
    "dialog_filters",
    "epub_layout",
    "extract_text",
    "format_for",
    "human_size",
    # pdfdoc 的 is_available。MOBI 的那个不在这里——包级名字会撞车，用
    # core.mobidoc.is_available()（见上面 _LAZY_MOBI_NAMES 的注释）。
    "is_available",
    "is_drm",
    "kind_for",
    "looks_like_epub",
    "looks_like_mobi",
    "marks_from",
    "open_book",
    "open_book_kind",
    "open_comic",
    "open_epub_fixed",
    "open_mobi",
    "open_pdf",
    "page_count",
    "page_sizes",
    "parse_epub",
    "parse_fixed",
    "pdf_choice",
    "reencode_options",
    "render_page_bmp",
    "score_pattern",
    "sniff",
    "split_chapters",
    "split_chapters_auto",
    "support_note",
    "write_bmp",
]
