"""Headless UI tests: window chrome, scrolling, chapter flipping, boss key, persistence.

Runs under Qt's "offscreen" platform plugin, so it needs no display and no window
manager - which also means it never steals focus while you are working.

Offscreen is a real QPA plugin, not a mock: widgets lay out, the scrollbar has a real
range (measured: 26 266 px for 300 paragraphs) and events dispatch normally, so
scrolling and chapter transitions are genuinely exercised here. What it CANNOT show is
pixels - fonts are a fallback and there is no compositor - so nothing here asserts on
appearance, and the real-platform smoke run (--smoke) covers what it cannot.

    py -3.12 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Must be set before QApplication exists, and therefore before minireader.qt imports
# Qt at all. setdefault, so an explicit QT_QPA_PLATFORM from the caller still wins.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import (  # noqa: E402
    QApplication,
    QEvent,
    QGroupBox,
    QKeyEvent,
    QLabel,
    QMimeData,
    QMouseEvent,
    QObject,
    QPoint,
    QPointF,
    QUrl,
    QWheelEvent,
    Qt,
    Signal,
    install_message_filter,
    native_platform_available,
)

from minireader import snap as snapmod  # noqa: E402
from minireader.core.model import book_id  # noqa: E402
from minireader.hotkeys import (  # noqa: E402
    HotkeyError,
    NullHotkeys,
    format_hotkey,
    make_manager,
    parse_hotkey,
)
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui.settings_dialog import NAV_ITEMS, SettingsDialog  # noqa: E402
from minireader.ui.window import LEAVE_HIDE_OPACITY, MiniWindow  # noqa: E402

# Qt's offscreen plugin has no fonts of its own and no window manager, so it warns
# about both on every run. Those two lines are expected here and would otherwise bury
# the actual test results; anything else is still printed.
_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

# One QApplication for the whole process; Qt aborts if a second one is created.
_app = QApplication.instance() or QApplication([])

BODY = "正文内容。" * 80


def make_book(directory: Path, name: str = "book.txt", chapters: int = 3) -> Path:
    """Each chapter gets distinct text, so "did the chapter change?" is answerable."""
    parts = []
    for i in range(1, chapters + 1):
        parts.append(f"第{i}章 标题{i}\n{f"这是第{i}章的正文。" * 70}{BODY}")
    path = directory / name
    path.write_bytes("\n".join(parts).encode("utf-8"))
    return path


def same_path(a: str | None, b: Path) -> bool:
    """QUrl.toLocalFile() hands back forward slashes on Windows; Path equality
    normalises separators, so this compares what the user means rather than spelling."""
    return a is not None and Path(a) == b


def assert_at_leave_hide_floor(testcase: unittest.TestCase, window, msg: str = "") -> None:
    """断言窗口已压到移出隐身下限 LEAVE_HIDE_OPACITY。

    setWindowOpacity 在 Windows 落到 LWA_ALPHA，alpha 只有 BYTE 精度（1/255）：传入
    0.01 会被量化到最近的 alpha 字节，回读为 2/255≈0.0078（offscreen 平台同样按这个
    粒度回读），所以不能拿浮点等值和常量比。这里钉死两件事：
      1. 严格非零——回到 0 就是点击穿透 bug 复发（移回鼠标收不到 enterEvent）；
      2. 与常量只差量化误差（±2 个 alpha 阶）。
    """
    op = window.windowOpacity()
    testcase.assertGreater(
        op, 0.0, msg or "隐身下限必须非零：alpha=0 会点击穿透，窗口移回鼠标唤不醒"
    )
    testcase.assertAlmostEqual(
        op, LEAVE_HIDE_OPACITY, delta=2 / 255.0,
        msg=msg or "整窗应压到 LEAVE_HIDE_OPACITY（允许 LWA_ALPHA 的 1/255 量化误差）",
    )


def wheel(widget, down: int) -> None:
    """Deliver a real QWheelEvent.

    `down` is positive to scroll toward the END of the text, which is the direction a
    user gets by rolling the wheel towards themselves - Qt reports that as a NEGATIVE
    angleDelta. The helper takes the human direction so call sites read unambiguously.
    """
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        pos,
        pos,
        QPoint(0, 0),
        QPoint(0, -120 * down),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    widget.wheelEvent(event)


class FakeTray(QObject):
    """Stands in for the real tray icon, which the offscreen plugin cannot host.

    This is honest about what it tests: not QSystemTrayIcon, but the *window's* contract
    with it - that every signal is wired to the right action, that the tooltip follows
    the disguise title, and that the icon's notion of "hidden" tracks the window's. That
    contract is the part with bugs in it; QSystemTrayIcon itself is Qt's problem, and
    `--check` reports whether a real tray exists on the machine that is running.
    """

    toggleRequested = Signal()
    showRequested = Signal()
    hideRequested = Signal()
    openRequested = Signal()
    settingsRequested = Signal()
    quitRequested = Signal()
    opacityRequested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.title = None
        self.hidden_state = None
        self.opacity = None
        self.shutdown_called = False

    def set_title(self, title: str) -> None:
        self.title = title

    def set_hidden_state(self, hidden: bool) -> None:
        self.hidden_state = bool(hidden)

    def set_opacity(self, percent: int) -> None:
        self.opacity = int(percent)

    def shutdown(self) -> None:
        self.shutdown_called = True


def _dispose_window(window: MiniWindow) -> None:
    """Quiesce every event source on a test window, then queue it for deletion.

    This has to stop *all* producers of native events, not just the two debounced
    savers. Qt 5 only delivers a DeferredDelete while the event dispatcher's queue
    is quiet: ``QCoreApplicationPrivate::sendPostedEvents`` requeues it while
    ``QAbstractEventDispatcher::hasPendingEvents()`` is true. A running
    ``QPropertyAnimation`` drives the global animation timer continuously, so a
    window that was mid-fade when a test ended stayed alive across every later
    module: deleteLater() never landed, the hidden window kept receiving queued
    scroll events (restarting its 900 ms progress saver), and that saver fired in
    a later test - after the sandbox below had restored the real %APPDATA% -
    writing invented reading progress into the user's actual progress.json. The
    first window to leak was one with a 160 ms opacity fade still running.
    """
    window._quit_on_close = False
    window._geom_timer.stop()
    window._progress_timer.stop()
    window._snap_timer.stop()
    window._leave_hide_timer.stop()
    window._opacity_anim.stop()
    window.autoscroller.stop()
    window.close()
    window.deleteLater()


class WindowTestCase(unittest.TestCase):
    """Sandboxes %APPDATA% so tests never touch the real config, and gives every test
    a fresh window."""

    def setUp(self) -> None:
        self._appdata = os.environ.get("APPDATA")
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-ui-"))
        os.environ["APPDATA"] = str(self.tmp)

        self.settings = Settings(wheel_animation_ms=0)
        # Loaded once and shared, exactly as __main__ does.
        self.progress = ProgressStore.load()
        self.library = Library.load()
        self.windows: list[MiniWindow] = []
        self.book_path = make_book(self.tmp)

    def tearDown(self) -> None:
        for window in self.windows:
            _dispose_window(window)
        _app.processEvents()
        # Force the queued DeferredDelete events to land NOW. Plain processEvents()
        # does not guarantee this: Qt 5 requeues a DeferredDelete while the event
        # dispatcher still has pending native events, so a window that was animating
        # opacity when the test ended survived deleteLater() forever - see
        # _dispose_window. A zombie window keeps receiving events in later modules,
        # its 900 ms progress saver restarts and then fires *after* %APPDATA% was put
        # back, writing test data into the user's real progress.json. Under a file
        # sandbox the denied temp-file write additionally spins CPython's mkstemp
        # retry loop (TMP_MAX attempts), which looks exactly like a hung test run.
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        _app.processEvents()
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata

    def make_window(self, path: Path | None = None, **kw) -> MiniWindow:
        """A window over the process-wide store, mirroring how the app builds one.

        The store is deliberately shared: MiniWindow saves the whole progress file, so
        giving each test window its own empty store would let the second window's save
        wipe the first window's entry - a test artefact, not app behaviour.
        """
        window = MiniWindow(self.settings, self.progress, self.library, **kw)
        self.windows.append(window)
        window._quit_on_close = False
        window.resize(480, 360)
        window.show()
        _app.processEvents()
        if path is not None:
            self.assertTrue(window.load_path(path))
            _app.processEvents()
        return window


class TestTeardownHygiene(WindowTestCase):
    """The sandbox fixture must leave NO window behind, no matter what was running."""

    def _assert_gone_after_dispose(self, window: MiniWindow) -> None:
        _dispose_window(window)
        _app.processEvents()
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        _app.processEvents()
        # The window is already disposed; the class tearDown must not touch it again.
        self.windows.clear()
        self.assertNotIn(window, _app.topLevelWidgets())

    def test_a_window_mid_opacity_fade_is_destroyed(self) -> None:
        """A running QPropertyAnimation used to pin the widget alive: Qt requeues
        DeferredDelete while its animation timer keeps native events pending, so the
        hidden window leaked into every later module and its debounced progress save
        fired there with the real %APPDATA% restored."""
        from minireader import qt as qt_compat

        window = self.make_window(path=self.book_path)
        window.apply_opacity(40)
        self.assertEqual(
            int(window._opacity_anim.state()),
            int(qt_compat.QtCore.QAbstractAnimation.Running),
            "test premise: the 160 ms fade is actually in flight",
        )
        self._assert_gone_after_dispose(window)

    def test_a_window_with_autoscroll_running_is_destroyed(self) -> None:
        """The 40 ms auto-scroll timer is the other native-event source that must be
        stopped before deleteLater can land."""
        window = self.make_window(path=self.book_path)
        window.toggle_autoscroll()
        self.assertTrue(window.autoscroller.is_running())
        self._assert_gone_after_dispose(window)


class TestWindowChrome(WindowTestCase):
    def test_window_is_frameless_and_on_top(self) -> None:
        window = self.make_window()
        flags = window.windowFlags()
        self.assertTrue(flags & Qt.FramelessWindowHint, "no OS title bar")
        self.assertTrue(flags & Qt.WindowStaysOnTopHint, "always on top")

    def test_window_stays_out_of_the_taskbar_and_alt_tab(self) -> None:
        """Qt.Tool is what removes the taskbar button and the Alt+Tab entry - the whole
        point of a 摸鱼 window. It is set once at construction and never toggled."""
        window = self.make_window()
        self.assertTrue(window.windowFlags() & Qt.Tool)

    def test_flags_are_not_toggled_when_hiding(self) -> None:
        """setWindowFlags() recreates the native window, which would orphan a
        registered global hotkey. Hiding must not touch the flags."""
        window = self.make_window(path=self.book_path)
        before = window.windowFlags()
        window._boss_available = True
        window.toggle_boss()
        window.toggle_boss()
        self.assertEqual(window.windowFlags(), before)

    def test_status_bar_reports_progress(self) -> None:
        window = self.make_window(path=self.book_path)
        # 章节号固定在底栏中部的 chapter_label 上，书名/编码/体积仍在 status_label。
        # 阶段 2 后 status_label 是 ElidedLabel：完整文本要从 full_text() 读，text()
        # 在窄窗口里是省略后的版本。
        self.assertIn("1/3", window.chapter_label.text())
        text = window.status_label.full_text()
        self.assertIn("第1章 标题1", text, text)
        self.assertIn("UTF-8", text, text)

    def test_the_title_bar_names_the_book(self) -> None:
        window = self.make_window(path=self.book_path)
        self.assertIn("book", window.title_label.text().lower())

    def test_a_window_with_no_book_says_so(self) -> None:
        window = self.make_window()
        self.assertIn("还没有打开文件", window.status_label.text())
        self.assertIn("打开", window.reader.toPlainText(), "the placeholder explains how to start")

    def test_the_boss_hint_is_shown_only_when_the_hotkey_works(self) -> None:
        window = self.make_window()
        window.set_boss_hint(False, self.settings.boss_key)
        self.assertFalse(window._boss_available)
        window.set_boss_hint(True, self.settings.boss_key)
        self.assertTrue(window._boss_available)


class TestOpeningFiles(WindowTestCase):
    def test_open_txt_splits_chapters(self) -> None:
        window = self.make_window()
        self.assertTrue(window.load_path(self.book_path))
        self.assertEqual(window.book.chapter_count, 3)
        self.assertEqual(window.chapter_index, 0)

    def test_the_reader_shows_the_first_chapter(self) -> None:
        window = self.make_window(path=self.book_path)
        self.assertIn("正文内容", window.reader.toPlainText())

    def test_opening_detects_the_encoding(self) -> None:
        path = self.tmp / "gbk.txt"
        path.write_bytes(("第一章 甲\n" + BODY + "\n第二章 乙\n" + BODY).encode("gb18030"))
        window = self.make_window()
        self.assertTrue(window.load_path(path))
        self.assertIn(window.book.encoding, ("gb18030", "gbk"))
        # status_label 阶段 2 起是窄底栏的省略标签，完整文本走 full_text()。
        self.assertIn("甲", window.status_label.full_text())

    def test_a_missing_file_is_reported_and_changes_nothing(self) -> None:
        window = self.make_window(path=self.book_path)
        self.assertFalse(window.load_path(self.tmp / "ghost.txt"))
        self.assertEqual(window.book.chapter_count, 3, "the open book is untouched")
        self.assertIn("打不开", window.warn_label.text())
        self.assertIn("1/3", window.chapter_label.text())

    def test_a_binary_file_does_not_crash(self) -> None:
        path = self.tmp / "junk.bin"
        path.write_bytes(bytes(range(256)) * 200)
        window = self.make_window()
        window.load_path(path)  # either outcome, but no exception

    def test_drag_and_drop_accepts_txt_and_rejects_other_types(self) -> None:
        window = self.make_window()
        txt = QMimeData()
        txt.setUrls([QUrl.fromLocalFile(str(self.book_path))])
        self.assertTrue(same_path(window._first_txt(txt), self.book_path))

        other = QMimeData()
        other.setUrls([QUrl.fromLocalFile(str(self.tmp / "a.pdf"))])
        self.assertIsNone(window._first_txt(other))

        nothing = QMimeData()
        nothing.setText("hello")
        self.assertIsNone(window._first_txt(nothing))

    def test_drag_and_drop_is_case_insensitive_about_the_extension(self) -> None:
        upper = self.tmp / "BOOK.TXT"
        upper.write_bytes(self.book_path.read_bytes())
        window = self.make_window()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(upper))])
        self.assertTrue(same_path(window._first_txt(mime), upper))

    def test_drag_and_drop_picks_the_first_txt_among_several(self) -> None:
        window = self.make_window()
        mime = QMimeData()
        mime.setUrls(
            [
                QUrl.fromLocalFile(str(self.tmp / "a.pdf")),
                QUrl.fromLocalFile(str(self.book_path)),
                QUrl.fromLocalFile(str(self.tmp / "book.txt")),
            ]
        )
        self.assertTrue(same_path(window._first_txt(mime), self.book_path))


class TestScrolling(WindowTestCase):
    def test_the_document_actually_scrolls(self) -> None:
        window = self.make_window(path=self.book_path)
        self.assertGreater(window.reader.max_offset(), 0, "text must overflow the window")

    def test_one_wheel_notch_moves_about_three_lines(self) -> None:
        window = self.make_window(path=self.book_path)
        reader = window.reader
        expected = reader._line_px() * 3
        before = reader.current_offset()
        wheel(reader, 1)
        _app.processEvents()
        moved = reader.current_offset() - before
        self.assertAlmostEqual(moved, expected, delta=max(4.0, expected * 0.15))

    def test_scrolling_down_then_up_returns(self) -> None:
        window = self.make_window(path=self.book_path)
        reader = window.reader
        start = reader.current_offset()
        wheel(reader, 3)
        _app.processEvents()
        self.assertGreater(reader.current_offset(), start)
        wheel(reader, -3)
        _app.processEvents()
        self.assertEqual(reader.current_offset(), start)

    def test_scroll_is_clamped_to_the_document(self) -> None:
        window = self.make_window(path=self.book_path)
        reader = window.reader
        for _ in range(200):
            wheel(reader, 10)
            _app.processEvents()
        self.assertLessEqual(reader.current_offset(), reader.max_offset())
        self.assertGreaterEqual(reader.current_offset(), 0)

    def test_a_horizontal_wheel_event_is_ignored(self) -> None:
        window = self.make_window(path=self.book_path)
        reader = window.reader
        before = reader.current_offset()
        pos = QPointF(10, 10)
        event = QWheelEvent(
            pos, pos, QPoint(0, 0), QPoint(120, 0), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False
        )
        reader.wheelEvent(event)
        _app.processEvents()
        self.assertEqual(reader.current_offset(), before)


class TestChapterFlipping(WindowTestCase):
    def _to_bottom(self, window: MiniWindow) -> None:
        reader = window.reader
        reader.verticalScrollBar().setValue(reader.max_offset())
        _app.processEvents()

    def test_scrolling_past_the_end_goes_to_the_next_chapter(self) -> None:
        window = self.make_window(path=self.book_path)
        self._to_bottom(window)
        wheel(window.reader, 1)
        _app.processEvents()
        self.assertEqual(window.chapter_index, 1)
        self.assertIn("2/3", window.chapter_label.text())

    def test_scrolling_up_at_the_top_goes_to_the_previous_chapter(self) -> None:
        window = self.make_window(path=self.book_path)
        window.goto_chapter(2)
        _app.processEvents()
        reader = window.reader
        reader.verticalScrollBar().setValue(0)
        _app.processEvents()
        wheel(reader, -1)
        _app.processEvents()
        self.assertEqual(window.chapter_index, 1)

    def test_the_next_chapter_starts_at_the_top(self) -> None:
        window = self.make_window(path=self.book_path)
        self._to_bottom(window)
        wheel(window.reader, 1)
        _app.processEvents()
        self.assertEqual(window.reader.current_offset(), 0)

    def test_the_previous_chapter_starts_at_the_bottom(self) -> None:
        """Scrolling up into a chapter should land at its end, or the reader is bounced
        straight back down again."""
        window = self.make_window(path=self.book_path)
        window.goto_chapter(1)
        _app.processEvents()
        reader = window.reader
        reader.verticalScrollBar().setValue(0)
        _app.processEvents()
        wheel(reader, -1)
        _app.processEvents()
        self.assertEqual(reader.current_offset(), reader.max_offset())

    def test_flipping_past_the_last_chapter_is_refused(self) -> None:
        window = self.make_window(path=self.book_path)
        window.goto_chapter(2)
        self._to_bottom(window)
        wheel(window.reader, 1)
        _app.processEvents()
        self.assertEqual(window.chapter_index, 2, "no wrapping around")

    def test_flipping_before_the_first_chapter_is_refused(self) -> None:
        window = self.make_window(path=self.book_path)
        reader = window.reader
        reader.verticalScrollBar().setValue(0)
        _app.processEvents()
        wheel(reader, -1)
        _app.processEvents()
        self.assertEqual(window.chapter_index, 0)

    def test_goto_chapter_cancels_the_wheel_animation(self) -> None:
        """A running scroll animation would drag the new chapter back to the old
        offset the moment it ticks."""
        self.settings.wheel_animation_ms = 300
        window = self.make_window(path=self.book_path)
        reader = window.reader
        wheel(reader, 2)
        self.assertTrue(reader.animating, "the animation should be running")
        window.goto_chapter(1)
        self.assertFalse(reader.animating, "goto_chapter must stop it")

    def test_goto_chapter_clamps_out_of_range_requests(self) -> None:
        window = self.make_window(path=self.book_path)
        window.goto_chapter(99)
        self.assertEqual(window.chapter_index, 2)
        window.goto_chapter(-5)
        self.assertEqual(window.chapter_index, 0)

    def test_chapter_content_changes_when_flipping(self) -> None:
        window = self.make_window(path=self.book_path)
        first = window.reader.toPlainText()
        window.goto_chapter(1)
        _app.processEvents()
        self.assertNotEqual(window.reader.toPlainText(), first)


class TestBossKey(WindowTestCase):
    def test_hiding_is_refused_with_neither_a_hotkey_nor_a_tray(self) -> None:
        """Nothing could bring the window back: no global hotkey and no tray icon. The
        action must be refused, visibly, rather than obeyed."""
        window = self.make_window(path=self.book_path)
        self.assertIsNone(window.tray)
        window.set_boss_hint(False, self.settings.boss_key)
        window.toggle_boss()
        _app.processEvents()
        self.assertTrue(window.isVisible(), "must NOT hide")
        self.assertFalse(window.boss_hidden)
        self.assertIn("隐藏", window.warn_label.text())
        self.assertIn("不可用", window.hint_label.text())

    def test_hiding_is_allowed_when_only_the_tray_can_bring_it_back(self) -> None:
        """The tray is a visible, clickable way home, so it unlocks hiding on its own."""
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        window.set_boss_hint(False, self.settings.boss_key)
        self.assertTrue(window.can_hide())
        window.toggle_boss()
        _app.processEvents()
        self.assertFalse(window.isVisible(), "hidden")
        self.assertTrue(window.boss_hidden)
        self.assertTrue(tray.hidden_state)
        self.assertIn("托盘", window.hint_label.text())

    def test_hide_and_show_round_trip(self) -> None:
        window = self.make_window(path=self.book_path)
        window.set_boss_hint(True, self.settings.boss_key)
        self.assertTrue(window.isVisible())
        window.toggle_boss()
        _app.processEvents()
        self.assertFalse(window.isVisible(), "hidden")
        self.assertTrue(window.boss_hidden)
        window.toggle_boss()
        _app.processEvents()
        self.assertTrue(window.isVisible(), "back")
        self.assertFalse(window.boss_hidden)
        self.assertIn("1/3", window.chapter_label.text())

    def test_hiding_does_not_lose_the_reading_position(self) -> None:
        window = self.make_window(path=self.book_path)
        window.set_boss_hint(True, self.settings.boss_key)
        window.goto_chapter(1)
        _app.processEvents()
        reader = window.reader
        reader.verticalScrollBar().setValue(400)
        _app.processEvents()
        offset, chapter = reader.current_offset(), window.chapter_index
        window.toggle_boss()
        window.toggle_boss()
        _app.processEvents()
        self.assertEqual(window.chapter_index, chapter)
        self.assertEqual(window.reader.current_offset(), offset)

    def test_the_hidden_state_is_never_written_to_disk(self) -> None:
        """Still true with a tray icon: if the tray is switched off in settings and the
        hotkey then fails to register, a window that starts hidden is unrecoverable."""
        window = self.make_window(path=self.book_path)
        window.attach_tray(FakeTray())
        window.set_boss_hint(True, self.settings.boss_key)
        window.toggle_boss()
        _app.processEvents()
        window.save_geometry()
        window.persist_progress()
        for name in ("settings.json", "progress.json", "library.json"):
            path = self.tmp / "MiniReader" / name
            if path.exists():
                blob = path.read_text(encoding="utf-8")
                self.assertNotIn("hidden", blob, f"{name} must not record visibility")
                self.assertNotIn("visible", blob, f"{name} must not record visibility")

    def test_the_appearance_does_persist_even_though_visibility_does_not(self) -> None:
        """The distinction the settings file makes: opacity is a preference, being
        hidden right now is session state."""
        window = self.make_window(path=self.book_path)
        window.set_opacity_percent(65)
        self.assertTrue(window.settings.opacity == 65)
        window.persist_progress()
        blob = (self.tmp / "MiniReader" / "settings.json").read_text(encoding="utf-8")
        self.assertIn("opacity", blob)
        self.assertNotIn("hidden", blob)


class TestPersistence(WindowTestCase):
    def test_geometry_survives_a_restart(self) -> None:
        window = self.make_window(path=self.book_path)
        window.setGeometry(220, 140, 500, 380)
        _app.processEvents()
        window.save_geometry()

        raw = json.loads((self.tmp / "MiniReader" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["geometry"], {"x": 220, "y": 140, "w": 500, "h": 380})

        restored = Settings.load()
        self.assertEqual(restored.geometry, {"x": 220, "y": 140, "w": 500, "h": 380})

    def test_reading_progress_survives_a_restart(self) -> None:
        window = self.make_window(path=self.book_path)
        window.goto_chapter(2)
        _app.processEvents()
        window.reader.verticalScrollBar().setValue(250)
        _app.processEvents()
        window.persist_progress()

        store = ProgressStore.load()
        saved = store.get(book_id(self.book_path))
        self.assertEqual(saved.chapter, 2)
        self.assertGreater(saved.offset, 0)

        # A new window with the store loaded from disk resumes where the last one was -
        # this is the whole "记住阅读进度" promise.
        reopened = MiniWindow(self.settings, ProgressStore.load(), Library.load())
        self.windows.append(reopened)
        reopened._quit_on_close = False
        reopened.show()
        self.assertTrue(reopened.load_path(self.book_path))
        _app.processEvents()
        self.assertEqual(reopened.chapter_index, 2)
        self.assertAlmostEqual(reopened.reader.current_offset(), saved.offset, delta=2)

    def test_an_explicit_chapter_overrides_the_remembered_one(self) -> None:
        window = self.make_window(path=self.book_path)
        window.goto_chapter(2)
        window.persist_progress()
        window.load_path(self.book_path, chapter=0, offset=0)
        _app.processEvents()
        self.assertEqual(window.chapter_index, 0)

    def test_the_last_book_is_remembered(self) -> None:
        """Regression: this used to stay in memory only, so the next launch did not
        offer the book again unless the window had also been moved or resized."""
        window = self.make_window(path=self.book_path)
        window.persist_progress()
        self.assertEqual(Settings.load().last_book, str(self.book_path))

    def test_progress_is_keyed_per_book(self) -> None:
        other = make_book(self.tmp, "other.txt")
        a = self.make_window(path=self.book_path)
        a.goto_chapter(2)
        a.persist_progress()
        b = self.make_window(path=other)
        b.persist_progress()

        store = ProgressStore.load()
        self.assertEqual(store.get(book_id(self.book_path)).chapter, 2)
        self.assertEqual(store.get(book_id(other)).chapter, 0)

    def test_a_corrupt_config_does_not_stop_the_window_from_opening(self) -> None:
        config = self.tmp / "MiniReader"
        config.mkdir(parents=True, exist_ok=True)
        (config / "settings.json").write_text("{ broken", encoding="utf-8")
        (config / "progress.json").write_text("]]]", encoding="utf-8")
        (config / "library.json").write_text("nope", encoding="utf-8")
        window = self.make_window(path=self.book_path)
        self.assertEqual(window.chapter_index, 0)

    def test_persist_progress_with_no_book_is_harmless(self) -> None:
        window = self.make_window()
        window.persist_progress()
        self.assertEqual(ProgressStore.load().by_id, {})

    def test_last_book_is_not_rewritten_when_it_has_not_changed(self) -> None:
        """One settings write per book opened, not one per scroll."""
        window = self.make_window(path=self.book_path)
        window.persist_progress()
        before = self.settings.last_book
        window.persist_progress()
        self.assertEqual(self.settings.last_book, before)

    def test_saving_is_debounced_not_per_pixel(self) -> None:
        """Every scroll tick would otherwise hit the disk."""
        window = self.make_window(path=self.book_path)
        window._on_scrolled()
        self.assertTrue(window._progress_timer.isActive(), "a scroll schedules a save")
        window.persist_progress()
        self.assertFalse(window._progress_timer.isActive(), "an explicit save supersedes it")


class TestEdgesAt(WindowTestCase):
    """Pure geometry behind the resize border; no events needed."""

    def test_the_corners_are_two_edges_at_once(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 400, 300)
        edges = window.edges_at(QPoint(1, 1))
        self.assertTrue(edges & int(Qt.LeftEdge))
        self.assertTrue(edges & int(Qt.TopEdge))

    def test_each_side_is_detected(self) -> None:
        # 400×400：阶段 2 起窗口最小高 360，300 会被 Qt 钳到 360，底边断言就得跟着换。
        window = self.make_window()
        window.setGeometry(0, 0, 400, 400)
        self.assertTrue(window.edges_at(QPoint(1, 200)) & int(Qt.LeftEdge))
        self.assertTrue(window.edges_at(QPoint(399, 200)) & int(Qt.RightEdge))
        self.assertTrue(window.edges_at(QPoint(200, 1)) & int(Qt.TopEdge))
        self.assertTrue(window.edges_at(QPoint(200, 399)) & int(Qt.BottomEdge))

    def test_the_middle_is_not_the_border(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 400, 300)
        self.assertEqual(window.edges_at(QPoint(200, 150)), 0)

    def test_the_resize_band_covers_the_visible_frame(self) -> None:
        """The 4 px frame is the only thing between the user and a resize, so a press
        on it must count."""
        window = self.make_window()
        window.setGeometry(0, 0, 400, 300)
        self.assertTrue(window.edges_at(QPoint(2, 150)) & int(Qt.LeftEdge))


def _wmouse(kind, local, glob, button, buttons) -> QMouseEvent:
    """A real QMouseEvent (so it survives the super().eventFilter type check) with the
    QPointF local/global constructor that is identical on PyQt5 and PySide6."""
    return QMouseEvent(
        kind, QPointF(local.x(), local.y()), QPointF(glob.x(), glob.y()),
        button, buttons, Qt.NoModifier,
    )


class TestEdgeResize(WindowTestCase):
    """Drag the four edges and four corners to size the borderless window.

    Covers the six promised guarantees: 8-direction hot zones, the 320x360 floor,
    geometry persistence, no drag/resize collision, resize still works after the
    topmost hide/rebuild, and resizing does not crash while the window is sitting at
    the mouse-leave hide opacity. Child-surface routing widens the grip past the 4 px
    transparent frame onto the title bar / reader viewport / status bar.
    """

    def _gesture(self, window, target, win_start, dx, dy):
        """Press at a window-space point on `target`, move by (dx, dy), release. All
        coordinates are converted through mapToGlobal like the platform plugin does."""
        g0 = window.mapToGlobal(win_start)
        press = _wmouse(QEvent.MouseButtonPress, target.mapFromGlobal(g0), g0,
                        Qt.LeftButton, Qt.LeftButton)
        _app.sendEvent(target, press)
        g1 = QPoint(g0.x() + dx, g0.y() + dy)
        _app.sendEvent(
            target,
            _wmouse(QEvent.MouseMove, target.mapFromGlobal(g1), g1,
                    Qt.NoButton, Qt.LeftButton),
        )
        _app.sendEvent(
            target,
            _wmouse(QEvent.MouseButtonRelease, target.mapFromGlobal(g1), g1,
                    Qt.LeftButton, Qt.NoButton),
        )
        _app.processEvents()
        return press

    # -- pure geometry -----------------------------------------------------

    def test_resized_geometry_anchors_every_edge_and_corner(self) -> None:
        window = self.make_window()
        window.setGeometry(100, 80, 480, 400)
        start = window.geometry()
        cases = {
            int(Qt.RightEdge): (50, 0, (100, 80, 530, 400)),
            int(Qt.LeftEdge): (50, 0, (150, 80, 430, 400)),
            int(Qt.BottomEdge): (0, 40, (100, 80, 480, 440)),
            int(Qt.TopEdge): (0, 40, (100, 120, 480, 360)),
            int(Qt.TopEdge) | int(Qt.LeftEdge): (50, 40, (150, 120, 430, 360)),
            int(Qt.BottomEdge) | int(Qt.RightEdge): (50, 40, (100, 80, 530, 440)),
            int(Qt.TopEdge) | int(Qt.RightEdge): (50, 40, (100, 120, 530, 360)),
            int(Qt.BottomEdge) | int(Qt.LeftEdge): (50, 40, (150, 80, 430, 440)),
        }
        for edges, (dx, dy, expected) in cases.items():
            with self.subTest(edges=edges):
                self.assertEqual(
                    window.resized_geometry(start, edges, dx, dy).getRect(), expected
                )

    def test_cursor_shape_matches_the_edge_mask(self) -> None:
        shape = MiniWindow._cursor_for_edges
        self.assertEqual(shape(0), None)
        self.assertEqual(shape(int(Qt.LeftEdge) | int(Qt.TopEdge)), Qt.SizeFDiagCursor)
        self.assertEqual(shape(int(Qt.RightEdge) | int(Qt.BottomEdge)), Qt.SizeFDiagCursor)
        self.assertEqual(shape(int(Qt.RightEdge) | int(Qt.TopEdge)), Qt.SizeBDiagCursor)
        self.assertEqual(shape(int(Qt.LeftEdge) | int(Qt.BottomEdge)), Qt.SizeBDiagCursor)
        self.assertEqual(shape(int(Qt.LeftEdge)), Qt.SizeHorCursor)
        self.assertEqual(shape(int(Qt.BottomEdge)), Qt.SizeVerCursor)

    # -- minimum size ------------------------------------------------------

    def test_resize_cannot_shrink_below_the_minimum_size(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 480, 400)
        self.assertEqual((window.minimumWidth(), window.minimumHeight()), (320, 360))
        # Pull each edge way past the floor through the real handlers.
        self._gesture(window, window, QPoint(window.width() - 2, 200), -1000, 0)
        self.assertEqual(window.width(), 320, "right edge floors at minimum width")
        self.assertEqual(window.x(), 0)

        window.setGeometry(0, 0, 480, 400)
        self._gesture(window, window, QPoint(2, 200), 1000, 0)
        self.assertEqual(window.width(), 320, "left edge floors, right side stays put")
        self.assertEqual(window.x(), 480 - 320)

        window.setGeometry(0, 0, 480, 400)
        self._gesture(window, window, QPoint(200, window.height() - 2), 0, -1000)
        self.assertEqual(window.height(), 360, "bottom edge floors at minimum height")

        window.setGeometry(0, 0, 480, 400)
        self._gesture(window, window, QPoint(200, 2), 0, 1000)
        self.assertEqual(window.height(), 360, "top edge floors, bottom stays put")
        self.assertEqual(window.y(), 400 - 360)

    # -- persistence --------------------------------------------------------

    def test_a_finished_resize_is_persisted(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 480, 400)
        path = self.tmp / "MiniReader" / "settings.json"
        self._gesture(window, window, QPoint(window.width() - 2, 200), 60, 0)
        self.assertEqual(window.width(), 540)
        # Sizing only arms the debounced saver; nothing is on disk yet.
        self.assertTrue(window._geom_timer.isActive())
        self.assertFalse(path.exists(), "a resize only debounces; no settings write yet")
        window._geom_timer.timeout.emit()          # the 500 ms saver firing
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["geometry"], {"x": 0, "y": 0, "w": 540, "h": 400})

    # -- drag vs resize do not collide ------------------------------------

    def test_an_edge_press_only_sizes_and_a_body_press_only_moves(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 480, 400)
        # Edge -> resize bookkeeping, never a drag origin.
        press = self._gesture(window, window, QPoint(window.width() - 2, 200), 40, 0)
        self.assertTrue(press.isAccepted())
        self.assertEqual(window.width(), 520)
        self.assertIsNone(window._drag_origin)

        window.setGeometry(0, 0, 480, 400)
        g = window.mapToGlobal(QPoint(200, 200))
        body_press = _wmouse(QEvent.MouseButtonPress, QPoint(200, 200), g,
                             Qt.LeftButton, Qt.LeftButton)
        _app.sendEvent(window, body_press)
        self.assertEqual(window._resize_edges, 0, "an interior press is not a resize")
        self.assertIsNotNone(window._drag_origin, "an interior press is a move")
        _app.sendEvent(
            window,
            _wmouse(QEvent.MouseButtonRelease, QPoint(200, 200), g,
                    Qt.LeftButton, Qt.NoButton),
        )

    # -- child surfaces extend the grip past the 4px frame ----------------

    def test_child_surfaces_route_the_edge_ring_to_resize(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 480, 400)

        # Top ring over the title bar (which otherwise starts a drag).
        x0, y0, w0, h0 = window.x(), window.y(), window.width(), window.height()
        self._gesture(window, window.title_bar, QPoint(240, 8), 0, -30)
        self.assertEqual(window._resize_edges, 0, "release clears the resize state")
        self.assertEqual(window.y(), y0 - 30)
        self.assertEqual(window.height(), h0 + 30)

        # Left ring over the reader viewport.
        window.setGeometry(0, 0, 480, 400)
        x0, w0 = window.x(), window.width()
        self._gesture(window, window.reader.viewport(), QPoint(8, 200), -30, 0)
        self.assertEqual(window.x(), x0 - 30)
        self.assertEqual(window.width(), w0 + 30)

        # Bottom ring over the status bar.
        window.setGeometry(0, 0, 480, 400)
        h0 = window.height()
        self._gesture(window, window.status_bar, QPoint(240, window.height() - 8), 0, 30)
        self.assertEqual(window.height(), h0 + 30)

    def test_interior_events_on_children_are_not_swallowed(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 480, 400)
        # A press in the middle of the text must not start a resize.
        g = window.mapToGlobal(QPoint(200, 200))
        target = window.reader.viewport()
        local = target.mapFromGlobal(g)
        _app.sendEvent(target, _wmouse(QEvent.MouseButtonPress, local, g,
                                       Qt.LeftButton, Qt.LeftButton))
        self.assertEqual(window._resize_edges, 0)
        _app.sendEvent(target, _wmouse(QEvent.MouseButtonRelease, local, g,
                                       Qt.LeftButton, Qt.NoButton))
        # A press in the middle of the title bar is still a drag, not a resize.
        g = window.mapToGlobal(QPoint(240, 25))
        local = window.title_bar.mapFromGlobal(g)
        _app.sendEvent(window.title_bar,
                       _wmouse(QEvent.MouseButtonPress, local, g, Qt.LeftButton, Qt.LeftButton))
        self.assertEqual(window._resize_edges, 0, "title bar centre stays a drag handle")
        self.assertIsNotNone(window._drag_origin)
        _app.sendEvent(window.title_bar,
                       _wmouse(QEvent.MouseButtonRelease, local, g, Qt.LeftButton, Qt.NoButton))

    # -- survives the topmost native rebuild ------------------------------

    def test_resize_still_works_after_toggling_always_on_top(self) -> None:
        window = self.make_window()
        window.setGeometry(120, 90, 480, 400)
        before = (120, 90, 480, 400)
        window.toggle_on_top()          # hide -> setWindowFlags (new HWND) -> show
        _app.processEvents()
        self.assertEqual(window.geometry().getRect(), before)
        self._gesture(window, window, QPoint(window.width() - 2, 200), 40, 0)
        self.assertEqual(window.width(), 520, "resize survives the native flag rebuild")

    # -- does not crash at the mouse-leave hide floor ---------------------

    def test_resize_works_while_the_window_is_at_leave_hide_opacity(self) -> None:
        window = self.make_window()
        window.setGeometry(0, 0, 480, 400)
        window.settings.mouse_leave_hide = True
        window._apply_leave_hide()
        assert_at_leave_hide_floor(self, window)
        width_before = window.width()
        self._gesture(window, window, QPoint(window.width() - 2, 200), 30, 0)
        self.assertEqual(window.width(), width_before + 30)
        assert_at_leave_hide_floor(self, window, "缩放手势不应改变移出隐身的不透明度下限")


class TestHotkeyParsing(unittest.TestCase):
    """Parsing is pure, so it is tested without a window - the registration itself is
    covered by the real-platform smoke run."""

    def test_common_spellings_agree(self) -> None:
        expected = parse_hotkey("Ctrl+Shift+`")
        for spec in ("ctrl + shift + `", "CTRL+SHIFT+`", " ctrl+shift+` "):
            self.assertEqual(parse_hotkey(spec), expected, spec)

    def test_modifiers_are_distinct(self) -> None:
        ctrl, _ = parse_hotkey("Ctrl+B")
        alt, _ = parse_hotkey("Alt+B")
        shift, _ = parse_hotkey("Shift+B")
        self.assertEqual(len({ctrl, alt, shift}), 3)

    def test_an_unparseable_spec_is_rejected_rather_than_guessed(self) -> None:
        for spec in ("", "   ", "B", "Ctrl+", "Ctrl+Nonsense", "Hyper+B", "Ctrl+Shift+", "Ctrl+B+C"):
            with self.subTest(spec=spec):
                with self.assertRaises(HotkeyError):
                    parse_hotkey(spec)

    def test_round_trip_through_format(self) -> None:
        self.assertEqual(format_hotkey("ctrl+shift+`"), "Ctrl+Shift+`")

    def test_a_bare_letter_is_refused(self) -> None:
        """A global plain-letter hotkey would swallow that key in every application."""
        with self.assertRaises(HotkeyError):
            parse_hotkey("B")

    def test_bare_function_keys_are_allowed(self) -> None:
        """F1-F24 have no other use in a text box, so they are safe unmodified."""
        mods, vk = parse_hotkey("F9")
        self.assertEqual(mods, 0)
        self.assertGreater(vk, 0)

    def test_an_unknown_spec_formats_as_itself(self) -> None:
        self.assertEqual(format_hotkey("nonsense"), "nonsense")


class TestNullHotkeys(WindowTestCase):
    def test_registering_always_fails_gracefully(self) -> None:
        """The null manager stands in when the global hotkey cannot exist at all; the
        window must be able to call it without special-casing."""
        manager = NullHotkeys()
        ok, message = manager.register("boss", "Ctrl+Shift+`", 0)
        self.assertFalse(ok)
        self.assertTrue(message, "a failure must come with something to show the user")
        manager.attach(_app)
        manager.detach(_app)
        manager.unregister_all()

    def test_make_manager_returns_something_usable_on_any_platform(self) -> None:
        manager = make_manager(lambda _name: None)
        self.assertTrue(hasattr(manager, "register"))
        self.assertTrue(hasattr(manager, "unregister_all"))
        manager.unregister_all()


class TestPlatformGuard(unittest.TestCase):
    def test_native_platform_is_reported_correctly(self) -> None:
        """Global hotkeys need a real window system; offscreen must say so instead of
        pretending the boss key works."""
        expected = _app.platformName() in ("windows", "xcb", "cocoa")
        self.assertEqual(native_platform_available(), expected)


class TestOpacity(WindowTestCase):
    """不透明度 and 失焦自动变淡."""

    def test_the_default_is_fully_opaque(self) -> None:
        """Opt-in on purpose: any opacity below 100% makes Windows composite the window
        in software, which turns off ClearType sub-pixel antialiasing and softens every
        glyph edge. A reader should not get that by accident."""
        window = self.make_window()
        self.assertEqual(self.settings.opacity, 100)
        self.assertFalse(self.settings.dim_on_blur)
        self.assertEqual(window.opacity_percent(), 100)

    def test_setting_opacity_reaches_the_window(self) -> None:
        window = self.make_window()
        window.settings.opacity = 70
        window.apply_opacity(70, animate=False)
        self.assertEqual(window.opacity_percent(), 70)
        self.assertAlmostEqual(window.windowOpacity(), 0.70, places=2)

    def test_opacity_is_clamped_to_a_usable_range(self) -> None:
        """Below ~30% a reading window is a rumour; the settings clamp floor is the same
        number the dialog slider uses."""
        window = self.make_window()
        window.settings.opacity = 5
        window.settings.clamp()
        self.assertEqual(window.settings.opacity, 30)
        window.settings.opacity = 500
        window.settings.clamp()
        self.assertEqual(window.settings.opacity, 100)

    def test_an_absurd_percentage_cannot_escape_the_opacity_range(self) -> None:
        window = self.make_window()
        window.apply_opacity(-50, animate=False)
        self.assertEqual(window.opacity_percent(), 0)
        self.assertEqual(window.windowOpacity(), 0.0)
        window.apply_opacity(9999, animate=False)
        self.assertEqual(window.windowOpacity(), 1.0)

    def test_dimming_on_blur_uses_the_configured_dim_level(self) -> None:
        window = self.make_window()
        window.settings.opacity = 90
        window.settings.dim_on_blur = True
        window.settings.dim_opacity = 40
        self.assertEqual(window.settings.target_opacity(active=True), 90)
        self.assertEqual(window.settings.target_opacity(active=False), 40)

    def test_the_dim_level_never_brightens_the_window(self) -> None:
        """A dim level above the normal opacity would make the window *more* opaque when
        it loses focus, which is the opposite of the feature."""
        settings = Settings(opacity=40, dim_on_blur=True, dim_opacity=90)
        self.assertEqual(settings.target_opacity(active=False), 40)

    def test_dimming_is_off_unless_asked_for(self) -> None:
        settings = Settings(opacity=80, dim_on_blur=False, dim_opacity=30)
        self.assertEqual(settings.target_opacity(active=False), 80)

    def test_refresh_opacity_is_a_no_op_while_hidden(self) -> None:
        """A hidden window has nothing to dim, and touching opacity while hidden is how
        you get a window that comes back at the wrong brightness."""
        window = self.make_window()
        window.settings.opacity = 75
        window.apply_opacity(75, animate=False)
        window.settings.dim_on_blur = True
        window.settings.dim_opacity = 40
        window.hide_to_boss()
        window.refresh_opacity()
        self.assertEqual(window.opacity_percent(), 75, "must not dim while hidden")

    def test_an_open_dialog_suppresses_the_dim(self) -> None:
        """While our own settings dialog is up the main window is inactive by
        definition; dimming it then makes the app look broken."""
        window = self.make_window()
        window.settings.opacity = 100
        window.settings.dim_on_blur = True
        window.settings.dim_opacity = 40
        window._dialog_open = True
        window.refresh_opacity()
        self.assertEqual(window.opacity_percent(), 100)
        window._dialog_open = False

    def test_the_activation_event_is_what_drives_the_dimming(self) -> None:
        """Focus events do not fire for an already-focused top-level window, so
        ActivationChange is the signal that actually arrives."""
        window = self.make_window()
        window.settings.dim_on_blur = True
        window.settings.dim_opacity = 45
        window.settings.opacity = 100
        window._dialog_open = True      # pretend we are active, whatever the QPA says
        window.refresh_opacity()
        self.assertEqual(window.opacity_percent(), 100)

    def test_opacity_survives_a_restart(self) -> None:
        window = self.make_window()
        window.set_opacity_percent(72)
        reloaded = Settings.load()
        self.assertEqual(reloaded.opacity, 72)

    def test_the_tray_opacity_preset_reaches_the_window(self) -> None:
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        tray.opacityRequested.emit(50)
        _app.processEvents()
        self.assertEqual(window.settings.opacity, 50)
        self.assertEqual(tray.opacity, 50, "the menu must show the new value as checked")
        self.assertEqual(window.opacity_percent(), 50)

    def test_an_out_of_range_tray_preset_is_clamped(self) -> None:
        window = self.make_window()
        window.attach_tray(FakeTray())
        window.set_opacity_percent(-10)
        self.assertEqual(window.settings.opacity, 30)


class TestDisguise(WindowTestCase):
    """窗口标题伪装: the OS title, the Win32 title and the tray tooltip must agree."""

    def test_the_default_title_is_the_application_name(self) -> None:
        window = self.make_window()
        self.assertEqual(window.window_title(), "MiniReader")
        self.assertEqual(window.windowTitle(), "MiniReader")

    def test_a_disguise_replaces_the_window_title(self) -> None:
        window = self.make_window()
        window.settings.disguise_title = "Microsoft Word - 季度报告.docx"
        window.apply_disguise()
        self.assertEqual(window.windowTitle(), "Microsoft Word - 季度报告.docx")

    def test_the_disguise_also_covers_the_tray_tooltip(self) -> None:
        """The tooltip is the only hoverable surface left once the taskbar button is
        gone, so a disguise that skips it gives the game away on hover."""
        window = self.make_window()
        tray = FakeTray()
        window.attach_tray(tray)
        window.settings.disguise_title = "记事本"
        window.apply_disguise()
        self.assertEqual(tray.title, "记事本")

    def test_a_disguise_later_reaches_an_already_attached_tray(self) -> None:
        window = self.make_window()
        tray = FakeTray()
        window.attach_tray(tray)
        window.settings.disguise_title = "报表.xlsx"
        window.apply_preferences()
        self.assertEqual(tray.title, "报表.xlsx")

    def test_an_empty_disguise_means_no_disguise(self) -> None:
        window = self.make_window()
        window.settings.disguise_title = ""
        window.apply_disguise()
        self.assertEqual(window.windowTitle(), "MiniReader")

    def test_a_disguise_is_not_allowed_to_be_a_paragraph(self) -> None:
        """It goes into a Win32 call and a tooltip; a 10 000-character title is not a
        disguise, it is a bug report."""
        settings = Settings(disguise_title="字" * 5000)
        settings.clamp()
        self.assertLessEqual(len(settings.disguise_title), 120)

    def test_the_in_window_label_still_shows_the_real_book(self) -> None:
        """The disguise is for the OS chrome. What you are reading should stay honest,
        otherwise you cannot tell two windows apart yourself."""
        window = self.make_window(path=self.book_path)
        window.settings.disguise_title = "系统日志"
        window.apply_disguise()
        self.assertIn("book", window.title_label.text())


class TestSnapping(WindowTestCase):
    """边缘吸附, driven through the window rather than through snap.py."""

    def place(self, window, x, y):
        window.move(x, y)
        _app.processEvents()

    def test_snapping_runs_after_the_move_settles(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = True
        window.settings.snap_distance = 14
        area = snapmod.active_work_area(window.x(), window.y(), window.width(), window.height(),
                                       [(0, 0, 1920, 1040)])
        self.assertIsNotNone(area)
        self.place(window, 6, 200)
        self.assertFalse(window._snapping)
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual(window.x(), 0, "pulled to the left edge")

    def test_a_resize_gesture_vetoes_the_snap(self) -> None:
        """Otherwise the snap fights the edge the user is dragging."""
        window = self.make_window()
        window.settings.snap_enabled = True
        window._gesture_active = True
        window._gesture_resized = True
        self.place(window, 6, 200)
        before = window.x()
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual(window.x(), before, "a resize must not be turned into a move")
        self.assertFalse(window._gesture_resized, "the veto is consumed, not sticky")

    def test_a_resize_outside_a_gesture_does_not_veto_the_next_drag(self) -> None:
        """Construction calls setGeometry(), which fires a resize with no gesture behind
        it. If that set the veto, the first drag after launch would never snap."""
        window = self.make_window()
        window.settings.snap_enabled = True
        window.resize(window.width() + 40, window.height())
        _app.processEvents()
        self.assertFalse(window._gesture_resized)

    def test_begin_drag_clears_the_resize_veto(self) -> None:
        """Each gesture decides for itself: a resize must not poison the next drag."""
        window = self.make_window()
        window._gesture_resized = True
        window.begin_drag(_fake_press(window))
        self.assertFalse(window._gesture_resized)
        self.assertTrue(window._gesture_active)

    def test_resizing_stops_a_pending_snap(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = True
        window._gesture_active = True
        window._snap_timer.start()
        self.assertTrue(window._snap_timer.isActive())
        window.resize(window.width() + 30, window.height())
        _app.processEvents()
        self.assertFalse(window._snap_timer.isActive())
        self.assertTrue(window._gesture_resized, "a resize inside a gesture is recorded")

    def test_nothing_moves_when_snapping_is_off(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = False
        self.place(window, 6, 200)
        before = (window.x(), window.y())
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual((window.x(), window.y()), before)

    def test_nothing_moves_while_a_manual_drag_is_still_in_progress(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = True
        self.place(window, 6, 200)
        window._drag_origin = QPoint(1, 1)
        before = window.x()
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual(window.x(), before)
        window._drag_origin = None

    def test_a_dragged_off_screen_window_is_pulled_back(self) -> None:
        """With the taskbar hidden there is no taskbar button to right-click either, so
        an unreachable window is unrecoverable. This runs even with snapping off - the
        clamp is a separate promise from the magnetic pull."""
        from minireader.qt import screen_rects

        window = self.make_window()
        window.settings.snap_enabled = False
        window.settings.keep_on_screen = True
        self.place(window, 20000, 20000)
        window._snap_timer.stop()
        window._maybe_snap()
        areas = screen_rects()
        self.assertTrue(areas, "the offscreen plugin still reports a screen")
        self.assertTrue(
            snapmod.reachable(
                (window.x(), window.y(), window.width(), window.height()), areas[0]
            ),
            f"window at {window.x()},{window.y()} must still be grabbable",
        )

    def test_the_clamp_can_be_turned_off(self) -> None:
        """The opt-out has to work, or a deliberate placement gets yanked back."""
        window = self.make_window()
        window.settings.snap_enabled = True
        window.settings.keep_on_screen = False
        self.place(window, 20000, 20000)
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual(window.x(), 20000, "an explicit opt-out must be respected")

    def test_the_clamp_does_not_need_snapping_to_be_enabled(self) -> None:
        """Recoverability and the magnetic pull are separate settings. Tying the clamp
        to `snap_enabled` meant that switching off snapping silently disabled the net
        that keeps a dragged-away window grabbable."""
        from minireader.qt import screen_rects

        window = self.make_window()
        window.settings.snap_enabled = False
        window.settings.keep_on_screen = True
        self.place(window, 30000, 30000)
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertNotEqual(window.x(), 30000, "the clamp must still run")
        self.assertTrue(
            snapmod.reachable(
                (window.x(), window.y(), window.width(), window.height()), screen_rects()[0]
            )
        )

    def test_a_window_inside_the_screen_is_left_alone(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = True
        window.settings.snap_distance = 14
        self.place(window, 700, 300)
        before = (window.x(), window.y())
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual((window.x(), window.y()), before)

    def test_moving_arms_the_snap_timer(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = True
        window._snap_timer.stop()
        window.move(window.x() + 3, window.y() + 3)
        _app.processEvents()
        self.assertTrue(window._snap_timer.isActive(), "the settle timer must be armed")

    def test_snapping_itself_does_not_re_arm_the_timer(self) -> None:
        """Or the window would keep re-snapping forever in a feedback loop."""
        window = self.make_window()
        window.settings.snap_enabled = True
        window._snapping = True
        window._snap_timer.stop()
        window.move(0, 0)
        _app.processEvents()
        self.assertFalse(window._snap_timer.isActive())
        window._snapping = False

    def test_the_snap_distance_setting_is_honoured(self) -> None:
        window = self.make_window()
        window.settings.snap_enabled = True
        window.settings.snap_distance = 0
        self.place(window, 7, 200)
        before = window.x()
        window._snap_timer.stop()
        window._maybe_snap()
        self.assertEqual(window.x(), before, "distance 0 means no pull")


class TestLeaveHide(WindowTestCase):
    """鼠标移出隐身：整窗压到 LEAVE_HIDE_OPACITY（肉眼全透但必须可命中），
    enterEvent / 托盘 / 关闭开关都能恢复用户原来的不透明度。"""

    def _armed_window(self, opacity: int | None = None) -> MiniWindow:
        window = self.make_window(path=self.book_path)
        window.settings.mouse_leave_hide = True
        if opacity is not None:
            window.settings.opacity = opacity
            window.apply_opacity(opacity, animate=False)
        return window

    def test_the_hide_floor_is_the_nonzero_constant(self) -> None:
        """隐身到点后 opacity 必须等于 LEAVE_HIDE_OPACITY 且严格大于 0。

        这是点击穿透 bug 的核心回归守卫：alpha=0 时 Windows 让 layered window 对鼠标
        穿透，移回指针收不到 enterEvent，窗口恢复不回来。
        """
        window = self._armed_window()
        window._apply_leave_hide()
        assert_at_leave_hide_floor(self, window)

    def test_the_floor_constant_is_visually_invisible(self) -> None:
        """常量自身也要守住：必须是正的、且小到肉眼等同全透。改成 0（穿透回归）或
        改成明显可见的值（隐身穿帮）都应让这条变红。"""
        self.assertGreater(LEAVE_HIDE_OPACITY, 0.0)
        self.assertLessEqual(LEAVE_HIDE_OPACITY, 0.02)

    def test_enter_restores_the_saved_opacity_after_a_hide(self) -> None:
        """移出→隐身→移回：enterEvent 必须把用户的不透明度原样还回来。

        offscreen 平台没有真实指针，这里直接投递 QEvent.Enter/Leave 验证事件处理；
        「0.01 时物理指针真的能命中窗口并触发 enterEvent」是真实桌面行为，需在真实
        桌面（--smoke / 物理移动鼠标）验证，offscreen 测不了命中测试。
        """
        window = self._armed_window(opacity=70)
        window.leaveEvent(QEvent(QEvent.Leave))
        window._apply_leave_hide()
        assert_at_leave_hide_floor(self, window)

        window.enterEvent(QEvent(QEvent.Enter))
        _app.processEvents()
        self.assertAlmostEqual(window.windowOpacity(), 0.7, places=2)
        self.assertIsNone(window._opacity_before_hide)

    def test_enter_within_the_delay_cancels_the_pending_hide(self) -> None:
        """100ms 防抖窗口内移回：计时器被撤掉，窗口根本不该降透明度。"""
        window = self._armed_window(opacity=80)
        window.leaveEvent(QEvent(QEvent.Leave))
        self.assertTrue(window._leave_hide_timer.isActive())
        window.enterEvent(QEvent(QEvent.Enter))
        _app.processEvents()
        self.assertFalse(window._leave_hide_timer.isActive())
        self.assertIsNone(window._opacity_before_hide)
        self.assertAlmostEqual(window.windowOpacity(), 0.8, places=2)

    def test_compat_mode_uses_the_same_nonzero_floor(self) -> None:
        """translucent_bg=False（无逐像素 alpha）下隐身同样只降到正下限，不能因为
        兼容模式就退回点击穿透的 0；COMPAT_MIN_OPACITY 只管普通滑块，不管隐身。"""
        self.settings.translucent_bg = False
        window = self.make_window(path=self.book_path)
        window.settings.mouse_leave_hide = True
        window._apply_leave_hide()
        assert_at_leave_hide_floor(self, window)

    def test_repeated_hide_does_not_save_the_floor_as_the_user_value(self) -> None:
        """定时器连续到点两次也只能捕获第一次的用户值；否则恢复时窗口只剩 1%。"""
        window = self._armed_window(opacity=60)
        window._apply_leave_hide()
        window._apply_leave_hide()
        self.assertAlmostEqual(window._opacity_before_hide, 0.6, places=2)
        window.enterEvent(QEvent(QEvent.Enter))
        self.assertAlmostEqual(window.windowOpacity(), 0.6, places=2)


class TestTrayWiring(WindowTestCase):
    """What the window promises a tray icon. The real QSystemTrayIcon is Qt's business;
    this is ours, and it is where the wiring bugs live."""

    def test_a_window_without_a_tray_still_works(self) -> None:
        """The offscreen plugin has no tray, and a machine with the tray disabled has
        none either. Nothing may depend on one existing."""
        window = self.make_window(path=self.book_path)
        self.assertIsNone(window.tray)
        window.apply_preferences()
        window.update_boss_capability()
        self.assertTrue(window.isVisible())

    def test_attaching_syncs_the_current_state(self) -> None:
        window = self.make_window()
        window.settings.opacity = 60
        window.settings.disguise_title = "伪装"
        window.hide_to_boss()
        tray = FakeTray()
        window.attach_tray(tray)
        self.assertEqual(tray.title, "伪装")
        self.assertEqual(tray.opacity, 60)
        self.assertTrue(tray.hidden_state, "the icon must not say 隐藏 while hidden")

    def test_the_show_and_hide_signals_drive_hide_and_present(self) -> None:
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        window.set_boss_hint(True, self.settings.boss_key)

        # 菜单的"隐藏窗口"：走老板键那套真隐藏。
        tray.hideRequested.emit()
        _app.processEvents()
        self.assertTrue(window.boss_hidden)
        self.assertTrue(tray.hidden_state)

        # 托盘单击/双击的语义现在是"恢复"而不是"切换"：隐藏时 showRequested 把窗口
        # 请回来；窗口本来就在时再发一次也只是抬到前台，不会把它藏掉。
        tray.showRequested.emit()
        _app.processEvents()
        self.assertFalse(window.boss_hidden)
        self.assertFalse(tray.hidden_state)
        self.assertTrue(window.isVisible())
        tray.showRequested.emit()
        _app.processEvents()
        self.assertTrue(window.isVisible(), "单击托盘永远是恢复，不是切换")

    def test_tray_present_undoes_a_mouse_leave_hide(self) -> None:
        """窗口被移出隐身压到近全透（LEAVE_HIDE_OPACITY）时，托盘恢复必须连同用户的
        不透明度一起还回来。"""
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        window.settings.mouse_leave_hide = True
        window.settings.opacity = 70
        window.apply_opacity(70, animate=False)
        window.leaveEvent(QEvent(QEvent.Leave))
        # 直接让隐身到点，不真实等待 100ms。
        window._apply_leave_hide()
        assert_at_leave_hide_floor(self, window)

        tray.showRequested.emit()
        _app.processEvents()
        self.assertTrue(window.isVisible())
        self.assertAlmostEqual(window.windowOpacity(), 0.7, places=2)
        self.assertIsNone(window._opacity_before_hide)

    def test_the_open_signal_opens_a_file_dialog(self) -> None:
        """Asserted by substitution: the dialog is modal and would block the suite."""
        window = self.make_window()
        calls = []
        window.open_dialog = lambda: calls.append("open")
        window.attach_tray(FakeTray())
        window.tray.openRequested.emit()
        _app.processEvents()
        self.assertEqual(calls, ["open"])

    def test_the_settings_signal_opens_the_dialog(self) -> None:
        window = self.make_window()
        calls = []
        window.open_settings = lambda: calls.append("settings")
        window.attach_tray(FakeTray())
        window.tray.settingsRequested.emit()
        _app.processEvents()
        self.assertEqual(calls, ["settings"])

    def test_the_quit_signal_really_quits(self) -> None:
        window = self.make_window()
        window.attach_tray(FakeTray())
        window.settings.close_to_tray = True
        window.quit_app()
        _app.processEvents()
        self.assertFalse(window.isVisible(), "退出 must leave, not hide")

    def test_close_hides_to_the_tray_when_asked(self) -> None:
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        window.settings.close_to_tray = True
        window.close()
        _app.processEvents()
        self.assertFalse(window.isVisible())
        self.assertTrue(window.boss_hidden, "hidden, not destroyed")
        self.assertFalse(tray.shutdown_called, "the tray outlives the window")

    def test_close_hides_now_that_the_app_lives_in_the_tray(self) -> None:
        """新常驻模型：✕ 一律缩起来，只要还有办法（托盘/老板键）把窗口叫回来。

        旧行为是"close_to_tray 关着时 ✕ 真退出"，入口统一到 main.py 之后
        setQuitOnLastWindowClosed(False)，✕ 不再承担退出职责——退出只认托盘菜单。
        """
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        self.assertFalse(self.settings.close_to_tray, "前提：设置项默认仍是关的")
        window.close()
        _app.processEvents()
        self.assertFalse(window.isVisible())
        self.assertTrue(window.boss_hidden, "hidden, not destroyed")
        self.assertFalse(tray.shutdown_called, "the tray outlives the window")

    def test_close_really_closes_when_nothing_can_bring_it_back(self) -> None:
        """没有托盘、老板键也不可用时藏起来就是永久丢失——这种环境下 ✕ 必须真关。"""
        window = self.make_window(path=self.book_path)
        self.assertIsNone(window.tray)
        self.assertFalse(window.can_hide())
        window.close()
        _app.processEvents()
        self.assertFalse(window.isVisible())
        self.assertFalse(window.boss_hidden)

    def test_really_quit_bypasses_the_hide_even_with_a_tray(self) -> None:
        """_really_quit 是唯一的硬退出开关：有托盘也照样真关。"""
        window = self.make_window(path=self.book_path)
        tray = FakeTray()
        window.attach_tray(tray)
        window._really_quit = True
        window.close()
        _app.processEvents()
        self.assertTrue(tray.shutdown_called)
        self.assertFalse(window.isVisible())
        self.assertFalse(window.boss_hidden)

    def test_the_tray_can_be_created_and_destroyed_at_runtime(self) -> None:
        """Unlike window flags, a tray icon does not need a restart - so turning the
        setting off takes effect immediately."""
        window = self.make_window()
        self.settings.show_tray = False
        window.settings.show_tray = False
        window.sync_tray()
        self.assertIsNone(window.tray, "no tray available offscreen, and none wanted")

    def test_setting_the_disguise_before_the_tray_existed_is_not_lost(self) -> None:
        window = self.make_window()
        window.settings.disguise_title = "先设置后托盘"
        window.apply_disguise()
        tray = FakeTray()
        window.attach_tray(tray)
        self.assertEqual(tray.title, "先设置后托盘")

    def test_unbinding_the_boss_key_revokes_capability(self) -> None:
        """`register_all` reports by omission, so an unbound chord must clear a
        previously granted "available" rather than leaving it stale."""
        window = self.make_window()
        window.set_boss_hint(True, self.settings.boss_key)
        self.assertTrue(window._boss_available)
        window.update_boss_capability({})
        self.assertFalse(window._boss_available)
        self.assertIn("不可用", window.hint_label.text())

    def test_a_failed_boss_key_with_a_tray_says_it_can_still_hide(self) -> None:
        window = self.make_window()
        window.attach_tray(FakeTray())
        window.update_boss_capability({"boss_key": (False, "被占用")})
        self.assertIn("托盘", window.hint_label.text())
        self.assertTrue(window.can_hide())


class TestSettingsDialog(WindowTestCase):
    """The dialog is the only writer of Settings, so these tests are about what it
    writes and what it refuses to write."""

    def dialog(self, **kw):
        settings = kw.pop("settings", None) or self.settings
        return SettingsDialog(settings, None)

    def test_it_opens_on_the_current_values(self) -> None:
        self.settings.opacity = 62
        self.settings.snap_distance = 22
        self.settings.disguise_title = "伪装标题"
        dlg = self.dialog()
        try:
            self.assertEqual(dlg.opacity_slider.value(), 62)
            self.assertEqual(dlg.snap_spin.value(), 22)
            self.assertEqual(dlg.title_edit.text(), "伪装标题")
            self.assertEqual(dlg.hotkey_edits["boss_key"].spec(), self.settings.boss_key)
        finally:
            dlg.deleteLater()

    def test_applying_writes_every_field_back(self) -> None:
        dlg = self.dialog()
        try:
            dlg.opacity_slider.setValue(55)
            dlg.dim_check.setChecked(True)
            dlg.dim_slider.setValue(35)
            dlg.snap_check.setChecked(False)
            dlg.snap_spin.setValue(25)
            dlg.title_edit.setText("季度报告")
            dlg.tray_check.setChecked(False)
            dlg.onscreen_check.setChecked(False)
            target = dlg.apply_to(Settings())
            self.assertEqual(target.opacity, 55)
            self.assertTrue(target.dim_on_blur)
            self.assertEqual(target.dim_opacity, 35)
            self.assertFalse(target.snap_enabled)
            self.assertEqual(target.snap_distance, 25)
            self.assertEqual(target.disguise_title, "季度报告")
            self.assertFalse(target.show_tray)
            self.assertFalse(target.keep_on_screen)
            # 「点 ✕ 隐藏到托盘」复选框已下线：对话框里必须连属性都不存在，
            # apply_to 也不许再碰这个设置（它退化成纯兼容字段）。
            self.assertFalse(
                hasattr(dlg, "close_tray_check"),
                "close_to_tray 复选框必须已移除",
            )
            self.assertFalse(target.close_to_tray)
        finally:
            dlg.deleteLater()

    def test_applying_clamps_what_the_widgets_cannot_prevent(self) -> None:
        dlg = self.dialog()
        try:
            dlg.title_edit.setText("字" * 4000)
            target = dlg.apply_to(Settings())
            self.assertLessEqual(len(target.disguise_title), 120)
        finally:
            dlg.deleteLater()

    def test_the_dialog_never_touches_geometry_or_the_last_book(self) -> None:
        """They are session state: 恢复默认 must not move the window or forget the book."""
        self.settings.geometry = {"x": 11, "y": 22, "w": 480, "h": 360}
        self.settings.last_book = "C:\\books\\a.txt"
        dlg = self.dialog()
        try:
            dlg._restore_defaults()
            target = dlg.apply_to(self.settings)
            self.assertEqual(target.geometry, {"x": 11, "y": 22, "w": 480, "h": 360})
            self.assertEqual(target.last_book, "C:\\books\\a.txt")
        finally:
            dlg.deleteLater()

    def test_restore_defaults_resets_preferences(self) -> None:
        self.settings.opacity = 45
        self.settings.disguise_title = "临时"
        self.settings.hotkey_next = "Ctrl+Right"
        dlg = self.dialog()
        try:
            dlg._restore_defaults()
            target = dlg.apply_to(Settings())
            fresh = Settings()
            self.assertEqual(target.opacity, fresh.opacity)
            self.assertEqual(target.disguise_title, fresh.disguise_title)
            self.assertEqual(target.hotkey_next, fresh.hotkey_next)
            self.assertEqual(target.boss_key, fresh.boss_key)
        finally:
            dlg.deleteLater()

    def test_a_valid_hotkey_is_accepted_and_normalised(self) -> None:
        dlg = self.dialog()
        try:
            edit = dlg.hotkey_edits["hotkey_next"]
            edit.set_spec("ctrl + right")
            self.assertEqual(edit.spec(), "Ctrl+Right")
            self.assertEqual(edit.problem(), "")
        finally:
            dlg.deleteLater()

    def test_an_invalid_hotkey_is_flagged_and_blocks_ok(self) -> None:
        """Silently dropping a binding the user did not mean to clear is worse than
        making them look at the problem."""
        dlg = self.dialog()
        try:
            dlg.hotkey_edits["hotkey_next"].set_spec("Banana")
            self.assertTrue(dlg.hotkey_edits["hotkey_next"].problem())
            self.assertFalse(dlg._ok_button.isEnabled())
            self.assertTrue(dlg.hotkey_note.text())
        finally:
            dlg.deleteLater()

    def test_a_duplicate_hotkey_is_flagged_before_saving(self) -> None:
        dlg = self.dialog()
        try:
            dlg.hotkey_edits["hotkey_next"].set_spec("Ctrl+Shift+`")
            self.assertIn("同一个组合键", dlg.hotkey_note.text())
            self.assertFalse(dlg._ok_button.isEnabled())
        finally:
            dlg.deleteLater()

    def test_clearing_a_duplicate_restores_ok(self) -> None:
        dlg = self.dialog()
        try:
            dlg.hotkey_edits["hotkey_next"].set_spec("Ctrl+Shift+`")
            self.assertFalse(dlg._ok_button.isEnabled())
            dlg.hotkey_edits["hotkey_next"].set_spec("")
            self.assertTrue(dlg._ok_button.isEnabled(), "a blank binding cannot collide")
        finally:
            dlg.deleteLater()

    def test_escaping_a_hotkey_field_clears_it_without_closing_the_dialog(self) -> None:
        """Losing the whole form because you wanted to unbind one chord would be
        infuriating. Asserted by watching for a reject() call rather than on visibility,
        because a dialog that was never shown is trivially "not visible"."""
        dlg = self.dialog()
        try:
            rejected = []
            dlg.reject = lambda: rejected.append(True)
            edit = dlg.hotkey_edits["boss_key"]
            edit.set_spec("Ctrl+Shift+`")
            event = _fake_key(Qt.Key_Escape)
            edit.keyPressEvent(event)
            self.assertEqual(edit.spec(), "")
            self.assertTrue(event.accepted, "the keystroke is consumed")
            self.assertEqual(rejected, [], "Esc must clear the binding, not cancel the dialog")
        finally:
            dlg.deleteLater()

    def test_backspace_also_clears_a_binding(self) -> None:
        dlg = self.dialog()
        try:
            edit = dlg.hotkey_edits["boss_key"]
            edit.set_spec("Ctrl+Shift+`")
            edit.keyPressEvent(_fake_key(Qt.Key_Backspace))
            self.assertEqual(edit.spec(), "")
        finally:
            dlg.deleteLater()

    def test_a_keypress_becomes_a_binding(self) -> None:
        dlg = self.dialog()
        try:
            edit = dlg.hotkey_edits["boss_key"]
            edit.set_spec("")
            edit.keyPressEvent(_fake_key(Qt.Key_F9, Qt.ControlModifier))
            self.assertEqual(edit.spec(), "Ctrl+F9")
            self.assertEqual(parse_hotkey(edit.spec()), parse_hotkey("Ctrl+F9"))
        finally:
            dlg.deleteLater()

    def test_a_bare_letter_is_rejected_with_an_explanation(self) -> None:
        """RegisterHotKey would steal a bare letter from every other program."""
        dlg = self.dialog()
        try:
            edit = dlg.hotkey_edits["boss_key"]
            edit.keyPressEvent(_fake_key(Qt.Key_A))
            self.assertTrue(edit.problem())
            self.assertIn("Ctrl", edit.toolTip() + dlg.hotkey_note.text())
        finally:
            dlg.deleteLater()

    def test_a_modifier_alone_is_ignored_rather_than_binding_the_modifier(self) -> None:
        dlg = self.dialog()
        try:
            edit = dlg.hotkey_edits["boss_key"]
            edit.set_spec("Ctrl+Shift+`")
            edit.keyPressEvent(_fake_key(Qt.Key_Control, Qt.ControlModifier))
            self.assertEqual(edit.spec(), "Ctrl+Shift+`", "unchanged: still waiting for a key")
        finally:
            dlg.deleteLater()

    def test_the_dim_slider_is_disabled_until_dimming_is_on(self) -> None:
        """An enabled control that does nothing is a lie about how the app behaves."""
        dlg = self.dialog()
        try:
            dlg.dim_check.setChecked(False)
            self.assertFalse(dlg.dim_slider.isEnabled())
            dlg.dim_check.setChecked(True)
            self.assertTrue(dlg.dim_slider.isEnabled())
        finally:
            dlg.deleteLater()

    def test_the_snap_distance_is_disabled_until_snapping_is_on(self) -> None:
        dlg = self.dialog()
        try:
            dlg.snap_check.setChecked(False)
            self.assertFalse(dlg.snap_spin.isEnabled())
            dlg.snap_check.setChecked(True)
            self.assertTrue(dlg.snap_spin.isEnabled())
        finally:
            dlg.deleteLater()

    def test_changing_the_restart_only_flags_shows_a_note(self) -> None:
        """Window flags cannot change at runtime without orphaning the registered
        hotkey, so the honest thing is to say a restart is needed."""
        dlg = self.dialog()
        try:
            self.assertEqual(dlg.restart_note.text(), "")
            dlg.taskbar_check.setChecked(not self.settings.hide_from_taskbar)
            self.assertIn("重启", dlg.restart_note.text())
        finally:
            dlg.deleteLater()

    def test_restoring_the_flags_clears_the_restart_note(self) -> None:
        dlg = self.dialog()
        try:
            original = self.settings.hide_from_taskbar
            dlg.taskbar_check.setChecked(not original)
            self.assertIn("重启", dlg.restart_note.text())
            dlg.taskbar_check.setChecked(original)
            self.assertEqual(dlg.restart_note.text(), "")
        finally:
            dlg.deleteLater()

    def test_every_hotkey_field_is_offered(self) -> None:
        from minireader.storage import HOTKEY_FIELDS

        dlg = self.dialog()
        try:
            self.assertEqual(set(dlg.hotkey_edits), {name for name, _, _ in HOTKEY_FIELDS})
        finally:
            dlg.deleteLater()

    def test_the_dialog_never_opens_taller_than_the_screen(self) -> None:
        """The groups stack to ~713 px, which does not fit a 1366x768 office laptop with
        its taskbar - the machine this app is for. `resize()` alone does NOT fix that:
        the layout re-activates on show() and grows the dialog back (measured: a 720 px
        dialog stayed 720 px on the offscreen plugin's 600 px screen)."""
        avail = QApplication.desktop().availableGeometry()
        limit = max(320, avail.height() - 90)
        dlg = self.dialog()
        try:
            self.assertLessEqual(dlg.height(), limit, f"screen is {avail.height()}px tall")
        finally:
            dlg.deleteLater()

    def test_the_ok_button_stays_inside_the_dialog(self) -> None:
        """However the height is capped, 确定 must not be pushed off the bottom."""
        dlg = self.dialog()
        try:
            ok = dlg._ok_button
            self.assertTrue(ok.isVisible() or not dlg.isVisible())
            self.assertTrue(
                dlg.rect().contains(ok.geometry().center()),
                f"OK at {ok.geometry().getRect()} inside {dlg.rect().getRect()}",
            )
        finally:
            dlg.deleteLater()

    def test_content_either_fits_or_scrolls(self) -> None:
        """The invariant that holds on any screen size: nothing is simply cut off. This
        is asserted as a disjunction because the offscreen screen (600 px) is shorter
        than the content, while a real 1080p screen is taller."""
        dlg = self.dialog()
        try:
            area = dlg._scroll
            content = area.widget().sizeHint().height()
            viewport = area.viewport().height()
            scrolls = area.verticalScrollBar().maximum() > 0
            self.assertTrue(
                scrolls or viewport >= content,
                f"viewport {viewport} < content {content} and no scrollbar",
            )
        finally:
            dlg.deleteLater()

    def test_the_scroll_area_is_capped_rather_than_the_dialog(self) -> None:
        """Capping the *dialog* is what failed; the working lever is the scroll area's
        maximum height, because that changes what the layout asks for."""
        dlg = self.dialog()
        try:
            self.assertGreaterEqual(dlg._scroll.maximumHeight(), 120)
            self.assertTrue(dlg.maximumHeight() < 20000, "a backstop maximum is set")
        finally:
            dlg.deleteLater()

    def test_the_button_row_is_outside_the_scrolling_area(self) -> None:
        """So it cannot be scrolled away."""
        dlg = self.dialog()
        try:
            self.assertFalse(dlg._scroll.isAncestorOf(dlg._ok_button))
        finally:
            dlg.deleteLater()

    def test_the_preview_reports_what_the_window_should_look_like(self) -> None:
        """Live preview is what makes the ClearType trade-off visible before saving
        rather than a surprise afterwards."""
        seen = []
        dlg = SettingsDialog(self.settings, None, on_preview=seen.append)
        try:
            dlg.opacity_slider.setValue(64)
            self.assertTrue(seen, "the preview callback must fire while dragging")
            self.assertEqual(seen[-1].opacity, 64)
        finally:
            dlg.deleteLater()

    def test_the_dialog_settings_copy_leaves_the_original_alone(self) -> None:
        dlg = self.dialog()
        try:
            dlg.opacity_slider.setValue(48)
            copy = dlg.settings()
            self.assertEqual(copy.opacity, 48)
            self.assertEqual(self.settings.opacity, 100, "cancel must not have changed it")
        finally:
            dlg.deleteLater()


class TestSettingsNavigation(WindowTestCase):
    """Telegram-style settings shell (方案 B): a four-row left nav drives a
    QStackedWidget of four pages; the four group builders are reused verbatim.
    These pin the navigation/paging/footer wiring and the promise that preview,
    restore and (window-side) rollback work identically from every page.
    """

    def dialog(self, **kw):
        settings = kw.pop("settings", None) or self.settings
        return SettingsDialog(settings, None, **kw)

    def _dispose(self, dialog) -> None:
        dialog.deleteLater()
        _app.sendPostedEvents(None, QEvent.DeferredDelete)

    def _restore_button(self, dialog):
        return next(
            b for b in dialog._ok_button.parent().buttons() if b.text() == "恢复默认"
        )

    # -- nav <-> pages ------------------------------------------------------

    def test_the_nav_lists_four_groups_and_each_row_shows_its_page(self) -> None:
        dlg = self.dialog()
        try:
            self.assertEqual(dlg._nav.count(), 4)
            self.assertEqual(dlg._stack.count(), 4)
            self.assertEqual(
                [dlg._nav.item(i).text() for i in range(4)],
                [title for title, _ in NAV_ITEMS],
            )
            for row in range(4):
                dlg._nav.setCurrentRow(row)
                _app.processEvents()
                self.assertIs(dlg._stack.currentWidget(), dlg._pages[row])
                self.assertIs(
                    dlg._scroll, dlg._page_areas[row],
                    "_scroll must alias the CURRENT page's scroll area",
                )
                self.assertEqual(dlg._stack.indexOf(dlg._pages[row]), row)
        finally:
            self._dispose(dlg)

    def test_each_page_holds_exactly_its_own_group(self) -> None:
        dlg = self.dialog()
        try:
            controls = [
                ("正文排版", (dlg.font_combo, dlg.size_slider, dlg.margin_slider)),
                ("外观", (dlg.opacity_slider, dlg.bg_alpha_slider, dlg.compat_check)),
                ("窗口", (dlg.snap_check, dlg.leave_hide_check,
                          dlg.on_top_check, dlg.taskbar_check)),
                ("全局快捷键", tuple(dlg.hotkey_edits.values())),
            ]
            for row, (group_title, members) in enumerate(controls):
                page = dlg._pages[row]
                boxes = page.findChildren(QGroupBox)
                self.assertEqual(len(boxes), 1, f"{group_title} 页就一张分组卡片")
                self.assertEqual(boxes[0].title(), group_title)
                headings = [
                    child for child in page.findChildren(QLabel)
                    if child.objectName() == "PageTitle"
                ]
                self.assertEqual([h.text() for h in headings], [NAV_ITEMS[row][0]])
                for control in members:
                    self.assertTrue(
                        page.isAncestorOf(control),
                        f"{type(control).__name__} must live on the {group_title} page",
                    )
            # Controls never leak onto another page.
            self.assertFalse(dlg._pages[2].isAncestorOf(dlg.size_slider))
            self.assertFalse(dlg._pages[0].isAncestorOf(dlg.snap_check))
        finally:
            self._dispose(dlg)

    def test_keyboard_up_and_down_move_between_pages(self) -> None:
        dlg = self.dialog()
        try:
            dlg._nav.setCurrentRow(0)
            dlg._nav.setFocus()

            def press(key) -> None:
                _app.sendEvent(
                    dlg._nav, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
                )
                _app.processEvents()

            press(Qt.Key_Down)
            self.assertEqual(dlg._nav.currentRow(), 1)
            self.assertIs(dlg._stack.currentWidget(), dlg._pages[1])
            press(Qt.Key_Down)
            self.assertEqual(dlg._nav.currentRow(), 2)
            press(Qt.Key_Up)
            self.assertEqual(dlg._nav.currentRow(), 1)
            self.assertIs(dlg._stack.currentWidget(), dlg._pages[1])
        finally:
            self._dispose(dlg)

    # -- footer works from every page --------------------------------------

    def test_the_footer_buttons_are_reachable_on_every_page(self) -> None:
        dlg = self.dialog()
        try:
            for row in range(4):
                dlg._nav.setCurrentRow(row)
                _app.processEvents()
                ok = dlg._ok_button
                self.assertTrue(ok.isEnabled())
                self.assertTrue(
                    dlg.rect().contains(ok.geometry().center()),
                    f"确定 must stay inside the dialog on page {row}",
                )
                self.assertTrue(self._restore_button(dlg).isEnabled())
                for area in dlg._page_areas:
                    self.assertFalse(
                        area.isAncestorOf(ok), "确定 cannot be scrolled away"
                    )
        finally:
            self._dispose(dlg)

    def test_restore_defaults_works_from_a_non_first_page(self) -> None:
        dlg = self.dialog()
        try:
            dlg._nav.setCurrentRow(2)              # 窗口页 current
            dlg.size_slider.setValue(40)          # mutate a HIDDEN typography control
            dlg.opacity_slider.setValue(33)        # and a hidden appearance control
            self._restore_button(dlg).click()
            _app.processEvents()
            self.assertEqual(dlg.size_slider.value(), Settings().font_size)
            self.assertEqual(dlg.opacity_slider.value(), Settings().opacity)
            self.assertEqual(dlg.settings().font_size, Settings().font_size)
            self.assertEqual(dlg._nav.currentRow(), 2, "恢复默认不改变当前页")
        finally:
            self._dispose(dlg)

    def test_the_restart_note_in_the_footer_persists_across_pages(self) -> None:
        dlg = self.dialog()
        try:
            dlg._nav.setCurrentRow(2)
            dlg.taskbar_check.setChecked(not self.settings.hide_from_taskbar)
            self.assertIn("重启", dlg.restart_note.text())
            for row in (0, 1, 3):
                dlg._nav.setCurrentRow(row)
                self.assertIn(
                    "重启", dlg.restart_note.text(),
                    "重启提示在常驻页脚里，切页不能把它丢掉",
                )
        finally:
            self._dispose(dlg)

    # -- live preview and the cancel/rollback boundary ---------------------

    def test_preview_fires_from_preview_pages_and_is_silent_elsewhere(self) -> None:
        seen: list = []
        dlg = self.dialog(on_preview=seen.append)
        try:
            dlg._nav.setCurrentRow(0)
            dlg.size_slider.setValue(31)
            self.assertTrue(seen, "正文排版页拖字号必须实时预览")
            self.assertEqual(seen[-1].font_size, 31)

            quiet = len(seen)
            dlg._nav.setCurrentRow(2)
            dlg.snap_check.setChecked(False)      # window-behaviour flag, save-only
            self.assertEqual(len(seen), quiet, "窗口行为项没有实时预览")

            dlg._nav.setCurrentRow(3)
            next(iter(dlg.hotkey_edits.values())).set_spec("")  # capture, save-only
            self.assertEqual(len(seen), quiet, "快捷键捕获没有实时预览")

            dlg._nav.setCurrentRow(1)
            dlg.opacity_slider.setValue(70)
            self.assertGreater(len(seen), quiet, "外观页拖不透明度必须实时预览")
            self.assertEqual(seen[-1].opacity, 70)
        finally:
            self._dispose(dlg)

    def test_mutating_controls_on_every_page_never_writes_settings(self) -> None:
        """The dialog half of cancel/rollback: controls on ANY page only edit dialog
        state; the live Settings object is touched solely through apply_to()."""
        original = (
            self.settings.font_size, self.settings.opacity,
            self.settings.snap_enabled, self.settings.boss_key,
        )
        dlg = self.dialog()
        try:
            dlg._nav.setCurrentRow(0)
            dlg.size_slider.setValue(31)
            dlg._nav.setCurrentRow(1)
            dlg.opacity_slider.setValue(44)
            dlg._nav.setCurrentRow(2)
            dlg.snap_check.setChecked(not original[2])
            dlg._nav.setCurrentRow(3)
            next(iter(dlg.hotkey_edits.values())).set_spec("")
            self.assertEqual(
                (self.settings.font_size, self.settings.opacity,
                 self.settings.snap_enabled, self.settings.boss_key),
                original,
                "fiddling with any page must not write the open Settings",
            )
        finally:
            self._dispose(dlg)

        # A freshly opened dialog still shows the pre-open values.
        again = self.dialog()
        try:
            self.assertEqual(again.size_slider.value(), original[0])
            self.assertEqual(again.opacity_slider.value(), original[1])
            self.assertEqual(again.snap_check.isChecked(), original[2])
            self.assertEqual(again.hotkey_edits["boss_key"].spec(), original[3])
        finally:
            self._dispose(again)

    # -- session memory ----------------------------------------------------

    def test_the_dialog_reopens_on_the_last_selected_page(self) -> None:
        dlg = self.dialog()
        try:
            dlg._nav.setCurrentRow(3)
        finally:
            self._dispose(dlg)
        second = self.dialog()
        try:
            self.assertEqual(second._nav.currentRow(), 3)
            self.assertIs(second._stack.currentWidget(), second._pages[3])
        finally:
            second._nav.setCurrentRow(0)   # reset process-wide memory for other tests
            self._dispose(second)

    # -- visual red lines --------------------------------------------------

    def test_the_nav_stylesheet_uses_card_field_accent_and_a_pill(self) -> None:
        from minireader.ui.settings_dialog import _stylesheet
        from minireader.ui.theme import PALETTE

        qss = _stylesheet()
        self.assertIn("QListWidget#SettingsNav", qss)
        self.assertIn(PALETTE["card"], qss, "导航列用 card 底")
        self.assertIn(PALETTE["field"], qss, "选中项用 field 底")
        self.assertIn(
            "border-left: 3px solid " + PALETTE["accent"], qss,
            "选中项左侧 3px accent 竖条",
        )
        self.assertIn("QListWidget#SettingsNav::item:hover", qss, "悬停高亮")
        self.assertIn("border-radius: 6px", qss, "圆角 pill")
        self.assertIn("QWidget#SettingsFooter", qss)
        self.assertIn("font-size: 13px", qss)


class _FakeEvent:
    """The minimum an event needs to be for the handlers under test.

    `globalPosition()` returns a QPointF and `globalPos()` a QPoint, because that is the
    asymmetry the Qt binding actually has: `event_global_pos()` prefers the former and
    calls `.toPoint()` on it.
    """

    def __init__(self, pos=QPoint(0, 0), button=Qt.LeftButton, key=0, mods=0) -> None:
        self._pos = pos
        self._button = button
        self._key = key
        self._mods = mods
        self.accepted = False

    def pos(self):
        return self._pos

    def globalPos(self):
        return self._pos

    def globalPosition(self):
        return QPointF(self._pos)

    def button(self):
        return self._button

    def buttons(self):
        return self._button

    def key(self):
        return self._key

    def modifiers(self):
        return self._mods

    def type(self):
        return 0

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def _fake_press(window) -> _FakeEvent:
    return _FakeEvent(pos=QPoint(window.width() // 2, 5))


def _fake_key(key: int, mods: int = 0) -> _FakeEvent:
    return _FakeEvent(key=key, mods=mods)


if __name__ == "__main__":
    unittest.main(verbosity=2)
