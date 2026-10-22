"""Hotkey parsing, Qt-keypress translation, and bulk registration.

The important tests here are the two halves agreeing. The settings dialog turns a Qt
keypress into a spec (`spec_from_qt`), and the registration path turns a spec back into
Win32 modifiers and a virtual-key code (`parse_hotkey`). If those two disagree, the
dialog happily records a binding that can never be registered - and the user only finds
out when they press the key and nothing happens.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from minireader.hotkeys import (  # noqa: E402
    Conflict,
    HotkeyError,
    NullHotkeys,
    find_conflicts,
    format_hotkey,
    parse_hotkey,
    parse_to_vks,
    register_all,
    spec_from_qt,
)
from minireader.qt import Qt, enum_value  # noqa: E402
from minireader.storage import HOTKEY_FIELDS, Settings  # noqa: E402


def qt(name: str) -> int:
    """A Qt key enum by name, tolerating Qt 5's flat namespace and Qt 6's nesting.

    `enum_value()` rather than `int()`: Qt 6's scoped enums are not int subclasses,
    so `int(Qt.Key_A)` raises TypeError on PySide6 6.12.
    """
    value = getattr(Qt, name, None)
    if value is None:
        value = getattr(Qt.Key, name.split("_", 1)[1], None)
    assert value is not None, f"this binding has no {name}"
    return enum_value(value)


def mod(name: str) -> int:
    value = getattr(Qt, name, None)
    if value is None:
        value = getattr(Qt.KeyboardModifier, name, None)
    assert value is not None, f"this binding has no {name}"
    return enum_value(value)


class TestParseHotkey(unittest.TestCase):
    def test_ctrl_shift_backtick_is_the_shipped_default(self) -> None:
        mods, vk = parse_hotkey("Ctrl+Shift+`")
        self.assertTrue(mods & 0x0002, "MOD_CONTROL")
        self.assertTrue(mods & 0x0004, "MOD_SHIFT")
        self.assertEqual(vk, 0xC0, "VK_OEM_3")

    def test_parsing_is_case_insensitive_and_ignores_spaces(self) -> None:
        self.assertEqual(parse_hotkey("ctrl + shift + `"), parse_hotkey("CTRL+SHIFT+`"))

    def test_a_bare_letter_is_refused(self) -> None:
        """RegisterHotKey would accept it and steal that key from every other program."""
        with self.assertRaises(HotkeyError):
            parse_hotkey("A")

    def test_a_bare_function_key_is_allowed(self) -> None:
        mods, vk = parse_hotkey("F9")
        self.assertEqual(mods, 0)
        self.assertEqual(vk, 0x78, "VK_F9")

    def test_a_bare_name_key_is_refused(self) -> None:
        with self.assertRaises(HotkeyError):
            parse_hotkey("space")

    def test_nonsense_is_refused(self) -> None:
        for bad in ("", "   ", "Ctrl", "Ctrl+", "+", "Ctrl+Banana", "Hyper+A"):
            with self.assertRaises(HotkeyError, msg=bad):
                parse_hotkey(bad)

    def test_a_repeated_modifier_is_accepted_as_the_same_bit(self) -> None:
        """Harmless duplication: the dialog builds specs from flag bits, so it cannot
        produce this, and OR-ing the same bit twice changes nothing."""
        self.assertEqual(parse_hotkey("Ctrl+Ctrl+A"), parse_hotkey("Ctrl+A"))

    def test_function_keys_are_all_supported(self) -> None:
        for index in range(1, 25):
            mods, vk = parse_hotkey(f"Ctrl+F{index}")
            self.assertTrue(mods & 0x0002)
            self.assertGreater(vk, 0)

    def test_modifier_only_aliases(self) -> None:
        self.assertEqual(parse_hotkey("Control+A"), parse_hotkey("Ctrl+A"))
        self.assertEqual(parse_hotkey("Win+A"), parse_hotkey("Meta+A"))


class TestFormatHotkey(unittest.TestCase):
    def test_the_default_round_trips(self) -> None:
        self.assertEqual(format_hotkey("ctrl+shift+`"), "Ctrl+Shift+`")

    def test_unknown_specs_are_returned_unchanged_rather_than_raising(self) -> None:
        """This runs while drawing the UI, where raising would take the window down."""
        self.assertEqual(format_hotkey("Ctrl+Banana"), "Ctrl+Banana")
        self.assertEqual(format_hotkey(""), "")

    def test_none_does_not_explode(self) -> None:
        self.assertEqual(format_hotkey(None), "")


class TestSpecFromQt(unittest.TestCase):
    """The dialog's half of the contract."""

    def test_a_chord_becomes_a_registerable_spec(self) -> None:
        spec = spec_from_qt(qt("Key_A"), mod("ControlModifier") | mod("ShiftModifier"))
        self.assertIsNotNone(spec)
        self.assertEqual(parse_hotkey(spec), parse_hotkey("Ctrl+Shift+A"))

    def test_every_modifier_is_translated(self) -> None:
        spec = spec_from_qt(
            qt("Key_A"),
            mod("ControlModifier") | mod("AltModifier") | mod("ShiftModifier") | mod("MetaModifier"),
        )
        self.assertEqual(parse_hotkey(spec), parse_hotkey("Ctrl+Alt+Shift+Win+A"))

    def test_the_backtick_default_can_be_typed_back_in(self) -> None:
        spec = spec_from_qt(qt("Key_QuoteLeft"), mod("ControlModifier") | mod("ShiftModifier"))
        self.assertEqual(parse_hotkey(spec), parse_hotkey("Ctrl+Shift+`"))

    def test_function_keys_and_navigation_keys(self) -> None:
        for name, expected in (
            ("Key_F9", "Ctrl+F9"),
            ("Key_Home", "Ctrl+Home"),
            ("Key_PageDown", "Ctrl+PageDown"),
            ("Key_Left", "Ctrl+Left"),
            ("Key_Space", "Ctrl+Space"),
        ):
            spec = spec_from_qt(qt(name), mod("ControlModifier"))
            self.assertIsNotNone(spec, name)
            self.assertEqual(format_hotkey(spec), expected)

    def test_digits_and_punctuation(self) -> None:
        for name, expected in (
            ("Key_1", "Ctrl+1"),
            ("Key_Minus", "Ctrl+-"),
            ("Key_BracketLeft", "Ctrl+["),
            ("Key_Comma", "Ctrl+,"),
        ):
            spec = spec_from_qt(qt(name), mod("ControlModifier"))
            self.assertEqual(format_hotkey(spec), expected, name)

    def test_a_modifier_on_its_own_has_no_spec(self) -> None:
        for name in ("Key_Control", "Key_Shift", "Key_Alt", "Key_Meta"):
            self.assertIsNone(spec_from_qt(qt(name), 0), name)

    def test_an_unnamed_key_has_no_spec(self) -> None:
        self.assertIsNone(spec_from_qt(0xFFFFFF, mod("ControlModifier")))

    def test_a_bare_letter_is_returned_unvalidated_on_purpose(self) -> None:
        """So the dialog can show the user what they pressed and explain the refusal,
        rather than appearing to ignore the keystroke."""
        spec = spec_from_qt(qt("Key_A"), 0)
        self.assertEqual(spec, "a")
        with self.assertRaises(HotkeyError):
            parse_hotkey(spec)

    def test_a_bare_function_key_is_returned_and_is_registerable(self) -> None:
        spec = spec_from_qt(qt("Key_F9"), 0)
        self.assertEqual(spec, "f9")
        self.assertEqual(parse_hotkey(spec)[1], 0x78)


