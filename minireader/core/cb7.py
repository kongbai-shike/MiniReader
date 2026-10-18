"""Read the image pages inside a CB7 (7-Zip) comic archive.

7z is the same story as RAR in one respect and different in another. Same: the
container cannot be unpacked by the standard library, so the bytes have to come
from an external program. Different: there is exactly one program worth driving
(7-Zip), and its command line already prints the machine-readable listing this
module needs, so no third-party Python package is involved at all. `cb7` never
imports `py7zr`; the plan's option B applies here more cheaply than it did to
RAR, because 7-Zip is the *only* tool rather than one of two.

**Why the tool is not bundled.** Same reason as `rar.py`: adding a ~1.5 MB
binary to a pure-Python exe for one comic format is a bad trade, and the reader
says plainly what to install instead.

**Why the probe is separate from `rar.find()`.** `rar.find()` prefers `unrar`
because WinRAR is the reference implementation for RAR. That preference is
exactly wrong here: `unrar` cannot open a 7z at all. So this module asks its own
question - "is there a 7z?" - and only borrows `rar`'s answer when that answer
happens to be a 7z. Sharing one cache would mean that installing both programs
leaves CB7 permanently unreadable, because the shared cache would keep returning
`unrar`.

**Why the persisted answer is the same `Settings` pair.** `unrar_path` /
`unrar_kind` are really "the external extractor we found", and `unrar_kind` is
already `"7z"` when the found tool is 7-Zip. One 7z.exe serves both formats, so
storing it twice would let the two copies disagree after a reinstall. When the
stored tool is `unrar`, this module falls through to its own probe.

**Why every subprocess gets `-p-` and a timeout.** A password-protected 7z makes
7-Zip print a prompt and wait on stdin forever; `-p-` disables the prompt and a
30 second timeout catches the archives where the tool is merely slow. Every call
also passes `CREATE_NO_WINDOW` - this is a GUI program, and without it a console
window flashes on screen for every page turn. All three live in `rar.py` and are
reused from there, so the two comic containers cannot drift apart.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

import os
import shutil

from . import rar
from .archives import HEAD_BYTES, ArchiveEntry, ArchiveError, is_page_name, natural_key

__all__ = [
    "MESSAGE_NO_TOOL",
    "find",
    "forget",
    "known",
    "list_pages",
    "looks_like_cb7",
    "read_page",
    "read_page_head",
    "read_page_heads",
    "remember",
    "require",
]

#: Shown when no 7-Zip could be found. Names the one program that can help and
#: says what to do next, in the same shape as `rar.MESSAGE_NO_TOOL`.
MESSAGE_NO_TOOL = "打开 CB7 需要 7-Zip，请先安装，然后重试"

#: The six bytes every 7z archive starts with. Enough to tell a CB7 from a CBZ
#: that was renamed, which matters because the two need different readers.
MAGIC = b"7z\xbc\xaf\x27\x1c"

#: Seconds to wait for one extraction. Deliberately the same number as
#: `rar.EXTRACT_TIMEOUT`: both are "the tool is waiting for something that will
#: never come" and the user needs the window back either way.
EXTRACT_TIMEOUT = rar.EXTRACT_TIMEOUT


# -- the cache ---------------------------------------------------------------

#: Last known answer. `None` means either "never probed" or "probed and found
#: nothing"; `_PROBED` tells those two apart, and is only set on success.
_CACHED: rar.Tool | None = None
_PROBED = False


def known() -> rar.Tool | None:
    """The cached answer, without probing. `None` when nothing is known.

    Callers that must not block (a repaint, a tooltip) use this; callers that can
    afford one probe use `find()`.
    """
    return _CACHED


def remember(path: str, kind: str = "") -> rar.Tool | None:
    """Trust a persisted answer. Returns the `Tool` when it is usable for 7z.

    Called once at startup with whatever `Settings` remembers. An empty path, a
    kind that is not `7z` (a `unrar` cannot open a 7z), or a path that no longer
    exists all return `None` *without* marking the cache as probed, so the next
    `find()` does a real probe instead of trusting a dead answer.
    """
    global _CACHED, _PROBED

    if not path or kind != rar.TOOL_SEVENZIP:
        return None
    if not os.path.isfile(path):
        return None
    _CACHED = rar.Tool(path=path, kind=kind)
    _PROBED = True
    return _CACHED


def forget() -> None:
    """Throw the cached answer away. The settings dialog's "重新检测" calls this."""
    global _CACHED, _PROBED
    _CACHED = None
    _PROBED = False


