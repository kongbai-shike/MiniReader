"""Launch-path tests: `main()` itself, not the pieces it calls.

Why this file exists. The 防偷窥 change rewrote the global-hotkey dispatcher inside
`minireader/__main__.py::main()` and built the dispatch table one line too early::

    actions = hotkey_actions(window, settings)      # <-- `window` not assigned yet
    ...
    window = MiniWindow(settings, progress, library, hotkeys=manager)

Every test was green and the 42-check manual script was green, because both call
`hotkey_actions()` / `window.toggle_stealth_mode()` **directly** — nothing in the
whole suite executed `main()`. The first double-click on `run.bat` died with::

    UnboundLocalError: cannot access local variable 'window' where it is not
    associated with a value

So the lesson is not "test more", it is "the entry point needs a test that runs the
entry point". This file does the cheapest honest version of that: `main(["--smoke",
"0.2"], app=...)` really builds the window, shows it, runs a real event loop for
200 ms and returns an exit code.

What is deliberately NOT exercised here: `register_all` is stubbed. Registering
global hotkeys would grab the developer's real chords (Ctrl+Shift+L and friends) for
the duration of the run, and `tests/test_hotkeys.py` already covers registration
against a fake manager. Everything else — settings, theme, window, tray attempt,
capability probing, the smoke report — is the production code path.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

# Must be set before QApplication exists, and therefore before minireader.qt imports
# Qt at all. setdefault, so an explicit QT_QPA_PLATFORM from the caller still wins.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])


class LaunchPathTest(unittest.TestCase):
    """`main()` must survive being called the way `run.bat` calls it."""

    def setUp(self) -> None:
        # Redirect %APPDATA% so the launch reads an empty config and cannot write to
        # the real one: `--smoke` ends with the window closing, and closing a window
        # saves progress (and settings) on the way out.
        #
        # mkdtemp + rmtree(ignore_errors=True) rather than TemporaryDirectory: the
        # window keeps `reading.db` open through `BookmarkStore`, and whether the
        # connection is gone by the time cleanup runs depends on when the binding's
        # C++ side drops the widget — PyQt5 keeps it alive longer than PySide6, which
        # turned plain cleanup into "PermissionError: WinError 32". The test closes the
        # store explicitly below; ignore_errors is the belt to that braces.
        self._tmp = tempfile.mkdtemp(prefix="mr-launch-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self._tmp
        self.addCleanup(self._restore_appdata)
        self._window = None

    def _restore_appdata(self) -> None:
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata

    def _run_main(self, *argv: str) -> tuple[int, str, list]:
        """Call `main()` with the hotkey registration stubbed out."""
        from minireader import hotkeys as hotkeys_mod
        from minireader import __main__ as entry

        calls: list[str] = []

        def fake_register_all(manager, wanted, hwnd, labels=None):
            calls.append("register_all")
            return {}

        def on_ready(window, tray, app) -> None:
            # The same hook root `main.py` uses for its single-instance server. Handy
            # here because the window is otherwise unreachable from the outside, and
            # closing it is what releases reading.db before the temp dir goes away.
            self._window = window
            self.addCleanup(self._close_window)

        real = hotkeys_mod.register_all
        hotkeys_mod.register_all = fake_register_all
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
                code = entry.main(list(argv), app=_app, on_ready=on_ready)
        finally:
            hotkeys_mod.register_all = real
        return code, buffer.getvalue(), calls

    def _close_window(self) -> None:
        window = self._window
        if window is None:
            return
        window.close()            # closeEvent -> store.close(): drop the sqlite handle
        window.deleteLater()
        _app.processEvents()
        self._window = None

    def test_main_builds_the_window_and_returns_zero(self) -> None:
        """The regression: `main()` crashed with UnboundLocalError on `window`.

        A smoke run is the only assertion that needs to be made here — if `main()`
        gets far enough to show a window and run its event loop, the ordering bug
        cannot come back, and any other exception still lands in this test.
        """
        code, output, calls = self._run_main("--smoke", "0.2")

        self.assertEqual(code, 0, output)
        self.assertIn("[smoke] 窗口已显示", output)
        # Reaching this print means the window, its tray probe and its capability
        # update all completed; the old crash happened before `MiniWindow` existed.
        self.assertIn("[smoke] 可隐藏=", output)
        self.assertEqual(calls, ["register_all"])
        self.assertIsNotNone(self._window, "on_ready 没拿到窗口")


if __name__ == "__main__":
    unittest.main()
