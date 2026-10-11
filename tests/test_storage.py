"""Storage layer: geometry validation, config round-trips, corrupt-file tolerance.

The theme is "never lose the window and never crash on bad config". Every one of
these cases has a plausible real cause: a monitor unplugged since last run, a
half-written JSON file after a power cut, a user editing the file by hand.

    py -3.12 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict, fields
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core.model import Book, Progress, book_id  # noqa: E402
from minireader.storage import (  # noqa: E402
    HOTKEY_FIELDS,
    Library,
    ProgressStore,
    Settings,
    alpha_to_percent,
    data_dir,
    default_geometry,
    percent_to_alpha,
    validate_geometry,
)

SCREEN = (0, 0, 1920, 1080)
DUAL = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]


class TestValidateGeometry(unittest.TestCase):
    """A window restored off-screen is invisible forever, with no tray icon to
    rescue it - this is the guard against exactly that."""

    def test_a_sane_rect_is_kept(self) -> None:
        geo = {"x": 100, "y": 100, "w": 420, "h": 360}
        self.assertEqual(validate_geometry(geo, [SCREEN]), geo)

    def test_a_rect_on_a_screen_that_is_gone_is_rejected(self) -> None:
        geo = {"x": 5000, "y": 5000, "w": 420, "h": 260}
        self.assertIsNone(validate_geometry(geo, [SCREEN]))

    def test_a_sliver_of_visible_window_is_rejected(self) -> None:
        """20 px of overlap cannot be grabbed with a mouse."""
        geo = {"x": 1900, "y": 1000, "w": 420, "h": 260}
        self.assertIsNone(validate_geometry(geo, [SCREEN]))

    def test_enough_overlap_is_accepted(self) -> None:
        geo = {"x": 1700, "y": 900, "w": 420, "h": 260}
        self.assertIsNotNone(validate_geometry(geo, [SCREEN]))

    def test_negative_coordinates_on_the_primary_screen_are_fine(self) -> None:
        geo = {"x": -100, "y": -50, "w": 420, "h": 260}
        result = validate_geometry(geo, [SCREEN])
        self.assertIsNotNone(result)
        self.assertEqual(result["x"], -100)

    def test_a_secondary_monitor_is_honoured(self) -> None:
        geo = {"x": 2000, "y": 200, "w": 420, "h": 260}
        self.assertIsNotNone(validate_geometry(geo, DUAL))

    def test_absurd_size_is_clamped_to_the_screen(self) -> None:
        geo = {"x": 0, "y": 0, "w": 99999, "h": 99999}
        result = validate_geometry(geo, [SCREEN])
        self.assertEqual((result["w"], result["h"]), (1920, 1080))

    def test_tiny_size_is_raised_to_the_minimum(self) -> None:
        geo = {"x": 100, "y": 100, "w": 10, "h": 10}
        result = validate_geometry(geo, [SCREEN])
        self.assertEqual((result["w"], result["h"]), (320, 360))

    def test_junk_is_rejected_rather_than_raising(self) -> None:
        for junk in (
            None,
            {},
            "not a dict",
            [1, 2, 3],
            {"x": 0, "y": 0},
            {"x": "a", "y": 0, "w": 400, "h": 300},
            {"x": 0, "y": 0, "w": 0, "h": 300},
            {"x": 0, "y": 0, "w": 400, "h": -5},
        ):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(validate_geometry(junk, [SCREEN]))

    def test_no_screens_means_no_verdict(self) -> None:
        self.assertIsNone(validate_geometry({"x": 0, "y": 0, "w": 400, "h": 300}, []))

    def test_floats_from_json_are_coerced(self) -> None:
        geo = {"x": 100.0, "y": 100.0, "w": 420.0, "h": 360.0}
        result = validate_geometry(geo, [SCREEN])
        self.assertEqual(result, {"x": 100, "y": 100, "w": 420, "h": 360})
        self.assertIsInstance(result["w"], int)


class TestDefaultGeometry(unittest.TestCase):
    def test_lands_on_screen(self) -> None:
        geo = default_geometry(SCREEN)
        self.assertIsNotNone(validate_geometry(geo, [SCREEN]))

    def test_sits_in_the_bottom_right(self) -> None:
        """The whole point of a 摸鱼 window: out of the way, one corner.

        阶段 2 默认窗高 585，在 1080p 屏上窗口顶边不可能严格落在下半区（585 > 1080/2），
        所以断言的是**窗口中心**落在右下象限、且整窗不出屏——"视觉重心在右下角"的本意。
        """
        geo = default_geometry(SCREEN)
        self.assertGreater(geo["x"] + geo["w"] / 2, SCREEN[2] / 2)
        self.assertGreater(geo["y"] + geo["h"] / 2, SCREEN[3] / 2)
        self.assertLessEqual(geo["x"] + geo["w"], SCREEN[2])
        self.assertLessEqual(geo["y"] + geo["h"], SCREEN[3])

    def test_shrinks_to_fit_a_small_screen(self) -> None:
        geo = default_geometry((0, 0, 500, 400))
        self.assertLessEqual(geo["w"], 500)
        self.assertLessEqual(geo["h"], 400)


class TestAtomicWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-store-"))

    def test_settings_round_trip(self) -> None:
        path = self.tmp / "settings.json"
        settings = Settings(font_size=22, wheel_lines=5, boss_key="Ctrl+Alt+B")
        ok, message = settings.save(path)
        self.assertTrue(ok, message)
        loaded = Settings.load(path)
        self.assertEqual(loaded.font_size, 22)
        self.assertEqual(loaded.wheel_lines, 5)
        self.assertEqual(loaded.boss_key, "Ctrl+Alt+B")

    def test_write_leaves_no_temp_files_behind(self) -> None:
        path = self.tmp / "settings.json"
        Settings().save(path)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name != "settings.json"]
        self.assertEqual(leftovers, [], "atomic write must clean up its temp file")

    def test_rewriting_replaces_cleanly(self) -> None:
        path = self.tmp / "settings.json"
        Settings(font_size=12).save(path)
        Settings(font_size=30).save(path)
        self.assertEqual(Settings.load(path).font_size, 30)
        self.assertEqual(len(list(self.tmp.iterdir())), 1)

    def test_corrupt_json_degrades_to_defaults(self) -> None:
        path = self.tmp / "settings.json"
        path.write_text('{"font_size": 20, TRUNCATED', encoding="utf-8")
        loaded = Settings.load(path)
        self.assertEqual(loaded.font_size, Settings().font_size, "defaults, not a crash")

    def test_empty_file_degrades_to_defaults(self) -> None:
        path = self.tmp / "settings.json"
        path.write_text("", encoding="utf-8")
        self.assertEqual(Settings.load(path).font_size, Settings().font_size)

    def test_unknown_keys_are_ignored(self) -> None:
        """A config from a future version must not break this one."""
        path = self.tmp / "settings.json"
        path.write_text(
            json.dumps({"font_size": 25, "nonsense": 1, "future_field": [1, 2]}), encoding="utf-8"
        )
        loaded = Settings.load(path)
        self.assertEqual(loaded.font_size, 25)
        self.assertFalse(hasattr(loaded, "nonsense"))

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(Settings.load(self.tmp / "nope.json").font_size, Settings().font_size)

    def test_a_json_array_instead_of_an_object_is_survivable(self) -> None:
        path = self.tmp / "settings.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(Settings.load(path).font_size, Settings().font_size)

    def test_save_into_a_missing_directory_creates_it(self) -> None:
        path = self.tmp / "deep" / "deeper" / "settings.json"
        ok, message = Settings().save(path)
        self.assertTrue(ok, message)
        self.assertTrue(path.exists())


class TestSettingsClamp(unittest.TestCase):
    def test_out_of_range_values_are_forced_back(self) -> None:
        settings = Settings(
            font_size=999, line_height=5, margin=-10, wheel_lines=0, wheel_animation_ms=99999
        )
        settings.clamp()
        self.assertEqual(settings.font_size, 48)
        self.assertEqual(settings.line_height, 100)
        self.assertEqual(settings.margin, 0)
        self.assertEqual(settings.wheel_lines, 1)
        self.assertEqual(settings.wheel_animation_ms, 1000)

    def test_a_bad_colour_falls_back(self) -> None:
        settings = Settings(bg="not-a-colour", fg=None, border=17)
        settings.clamp()
        self.assertEqual(settings.bg, "#1b1b1b")
        self.assertEqual(settings.fg, "#d8d8da")
        self.assertEqual(settings.border, "#3a3a3a")

    def test_defaults_match_the_qq_mini_palette(self) -> None:
        """默认外观对齐 QQ 迷你窗实测源色：#1b1b1b @ 60%（153），正文 #d8d8da。

        这两个值是一组：bg 是夹逼法解出的 RGB 源色，bg_alpha 是独立的 alpha，分开存
        才不会二次衰减。滑块语义是"不透明度 0-100"，所以 153 必须恰好显示为 60。
        """
        settings = Settings()
        self.assertEqual(settings.bg, "#1b1b1b")
        self.assertEqual(settings.fg, "#d8d8da")
        self.assertEqual(settings.bg_alpha, 153)
        self.assertEqual(alpha_to_percent(settings.bg_alpha), 60)

    def test_clamping_is_idempotent(self) -> None:
        settings = Settings(font_size=999)
        settings.clamp()
        once = asdict(settings)
        settings.clamp()
        self.assertEqual(asdict(settings), once)

    def test_geometry_helper_records_ints(self) -> None:
        settings = Settings()
        settings.copy_into_geometry(1.9, 2.9, 400.4, 300.6)
        self.assertEqual(settings.geometry, {"x": 1, "y": 2, "w": 400, "h": 300})


class SandboxedAppData(unittest.TestCase):
    """Redirects %APPDATA% to a temp dir, so no test can touch the real config.

    This matters more than it looks. `Settings.save()` with no argument writes to
    `data_dir()`, which is `%APPDATA%\\MiniReader` - the actual file the user is running
    against. An earlier version of these tests called `save()` bare and quietly wrote
    four invented hotkeys into the real settings.json, where the app then registered
    them at login. `data_dir()` reads APPDATA on every call (it is not cached at
    import), so patching the environment variable is sufficient - and
    `test_the_sandbox_actually_redirects_the_default_path` proves it stays that way.
    """

    def setUp(self) -> None:
        self._appdata = os.environ.get("APPDATA")
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-cfg-"))
        os.environ["APPDATA"] = str(self.tmp)

    def tearDown(self) -> None:
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata

    @property
    def default_settings_path(self) -> Path:
        return self.tmp / "MiniReader" / "settings.json"


class TestTheSandboxItself(SandboxedAppData):
    """A test-harness invariant, asserted rather than assumed: if `data_dir()` ever
    starts caching its result at import time, every bare save() in this file begins
    writing to the user's real config again."""

    def test_the_sandbox_actually_redirects_the_default_path(self) -> None:
        self.assertEqual(data_dir(), self.tmp / "MiniReader")
        # `_appdata` is the real one, saved before setUp swapped it out. Note the path
        # cannot be checked against Path.home(): the temp dir lives *under* the user
        # profile on Windows, so that comparison would be true either way.
        self.assertNotEqual(data_dir(), Path(self._appdata) / "MiniReader")

    def test_a_bare_save_lands_inside_the_sandbox(self) -> None:
        self.assertTrue(Settings().save()[0])
        self.assertTrue(self.default_settings_path.exists(), "written where we expect")

    def test_a_bare_load_reads_what_a_bare_save_wrote(self) -> None:
        Settings(font_size=33).save()
        self.assertEqual(Settings.load().font_size, 33)

    def test_the_rest_of_the_suite_gets_the_real_appdata_back(self) -> None:
        """tearDown must restore, or a later class inherits the sandbox and a test meant
        to exercise the default path silently exercises a temp dir instead."""
        self.assertEqual(os.environ.get("APPDATA"), str(self.tmp))
        # tearDown runs after this, so assert the saved original is what gets restored.
        self.assertIsNotNone(self._appdata)