class FakeManager:
    """Records what `register_all` asked for, and in what order."""

    def __init__(self, verdicts: dict[str, tuple[bool, str]] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.unregistered = 0
        self._verdicts = verdicts or {}

    def unregister_all(self) -> None:
        self.unregistered += 1
        self.calls.clear()

    def register(self, name: str, spec: str, hwnd: int) -> tuple[bool, str]:
        self.calls.append((name, spec, hwnd))
        return self._verdicts.get(name, (True, format_hotkey(spec)))


class TestRegisterAll(unittest.TestCase):
    def test_everything_bound_is_registered_in_order(self) -> None:
        manager = FakeManager()
        results = register_all(
            manager,
            {"boss_key": "Ctrl+Shift+`", "hotkey_next": "Ctrl+Right"},
            hwnd=1234,
        )
        self.assertEqual([name for name, _, _ in manager.calls], ["boss_key", "hotkey_next"])
        self.assertTrue(all(ok for ok, _ in results.values()))
        self.assertEqual(manager.calls[0][2], 1234)

    def test_it_replaces_the_previous_set(self) -> None:
        """Re-registering after a settings change must not leave the old chords live."""
        manager = FakeManager()
        register_all(manager, {"boss_key": "Ctrl+Shift+`"}, hwnd=1)
        register_all(manager, {"boss_key": "Alt+`"}, hwnd=1)
        self.assertEqual(manager.unregistered, 2)
        self.assertEqual([spec for _, spec, _ in manager.calls], ["Alt+`"])

    def test_unbound_entries_are_skipped(self) -> None:
        manager = FakeManager()
        results = register_all(
            manager, {"boss_key": "Ctrl+Shift+`", "hotkey_next": "", "hotkey_prev": "   "}, hwnd=1
        )
        self.assertEqual([name for name, _, _ in manager.calls], ["boss_key"])
        self.assertNotIn("hotkey_next", results)

    def test_no_bindings_at_all_still_clears(self) -> None:
        manager = FakeManager()
        self.assertEqual(register_all(manager, {}, hwnd=1), {})
        self.assertEqual(manager.unregistered, 1)

    def test_an_invalid_spec_fails_without_stopping_the_others(self) -> None:
        manager = FakeManager()
        results = register_all(
            manager,
            {"boss_key": "Ctrl+Shift+`", "hotkey_next": "Banana", "hotkey_prev": "Ctrl+Left"},
            hwnd=1,
        )
        self.assertTrue(results["boss_key"][0])
        self.assertFalse(results["hotkey_next"][0])
        self.assertIn("banana", results["hotkey_next"][1].lower())
        self.assertTrue(results["hotkey_prev"][0], "a bad sibling must not be fatal")
        self.assertEqual([name for name, _, _ in manager.calls], ["boss_key", "hotkey_prev"])

    def test_a_duplicate_chord_is_caught_before_windows_sees_it(self) -> None:
        """Win32 would report the second as "held by another program" (1409), which sends
        the user looking for a phantom application instead of their own two bindings."""
        manager = FakeManager()
        results = register_all(
            manager,
            {"boss_key": "Ctrl+Shift+`", "hotkey_next": "ctrl + shift + `"},
            hwnd=1,
            labels={"boss_key": "老板键", "hotkey_next": "下一章"},
        )
        self.assertTrue(results["boss_key"][0])
        self.assertFalse(results["hotkey_next"][0])
        self.assertIn("老板键", results["hotkey_next"][1], "name the chord it collided with")
        self.assertEqual(len(manager.calls), 1, "the duplicate must never reach the OS")

    def test_different_chords_are_not_flagged_as_duplicates(self) -> None:
        manager = FakeManager()
        results = register_all(
            manager,
            {"hotkey_next": "Ctrl+Right", "hotkey_prev": "Ctrl+Left"},
            hwnd=1,
        )
        self.assertTrue(all(ok for ok, _ in results.values()))

    def test_the_same_key_with_different_modifiers_is_not_a_duplicate(self) -> None:
        manager = FakeManager()
        results = register_all(
            manager, {"a": "Ctrl+A", "b": "Ctrl+Shift+A", "c": "Alt+A"}, hwnd=1
        )
        self.assertTrue(all(ok for ok, _ in results.values()))

    def test_a_real_registration_failure_is_reported_verbatim(self) -> None:
        manager = FakeManager({"boss_key": (False, "这个组合键已经被别的程序占用了，换一个")})
        results = register_all(manager, {"boss_key": "Ctrl+Shift+`"}, hwnd=1)
        self.assertFalse(results["boss_key"][0])
        self.assertIn("占用", results["boss_key"][1])

    def test_it_works_with_the_non_windows_manager(self) -> None:
        """`NullHotkeys` has the same two methods, so the same code path serves both."""
        results = register_all(NullHotkeys(lambda name: None), {"boss_key": "Ctrl+Shift+`"}, hwnd=0)
        self.assertIn("boss_key", results)


class TestParseToVks(unittest.TestCase):
    """唤醒键是**轮询**的，不是注册的 —— 所以它允许纯修饰键。

    parse_hotkey 存在的目的是喂 RegisterHotKey，纯修饰键会被它拒掉（"会抢走全系统的
    输入"）；轮询只观察、不占用，那条规则不适用。这两个函数的分工就是本组测试。
    """

    def test_a_pure_modifier_chord_parses(self) -> None:
        self.assertEqual(parse_to_vks("Ctrl+Shift"), [0x11, 0x10])

    def test_a_bare_key_parses(self) -> None:
        self.assertEqual(parse_to_vks("F8"), [0x77])

    def test_the_usual_chord_uses_the_same_key_table(self) -> None:
        self.assertEqual(parse_to_vks("Ctrl+`"), [0x11, 0xC0])

    def test_empty_means_no_wake_key(self) -> None:
        self.assertEqual(parse_to_vks(""), [])
        self.assertEqual(parse_to_vks("   "), [])
        self.assertEqual(parse_to_vks(None), [])

    def test_whitespace_and_case_do_not_matter(self) -> None:
        self.assertEqual(parse_to_vks(" ctrl + SHIFT "), [0x11, 0x10])

    def test_an_unknown_token_still_raises(self) -> None:
        with self.assertRaises(HotkeyError) as ctx:
            parse_to_vks("Ctrl+Banana")
        self.assertIn("banana", str(ctx.exception).lower())


class TestFindConflicts(unittest.TestCase):
    """内部去重：只回答"这次改动的键里有没有两个落在同一个组合键上"。"""

    def test_a_collision_between_the_wake_key_and_a_registered_chord(self) -> None:
        keys = {"stealth_wake_key": "Ctrl+Shift+L", "stealth_mode_key": "Ctrl+Shift+L"}
        conflicts = find_conflicts(
            ["stealth_wake_key"],
            keys,
            {"stealth_wake_key": "唤醒键", "stealth_mode_key": "隐私模式"},
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].names, ("stealth_wake_key", "stealth_mode_key"))
        self.assertEqual(conflicts[0].specs, ("Ctrl+Shift+L", "Ctrl+Shift+L"))
        self.assertIn("唤醒键", conflicts[0].message)
        self.assertIn("隐私模式", conflicts[0].message)
        self.assertIn("同一个组合键", conflicts[0].message)

    def test_the_shipped_default_wake_key_is_not_a_conflict(self) -> None:
        """Ctrl+Shift 是 Ctrl+Shift+` 的前半段，但这是出厂配置：按"重复"处理会让
        恢复默认 + 确定永远存不下去。轮询只在窗口藏着时进行，老板键又会先退出隐私模式。"""
        keys = {
            "stealth_wake_key": "Ctrl+Shift",
            "boss_key": "Ctrl+Shift+`",
            "stealth_mode_key": "Ctrl+Shift+L",
        }
        self.assertEqual(find_conflicts(["stealth_wake_key"], keys), [])

    def test_only_a_changed_name_can_start_a_conflict(self) -> None:
        keys = {"a": "Ctrl+A", "b": "Ctrl+A"}
        self.assertEqual(find_conflicts([], keys), [])
        self.assertEqual(find_conflicts(["c"], {**keys, "c": "Ctrl+B"}), [])

    def test_a_pair_is_reported_once_even_when_both_sides_changed(self) -> None:
        keys = {"a": "Ctrl+A", "b": "Ctrl+A"}
        conflicts = find_conflicts({"a": "Ctrl+A", "b": "Ctrl+A"}, keys)
        self.assertEqual(len(conflicts), 1)

    def test_changed_may_be_a_mapping_of_new_specs(self) -> None:
        keys = {"a": "Ctrl+A", "b": "Ctrl+B"}
        conflicts = find_conflicts({"b": "Ctrl+A"}, keys)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].names, ("b", "a"))

    def test_the_same_key_with_different_modifiers_is_not_a_conflict(self) -> None:
        keys = {"a": "Ctrl+A", "b": "Ctrl+Shift+A"}
        self.assertEqual(find_conflicts(["a", "b"], keys), [])

    def test_empty_and_broken_specs_are_skipped(self) -> None:
        keys = {"a": "", "b": "Banana", "c": "Ctrl+A"}
        self.assertEqual(find_conflicts(["a", "b"], keys), [])
        # 空的那一行不该把和它无关的 c 也拖进来。
        self.assertEqual(find_conflicts(["a"], keys), [])

    def test_labels_fall_back_to_field_names(self) -> None:
        conflicts = find_conflicts(["x"], {"x": "Ctrl+A", "y": "Ctrl+A"})
        self.assertIn("「x」", conflicts[0].message)
        self.assertIn("「y」", conflicts[0].message)

    def test_the_result_is_a_conflict_dataclass(self) -> None:
        conflict = find_conflicts(["x"], {"x": "Ctrl+A", "y": "Ctrl+A"})[0]
        self.assertIsInstance(conflict, Conflict)


