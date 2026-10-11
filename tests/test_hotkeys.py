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
    HotkeyError,
    NullHotkeys,
    format_hotkey,
    parse_hotkey,
    register_all,
    spec_from_qt,
)
from minireader.qt import Qt  # noqa: E402


def qt(name: str) -> int:
    """A Qt key enum by name, tolerating Qt 5's flat namespace and Qt 6's nesting."""
    value = getattr(Qt, name, None)
    if value is None:
        value = getattr(Qt.Key, name.split("_", 1)[1], None)
    assert value is not None, f"this binding has no {name}"
    return int(value)


def mod(name: str) -> int:
    value = getattr(Qt, name, None)
    if value is None:
        value = getattr(Qt.KeyboardModifier, name, None)
    assert value is not None, f"this binding has no {name}"
    return int(value)


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


if __name__ == "__main__":
    unittest.main()
