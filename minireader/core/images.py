"""List the image pages inside a folder, or open a single image as a book.

The folder case has one decision worth recording: comic folders on disk come in
two shapes - a flat pile of `001.jpg`, `002.jpg`, ... or a folder of
*sub*folders, one per chapter. Rather than asking the user which one they meant,
`list_folder_pages()` looks: if the top level already holds images, those are
the book; if it holds none but does hold subfolders, it walks into them in
natural order and concatenates. A folder with both is treated as flat, because
a stray cover image next to chapter folders should not silently reorder the
book.

Like `archives.py`, this hands back *references* (absolute paths) and never
decodes anything. Deliberately Qt-free.
"""

from __future__ import annotations

import os

from .archives import ArchiveError, natural_key
from .formats import IMAGE_EXTS

#: How deep to walk when a folder holds subfolders instead of images. Two is
#: enough for `书/第1话/*.jpg` and `书/卷1/第1话/*.jpg`; deeper than that is
#: usually a mis-picked directory (a whole Downloads tree) and walking it would
#: take seconds before the user sees anything.
MAX_DEPTH = 2


def is_image_file(name: str) -> bool:
    """True when `name` ends in an image extension and is not a dotfile.

    Public because `comic.py` needs the same judgement when deciding whether an
    unrecognised file is a single image rather than a broken archive.
    """
    base = os.path.basename(name)
    if not base or base.startswith("."):
        return False
    dot = base.rfind(".")
    if dot <= 0:
        return False
    return base[dot:].lower() in IMAGE_EXTS


#: Kept as an alias so the module's own callers read naturally.
_is_image_file = is_image_file


def _images_here(directory: str) -> list[str]:
    try:
        entries = os.listdir(directory)
    except OSError as exc:
        raise ArchiveError(f"读不了这个文件夹：{exc.strerror or exc}") from exc
    found = [os.path.join(directory, n) for n in entries if _is_image_file(n)]
    found = [p for p in found if os.path.isfile(p)]
    found.sort(key=lambda p: natural_key(os.path.basename(p)))
    return found


def _subdirs(directory: str) -> list[str]:
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    found = [
        os.path.join(directory, n)
        for n in entries
        if not n.startswith(".") and os.path.isdir(os.path.join(directory, n))
    ]
    found.sort(key=lambda p: natural_key(os.path.basename(p)))
    return found


def list_folder_pages(path) -> list[str]:
    """Every image in `path`, in reading order, as absolute file paths.

    Raises `ArchiveError` with a user-facing message when the folder holds no
    images at all.
    """
    root = os.path.abspath(str(path))
    if not os.path.isdir(root):
        raise ArchiveError(f"找不到这个文件夹：{os.path.basename(root) or root}")

    pages = _images_here(root)
    if pages:
        return pages

    # No images at the top: treat the subfolders as chapters, in order.
    for sub in _walk_subdirs(root, depth=1):
        pages.extend(_images_here(sub))
    if pages:
        return pages

    raise ArchiveError("这个文件夹里没有能看的图片")


def folder_has_images(path) -> bool:
    """True when `path` is a folder we could open as a comic.

    Used by drag-and-drop: a folder that holds no images anywhere we would look
    should be refused *before* the drop is accepted, otherwise the user gets an
    accepted drop followed by an error box.
    """
    root = os.path.abspath(str(path))
    if not os.path.isdir(root):
        return False
    if _images_here(root):
        return True
    return any(_images_here(sub) for sub in _walk_subdirs(root, depth=1))


def _walk_subdirs(root: str, *, depth: int) -> list[str]:
    """Subdirectories of `root` down to MAX_DEPTH, depth-first in natural order."""
    out: list[str] = []
    for sub in _subdirs(root):
        out.append(sub)
        if depth < MAX_DEPTH:
            out.extend(_walk_subdirs(sub, depth=depth + 1))
    return out


def list_single_page(path) -> list[str]:
    """A single image file as a one-page book."""
    target = os.path.abspath(str(path))
    if not os.path.isfile(target):
        raise ArchiveError(f"找不到这个文件：{os.path.basename(target) or target}")
    if not _is_image_file(target):
        raise ArchiveError("这不是一个能看的图片文件")
    return [target]


def read_file_head(path, limit: int) -> bytes:
    """The first `limit` bytes of a file - enough for an image header.

    A plain open/read, so the caller does not need to know whether the page came
    out of a zip or off the disk.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError as exc:
        raise ArchiveError(f"读不出这一页：{exc.strerror or exc}") from exc


def read_file(path) -> bytes:
    """A whole file. Used for a page the reader is about to decode anyway."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise ArchiveError(f"读不出这一页：{exc.strerror or exc}") from exc


def read_file_heads(paths, limit: int) -> list[bytes]:
    """The first `limit` bytes of many files, staying aligned with `paths`.

    The folder-shaped twin of `archives.read_member_heads()`: same "one page
    failing must not cost the others their placeholder height" contract, and the
    same `b""` answer for a file that cannot be read. A folder has no container
    to open once, so the saving here is smaller than for a zip - but the
    alignment guarantee is what `StripView` actually depends on, and having both
    sources answer with the same shape keeps the caller free of branches.
    """
    out: list[bytes] = []
    for path in paths:
        if path is None:
            out.append(b"")
            continue
        try:
            with open(path, "rb") as fh:
                out.append(fh.read(limit))
        except OSError:
            out.append(b"")
    return out
