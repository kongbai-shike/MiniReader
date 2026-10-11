"""Path policy: one place that knows where the app reads and writes.

Three locations, and the distinction is the whole point:

* **Bundled resources** (`resource_path`) are *read-only*. In a source checkout they
  live next to the package (e.g. ``assets/minireader.ico``); PyInstaller unpacks them
  under ``sys._MEIPASS`` and that is where they are read from in a frozen build. Never
  write here - under ``Program Files`` the directory is not writable anyway.
* **User data** (`user_data_dir`) is *writable* and per-user: ``%APPDATA%\\MiniReader``
  on Windows, ``~/.config/MiniReader`` elsewhere. Settings, the bookmark database and
  logs all live under it, so installing into ``Program Files`` cannot break saves.
* **Logs** (`log_dir`) are just ``user_data_dir()/logs``, factored out because the entry
  point asks for them before anything else (the excepthook needs a file to write to).

Nothing here is cached at import time: tests redirect ``APPDATA`` and expect every call
to re-read the environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MiniReader"


def is_frozen() -> bool:
    """True when running inside a PyInstaller-built exe.

    PyInstaller sets ``sys.frozen = True`` on every bootstrapped entry (both onefile and
    onedir); nothing else does, so the attribute is its own reliable signal.
    """
    return bool(getattr(sys, "frozen", False))


def resource_path(relative: str) -> str:
    """Resolve a *read-only* bundled resource for source runs and frozen builds.

    * frozen: ``sys._MEIPASS`` is the temp/root directory PyInstaller unpacks
      ``--add-data`` entries into;
    * source: this file is ``<root>/minireader/app_paths.py``, so the project root is
      two parents up - that is where ``assets/`` lives.

    Returns a str because Qt's ``QIcon(path)`` and friends accept str/Path equally, and
    most call sites historically pass strings.
    """
    relative = str(relative)
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).resolve().parent)
    else:
        base = Path(__file__).resolve().parent.parent
    return str(base / relative)


def user_data_dir() -> Path:
    """The per-user writable directory, created on every call.

    Windows: ``%APPDATA%\\MiniReader`` (roaming AppData, same place the settings file
    has always lived). Other platforms: ``$XDG_CONFIG_HOME/MiniReader`` or
    ``~/.config/MiniReader``.

    A failure to create the preferred location (read-only profile, a redirected
    APPDATA) degrades to ``~/.minireader`` rather than raising - a path policy must
    never be the thing that stops startup.
    """
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(root) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path.home() / f".{APP_NAME.lower()}"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def log_dir() -> Path:
    """``user_data_dir()/logs``, created. The entry point's FileHandler lives here."""
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