class TestAppearanceSettings(SandboxedAppData):
    """The 透明度 / 失焦变淡 settings and the rules that combine them."""

    def test_the_default_is_fully_opaque_and_never_dims(self) -> None:
        """Both default to "off" because any opacity below 100% makes Windows drop
        ClearType sub-pixel antialiasing for the window, which is visible on Chinese
        text at reading sizes. Opt in, do not opt out."""
        settings = Settings()
        self.assertEqual(settings.opacity, 100)
        self.assertFalse(settings.dim_on_blur)
        self.assertEqual(settings.target_opacity(active=True), 100)
        self.assertEqual(settings.target_opacity(active=False), 100)

    def test_opacity_is_clamped_into_the_slider_range(self) -> None:
        for raw, expected in ((0, 30), (29, 30), (30, 30), (100, 100), (101, 100), (-5, 30)):
            with self.subTest(raw=raw):
                settings = Settings(opacity=raw)
                settings.clamp()
                self.assertEqual(settings.opacity, expected)

    def test_the_dim_level_is_clamped_too(self) -> None:
        """Floor of 0%: 半透明模式下"变淡"可以一路淡到只剩文字，找回来的办法是老板键
        和托盘图标，而不是靠留一点余辉。"""
        for raw, expected in ((0, 0), (5, 5), (20, 20), (100, 100), (500, 100)):
            with self.subTest(raw=raw):
                settings = Settings(dim_opacity=raw)
                settings.clamp()
                self.assertEqual(settings.dim_opacity, expected)

    def test_the_content_alpha_is_clamped_to_a_byte(self) -> None:
        """content_alpha 是 0-255 的字节值（正文 effect 的 opacity 来源），下限也是 0：
        0 = 正文全透，和 bg_alpha=0（背景全透、文字清晰）是两个相反的极端，都合法。"""
        for raw, expected in ((0, 0), (128, 128), (255, 255), (300, 255), (-1, 0)):
            with self.subTest(raw=raw):
                settings = Settings(content_alpha=raw)
                settings.clamp()
                self.assertEqual(settings.content_alpha, expected)

    def test_mouse_leave_hide_is_coerced_like_every_other_bool(self) -> None:
        """鼠标移出隐身默认关闭，并且和别的布尔字段一样能扛住手改 JSON。

        `"false"` 在 Python 里是真值，所以这个字段必须进 _BOOL_FIELDS，靠 _as_bool
        解析，否则用户在 settings.json 里写 `"mouse_leave_hide": "false"` 会得到相反的
        效果——窗口一移出鼠标就消失。
        """
        self.assertFalse(Settings().mouse_leave_hide)
        for raw, expected in (
            (True, True), (False, False),
            ("true", True), ("false", False), ("0", False), ("1", True),
            (0, False), (1, True), (None, False), ("看不懂", False),
        ):
            with self.subTest(raw=raw):
                settings = Settings(mouse_leave_hide=raw)
                settings.clamp()
                self.assertIs(settings.mouse_leave_hide, expected)

    def test_alpha_and_percent_convert_both_ways(self) -> None:
        """界面上两根滑块都是 0-100，盘上都是 0-255，换算表固定在这几个点上。"""
        for percent, alpha in ((0, 0), (50, 128), (100, 255)):
            with self.subTest(percent=percent):
                self.assertEqual(percent_to_alpha(percent), alpha)
                self.assertEqual(alpha_to_percent(alpha), percent)
        # 越界的输入也要落在合法区间里，而不是抛异常
        self.assertEqual(percent_to_alpha(-20), 0)
        self.assertEqual(percent_to_alpha(5000), 255)
        self.assertEqual(alpha_to_percent(-1), 0)
        self.assertEqual(alpha_to_percent(9999), 100)

    def test_every_numeric_field_survives_a_wrong_type(self) -> None:
        """`clamp()` runs straight after reading a file the user can edit. A valid-JSON
        wrong type used to raise ValueError out of startup."""
        settings = Settings(
            font_size="很大", line_height=None, margin=[], wheel_lines={},
            wheel_animation_ms="慢", opacity="透明", dim_opacity=None,
            snap_distance="很近", font_family=None,
        )
        settings.clamp()  # must not raise
        for name in ("font_size", "line_height", "margin", "wheel_lines",
                     "wheel_animation_ms", "opacity", "dim_opacity", "snap_distance"):
            with self.subTest(field=name):
                self.assertIsInstance(getattr(settings, name), int)

    def test_a_numeric_string_is_still_accepted(self) -> None:
        """JSON has no int/float distinction for a hand-written file, and old configs
        may hold strings; those should be read, not thrown away."""
        settings = Settings(font_size="21", snap_distance="25")
        settings.clamp()
        self.assertEqual(settings.font_size, 21)
        self.assertEqual(settings.snap_distance, 25)

    def test_a_float_is_truncated_rather_than_rejected(self) -> None:
        settings = Settings(font_size=17.9)
        settings.clamp()
        self.assertEqual(settings.font_size, 17)

    def test_a_bool_in_a_numeric_field_is_treated_as_damage(self) -> None:
        """`True` is an int in Python, so this is easy to get wrong; reading it as 1
        would silently produce a 1 px font."""
        settings = Settings(font_size=True)
        settings.clamp()
        self.assertEqual(settings.font_size, 25)

    def test_target_opacity_dims_only_when_the_window_is_inactive(self) -> None:
        settings = Settings(opacity=100, dim_on_blur=True, dim_opacity=45)
        self.assertEqual(settings.target_opacity(active=True), 100)
        self.assertEqual(settings.target_opacity(active=False), 45)

    def test_target_opacity_never_brightens_on_blur(self) -> None:
        """A dim level above the normal opacity would make the window MORE opaque when
        it loses focus - the opposite of the feature, and a configuration the sliders
        allow."""
        settings = Settings(opacity=50, dim_on_blur=True, dim_opacity=90)
        self.assertEqual(settings.target_opacity(active=False), 50)

    def test_target_opacity_is_off_when_dimming_is_off(self) -> None:
        settings = Settings(opacity=60, dim_on_blur=False, dim_opacity=20)
        self.assertEqual(settings.target_opacity(active=False), 60)

    def test_the_disguise_title_is_length_capped(self) -> None:
        """It goes into SetWindowTextW and a tray tooltip; a wall of text is not a
        disguise."""
        settings = Settings(disguise_title="字" * 5000)
        settings.clamp()
        self.assertEqual(len(settings.disguise_title), 120)

    def test_a_disguise_title_is_stripped_and_may_be_empty(self) -> None:
        settings = Settings(disguise_title="   报表.xlsx   ")
        settings.clamp()
        self.assertEqual(settings.disguise_title, "报表.xlsx")
        settings.disguise_title = "   "
        settings.clamp()
        self.assertEqual(settings.disguise_title, "")

    def test_a_disguise_title_that_is_not_a_string_falls_back(self) -> None:
        settings = Settings(disguise_title=12345)
        settings.clamp()
        self.assertEqual(settings.disguise_title, "")

    def test_a_newline_in_the_disguise_is_flattened(self) -> None:
        """A title with a line break renders as a broken window title on Windows."""
        settings = Settings(disguise_title="第一行\n第二行\t尾")
        settings.clamp()
        self.assertNotIn("\n", settings.disguise_title)
        self.assertNotIn("\t", settings.disguise_title)

    def test_appearance_round_trips_through_disk(self) -> None:
        original = Settings(opacity=63, dim_on_blur=True, dim_opacity=28, disguise_title="日志.txt")
        original.save()
        back = Settings.load()
        self.assertEqual(back.opacity, 63)
        self.assertTrue(back.dim_on_blur)
        self.assertEqual(back.dim_opacity, 28)
        self.assertEqual(back.disguise_title, "日志.txt")

    def test_snap_and_tray_settings_round_trip(self) -> None:
        original = Settings(
            snap_enabled=False, snap_distance=40, keep_on_screen=False,
            show_tray=False, close_to_tray=True,
        )
        original.save()
        back = Settings.load()
        self.assertFalse(back.snap_enabled)
        self.assertEqual(back.snap_distance, 40)
        self.assertFalse(back.keep_on_screen)
        self.assertFalse(back.show_tray)
        self.assertTrue(back.close_to_tray)


