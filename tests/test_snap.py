"""Edge snapping and off-screen clamping. Pure geometry, no Qt and no window.

These tests are the specification: each one names a behaviour a reader would notice if
it regressed. All coordinates are in Qt logical pixels - the same space the window
reports - which is the whole reason `snap.py` takes work areas as an argument instead of
asking Windows for them (GetMonitorInfoW answers in physical pixels, and mixing the two
breaks on any scaled display).
"""

from __future__ import annotations

import unittest

from minireader import snap


class TestOverlapArea(unittest.TestCase):
    def test_identical_rects(self) -> None:
        self.assertEqual(snap.overlap_area((0, 0, 10, 10), (0, 0, 10, 10)), 100)

    def test_disjoint_rects(self) -> None:
        self.assertEqual(snap.overlap_area((0, 0, 10, 10), (20, 0, 10, 10)), 0)

    def test_touching_edges_do_not_overlap(self) -> None:
        self.assertEqual(snap.overlap_area((0, 0, 10, 10), (10, 0, 10, 10)), 0)

    def test_partial_overlap(self) -> None:
        self.assertEqual(snap.overlap_area((0, 0, 10, 10), (5, 5, 10, 10)), 25)


class TestActiveWorkArea(unittest.TestCase):
    ONE = [(0, 0, 1920, 1040)]
    TWO = [(0, 0, 1920, 1040), (1920, 0, 2560, 1400)]

    def test_single_screen_is_used_whatever_the_position(self) -> None:
        for x, y in ((0, 0), (900, 500), (5000, 5000), (-400, -400)):
            self.assertEqual(snap.active_work_area(x, y, 480, 360, self.ONE), self.ONE[0])

    def test_the_screen_containing_the_centre_wins(self) -> None:
        # Straddling the seam but mostly on the right screen.
        area = snap.active_work_area(1800, 200, 480, 360, self.TWO)
        self.assertEqual(area, self.TWO[1])

    def test_a_mostly_off_screen_window_still_picks_the_nearest_screen(self) -> None:
        area = snap.active_work_area(1900, 3900, 480, 360, self.TWO)
        self.assertIsNotNone(area)

    def test_no_screens_is_not_a_crash(self) -> None:
        self.assertIsNone(snap.active_work_area(0, 0, 480, 360, []))


class TestSnapPosition(unittest.TestCase):
    AREA = [(0, 0, 1920, 1040)]

    def snap(self, x, y, distance=14, w=480, h=360):
        return snap.snap_position(x, y, w, h, self.AREA, distance)

    def test_a_window_in_the_middle_does_not_move(self) -> None:
        self.assertEqual(self.snap(700, 400), (700, 400))

    def test_left_edge(self) -> None:
        self.assertEqual(self.snap(6, 400), (0, 400))

    def test_right_edge(self) -> None:
        self.assertEqual(self.snap(1920 - 480 - 6, 400), (1920 - 480, 400))

    def test_top_edge(self) -> None:
        self.assertEqual(self.snap(700, 9), (700, 0))

    def test_bottom_edge_accounts_for_the_taskbar(self) -> None:
        """The bottom edge is the work area's bottom, not the screen's."""
        self.assertEqual(self.snap(700, 1040 - 360 - 3), (700, 1040 - 360))

    def test_all_four_corners(self) -> None:
        self.assertEqual(self.snap(5, 5), (0, 0))
        self.assertEqual(self.snap(1920 - 485, 5), (1920 - 480, 0))
        self.assertEqual(self.snap(5, 1040 - 365), (0, 1040 - 360))
        self.assertEqual(self.snap(1920 - 485, 1040 - 365), (1920 - 480, 1040 - 360))

    def test_just_outside_the_distance_is_a_no_op(self) -> None:
        self.assertEqual(self.snap(15, 400), (15, 400))
        self.assertEqual(self.snap(14, 400), (0, 400))

    def test_distance_zero_disables_the_pull_but_keeps_an_exact_edge(self) -> None:
        self.assertEqual(self.snap(1, 400, distance=0), (1, 400))
        self.assertEqual(self.snap(0, 400, distance=0), (0, 400))

    def test_axes_are_independent(self) -> None:
        """Near the left edge and the top edge at once: both should engage."""
        self.assertEqual(self.snap(7, 7), (0, 0))

    def test_a_window_wider_than_the_screen_still_snaps_to_the_left(self) -> None:
        x, y = snap.snap_position(5, 5, 3000, 500, self.AREA, 14)
        self.assertEqual(x, 0)

    def test_the_nearest_screen_is_the_one_snapped_to(self) -> None:
        two = [(0, 0, 1920, 1040), (1920, 0, 2560, 1400)]
        # Sitting near the left edge of the *second* screen.
        x, y = snap.snap_position(1930, 700, 480, 360, two, 14)
        self.assertEqual(x, 1920, "should snap to the second screen's left edge")
        self.assertEqual(y, 700)

    def test_no_screens_leaves_the_position_alone(self) -> None:
        self.assertEqual(snap.snap_position(5, 5, 480, 360, [], 14), (5, 5))


