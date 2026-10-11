"""自动滚动：按恒定速度把阅读区往下推，到底自停。

**为什么只依赖一条滚动条，而不是依赖 ReaderView。** ReaderView 已经有一套滚轮缓动
（`_animate_to` / `_cancel_anim`），它和自动滚动都得写 `verticalScrollBar()`；把两者
放在同一个类里，缓动一回调就会把自动滚动推到的位置拽回去，而且谁该停、谁该让路这种
决定属于窗口，不属于引擎。只依赖 `QScrollBar` 还有两个直接好处：引擎可以用一条普通
滚动条测，不需要文档排版、不需要显示设备；它也物理上碰不到视图的动画机器。

**为什么按时间累计，而不是"每拍加固定像素"。** `QTimer` 只保证"不早于"，机器一忙它
就会晚到：按拍数计速会让真实速度随负载漂移，而且只在别人的慢机器上漂——开发机上永远
看不见。所以每一拍都量真实的经过时间，把不足一像素的余量留到下一拍。时钟可注入，
测试用手推的假时钟，不必 sleep。
"""

from __future__ import annotations

import time
from math import isfinite

from ..qt import QObject, QTimer, Qt, Signal

# 10 px/s 以下基本看不出在动；400 px/s 大约一秒一屏，再快就只剩"字在飞"。越界的值
# 一律夹住而不是报错——速度来自设置面板和手改的 JSON，那两种地方什么都可能出现。
MIN_SPEED = 10.0
MAX_SPEED = 400.0
DEFAULT_SPEED = 60.0

DEFAULT_INTERVAL_MS = 40
# 一秒一跳已经不叫自动滚动，而是一格一格地跳页；再慢就没有存在的意义。
_MAX_INTERVAL_MS = 1000


# ---------------------------------------------------------------------------
# 值清洗
# ---------------------------------------------------------------------------

