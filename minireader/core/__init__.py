"""Pure reading logic. Nothing in here may import Qt - see the tests."""

import importlib

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
    {"EpubContent", "EpubError", "Segment", "looks_like_epub", "parse_epub"}
)


def __getattr__(name: str):
    """按需给包补上 epub 的名字（PEP 562）。

    这里必须用 importlib.import_module，不能图省事写 `from . import epub`：
    后者走 importlib 的 _handle_fromlist，而 _handle_fromlist 会先 hasattr 一下
    本包有没有 epub 这个名字——于是又回到 __getattr__，无限递归到 RecursionError。
    import_module 直接按全名取子模块，绕开了这一步。
    """
    if name == "epub" or name in _LAZY_EPUB_NAMES:
        module = importlib.import_module(f"{__name__}.epub")
        # 拿到之后就把名字塞进 globals：后续访问不再走 __getattr__，
        # 也顺便让 `from minireader.core import parse_epub` 第二次是纯字典查找。
        globals()["epub"] = module
        for _name in _LAZY_EPUB_NAMES:
            globals()[_name] = getattr(module, _name)
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EPUB_NAMES | {"epub"})

__all__ = [
    "CHAPTER_RULES",
    "DEFAULT_CHAPTER_RE",
    "Book",
    "BookError",
    "Chapter",
    "ChapterRule",
    "DecodeError",
    "Decoded",
    "EpubContent",
    "EpubError",
    "Progress",
    "Segment",
    "SplitResult",
    "book_id",
    "chapters_from_marks",
    "clean_title",
    "decode_txt",
    "detect_chapter_re",
    "detect_encoding",
    "human_size",
    "looks_like_epub",
    "marks_from",
    "parse_epub",
    "reencode_options",
    "score_pattern",
    "split_chapters",
    "split_chapters_auto",
]