class TestReachable(unittest.TestCase):
    """Per-axis, not per-area.

    Judging by overlap *area* gets the important cases backwards: a 480x10 sliver of
    title bar at the top of a screen has a tiny area but is a perfectly good drag
    handle, while a 48x48 corner of a window that is otherwise off the left edge is not
    draggable (you cannot resize from a corner you cannot reach the opposite side of).
    """

    AREA = (0, 0, 1920, 1040)

    def test_a_normal_window_is_reachable(self) -> None:
        self.assertTrue(snap.reachable((100, 100, 480, 360), self.AREA))

    def test_a_title_bar_sliver_is_reachable(self) -> None:
        self.assertTrue(snap.reachable((860, 0, 480, 10), self.AREA, min_visible=48, min_grab=10))

    def test_a_48px_strip_is_a_usable_handle(self) -> None:
        self.assertTrue(snap.reachable((-432, 500, 480, 360), self.AREA, min_visible=48, min_grab=10))

    def test_less_than_48px_visible_is_not_reachable(self) -> None:
        self.assertFalse(snap.reachable((-460, 500, 480, 360), self.AREA, min_visible=48, min_grab=10))

    def test_fully_off_screen_is_not_reachable(self) -> None:
        self.assertFalse(snap.reachable((3000, 3000, 480, 360), self.AREA))

    def test_a_window_taller_than_the_screen_is_still_reachable(self) -> None:
        self.assertTrue(snap.reachable((500, 0, 480, 3000), self.AREA))


class TestClampPosition(unittest.TestCase):
    AREA = [(0, 0, 1920, 1040)]

    def clamp(self, x, y, w=480, h=360):
        return snap.clamp_position(x, y, w, h, self.AREA)

    def test_an_ordinary_position_is_untouched(self) -> None:
        self.assertEqual(self.clamp(700, 400), (700, 400))

    def test_far_off_the_right_ends_up_grabbable(self) -> None:
        """The contract is recoverability, not "fully on screen": 48 px of the window
        left showing at the edge is enough to drag it back, and clamping any harder
        would fight a deliberate half-off-screen placement."""
        x, y = self.clamp(5000, 400)
        self.assertTrue(snap.reachable((x, y, 480, 360), self.AREA[0]))
        self.assertEqual(x, 1920 - 48)
        self.assertEqual(y, 400, "the other axis must not be disturbed")

    def test_far_above_the_top_is_pulled_down(self) -> None:
        _, y = self.clamp(700, -500)
        self.assertGreaterEqual(y, 0)

    def test_a_partially_visible_window_keeps_its_position(self) -> None:
        """Clamping must not fight a deliberate half-off-screen placement."""
        self.assertEqual(self.clamp(-200, 400), (-200, 400))

    def test_far_left_is_clamped_so_some_of_it_shows(self) -> None:
        x, _ = self.clamp(-4000, 400)
        self.assertTrue(snap.reachable((x, 400, 480, 360), self.AREA[0]))

    def test_the_result_always_lies_between_the_two_extremes(self) -> None:
        for x in (-4000, -500, 5000, 40000):
            got, _ = self.clamp(x, 400)
            self.assertGreaterEqual(got, -480 + 48)
            self.assertLessEqual(got, 1920 - 48)

    def test_no_screens_is_not_a_crash(self) -> None:
        self.assertEqual(snap.clamp_position(5, 6, 480, 360, []), (5, 6))


if __name__ == "__main__":
    unittest.main()