def _as_float(value, fallback: float) -> float:
    """把来路不明的值读成可用的浮点数，读不出来就退回 fallback。

    与 `storage._as_int` 是同一套防御思路（宁可退化也不抛异常，因为调用方是自己写的
    程序读用户手改的配置），但这里多一条：NaN 和 ±inf 也算"读不出来"。夹取函数拦不住
    NaN——`min(nan, 400)` 原样返回 NaN，因为 NaN 与任何数比较都是 False，min 于是保留
    了左边的它。NaN 一旦进了累加器，`int(NaN)` 会抛 ValueError，滚动位置也变成 NaN。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类；数字字段里出现 True 是类型错误，读成 1 只会把错误藏起来。
        return fallback
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return fallback
    else:
        return fallback
    return number if isfinite(number) else fallback


def _as_int(value, fallback: int) -> int:
    """整数字段的防御性读取，与 `storage._as_int` 同款。

    有意重复这十几行而不跨层 import 一个下划线开头的私有名字：`ui` 不该为了一个
    类型守卫去依赖存储层。
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if isfinite(value) else fallback
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return fallback
    return fallback


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class AutoScroller(QObject):
    """恒定速度滚动一条 `QScrollBar` 的定时器引擎。

    契约（窗口侧据此接线）：

    * `started` / `stopped`：运行状态真的翻转了才发，`start()` 两次只发一次。
    * `reachedEnd`：到底或发现根本没有可滚动空间时发，**每次运行至多一次**。窗口收到
      它之后一般会换章，然后调 `reset_end_latch()` 再 `start()`。
    * 暂停不改运行状态：窗口的按钮仍然读作"暂停"，`is_running()` 仍为 True。
    """

    reachedEnd = Signal()
    started = Signal()
    stopped = Signal()

    def __init__(
        self,
        scrollbar,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        parent=None,
        clock=time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._bar = scrollbar
        self._clock = clock if callable(clock) else time.monotonic
        self._interval_ms = max(1, min(_as_int(interval_ms, DEFAULT_INTERVAL_MS), _MAX_INTERVAL_MS))

        self._speed = DEFAULT_SPEED
        self._running = False
        self._paused = False
        self._end_latched = False
        self._carry = 0.0                  # 不足一像素的余量，跨拍保留
        self._last = None                  # 上一次结算的时刻；None = 还没起表
        self._paused_at = None             # 暂停发生在哪一刻，恢复时用来前移表针

        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        precise = getattr(Qt, "PreciseTimer", None)
        if precise is not None:
            # 速度是按时间算的，粗定时器的容差不会改变平均速度，只会让每一拍的距离
            # 忽大忽小；自动滚动本来就慢，一拍的抖动就是肉眼可见的一顿。
            self._timer.setTimerType(precise)
        self._timer.timeout.connect(self._on_tick)

    # ---- 速度 ------------------------------------------------------------

    def set_speed(self, pixels_per_second: float) -> None:
        """设置速度（像素/秒）。夹取到 [MIN_SPEED, MAX_SPEED]，垃圾输入退回默认值。"""
        value = _as_float(pixels_per_second, DEFAULT_SPEED)
        self._speed = max(MIN_SPEED, min(value, MAX_SPEED))

    def speed(self) -> float:
        return float(self._speed)

    # ---- 运行状态 --------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return                          # 幂等：不重开定时器，也不重发 started
        if self._at_end():
            # 没有向下的余地（maximum == 0，或就停在底部）。这里既不发 started 也不起
            # 定时器：按钮不该先亮成"暂停"再自己弹回来，定时器更不该空转。当一次到底。
            self._carry = 0.0
            self._last = None
            self._reached_bottom()
            return
        self._running = True
        self._paused = False
        self._paused_at = None
        self._carry = 0.0
        # 表针从"现在"起算，而不是从第一拍起算：第一拍迟到多久，就该补多少像素，否则
        # 启动瞬间的一次卡顿会白白丢掉那段时间。这不移动滚动条，只记时间。
        self._last = self._now()
        self.started.emit()
        self._timer.start()

    def stop(self) -> None:
        # 无论此前是什么状态都先把定时器关掉：窗口关闭时调的正是这里，这是最后一道
        # "别把一个定时器留在身后"的保险。
        self._timer.stop()
        self._paused = False
        self._paused_at = None
        self._last = None
        if not self._running:
            return                          # 幂等：已经停了就不再发一次 stopped
        self._running = False
        self.stopped.emit()

    def toggle(self) -> None:
        """开着就停，停着就开。

        暂停刻意不参与：暂停是"还开着、只是不动"，按钮要显示"暂停"，窗口用它自己
        的状态去表达。把暂停混进 toggle，按钮文案就会和真实状态对不上。
        """
        if self._running:
            self.stop()
        else:
            self.start()

    def is_running(self) -> bool:
        """运行中即 True，**包括暂停**——按钮靠它决定显示"暂停"还是"自动滚动"。"""
        return self._running

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        """冻结位移但保持运行状态。可重复调用。"""
        flag = bool(paused)
        if flag == self._paused:
            return
        self._paused = flag
        now = self._now()
        if flag:
            # 记住暂停的瞬间。恢复时把表针整体前移暂停的时长——若只是把 _last 置空，
            # 恢复的第一拍会被迫重新校准、白丢一拍；若什么都不做，整段暂停会被当成
            # 一次巨大的 elapsed 走完，暂停十秒就当场跳十秒的像素。
            self._paused_at = now
        else:
            if self._last is not None and self._paused_at is not None:
                self._last += max(0.0, now - self._paused_at)
            self._paused_at = None

    def reset_end_latch(self) -> None:
        """允许下一次 start() 重新走到底部并再发一次 `reachedEnd()`。

        窗口换章后调它：新的一章有新的滚动范围，旧的锁存会把这一章立刻判成"已经到底"。
        """
        self._end_latched = False

    # ---- 内部 ------------------------------------------------------------

    def _now(self) -> float:
        """时钟读数一律转成 float：注入的假时钟常常返回 int，混用会让 elapsed 变成 int。"""
        return float(self._clock())

    def _at_end(self) -> bool:
        """True = 这条滚动条没有任何向下的余地。maximum == 0 也算（没东西可滚）。"""
        return int(self._bar.value()) >= int(self._bar.maximum())

    def _on_tick(self) -> None:
        """一拍。位移一律来自真实经过的时间，绝不来自"第几拍"。"""
        if not self._running or self._paused:
            # 暂停期间连表针都不动，恢复时靠前移补齐。已经停下时也要挡住：定时器虽然
            # stop() 了，正要销毁的那一刻事件队列里仍可能压着一次超时。
            return
        now = self._now()
        if not isfinite(now):
            # 时钟坏了（NaN/inf）：保住上一次的基准等它恢复，绝不能污染累加器。
            return
        last = self._last
        self._last = now
        if last is None:
            return
        elapsed = now - last
        if elapsed <= 0.0:
            # 重复读数或时钟回拨：这段时间当作没走过。
            return

        self._carry += self._speed * elapsed
        pixels = int(self._carry)           # 只走整像素
        if pixels <= 0:
            return                          # 余量不足一像素，攒到下一拍
        self._carry -= pixels

        bar = self._bar
        value = int(bar.value())
        maximum = int(bar.maximum())
        if value >= maximum:
            # 外部（滚轮、按键、换章）抢先把我们送到了底部。仍然按"到底"收场。
            self._reached_bottom()
            return
        target = value + pixels
        if target >= maximum:
            # 最后一段常常不足一拍；走满再报到底，而不是先溢出、下一拍才发现。
            bar.setValue(maximum)
            self._carry = 0.0
            self._reached_bottom()
            return
        bar.setValue(target)                # 目标恒 <= maximum，绝不产生超范围整数

    def _reached_bottom(self) -> None:
        """到底：先收摊，再报信。

        状态一律在发信号之前改完。窗口收到 reachedEnd 会当场换章并重新 start()；如果
        那时 `_running` 或锁存还没落定，新的一轮会被自己刚发出的信号立刻打断。
        """
        was_running = self._running
        self._running = False
        self._paused = False
        self._paused_at = None
        self._last = None
        self._timer.stop()
        if was_running:
            # 自动到底也是一次停下。少了这个信号，工具栏按钮会一直停在"暂停"上。
            self.stopped.emit()
        if self._end_latched:
            return                          # 锁存：一次运行只报一次到底
        self._end_latched = True
        self.reachedEnd.emit()
