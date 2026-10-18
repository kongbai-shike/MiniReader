"""Read the image pages inside a CBR/RAR comic archive.

RAR is the one comic container that cannot be unpacked in pure Python: the
format's compression is proprietary and `rarfile` - which does parse the
*headers* in Python, for both RAR3 and RAR5 - shells out to an external tool to
get the bytes out. So this module has two jobs that the ZIP reader
(`archives.py`) does not have: find a tool, and remember having found it.

**Why the tool is not bundled.** The plan's option B: ship no binary, use
whatever the user already has. Bundling `unrar` would mean redistributing a
program whose licence forbids it; bundling 7-Zip would add ~1.5 MB to an exe
that is otherwise pure Python. Neither is acceptable for a reader whose whole
point is being small, so the reader looks for the tool the user almost certainly
already installed and says so plainly when there is none.

**Why the answer is cached twice.** Probing means running four `which` calls and
four `os.path.isfile` calls, which is cheap - but it also means a subprocess
`check` per candidate, and doing that on every page turn would be absurd. So the
answer is cached in memory (`_CACHED` + `_PROBED`) and, through `Settings`, on
disk (`unrar_path` / `unrar_kind` / `unrar_checked`). The disk copy is trusted
at startup without re-probing, because re-probing would make the first page of
every session slower than the rest; the user can force a fresh look with the
"重新检测" button, and a failed probe is retried automatically next time
(`_PROBED` is only set on success, see `find()`).

**Why every subprocess gets `-p-` and a timeout.** A password-protected RAR
makes `unrar` print a prompt and wait on stdin forever. `-p-` disables the
prompt, and a 30 second timeout catches the archives where the tool decides to
be slow instead of interactive. Without both, opening one protected `.cbr`
freezes the window with no way out. Every call also passes
`CREATE_NO_WINDOW`: this is a GUI program, and without it a console window
flashes on screen for every page turn.

Deliberately Qt-free, like the rest of `minireader.core.*`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .archives import HEAD_BYTES, ArchiveEntry, ArchiveError, is_page_name, natural_key

__all__ = [
    "MESSAGE_NO_TOOL",
    "TOOL_KINDS",
    "Tool",
    "find",
    "forget",
    "known",
    "list_pages",
    "looks_like_rar",
    "read_page",
    "read_page_head",
    "read_page_heads",
    "remember",
    "require",
]

#: The two tools this module knows how to drive. `unrar` is the reference
#: implementation (it is what WinRAR ships) and `7z` is the one people are far
#: more likely to have, since 7-Zip is free and WinRAR is not.
TOOL_UNRAR = "unrar"
TOOL_SEVENZIP = "7z"
TOOL_KINDS = (TOOL_UNRAR, TOOL_SEVENZIP)

#: Shown when no tool could be found. One sentence, names both programs, and
#: says what to do next - the same shape as every other message in this package.
MESSAGE_NO_TOOL = "打开 CBR/RAR 需要 WinRAR 或 7-Zip，请先安装其中一个，然后重试"

#: Seconds to wait for one extraction. A 30 MB page on a cold disk is a few
#: hundred milliseconds; anything near this number means the tool is waiting for
#: something that will never come, and the user needs the window back.
EXTRACT_TIMEOUT = 30

#: `CREATE_NO_WINDOW`. A GUI process that spawns a console program without this
#: shows a black rectangle for the lifetime of the child - once per page turn.
#: Defined here rather than read from `subprocess` because the constant only
#: exists on Windows and this module is imported everywhere.
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Tool:
    """An external program that can extract a RAR, and which dialect to speak.

    `kind` is one of `TOOL_KINDS` and decides both the command line and which
    global `rarfile` reads. It is stored next to the path (rather than inferred
    from the filename) because `unrar.exe`, `UnRAR.exe` and a renamed
    `7z.exe` are all plausible, and guessing wrong produces "cannot execute"
    on the first page instead of at probe time.
    """

    path: str
    kind: str


# -- the cache ---------------------------------------------------------------

#: Last known answer. `None` means either "never probed" or "probed and found
#: nothing"; `_PROBED` tells those two apart.
_CACHED: Tool | None = None
_PROBED = False


def known() -> Tool | None:
    """The cached answer, without probing. `None` when nothing is known.

    Callers that must not block (a repaint, a tooltip) use this; callers that can
    afford one probe use `find()`.
    """
    return _CACHED


def remember(path: str, kind: str = "") -> Tool | None:
    """Trust a persisted answer. Returns the `Tool` when it is usable.

    Called once at startup with whatever `Settings` remembers. An empty path, an
    unknown kind, or a path that no longer exists all mean "the cache is stale" -
    the tool was uninstalled, or the settings file was copied from another
    machine - and the answer is then `None` with `_PROBED` left alone, so the
    next `find()` probes for real instead of trusting a dead path.
    """
    target = str(path or "").strip()
    wanted = str(kind or "").strip().lower()
    if not target or wanted not in TOOL_KINDS:
        return None
    if not os.path.isfile(target):
        return None
    global _CACHED, _PROBED
    _CACHED = Tool(path=target, kind=wanted)
    _PROBED = True
    return _CACHED


def forget() -> None:
    """Drop the cache, so the next `find()` probes again.

    This is what the settings dialog's "重新检测" button calls: the user has just
    installed 7-Zip and expects the reader to notice without a restart.
    """
    global _CACHED, _PROBED
    _CACHED = None
    _PROBED = False


def find(force: bool = False) -> Tool | None:
    """The tool to use, probing if the cache cannot answer. `None` if none.

    A *successful* probe is cached for the rest of the session. A failed one is
    **not**, deliberately: the user reading "请先安装 7-Zip", installing it, and
    then finding that every later attempt still says "install 7-Zip" (because
    the miss was cached) is a worse bug than paying four `which` calls again.
    The cost of a miss is a handful of failed `stat` calls, and misses only
    happen on machines that have no tool at all.
    """
    global _CACHED, _PROBED
    if force:
        forget()
    if _PROBED:
        return _CACHED
    found = probe()
    if found is not None:
        _CACHED = found
        _PROBED = True
    return found


def require() -> Tool:
    """`find()`, but raises the user-facing message when there is no tool."""
    found = find()
    if found is None:
        raise ArchiveError(MESSAGE_NO_TOOL)
    return found


def probe() -> Tool | None:
    """Look for a tool, in the order that finds the best one fastest.

    The order is the plan's, and each step is there for a reason:

    1. `unrar` on `PATH`. Best case: the reference implementation, no guessing.
    2. `7z` / `7za` on `PATH`. 7-Zip's installer does add itself to `PATH` on
       some versions, and it is the tool most people have.
    3. WinRAR's install directory. WinRAR does **not** add itself to `PATH` -
       this step is the only way to find it on a normal Windows install.
    4. 7-Zip's install directory, same reason.

    `shutil.which` and `os.path.isfile` are called through their modules rather
    than imported by name so the tests can stub them; that is also why this
    function takes no arguments and touches no globals - it is the pure part.
    """
    for name in ("unrar",):
        found = shutil.which(name)
        if found:
            return Tool(path=found, kind=TOOL_UNRAR)

    for name in ("7z", "7za"):
        found = shutil.which(name)
        if found:
            return Tool(path=found, kind=TOOL_SEVENZIP)

    for path in _winrar_candidates():
        if os.path.isfile(path):
            return Tool(path=path, kind=TOOL_UNRAR)

    for path in _sevenzip_candidates():
        if os.path.isfile(path):
            return Tool(path=path, kind=TOOL_SEVENZIP)

    return None


def _program_files() -> tuple[str, ...]:
    """The install roots to look under, most likely first.

    Built from the environment rather than hard-coded: a 32-bit Windows has no
    `ProgramW6432`, and a machine with a relocated Program Files puts the real
    answer in `ProgramFiles`. Duplicates are dropped so a machine where both
    variables agree does not stat every path twice.
    """
    roots: list[str] = []
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(name)
        if value and value not in roots:
            roots.append(value)
    return tuple(roots)


def _winrar_candidates() -> tuple[str, ...]:
    """Where WinRAR puts its command line extractor.

    `UnRAR.exe` and not `WinRAR.exe`: the latter is the GUI, which would open a
    window and do nothing useful with a command line.
    """
    return tuple(os.path.join(root, "WinRAR", "UnRAR.exe") for root in _program_files())


def _sevenzip_candidates() -> tuple[str, ...]:
    """Where 7-Zip puts its command line program, both bitnesses.

    `7z.exe` first, then the standalone `7za.exe`: some portable distributions
    ship only the latter.
    """
    out: list[str] = []
    for root in _program_files():
        out.append(os.path.join(root, "7-Zip", "7z.exe"))
        out.append(os.path.join(root, "7-Zip", "7za.exe"))
    return tuple(out)


# -- driving rarfile ---------------------------------------------------------


def _rarfile(tool: Tool):
    """`rarfile`, configured to use `tool`. Raises `ArchiveError` without it.

    Imported here rather than at module level for the reason every optional
    dependency in this package is (see `docs/DEVELOPMENT.md`, constraint 22):
    `minireader.core.*` must import on a machine where `rarfile` was never
    installed, and a module-level import would turn "CBR is unsupported" into
    "the whole app does not start".
    """
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - only on a bare interpreter
        raise ArchiveError(
            "打开 CBR/RAR 需要 rarfile 模块，请先执行：pip install rarfile"
        ) from exc

    if tool.kind == TOOL_UNRAR:
        rarfile.UNRAR_TOOL = tool.path
    else:
        rarfile.SEVENZIP_TOOL = tool.path

    # `rarfile` caches the chosen tool in a module global and only re-picks when
    # it is None. Clearing it makes switching tools (the 重新检测 button finding a
    # different program) take effect immediately instead of at the next restart.
    rarfile.CURRENT_SETUP = None
    return rarfile


def looks_like_rar(path) -> bool:
    """True when `path` has a RAR signature. Never raises.

    Same contract as `archives.looks_like_archive()`: drag-and-drop and the file
    dialog both call this on files the user picked, so a `.cbr` that is really an
    HTML error page must produce a friendly message rather than a traceback.
    `rarfile.is_rarfile` reads 7 bytes and handles both RAR3 and RAR5.
    """
    try:
        import rarfile
    except ImportError:  # pragma: no cover
        return False
    try:
        return bool(rarfile.is_rarfile(str(path)))
    except (OSError, ValueError):
        return False


def _open(tool: Tool, path) -> "object":
    """An open `rarfile.RarFile`, or `ArchiveError` with a Chinese message."""
    rarfile = _rarfile(tool)
    target = str(path)
    if not os.path.isfile(target):
        raise ArchiveError(f"找不到这个文件：{os.path.basename(target) or target}")
    try:
        return rarfile.RarFile(target)
    except rarfile.NotRarFile as exc:
        raise ArchiveError("这个压缩包已损坏或不是 RAR 格式") from exc
    except rarfile.BadRarFile as exc:
        raise ArchiveError(f"这个压缩包已损坏：{exc}") from exc
    except OSError as exc:
        raise ArchiveError(f"打不开这个文件：{exc.strerror or exc}") from exc


def list_pages(path) -> list[ArchiveEntry]:
    """Every image in the archive, in reading order. Raises `ArchiveError`.

    The listing normally needs no external tool at all: `rarfile` parses the
    block headers in Python for both RAR3 and RAR5, which is why a user without
    WinRAR still gets an accurate page count in the message that asks them to
    install one. `require()` is called by the caller (`comic.open_rar`) rather
    than here, so this function stays usable for diagnostics on a bare machine.

    When the headers *cannot* be parsed, the tool gets one chance to list the
    archive before we give up: `list_names_by_tool()` is the fallback, and this
    is the only production caller it has.
    """
    tool = require()
    rf = _open(tool, path)
    try:
        infos = rf.infolist()
        names = [
            info.filename
            for info in infos
            if not info.is_dir() and is_page_name(info.filename)
        ]
    except Exception as exc:  # rarfile's error tree is wide; all mean "unreadable"
        rf.close()
        try:
            names = [name for name in list_names_by_tool(tool, path) if is_page_name(name)]
        except ArchiveError:
            raise ArchiveError(f"读不出这个压缩包：{exc}") from exc
    else:
        rf.close()

    if not names:
        raise ArchiveError("这个压缩包里没有能看的图片")
    entries = [ArchiveEntry(ref=name, name=name) for name in names]
    entries.sort(key=lambda entry: natural_key(entry.name))
    return entries


def read_page(path, name: str) -> bytes:
    """The raw, still-encoded bytes of one member. Raises `ArchiveError`.

    Two ways to fail, and they are not the same failure. `rarfile` raising means
    it could not drive the tool (it has no timeout, and it refuses a tool it does
    not recognise); anything else means the member itself is unreadable. The
    first falls back to our own subprocess, which is the only production caller
    `read_page_by_tool()` has; the second is reported as is.
    """
    tool = require()
    rarfile = _rarfile(tool)
    rf = _open(tool, path)
    try:
        return rf.read(name)
    except rarfile.RarCannotExec:
        # rarfile could not drive the tool at all. Our own command line can, and
        # it is also the only path that carries a timeout.
        return read_page_by_tool(tool, path, name)
    except Exception as exc:
        raise ArchiveError(f"读不出这一页：{exc}") from exc
    finally:
        rf.close()


def read_page_head(path, name: str, limit: int = HEAD_BYTES) -> bytes:
    """The first `limit` bytes of one member - enough for an image header."""
    tool = require()
    rarfile = _rarfile(tool)
    rf = _open(tool, path)
    try:
        with rf.open(name) as fh:
            return fh.read(limit)
    except rarfile.RarCannotExec:
        # Same reason as `read_page()`: the tool exists but rarfile will not drive
        # it. Truncate here, because the fallback streams the whole member.
        return read_page_by_tool(tool, path, name)[:limit]
    except Exception as exc:
        raise ArchiveError(f"读不出这一页：{exc}") from exc
    finally:
        rf.close()


def read_page_heads(path, names, limit: int = HEAD_BYTES) -> list[bytes]:
    """`read_page_head()` for many members, opening the archive **once**.

    The same 10x that `archives.read_member_heads()` buys for ZIP applies here,
    and it matters more: every open of a RAR costs a subprocess launch for the
    first member, so per-page opens would spawn 300 processes to measure 300
    placeholders.

    Never raises, and stays aligned with `names`: a member that cannot be read
    gets `b""`, which the caller turns into "size unknown". One corrupt page must
    not cost the other 299 their placeholder height.
    """
    wanted = list(names)
    tool = find()
    if tool is None:
        # No tool: every page is "size unknown" rather than an exception in the
        # middle of a layout pass. `list_pages()` already raised the real
        # message if the book could not be opened at all.
        return [b"" for _ in wanted]
    rarfile = _rarfile(tool)
    rf = _open(tool, path)
    out: list[bytes] = []
    try:
        for name in wanted:
            if name is None:
                out.append(b"")
                continue
            try:
                with rf.open(name) as fh:
                    out.append(fh.read(limit))
            except rarfile.RarCannotExec:
                # Deliberately *not* falling back to `read_page_by_tool()` here.
                # This function exists to measure placeholders for the strip view;
                # the fallback is one subprocess per member, so a 300-page book
                # would spawn 300 processes at open time to save a default height.
                # Unknown sizes cost a slightly wrong placeholder, not a wrong page.
                out.append(b"")
            except Exception:
                out.append(b"")
    except Exception:
        return [b"" for _ in wanted]
    finally:
        rf.close()
    return out


# -- the fallback path: our own subprocess -----------------------------------
#
# `rarfile` already passes `-p-`, `stdin=DEVNULL` and `CREATE_NO_WINDOW`, and it
# raises `PasswordRequired` from the header *before* any tool runs, so the normal
# path cannot hang. It does, however, offer no way to put a timeout on the child.
# These two functions exist for the archives where the tool misbehaves instead of
# asking - and for the case where `rarfile` refuses a tool it cannot recognise.
# They are the only place that builds a command line by hand, so they are also
# the place that owns the `-p-` / timeout / no-window guarantees.


def _run(tool: Tool, args: list[str]) -> bytes:
    """Run one extraction, returning stdout. Raises `ArchiveError` on failure.

    Every guarantee this module promises about child processes lives in this
    one call: `-p-` is appended by the callers (it is a tool argument, not a
    subprocess flag), the timeout bounds the wait, and `CREATE_NO_WINDOW` keeps
    the console off the screen.
    """
    cmd = [tool.path] + args
    try:
        done = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=EXTRACT_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError(
            f"解压这一页超时（{EXTRACT_TIMEOUT} 秒），这个压缩包可能有密码或已损坏"
        ) from exc
    except FileNotFoundError as exc:
        raise ArchiveError(MESSAGE_NO_TOOL) from exc
    except OSError as exc:
        raise ArchiveError(f"调不起解压工具：{exc}") from exc
    if done.returncode != 0:
        text = (done.stderr or b"").decode("utf-8", "replace").strip()
        raise ArchiveError(f"解压失败：{text or done.returncode}")
    return done.stdout


def list_names_by_tool(tool: Tool, path) -> list[str]:
    """Member names straight from the tool, for archives `rarfile` cannot parse.

    `unrar lb` prints one bare name per line, which needs no parsing at all;
    7-Zip has no such mode, so `-slt` blocks are parsed and the archive's own
    record (the one with `Physical Size`) is skipped.
    """
    target = str(path)
    if tool.kind == TOOL_UNRAR:
        raw = _run(tool, ["lb", "-p-", "--", target])
        return [line.strip() for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]
    raw = _run(tool, ["l", "-slt", "-p-", "--", target])
    return _parse_sevenzip_listing(raw.decode("utf-8", "replace"))


def _parse_sevenzip_listing(text: str) -> list[str]:
    """The member paths out of `7z l -slt` output.

    `-slt` prints blank-line-separated records of `Key = Value` lines. The first
    record describes the archive itself and is recognised by `Physical Size`,
    which no member has; directories are recognised by `Folder = +` and skipped
    because they are not pages.
    """
    names: list[str] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition(" = ")
            if sep:
                fields[key.strip()] = value.strip()
        if "Path" not in fields or "Physical Size" in fields:
            continue
        if fields.get("Folder") == "+":
            continue
        names.append(fields["Path"])
    return names


def read_page_by_tool(tool: Tool, path, name: str) -> bytes:
    """One member's bytes straight from the tool - the `read_page()` fallback."""
    target = str(path)
    if tool.kind == TOOL_UNRAR:
        return _run(tool, ["p", "-inul", "-p-", "--", target, name])
    return _run(tool, ["x", "-so", "-p-", "--", target, name])
