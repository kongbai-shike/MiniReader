"""自动滚动引擎：速度夹取、按时间累计、暂停/恢复、到底自停。

全部用手推的假时钟驱动，不用 QTest.qWait 也不用 sleep——真实的 40 ms 定时器在负载
下可能 200 ms 才到，"每拍加固定像素"这种错误正好会被"等久一点再看"的宽松断言盖住。
唯一一个真定时器用例（TestRealTimer）只负责证明 timeout 接线是通的。

滚动条是**真的 QScrollBar**，取自 QScrollArea（shim 没有导出 QScrollBar 这个名字）。
offscreen 是真正的 QPA 插件，setValue/maximum 走的是 Qt 自己的实现，像素位移因此可以
当真断言；范围用 setRange 钉死，测试不依赖字体和文档排版。

    py -3.12 -m unittest tests.test_autoscroll -v
"""

from __future__ import annotations

import math
import os
import sys
import time
import unittest

# 必须在 minireader.qt 导入 Qt 之前设置。setdefault：调用方显式指定的平台优先。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.qt import (  # noqa: E402
    QApplication,
    QEvent,
    QObject,
    QScrollArea,
    install_message_filter,
)
from minireader.ui.autoscroll import (  # noqa: E402
    DEFAULT_SPEED,
    MAX_SPEED,
    MIN_SPEED,
    AutoScroller,
)

# offscreen 没有自带字体、也没有窗口管理器，每次都会念这两句。别的仍然照常打印，
# 所以真正的 Qt 抱怨不会被藏起来。
_NOISE = ("QFontDatabase", "does not support raise", "This plugin does not support")
install_message_filter(_NOISE)

# 整个进程只有一个 QApplication，Qt 不允许第二个。
_app = QApplication.instance() or QApplication([])