class _StubWindow:
    """Records which window action a dispatched chord reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def toggle_boss(self) -> None:
        self.calls.append("boss")

    def toggle_stealth_mode(self) -> None:
        self.calls.append("stealth")

    def step_chapter(self, delta: int) -> None:
        self.calls.append(f"chapter{delta:+d}")

    def set_opacity_percent(self, percent: int) -> None:
        self.calls.append(f"opacity{percent}")


class TestHotkeyDispatch(unittest.TestCase):
    """The dispatcher table is keyed by Settings field names.

    This is a regression guard, not decoration: it used to be keyed by short names
    ("boss", "next"), which can never equal what `register_all` registered
    ("boss_key", "hotkey_next") - so every global hotkey press did nothing, silently.
    Nothing caught it because the dispatcher was a closure inside `main()`.
    """

    def actions(self, settings=None):
        from minireader.__main__ import hotkey_actions

        self.window = _StubWindow()
        self.settings = settings or Settings()
        return hotkey_actions(self.window, self.settings)

    def test_every_registered_hotkey_has_an_action(self) -> None:
        actions = self.actions()
        self.assertEqual(
            set(actions), {name for name, _field, _label in HOTKEY_FIELDS}
        )

    def test_the_two_hiding_chords_reach_their_own_actions(self) -> None:
        """Two chords, two meanings: one hides the window, one does not."""
        actions = self.actions()
        actions["boss_key"]()
        actions["stealth_mode_key"]()
        self.assertEqual(self.window.calls, ["boss", "stealth"])

    def test_chapter_chords_carry_a_direction(self) -> None:
        actions = self.actions()
        actions["hotkey_next"]()
        actions["hotkey_prev"]()
        self.assertEqual(self.window.calls, ["chapter+1", "chapter-1"])

    def test_the_opacity_chords_clamp_and_use_the_live_value(self) -> None:
        """The value is read when the chord fires, not when the table was built."""
        settings = Settings(opacity=95)
        actions = self.actions(settings)
        actions["hotkey_opacity_up"]()
        self.assertEqual(self.window.calls, ["opacity100"], "上限 100")
        settings.opacity = 35
        actions["hotkey_opacity_down"]()
        self.assertEqual(self.window.calls[-1], "opacity30", "下限 30")



if __name__ == "__main__":
    unittest.main()