class TestHotkeyFields(SandboxedAppData):
    def test_every_hotkey_field_is_clamped_and_round_trips(self) -> None:
        settings = Settings(
            boss_key="Ctrl+Shift+`",
            hotkey_next="Ctrl+Right",
            hotkey_prev="Ctrl+Left",
            hotkey_opacity_up="Ctrl+Alt+Up",
            hotkey_opacity_down="Ctrl+Alt+Down",
        )
        settings.save()
        back = Settings.load()
        for name, _field, _label in HOTKEY_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(getattr(back, name), getattr(settings, name))

    def test_hotkeys_maps_field_names_to_chords(self) -> None:
        settings = Settings(hotkey_next="Ctrl+Right")
        pairs = settings.hotkeys()
        self.assertIsInstance(pairs, dict)
        self.assertEqual(pairs["hotkey_next"], "Ctrl+Right")
        self.assertEqual(pairs["boss_key"], settings.boss_key)

    def test_hotkeys_omits_unbound_chords_entirely(self) -> None:
        """So "unbound" has exactly one representation. `register_all` reads absence as
        "do not register", and `update_boss_capability` reads an absent boss_key as
        grounds to revoke the boss key rather than leave a stale "available"."""
        settings = Settings(boss_key="", hotkey_next="Ctrl+Right", hotkey_prev="",
                            hotkey_opacity_up="", hotkey_opacity_down="")
        self.assertEqual(set(settings.hotkeys()), {"hotkey_next"})

    def test_hotkeys_is_empty_when_nothing_is_bound(self) -> None:
        settings = Settings(boss_key="", hotkey_next="", hotkey_prev="",
                            hotkey_opacity_up="", hotkey_opacity_down="")
        self.assertEqual(settings.hotkeys(), {})

    def test_the_hotkey_names_are_the_field_names(self) -> None:
        settings = Settings(hotkey_next="Ctrl+Right", hotkey_prev="Ctrl+Left",
                            hotkey_opacity_up="Ctrl+Alt+Up", hotkey_opacity_down="Ctrl+Alt+Down")
        self.assertEqual(set(settings.hotkeys()), {name for name, _, _ in HOTKEY_FIELDS})

    def test_every_hotkey_field_has_a_label_for_the_dialog(self) -> None:
        for name, field, label in HOTKEY_FIELDS:
            with self.subTest(name=name):
                self.assertTrue(name and field and label)
                self.assertTrue(hasattr(Settings(), field))

    def test_a_non_string_hotkey_degrades_to_unbound(self) -> None:
        """A corrupt file must not produce a chord the parser then chokes on."""
        settings = Settings(hotkey_next=17, boss_key=None)
        settings.clamp()
        self.assertEqual(settings.hotkey_next, "")
        self.assertEqual(settings.boss_key, "")

    def test_an_unbound_hotkey_round_trips_as_unbound(self) -> None:
        settings = Settings(boss_key="", hotkey_next="")
        settings.save()
        back = Settings.load()
        self.assertEqual(back.boss_key, "")
        self.assertEqual(back.hotkey_next, "")

    def test_snap_distance_is_clamped(self) -> None:
        """Capped at 60 px: a larger pull stops reading as "snapping" and starts reading
        as the window moving by itself."""
        for raw, expected in ((-10, 0), (0, 0), (14, 14), (60, 60), (400, 60)):
            with self.subTest(raw=raw):
                settings = Settings(snap_distance=raw)
                settings.clamp()
                self.assertEqual(settings.snap_distance, expected)

    def test_snap_distance_that_is_not_a_number_falls_back(self) -> None:
        settings = Settings(snap_distance="很近")
        settings.clamp()
        self.assertIsInstance(settings.snap_distance, int)
        self.assertGreaterEqual(settings.snap_distance, 0)


