"""进程内探针：为什么第二次按老板键不恢复？

和 hotkey_roundtrip.py 的区别：那个是黑盒（只能看 IsWindowVisible）；这个在**同一个
进程**里跑 `main()`，把热键到达、分派、toggle_boss/show_from_boss/hide_to_boss、
以及窗口自己的 isVisible()/boss_hidden/_stealth_mode 逐拍打出来。键盘仍然由外部
线程用 SendInput 真注入，焦点在记事本。

用法：py -3.12 _manual/hotkey_probe.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from minireader.__main__ import main as run_gui  # noqa: E402
from minireader.qt import QTimer  # noqa: E402
from _manual.hotkey_roundtrip import (  # noqa: E402
    VK_L,
    VK_OEM_3,
    force_foreground,
    notepad_window,
    send_chord,
    top_level_windows,
    user32,
)

LOG: list[str] = []
T0 = time.monotonic()


def note(message: str) -> None:
    line = f"{time.monotonic() - T0:7.3f}  {message}"
    LOG.append(line)
    print(line, flush=True)


def wrap(obj, name: str, label: str | None = None) -> None:
    original = getattr(obj, name)

    def snapshot() -> str:
        if hasattr(obj, "isVisible"):
            return (
                f"visible={obj.isVisible()} boss_hidden={getattr(obj, 'boss_hidden', '?')} "
                f"stealth={getattr(obj, '_stealth_mode', '?')}"
            )
        return ""

    def wrapper(*args, **kwargs):
        note(f"-> {label or name}  {snapshot()}")
        try:
            result = original(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            note(f"!! {label or name} 抛异常: {exc!r}")
            raise
        note(f"<- {label or name}  {snapshot()}")
        return result

    setattr(obj, name, wrapper)


def main() -> int:
    state: dict = {"done": False, "window": None, "hwnd": 0, "last": None, "notepad": None}

    def on_ready(window, tray, app) -> None:
        state["window"] = window
        state["hwnd"] = int(window.winId())
        note(f"on_ready hwnd={state['hwnd']} tray={'有' if tray else '无'}")
        manager = window.hotkeys
        note(f"id→name: {dict(getattr(manager, '_id_to_name', {}))}")
        wrap(manager, "_queue", "hotkeys._queue")
        wrap(manager, "_fire", "hotkeys._fire")
        for name in ("toggle_boss", "hide_to_boss", "show_from_boss", "toggle_stealth_mode"):
            wrap(window, name)

        def sample() -> None:
            snapshot = (
                window.isVisible(),
                window.boss_hidden,
                window._stealth_mode,
                bool(user32.IsWindowVisible(state["hwnd"])),
            )
            if snapshot != state["last"]:
                state["last"] = snapshot
                note(
                    f"state  qt_visible={snapshot[0]} boss_hidden={snapshot[1]} "
                    f"stealth={snapshot[2]} os_visible={snapshot[3]}"
                )

        timer = QTimer(window)
        timer.setInterval(50)
        timer.timeout.connect(sample)
        timer.start()
        state["timer"] = timer

        def finish_poll() -> None:
            if state["done"]:
                app.quit()
            else:
                QTimer.singleShot(200, finish_poll)

        threading.Thread(target=worker, args=(state,), daemon=True).start()
        QTimer.singleShot(300, finish_poll)

    def worker(st: dict) -> None:
        hwnd = st["hwnd"]
        time.sleep(1.2)
        note(f"前台={user32.GetForegroundWindow()}（MiniReader={hwnd}）")
        proc = subprocess.Popen(["notepad.exe"])
        st["notepad"] = proc
        info = None
        deadline = time.time() + 30
        while time.time() < deadline and info is None:
            info = notepad_window(hwnd)
            time.sleep(0.2)
        if info is None:
            note("记事本起不来，放弃")
            st["done"] = True
            return
        note(f"记事本 hwnd={info['hwnd']}")

        def visible() -> bool:
            return bool(user32.IsWindowVisible(hwnd))

        def wait_os(target: bool, timeout: float = 2.0) -> float | None:
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                if visible() is target:
                    return time.monotonic() - start
                time.sleep(0.02)
            return None

        def focus_note() -> None:
            force_foreground(info["hwnd"])

        def scenario(label: str, gap: float, key: int) -> None:
            focus_note()
            note(f"--- {label}：间隔 {gap * 1000:.0f} ms ---")
            if not visible():
                note("    （窗口当前是藏着的，先按一次叫回来）")
                send_chord(key)
                wait_os(True)
                time.sleep(0.6)
            send_chord(key)
            t_hide = wait_os(False)
            if t_hide is None:
                note("    第 1 下没有藏起来 → 本场景作废")
                return
            time.sleep(gap)
            send_chord(key)
            t_show = wait_os(True)
            note(
                f"    藏起来用了 {t_hide * 1000:.0f} ms；"
                + (f"恢复用了 {t_show * 1000:.0f} ms" if t_show is not None else "恢复失败（2 秒内没出现）")
            )
            time.sleep(0.8)

        for gap in (1.2, 0.6, 0.3, 0.15, 0.05):
            scenario(f"老板键 连按（gap={gap}）", gap, VK_OEM_3)

        note("=== 防偷窥 ===")
        focus_note()
        send_chord(VK_L)
        note(f"    进入: {wait_os(False)}")
        time.sleep(0.3)
        send_chord(VK_L)
        note(f"    退出: {wait_os(True)}")
        time.sleep(0.6)

        note("=== 防偷窥态下老板键解锁 ===")
        focus_note()
        send_chord(VK_L)
        wait_os(False)
        time.sleep(0.4)
        send_chord(VK_OEM_3)
        note(f"    解锁: {wait_os(True)}")
        time.sleep(0.5)

        note("=== 窗口可见时按 Ctrl+Shift+L 两次（间隔 1s）===")
        focus_note()
        send_chord(VK_L)
        note(f"    进入: {wait_os(False)}")
        time.sleep(1.0)
        send_chord(VK_L)
        note(f"    退出: {wait_os(True)}")

        st["done"] = True
        if proc.poll() is None:
            proc.terminate()

    run_gui([], on_ready=on_ready)
    print("\n\n===== 探针日志 =====")
    print("\n".join(LOG))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())