class FakeClock:
    """手推的时钟。`set_now` 可以直接改，用来模拟时钟回拨或给出 NaN。

    两条刻意的规矩：

    * **读数取整到微秒。** 真时钟给的是干净的读数，不是浮点累加出来的尾巴。
    * **从 0 起算。** 真实的 monotonic 基数很大（开机以来的秒数），而 1000.04 - 1000.0
      在 double 里是 0.03999999999996……，乘上速度正好把 4.0 压成 3.9999，于是
      "第 2 拍走了 4 像素"这种断言变成了掷骰子。累计行为另有几百拍不均匀步长的用例把关。
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = 0.0
        self.set_now(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.set_now(self.now + float(seconds))

    def set_now(self, value: float) -> None:
        number = float(value)
        # NaN/inf 原样留着：那是"时钟坏了"那几条用例要喂进去的东西。
        self.now = round(number * 1_000_000) / 1_000_000 if math.isfinite(number) else number


class ScrollerTestCase(unittest.TestCase):
    """一条真的 QScrollBar + 一个手推的假时钟，外加信号记录。"""

    def setUp(self) -> None:
        self.clock = FakeClock()
        # 宿主对象必须保活：QScrollArea 被回收，它的滚动条会跟着被删掉，之后所有
        # setValue 都会变成 "wrapped C/C++ object has been deleted"。
        self._alive: list[object] = []

    def tearDown(self) -> None:
        # 这个基类以前没有 tearDown：start() 过的滚动器带着真 QTimer（10/40ms）跨用例
        # 存活，TestRealTimer 之外任何一个漏 stop 的用例都会让定时器在后续模块里继续
        # 对滚动条开火，解释器退出时还可能炸 "QScrollBar already deleted"。和
        # WindowTestCase 同一类问题：先停事件源，再强制 DeferredDelete 落地。
        for obj in self._alive:
            if isinstance(obj, AutoScroller):
                obj.stop()
            if isinstance(obj, QObject):
                obj.deleteLater()
        self._alive.clear()
        _app.processEvents()
        _app.sendPostedEvents(None, QEvent.DeferredDelete)
        _app.processEvents()

    def bar(self, maximum: int = 100000):
        """一条范围被钉死的真滚动条。maximum 默认给得很大，以免位移测试自己撞到底。"""
        area = QScrollArea()
        self._alive.append(area)
        bar = area.verticalScrollBar()
        bar.setRange(0, int(maximum))
        self._alive.append(bar)
        _app.processEvents()
        return bar

    def scroller(
        self,
        maximum: int = 100000,
        *,
        speed: float | None = None,
        interval_ms: int = 40,
        clock=None,
        start: bool = False,
    ):
        bar = self.bar(maximum)
        sc = AutoScroller(bar, interval_ms=interval_ms, clock=clock or self.clock)
        self._alive.append(sc)
        if speed is not None:
            sc.set_speed(speed)
        if start:
            sc.start()
        return sc, bar

    def record(self, sc) -> dict[str, list[int]]:
        """把三个信号都记下来。每个 lambda 用默认参数绑住自己的名字，避免共用闭包。"""
        seen: dict[str, list[int]] = {"started": [], "stopped": [], "reachedEnd": []}
        for name in seen:
            getattr(sc, name).connect(lambda _name=name: seen[_name].append(1))
        return seen

    def tick(self, sc, seconds: float) -> None:
        """推进假时钟，然后跑一拍。

        直接调 `_on_tick`：接口里没有公开的"走一拍"，而真定时器在测试里既慢又不定时——
        时序错误恰恰就藏在那种不确定性里。定时器接线本身由 TestRealTimer 负责。
        """
        self.clock.advance(seconds)
        sc._on_tick()

    def ticks(self, sc, seconds: float, count: int) -> None:
        for _ in range(count):
            self.tick(sc, seconds)


# ---------------------------------------------------------------------------
# 速度
# ---------------------------------------------------------------------------

class TestSpeedClamping(ScrollerTestCase):
    def test_the_default_speed_is_sixty(self) -> None:
        sc, _bar = self.scroller()
        self.assertEqual(sc.speed(), DEFAULT_SPEED)
        self.assertEqual(sc.speed(), 60.0)

    def test_a_speed_below_the_floor_is_raised_to_it(self) -> None:
        """低于 10 px/s 在屏幕上看不出在动，用户只会以为功能坏了。"""
        sc, _bar = self.scroller()
        for value in (0, 0.5, -1, -9999, 9.99):
            with self.subTest(value=value):
                sc.set_speed(value)
                self.assertEqual(sc.speed(), MIN_SPEED)

    def test_a_speed_above_the_ceiling_is_pulled_down_to_it(self) -> None:
        sc, _bar = self.scroller()
        for value in (401, 5000, 10**12, 1e308):
            with self.subTest(value=value):
                sc.set_speed(value)
                self.assertEqual(sc.speed(), MAX_SPEED)

    def test_a_speed_inside_the_range_is_kept_exactly(self) -> None:
        sc, _bar = self.scroller()
        for value in (10, 10.5, 60, 199.25, 400):
            with self.subTest(value=value):
                sc.set_speed(value)
                self.assertEqual(sc.speed(), float(value))

    def test_nan_falls_back_to_the_default_and_never_leaks_through(self) -> None:
        """NaN 是这里唯一真正阴的输入：min/max 和 NaN 比较永远是 False，夹取函数会
        原样把它放行，然后 int(NaN) 抛 ValueError、滚动位置变成 NaN。"""
        sc, _bar = self.scroller(start=True)
        sc.set_speed(float("nan"))
        self.assertEqual(sc.speed(), DEFAULT_SPEED)
        self.clock.advance(1.0)
        sc._on_tick()
        self.assertEqual(sc._bar.value(), int(DEFAULT_SPEED), "1 秒就是默认速度那么多像素")

    def test_infinities_are_treated_as_garbage_too(self) -> None:
        sc, _bar = self.scroller()
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                sc.set_speed(value)
                self.assertEqual(sc.speed(), DEFAULT_SPEED)

    def test_unreadable_values_fall_back_rather_than_raising(self) -> None:
        """速度会被写进 JSON、也会从设置面板来，两种地方什么都可能出现。"""
        sc, _bar = self.scroller()
        for value in (None, "快", "", object(), [], {}, b"60", "nan", float("nan")):
            with self.subTest(value=value):
                sc.set_speed(value)          # 不许抛
                self.assertEqual(sc.speed(), DEFAULT_SPEED)

    def test_a_numeric_string_is_accepted(self) -> None:
        """同 storage._as_int：能读出来的字符串就照读，对着 JSON 手改的人友好。"""
        sc, _bar = self.scroller()
        sc.set_speed("120")
        self.assertEqual(sc.speed(), 120.0)

    def test_a_bool_is_not_a_number(self) -> None:
        """True 是 int 的子类；数字字段里出现它说明写错了地方，读成 1 只会把错误藏起来。"""
        sc, _bar = self.scroller()
        sc.set_speed(True)
        self.assertEqual(sc.speed(), DEFAULT_SPEED)

    def test_a_new_speed_takes_effect_on_the_next_tick(self) -> None:
        sc, bar = self.scroller(speed=100, start=True)
        self.tick(sc, 0.1)
        self.assertEqual(bar.value(), 10)
        sc.set_speed(200)
        self.tick(sc, 0.1)
        self.assertEqual(bar.value(), 30)


# ---------------------------------------------------------------------------
# 恒定速度
# ---------------------------------------------------------------------------

class TestConstantSpeed(ScrollerTestCase):
    def test_the_total_distance_matches_speed_times_elapsed_over_uneven_ticks(self) -> None:
        """本文件的核心用例：每一拍的长短都不一样（5 ms 到 90 ms 混着来），总位移仍然
        必须等于 速度 x 真实经过的时间。按拍数计速的实现会在这里差出十几倍。"""
        sc, bar = self.scroller(speed=100, start=True)
        pattern = (0.005, 0.021, 0.04, 0.013, 0.077, 0.001, 0.033, 0.09)
        elapsed = 0.0
        for i in range(300):
            step = pattern[i % len(pattern)]
            elapsed += step
            self.tick(sc, step)
        expected = 100.0 * elapsed
        self.assertAlmostEqual(bar.value(), expected, delta=2.0)

    def test_the_distance_depends_on_time_not_on_the_number_of_ticks(self) -> None:
        """同样 1 秒，拆成 10 拍还是 1 拍，必须走到同一个地方。"""
        many, bar_many = self.scroller(speed=100, start=True)
        self.ticks(many, 0.1, 10)

        one, bar_one = self.scroller(speed=100, start=True)
        self.tick(one, 1.0)

        self.assertAlmostEqual(bar_many.value(), 100.0, delta=1.0)
        self.assertAlmostEqual(bar_one.value(), 100.0, delta=1.0)
        self.assertLessEqual(abs(bar_many.value() - bar_one.value()), 1)

    def test_a_tick_shorter_than_one_pixel_carries_its_remainder(self) -> None:
        """16 px/s、每拍 31.25 ms = 半像素。丢掉余量就等于把速度砍掉一半。

        步长取 2 的负幂：这类"精确到像素"的断言只有在时间能被 double 精确表示时才
        成立（0.05 秒并不是精确的二进制小数，累加到某一拍正好卡在 0.9999… 上）。"""
        sc, bar = self.scroller(speed=16, start=True)
        seen = []
        for _ in range(4):
            self.tick(sc, 0.03125)
            seen.append(bar.value())
        self.assertEqual(seen, [0, 1, 1, 2])

    def test_the_first_tick_measures_from_the_moment_of_start(self) -> None:
        """起跑的那一刻就该起表：定时器第一拍迟到多久，就该补多少像素，否则机器一忙
        起步那段速度就悄悄变慢。"""
        sc, bar = self.scroller(speed=100)
        sc.start()
        self.clock.advance(1.0)
        sc._on_tick()
        self.assertEqual(bar.value(), 100)

    def test_starting_never_moves_the_scrollbar(self) -> None:
        """自动滚动从读者当前的位置接着往下走；起跳一下就等于把人家的位置丢了。"""
        sc, bar = self.scroller(speed=MAX_SPEED)
        bar.setValue(1234)
        sc.start()
        self.assertEqual(bar.value(), 1234)
        self.clock.advance(1.0)
        self.assertEqual(bar.value(), 1234, "没有 tick 就不该动")

    def test_a_zero_length_tick_moves_nothing(self) -> None:
        sc, bar = self.scroller(speed=400, start=True)
        self.tick(sc, 0.0)
        self.tick(sc, 0.0)
        self.assertEqual(bar.value(), 0)

    def test_a_clock_that_goes_backwards_is_ignored(self) -> None:
        """单调时钟理论上不回拨，但注入的时钟、系统休眠恢复都可能给出负的差值；负的
        elapsed 会把位置往回带，那是比不动更糟的结果。"""
        sc, bar = self.scroller(speed=100, start=True)
        self.tick(sc, 0.125)
        self.assertEqual(bar.value(), 12, "1/8 秒 = 12.5 像素，整数部分先走，余量攒着")

        self.clock.set_now(self.clock.now - 5.0)
        sc._on_tick()
        self.assertEqual(bar.value(), 12, "回拨不会倒退")

        self.tick(sc, 0.5)
        self.assertEqual(bar.value(), 62, "然后照常接着走（含攒下的半像素）")

    def test_a_nan_clock_reading_is_ignored_and_recovers(self) -> None:
        """时钟给出 NaN 时既不能崩（int(NaN) 会抛），也不能把 NaN 灌进累加器。"""
        sc, bar = self.scroller(speed=100, start=True)
        before = self.clock.now
        self.clock.set_now(float("nan"))
        sc._on_tick()
        self.assertEqual(bar.value(), 0)
        self.assertTrue(sc._carry == sc._carry, "累加器不能变成 NaN")

        self.clock.set_now(before + 0.5)
        sc._on_tick()
        self.assertEqual(bar.value(), 50, "时钟恢复后接着算，不丢也不跳")

    def test_a_stopped_scroller_ignores_a_stray_tick(self) -> None:
        """定时器已经 stop() 还漏出一拍（正在销毁、事件队列里还压着一次超时），也不能动。"""
        sc, bar = self.scroller(speed=400, start=True)
        self.tick(sc, 0.5)
        moved = bar.value()
        sc.stop()
        self.tick(sc, 1.0)
        self.tick(sc, 1.0)
        self.assertEqual(bar.value(), moved)


# ---------------------------------------------------------------------------
# 到底
# ---------------------------------------------------------------------------

class TestReachingTheBottom(ScrollerTestCase):
    def test_the_exact_bottom_is_where_it_stops(self) -> None:
        """最后一拍不足一整段时，要走满到 maximum，而不是溢出或被截在差一点的地方。"""
        sc, bar = self.scroller(maximum=50, speed=100, start=True)
        self.tick(sc, 1.0)
        self.assertEqual(bar.value(), 50)
        self.assertEqual(bar.value(), bar.maximum())

    def test_the_end_is_reported_exactly_once(self) -> None:
        sc, bar = self.scroller(maximum=50, speed=100, start=True)
        seen = self.record(sc)
        self.tick(sc, 1.0)
        self.assertEqual(seen["reachedEnd"], [1])
        self.ticks(sc, 0.5, 10)
        self.assertEqual(seen["reachedEnd"], [1], "到底之后不许再报")
        self.assertEqual(bar.value(), 50)

    def test_the_end_stops_the_run(self) -> None:
        """到底必须真的收摊：留着定时器空转，就是关掉的窗口背后还有一个定时器。"""
        sc, _bar = self.scroller(maximum=50, speed=100, start=True)
        seen = self.record(sc)
        self.tick(sc, 1.0)
        self.assertFalse(sc.is_running())
        self.assertFalse(sc._timer.isActive())
        self.assertEqual(seen["stopped"], [1], "自动到底也是一次停下，按钮要跟着变")

    def test_nothing_is_reported_while_the_bottom_is_still_ahead(self) -> None:
        sc, bar = self.scroller(maximum=1000, speed=100, start=True)
        seen = self.record(sc)
        for _ in range(5):
            self.tick(sc, 0.5)
        self.assertEqual(seen["reachedEnd"], [])
        self.assertTrue(sc.is_running())
        self.assertLess(bar.value(), bar.maximum())

    def test_the_latch_lets_a_later_run_reach_the_end_again(self) -> None:
        """换章之后窗口会调 reset_end_latch()：新的范围要能重新走到底、重新报一次。"""
        sc, bar = self.scroller(maximum=50, speed=100, start=True)
        seen = self.record(sc)
        self.tick(sc, 1.0)

        bar.setValue(0)
        sc.reset_end_latch()
        sc.start()
        self.tick(sc, 1.0)
        self.assertEqual(seen["reachedEnd"], [1, 1])
        self.assertEqual(bar.value(), 50)

    def test_without_the_reset_the_end_is_not_reported_twice(self) -> None:
        """锁存就是为这件事存在的：一次运行只报一次，哪怕期间位置被人挪回上面。"""
        sc, bar = self.scroller(maximum=50, speed=100, start=True)
        seen = self.record(sc)
        self.tick(sc, 1.0)

        bar.setValue(0)
        sc.start()
        self.tick(sc, 1.0)
        self.assertEqual(seen["reachedEnd"], [1])
        self.assertEqual(bar.value(), 50, "该走还是要走到底，只是不再报信")

    def test_a_scrollbar_with_nothing_to_scroll_ends_at_once(self) -> None:
        """maximum == 0：没有可滚动的空间。必须当场当作到底，而不是让定时器空转。"""
        sc, bar = self.scroller(maximum=0, speed=100)
        seen = self.record(sc)
        sc.start()
        self.assertEqual(seen["reachedEnd"], [1])
        self.assertFalse(sc.is_running())
        self.assertFalse(sc._timer.isActive())
        self.assertEqual(seen["started"], [], "没真的开起来，按钮不该先亮成暂停")
        self.assertEqual(bar.value(), 0)

    def test_starting_from_the_bottom_ends_at_once(self) -> None:
        sc, bar = self.scroller(maximum=500, speed=100)
        bar.setValue(500)
        seen = self.record(sc)
        sc.start()
        self.assertEqual(seen["reachedEnd"], [1])
        self.assertFalse(sc.is_running())
        self.assertEqual(bar.value(), 500)

    def test_repeated_starts_on_an_empty_scrollbar_report_only_once(self) -> None:
        sc, _bar = self.scroller(maximum=0, speed=100)
        seen = self.record(sc)
        for _ in range(4):
            sc.start()
        self.assertEqual(seen["reachedEnd"], [1])

    def test_the_end_is_not_reported_when_someone_else_already_scrolled_to_it(self) -> None:
        """滚轮、按键也能把位置送到最底下。引擎下一拍发现自己已经在底部，同样要收摊；
        锁存保证"报一次"这条承诺对谁先到都成立。"""
        sc, bar = self.scroller(maximum=1000, speed=100, start=True)
        seen = self.record(sc)
        self.tick(sc, 0.5)
        bar.setValue(bar.maximum())
        self.tick(sc, 0.5)
        self.assertEqual(seen["reachedEnd"], [1])
        self.assertFalse(sc.is_running())


# ---------------------------------------------------------------------------
# 运行状态、暂停
# ---------------------------------------------------------------------------

class TestRunState(ScrollerTestCase):
    def test_start_twice_starts_once(self) -> None:
        sc, _bar = self.scroller(speed=100)
        seen = self.record(sc)
        sc.start()
        sc.start()
        sc.start()
        self.assertEqual(seen["started"], [1])
        self.assertTrue(sc._timer.isActive())
        self.assertTrue(sc.is_running())

    def test_stopping_when_already_stopped_is_a_no_op(self) -> None:
        sc, _bar = self.scroller(speed=100)
        seen = self.record(sc)
        sc.stop()
        sc.stop()
        self.assertEqual(seen["stopped"], [])
        self.assertFalse(sc.is_running())

    def test_start_and_stop_emit_once_each(self) -> None:
        sc, _bar = self.scroller(speed=100)
        seen = self.record(sc)
        sc.start()
        sc.stop()
        sc.start()
        sc.stop()
        self.assertEqual(seen["started"], [1, 1])
        self.assertEqual(seen["stopped"], [1, 1])

    def test_stop_leaves_the_timer_off_and_the_position_alone(self) -> None:
        sc, bar = self.scroller(speed=100, start=True)
        self.ticks(sc, 0.1, 3)
        moved = bar.value()
        self.assertTrue(sc._timer.isActive())
        sc.stop()
        self.assertFalse(sc._timer.isActive())
        self.assertEqual(bar.value(), moved)

    def test_toggle_alternates(self) -> None:
        sc, _bar = self.scroller(speed=100)
        seen = self.record(sc)
        sc.toggle()
        self.assertTrue(sc.is_running())
        sc.toggle()
        self.assertFalse(sc.is_running())
        self.assertEqual((seen["started"], seen["stopped"]), ([1], [1]))

    def test_pausing_keeps_the_running_state(self) -> None:
        """按钮靠 is_running() 决定显示"暂停"还是"自动滚动"：暂停期间它必须还是 True，
        否则按钮会跳回"启动"，读者以为滚动已经关掉了。"""
        sc, _bar = self.scroller(speed=100, start=True)
        sc.set_paused(True)
        self.assertTrue(sc.is_paused())
        self.assertTrue(sc.is_running())

    def test_pausing_is_idempotent_and_so_is_resuming(self) -> None:
        sc, _bar = self.scroller(speed=100, start=True)
        sc.set_paused(True)
        first = sc._paused_at
        self.clock.advance(2.0)
        sc.set_paused(True)
        self.assertEqual(sc._paused_at, first, "重复暂停不该把暂停起点往后挪")
        sc.set_paused(False)
        self.assertFalse(sc.is_paused())
        sc.set_paused(False)
        self.assertFalse(sc.is_paused())

    def test_a_paused_scroller_does_not_move(self) -> None:
        sc, bar = self.scroller(maximum=10000, speed=400, start=True)
        self.tick(sc, 0.5)
        moved = bar.value()
        sc.set_paused(True)
        self.ticks(sc, 1.0, 20)
        self.assertEqual(bar.value(), moved)

    def test_a_paused_scroller_never_reports_the_end(self) -> None:
        """停在半路不算读完这一章；暂停期间报到底，窗口会莫名其妙地翻页。

        20 px 的余量、128 px/s、每拍 5/32 秒 = 正好 20 像素：恢复后的第一拍就该到底。"""
        sc, bar = self.scroller(maximum=20, speed=128, start=True)
        seen = self.record(sc)
        sc.set_paused(True)
        self.ticks(sc, 5.0, 20)
        self.assertEqual(seen["reachedEnd"], [])
        self.assertTrue(sc.is_running())
        self.assertEqual(bar.value(), 0)

        sc.set_paused(False)
        self.tick(sc, 0.15625)
        self.assertEqual(seen["reachedEnd"], [1], "恢复之后照常走到底")
        self.assertEqual(bar.value(), 20)

    def test_resuming_does_not_count_the_pause_as_elapsed_time(self) -> None:
        """暂停三十秒不该在恢复的第一拍里一次性走三十秒的像素——那是肉眼可见的一跳。"""
        sc, bar = self.scroller(speed=128, start=True)
        self.tick(sc, 0.03125)
        self.tick(sc, 0.03125)
        self.assertEqual(bar.value(), 8, "两拍各 4 像素")

        sc.set_paused(True)
        self.clock.advance(30.0)
        sc._on_tick()
        self.assertEqual(bar.value(), 8, "暂停期间一动不动")
        sc.set_paused(False)
        self.tick(sc, 0.03125)
        self.assertEqual(bar.value(), 12, "只结算真正的 1/32 秒，而不是那三十秒")

    def test_the_fractional_remainder_survives_a_pause(self) -> None:
        """半像素的余量在暂停期间必须留着：丢了它，速度就会悄悄变慢。"""
        sc, bar = self.scroller(speed=10, start=True)
        self.tick(sc, 0.0625)
        self.assertEqual(bar.value(), 0, "0.625 像素先攒着")

        sc.set_paused(True)
        self.clock.advance(4.0)
        sc._on_tick()
        sc.set_paused(False)

        self.tick(sc, 0.03125)
        self.assertEqual(bar.value(), 0, "0.625 + 0.3125 还不够一个像素")
        self.tick(sc, 0.03125)
        self.assertEqual(bar.value(), 1, "攒下的两段合成一个整像素——余量丢了这里就还是 0")

    def test_pausing_before_start_does_not_freeze_the_next_run(self) -> None:
        """暂停只在运行中有意义；一个还没开起来的引擎被标了暂停，start() 必须照常走。"""
        sc, bar = self.scroller(speed=100)
        sc.set_paused(True)
        sc.start()
        self.assertFalse(sc.is_paused())
        self.tick(sc, 0.5)
        self.assertEqual(bar.value(), 50)

    def test_stopping_clears_the_pause(self) -> None:
        sc, _bar = self.scroller(speed=100, start=True)
        sc.set_paused(True)
        sc.stop()
        self.assertFalse(sc.is_paused())
        self.assertFalse(sc.is_running())

    def test_the_interval_argument_is_defensive_and_bounded(self) -> None:
        """间隔也是外部给的（窗口、以后可能是设置项），读不出来就用默认值。"""
        bar = self.bar()
        for value, expected in ((None, 40), ("快", 40), (0, 1), (-5, 1), (99999, 1000), (25, 25)):
            with self.subTest(value=value):
                sc = AutoScroller(bar, interval_ms=value, clock=self.clock)
                self._alive.append(sc)
                self.assertEqual(sc._timer.interval(), expected)


# ---------------------------------------------------------------------------
# 真定时器（只有一个用例）
# ---------------------------------------------------------------------------

class TestRealTimer(ScrollerTestCase):
    def test_the_timeout_is_wired_to_the_scrollbar(self) -> None:
        """唯一用真定时器的用例：证明 timeout -> _on_tick 这条线是通的，别的用例都在
        手推时间。速度取上限、间隔取 10 ms，所以事件循环一转就必然走出可断言的距离。

        上限断言比下限更有意义：位移按真实时间累计，所以它**不可能**快过 速度 x 经过
        的时间——那正是"按拍计速"会破坏的不变量。下限只要求动过，因为 offscreen 上的
        调度延迟没有上界。"""
        bar = self.bar(100000)
        sc = AutoScroller(bar, interval_ms=10, clock=time.monotonic)
        self._alive.append(sc)
        sc.set_speed(MAX_SPEED)
        started_at = time.monotonic()
        sc.start()
        deadline = started_at + 0.4
        while time.monotonic() < deadline:
            _app.processEvents()
            time.sleep(0.005)
        sc.stop()
        elapsed = time.monotonic() - started_at
        self.assertGreater(bar.value(), 0, "定时器没把位置推动")
        self.assertLessEqual(bar.value(), MAX_SPEED * elapsed + 2.0)
        self.assertFalse(sc._timer.isActive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