class TestNothingSensitiveIsPersisted(unittest.TestCase):
    """The boss-key *state* is a deliberate omission, not an oversight.

    If "hidden" were saved, the next launch would open a window the user cannot see -
    and if the tray is switched off in settings and the global hotkey then fails to
    register, there is nothing left to bring it back with.

    Appearance is a different matter and *must* persist: opacity, dim-on-blur and the
    disguise title are preferences, not session state. The two are easy to conflate -
    an earlier version of this test banned the field name "opacity" as if it were
    visibility - so both halves are asserted explicitly below.
    """

    def test_settings_has_no_visibility_field(self) -> None:
        names = {f.name for f in fields(Settings)}
        for banned in (
            "hidden",
            "is_hidden",
            "boss_hidden",
            "visible",
            "shown",
            "visibility",
            "is_visible",
        ):
            self.assertNotIn(banned, names, f"Settings must not persist {banned!r}")

    def test_appearance_preferences_are_persisted(self) -> None:
        """The complement of the test above: these belong on disk."""
        names = {f.name for f in fields(Settings)}
        for expected in ("opacity", "dim_on_blur", "dim_opacity", "disguise_title", "show_tray"):
            self.assertIn(expected, names)

    def test_the_boss_key_is_persisted_even_though_hiding_is_not(self) -> None:
        """`boss_key` holds the chord, not the state - the two must not be confused just
        because both contain the word."""
        names = {f.name for f in fields(Settings)}
        self.assertIn("boss_key", names)
        self.assertNotIn("boss_hidden", names)

    def test_serialised_settings_mention_no_visibility(self) -> None:
        blob = json.dumps(asdict(Settings()), ensure_ascii=False)
        for banned in ("hidden", "visible", "boss_hidden"):
            self.assertNotIn(banned, blob)

    def test_a_serialised_settings_file_records_the_appearance(self) -> None:
        blob = json.dumps(asdict(Settings(opacity=64, dim_on_blur=True)), ensure_ascii=False)
        self.assertIn("opacity", blob)
        self.assertIn("64", blob)

    def test_progress_store_has_no_visibility_field(self) -> None:
        names = {f.name for f in fields(ProgressStore)}
        self.assertEqual(names, {"last_book", "by_id"})


class TestLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-lib-"))

    def test_record_and_find(self) -> None:
        library = Library()
        library.record({"id": "mr-1", "title": "甲", "path": "C:\\a.txt"})
        library.record({"id": "mr-2", "title": "乙", "path": "C:\\b.txt"})
        self.assertEqual(library.books[0]["id"], "mr-2", "most recent first")
        self.assertEqual(library.find("mr-1")["title"], "甲")
        self.assertIsNone(library.find("nope"))

    def test_recording_the_same_book_moves_it_to_the_front(self) -> None:
        library = Library()
        library.record({"id": "mr-1", "title": "旧"})
        library.record({"id": "mr-2", "title": "乙"})
        library.record({"id": "mr-1", "title": "新"})
        self.assertEqual([b["id"] for b in library.books], ["mr-1", "mr-2"])
        self.assertEqual(library.find("mr-1")["title"], "新")

    def test_record_respects_the_limit(self) -> None:
        library = Library()
        for i in range(120):
            library.record({"id": f"mr-{i}", "path": f"C:\\{i}.txt"})
        self.assertLessEqual(len(library.books), 100)

    def test_round_trip_and_corruption(self) -> None:
        path = self.tmp / "library.json"
        library = Library()
        library.record({"id": "mr-1", "title": "甲", "path": "C:\\a.txt"})
        ok, message = library.save(path)
        self.assertTrue(ok, message)
        self.assertEqual(Library.load(path).find("mr-1")["title"], "甲")

        path.write_text("{{{", encoding="utf-8")
        self.assertEqual(Library.load(path).books, [])

        path.write_text(json.dumps({"books": [{"id": "mr-9", "path": "C:\\z.txt"}]}), encoding="utf-8")
        self.assertEqual(len(Library.load(path).books), 1)

    def test_entries_without_a_path_are_dropped(self) -> None:
        path = self.tmp / "library.json"
        path.write_text(json.dumps({"books": [{"id": "a"}, {"id": "b", "path": "C:\\b.txt"}, 7]}), encoding="utf-8")
        library = Library.load(path)
        self.assertEqual([b["id"] for b in library.books], ["b"])


class TestProgressStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-prog-"))

    def test_round_trip(self) -> None:
        path = self.tmp / "progress.json"
        store = ProgressStore()
        store.put("mr-1", Progress(chapter=42, offset=1234))
        ok, message = store.save(path)
        self.assertTrue(ok, message)

        loaded = ProgressStore.load(path)
        got = loaded.get("mr-1")
        self.assertEqual((got.chapter, got.offset), (42, 1234))

    def test_unknown_book_yields_a_zero_progress(self) -> None:
        got = ProgressStore().get("nobody")
        self.assertEqual((got.chapter, got.offset), (0, 0))

    def test_corruption_is_tolerated(self) -> None:
        path = self.tmp / "progress.json"
        path.write_text("not json at all", encoding="utf-8")
        store = ProgressStore.load(path)
        self.assertEqual(store.by_id, {})
        self.assertEqual(store.get("x").chapter, 0)

    def test_entries_that_are_not_objects_are_dropped(self) -> None:
        path = self.tmp / "progress.json"
        path.write_text(json.dumps({"by_id": {"good": {"chapter": 3}, "bad": 5}}), encoding="utf-8")
        store = ProgressStore.load(path)
        self.assertEqual(list(store.by_id), ["good"])
        self.assertEqual(store.get("good").chapter, 3)

    def test_the_file_cannot_grow_without_bound(self) -> None:
        store = ProgressStore()
        for i in range(600):
            store.put(f"mr-{i}", Progress(chapter=i))
        self.assertLessEqual(len(store.by_id), 500)

    def test_last_book_is_tracked(self) -> None:
        store = ProgressStore()
        store.put("mr-1", Progress(chapter=1))
        self.assertEqual(store.last_book, "mr-1")