def find(force: bool = False) -> rar.Tool | None:
    """Find a 7-Zip, caching a hit for the rest of the session.

    A miss is deliberately *not* cached: the user reads "请先安装 7-Zip", installs
    it, and every later page turn would still say "请先安装" because the miss was
    remembered. Four `which` calls are much cheaper than that bug.
    """
    global _CACHED, _PROBED

    if force:
        _CACHED = None
        _PROBED = False
    if _CACHED is not None:
        return _CACHED
    if _PROBED:
        return None

    borrowed = rar.known()
    if borrowed is not None and borrowed.kind == rar.TOOL_SEVENZIP:
        _CACHED = borrowed
        _PROBED = True
        return _CACHED

    tool = probe()
    if tool is not None:
        _CACHED = tool
        _PROBED = True
    return tool


def require() -> rar.Tool:
    """`find()`, or raise the message the user needs to see."""
    tool = find()
    if tool is None:
        raise ArchiveError(MESSAGE_NO_TOOL)
    return tool


def probe() -> rar.Tool | None:
    """Look for a 7-Zip, in the order a Windows user would expect. Never raises.

    Two steps, not four: unlike WinRAR, 7-Zip does add itself to `PATH` when the
    installer is asked to, so the `which` calls catch most installs and the
    install-directory check catches the rest. `7za` is the portable build, which
    ships without the GUI and therefore without a Start Menu entry to notice.

    `shutil.which` and `os.path.isfile` are called through the module rather than
    from-imported so tests can stub them.
    """
    for name in ("7z", "7za"):
        found = shutil.which(name)
        if found:
            return rar.Tool(path=found, kind=rar.TOOL_SEVENZIP)
    for path in rar._sevenzip_candidates():
        if os.path.isfile(path):
            return rar.Tool(path=path, kind=rar.TOOL_SEVENZIP)
    return None


# -- reading -----------------------------------------------------------------


def looks_like_cb7(path) -> bool:
    """True when the file starts with the 7z signature. Never raises."""
    try:
        with open(str(path), "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except (OSError, TypeError, ValueError):
        return False


def list_pages(path) -> list[ArchiveEntry]:
    """The image members, in reading order. Raises `ArchiveError` when unreadable.

    Unlike RAR there is no pure-Python fallback to prefer: 7z's header is itself
    compressed, so even the file names need the tool. One subprocess here is
    cheap - it is the per-page reads that would not be.
    """
    tool = require()
    target = os.path.abspath(str(path))
    if not os.path.isfile(target):
        raise ArchiveError(f"找不到这个文件：{os.path.basename(target) or target}")

    try:
        names = rar.list_names_by_tool(tool, target)
    except ArchiveError as exc:
        raise ArchiveError(f"读不出这个压缩包：{exc}") from exc

    names = [name for name in names if is_page_name(name)]
    if not names:
        raise ArchiveError("这个压缩包里没有能看的图片")

    entries = [ArchiveEntry(ref=name, name=name) for name in names]
    entries.sort(key=lambda entry: natural_key(entry.name))
    return entries


def read_page(path, name: str) -> bytes:
    """One page's bytes. Raises `ArchiveError` when it cannot be extracted."""
    tool = require()
    return rar.read_page_by_tool(tool, os.path.abspath(str(path)), name)


def read_page_head(path, name: str, limit: int = HEAD_BYTES) -> bytes:
    """The first `limit` bytes of one page - enough for a size probe."""
    return read_page(path, name)[:limit]


def read_page_heads(path, names, limit: int = HEAD_BYTES) -> list[bytes]:
    """Placeholder-size probes for many pages. Never raises; aligns with `names`.

    Deliberately returns nothing useful. `rar.py` can read several members from
    one archive handle, but 7-Zip has no way to stream several members in one
    invocation that costs less than N invocations - so measuring a 300-page book
    at open time would spawn 300 processes to save a default height. Unknown
    sizes cost a slightly wrong placeholder, not a wrong page; each page's real
    size arrives the first time it is displayed, through `set_page_size()`.
    """
    return [b"" for _ in names]
