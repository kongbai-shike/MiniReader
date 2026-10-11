"""Contrast audit of the palette.

This is a real gate, not decoration. The `muted` colour shipped initially at #7b8093 on
the #191b24 panel, which measures 4.37:1 - below the 4.5:1 WCAG AA floor for normal
text - and it is used for the status bar, the hint label and dialog secondary text, all
of them small. These tests are what keeps that from coming back.
"""

from __future__ import annotations

import unittest

from minireader.ui import theme


class TestContrastMath(unittest.TestCase):
    def test_black_on_white_is_the_maximum(self) -> None:
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=2)

    def test_a_colour_against_itself_is_one(self) -> None:
        for colour in theme.PALETTE.values():
            self.assertAlmostEqual(theme.contrast_ratio(colour, colour), 1.0, places=6)

    def test_the_ratio_is_symmetric(self) -> None:
        a = theme.contrast_ratio(theme.PALETTE["fg"], theme.PALETTE["bg"])
        b = theme.contrast_ratio(theme.PALETTE["bg"], theme.PALETTE["fg"])
        self.assertAlmostEqual(a, b, places=9)

    def test_relative_luminance_endpoints(self) -> None:
        self.assertAlmostEqual(theme.relative_luminance("#000000"), 0.0, places=6)
        self.assertAlmostEqual(theme.relative_luminance("#ffffff"), 1.0, places=6)

    def test_shorthand_and_case_are_accepted(self) -> None:
        self.assertAlmostEqual(
            theme.contrast_ratio("#fff", "#000"), theme.contrast_ratio("#FFFFFF", "#000000")
        )

    def test_a_rejected_colour_raises_rather_than_guessing(self) -> None:
        """Silently treating a typo as black would turn a contrast check into a
        rubber stamp, which is worse than a hard failure."""
        for bad in ("", "red", "#12345", "#1234567", "#gggggg", "  ", None):
            with self.assertRaises(ValueError):
                theme.contrast_ratio(bad, "#000000")

    def test_the_hash_is_optional_and_whitespace_is_ignored(self) -> None:
        self.assertAlmostEqual(
            theme.relative_luminance("  a0b0c0  "), theme.relative_luminance("#a0b0c0")
        )


class TestPalettePassesAA(unittest.TestCase):
    """Every audited pair, at the threshold for its size."""

    def test_the_audit_covers_every_pair(self) -> None:
        self.assertEqual(len(theme.audit()), len(theme.TEXT_PAIRS))
        self.assertGreaterEqual(len(theme.TEXT_PAIRS), 9)

    def test_every_pair_passes_its_threshold(self) -> None:
        for role, ratio, passes in theme.audit():
            floor = theme.AA_LARGE if "large" in role else theme.AA_NORMAL
            self.assertTrue(passes, f"{role} is {ratio:.2f}:1, below {floor}:1")
            self.assertGreaterEqual(round(ratio, 2), floor, role)

    def test_the_worst_pair_is_reported_and_still_passes(self) -> None:
        """Names the tightest case so a future palette tweak knows what it is up against."""
        role, ratio, _ = min(theme.audit(), key=lambda row: row[1])
        self.assertGreaterEqual(ratio, theme.AA_NORMAL, f"tightest pair is {role} at {ratio:.2f}:1")

    def test_the_old_muted_colour_would_have_failed(self) -> None:
        """A regression guard with teeth: the exact value that shipped, against the exact
        surface it was drawn on. If someone "restores" it, this fails."""
        self.assertLess(theme.contrast_ratio("#7b8093", "#191b24"), theme.AA_NORMAL)

    def test_the_replacement_muted_colour_passes(self) -> None:
        self.assertGreaterEqual(
            theme.contrast_ratio(theme.PALETTE["muted"], theme.PALETTE["panel"]), theme.AA_NORMAL
        )


class TestPaletteShape(unittest.TestCase):
    def test_every_colour_is_a_hex_string(self) -> None:
        for name, colour in theme.PALETTE.items():
            self.assertRegex(colour, r"^#[0-9a-f]{6}$", name)

    def test_the_keys_the_stylesheets_use_exist(self) -> None:
        """Both stylesheets address the palette by key at import time, so a missing key
        is an ImportError rather than a wrong colour. This pins the contract."""
        for key in ("bg", "panel", "field", "hover", "border", "fg", "muted", "warn", "accent"):
            self.assertIn(key, theme.PALETTE)

    def test_thresholds_are_the_wcag_values(self) -> None:
        self.assertEqual(theme.AA_NORMAL, 4.5)
        self.assertEqual(theme.AA_LARGE, 3.0)


if __name__ == "__main__":
    unittest.main()