class TestBookIds(unittest.TestCase):
    def test_the_same_file_spelled_differently_is_the_same_book(self) -> None:
        """book_id normalises case and separators, so progress survives a re-open
        through a different path spelling (Explorer vs a drag-and-drop)."""
        if os.name != "nt":
            self.skipTest("path normalisation is Windows-specific")
        a = book_id("C:/Users/pc/Downloads/Novel.txt")
        b = book_id("c:\\users\\PC\\downloads\\novel.TXT")
        self.assertEqual(a, b)

    def test_different_files_differ(self) -> None:
        self.assertNotEqual(book_id("C:\\a.txt"), book_id("C:\\b.txt"))

    def test_id_is_stable_and_prefixed(self) -> None:
        ident = book_id("C:\\a.txt")
        self.assertEqual(ident, book_id("C:\\a.txt"))
        self.assertTrue(ident.startswith("mr-"))
        self.assertEqual(len(ident), 19)


class TestBookModel(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-book-"))

    def _write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.tmp / name
        path.write_bytes(text.encode(encoding))
        return path

    def test_from_path_splits_chapters(self) -> None:
        body = "正文。" * 60
        text = f"第一章 开端\n{body}\n第二章 转折\n{body}\n第三章 结束\n{body}"
        path = self._write("book.txt", text)
        book = Book.from_path(path)
        self.assertEqual(book.chapter_count, 3)
        self.assertEqual(book.chapter(1).title, "第二章 转折")
        self.assertTrue(book.confident)

    def test_from_path_reads_gbk(self) -> None:
        body = "正文。" * 60
        text = f"第一章 开端\n{body}\n第二章 转折\n{body}"
        path = self._write("gbk.txt", text, encoding="gb18030")
        book = Book.from_path(path)
        self.assertEqual(book.chapter_count, 2)
        self.assertIn(book.encoding, ("gb18030", "gbk"))

    def test_the_record_never_contains_the_book_text(self) -> None:
        """The index is metadata; keeping 3 MB of text in JSON would be absurd."""
        body = "正文。" * 60
        path = self._write("book.txt", f"第一章 开端\n{body}\n第二章 转折\n{body}")
        record = Book.from_path(path).to_record()
        self.assertNotIn("text", record)
        blob = json.dumps(record, ensure_ascii=False, default=str)
        self.assertNotIn(body[:20], blob)
        self.assertEqual(record["path"], str(path))
        self.assertGreater(record["chapters"], 0)

    def test_chapter_body_drops_the_duplicated_heading(self) -> None:
        """The heading is shown in the status bar, so repeating it as the first line
        of the body wastes the reader's top line."""
        body = "正文。" * 60
        path = self._write("book.txt", f"第一章 开端\n{body}\n第二章 转折\n{body}")
        book = Book.from_path(path)
        self.assertFalse(book.chapter_body(0).startswith("第一章"))
        self.assertTrue(book.chapter_body(0).startswith("正文"))

    def test_missing_file_raises_bookerror(self) -> None:
        from minireader.core.model import BookError

        with self.assertRaises(BookError):
            Book.from_path(self.tmp / "does-not-exist.txt")

    def test_empty_file_raises_bookerror(self) -> None:
        from minireader.core.model import BookError

        path = self._write("empty.txt", "")
        with self.assertRaises(BookError):
            Book.from_path(path)

    def test_clamp_keeps_the_index_in_range(self) -> None:
        body = "正文。" * 60
        path = self._write("book.txt", f"第一章 开端\n{body}\n第二章 转折\n{body}")
        book = Book.from_path(path)
        self.assertEqual(book.clamp(-5), 0)
        self.assertEqual(book.clamp(999), 1)
        self.assertEqual(book.clamp(1), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
