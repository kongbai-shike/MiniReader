"""真键盘回环验证：焦点在**别的程序**里时，Ctrl+Shift+` / Ctrl+Shift+L 是否生效。

为什么需要这个脚本：`--smoke` 只证明 `RegisterHotKey` 返回了 TRUE，不证明
WM_HOTKEY 真的会回到我们的 nativeEventFilter、也不证明分派表认得这个名字。
本脚本注入真实键盘事件（SendInput），并**断言按键发出时前台窗口不是 MiniReader**，
所以窗口隐藏只可能来自全局热键；MiniReader 自己的本地 QShortcut 在这种情况下
是收不到按键的。

用法：
    py -3.12 _manual/hotkey_roundtrip.py

退出码 0 = 全部通过。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

REPO = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_OEM_3 = 0xC0  # 反引号，Esc 下面那个键
VK_L = 0x4C
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
WM_CLOSE = 0x0010

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT 是三个成员里最大的（32 字节），必须留在联合体里：少写它会让
    # sizeof(INPUT) 变成 32 而不是 40，SendInput 直接返回 0 / ERROR_INVALID_PARAMETER(87)。
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _window_info(hwnd: int) -> dict:
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "title": title.value,
        "cls": cls.value,
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "rect": [rect.left, rect.top, rect.right, rect.bottom],
    }


def top_level_windows(pid: int | None = None, only_visible: bool = True) -> list[dict]:
    found: list[dict] = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if pid is not None and wpid.value != pid:
            return True
        if only_visible and not user32.IsWindowVisible(hwnd):
            return True
        info = _window_info(hwnd)
        info["pid"] = wpid.value
        found.append(info)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def wait_for(predicate, timeout: float, interval: float = 0.15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def send_chord(key: int, mods=(VK_CONTROL, VK_SHIFT)) -> None:
    events: list[INPUT] = []

    def press(vk):
        item = INPUT()
        item.type = 1  # INPUT_KEYBOARD
        item.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
        events.append(item)

    def release(vk):
        item = INPUT()
        item.type = 1
        item.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        events.append(item)

    for mod in mods:
        press(mod)
    press(key)
    release(key)
    for mod in reversed(mods):
        release(mod)

    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(f"SendInput 只送出了 {sent}/{len(events)} 个事件：{ctypes.get_last_error()}")


def force_foreground(hwnd: int) -> bool:
    """把 hwnd 抢到前台，返回是否成功。"""
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.ShowWindow(hwnd, SW_RESTORE)
    # 前台锁定：本进程不是前台时 SetForegroundWindow 会被静默忽略。AttachThreadInput
    # 到当前前台线程是绕开它的标准做法；再不行就老实报告失败，不假装测过了。
    fg = user32.GetForegroundWindow()
    current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if fg_tid and fg_tid != current_tid:
            attached = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_tid, fg_tid, False)
    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


def app_window(pid: int) -> dict | None:
    for info in top_level_windows(pid):
        width = info["rect"][2] - info["rect"][0]
        height = info["rect"][3] - info["rect"][1]
        if info["cls"].startswith("Qt") and width > 40 and height > 40:
            return info
    return None


def notepad_window(exclude_hwnd: int, exclude: set[int] | None = None) -> dict | None:
    """找一个记事本窗口；exclude 里的 hwnd（启动前就存在的）不算。"""
    foreground = user32.GetForegroundWindow()
    candidates = []
    for info in top_level_windows():
        if info["hwnd"] == exclude_hwnd or not info["visible"]:
            continue
        if exclude and info["hwnd"] in exclude:
            continue
        title = info["title"]
        if "记事本" in title or "Notepad" in title or info["cls"] == "Notepad":
            candidates.append(info)
    if not candidates:
        return None
    for info in candidates:
        if info["hwnd"] == foreground:
            return info
    return candidates[0]


def main() -> int:
    results: list[dict] = []

    def record(name, ok, detail):
        results.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'OK ' if ok else 'FAIL'}] {name}: {detail}")

    # 0. 不能有别的 MiniReader 占着这组全局热键
    leftovers = [
        info
        for info in top_level_windows()
        if info["cls"].startswith("Qt") and info["title"] in ("MiniReader", "")
    ]
    if leftovers:
        print("已经有一个 Qt 窗口开着，先关掉它再跑本脚本：", file=sys.stderr)
        for info in leftovers:
            print("   ", json.dumps(info, ensure_ascii=False), file=sys.stderr)
        return 2

    # 默认测源码；设 MR_TEST_EXE=...\MiniReader.exe 就测打包产物。同一条脚本测
    # 两种形态，避免"源码过了、exe 没过"这种只在一侧成立的结论。
    exe = os.environ.get("MR_TEST_EXE")
    if exe:
        exe = os.path.abspath(exe)
        if not os.path.isfile(exe):
            print(f"MR_TEST_EXE 指向的 exe 不存在：{exe}", file=sys.stderr)
            return 2
        argv = [exe]
        cwd = os.path.dirname(exe)
        print(f"目标：打包产物 {exe}")
    else:
        argv = [sys.executable, "-m", "minireader"]
        cwd = REPO
        print(f"目标：源码 {REPO}")

    err = tempfile.NamedTemporaryFile("w+", suffix=".err.txt", delete=False, encoding="utf-8", errors="replace")
    app = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=err,
    )
    print(f"MiniReader 源码启动：pid={app.pid}（stderr → {err.name}）")

    note = None
    note_hwnd = None
    try:
        info = wait_for(lambda: app_window(app.pid), timeout=30)
        if info is None:
            return_code = app.poll()
            print(f"MiniReader 没起来（returncode={return_code}）：", file=sys.stderr)
            err.flush()
            print(open(err.name, encoding="utf-8", errors="replace").read()[-3000:], file=sys.stderr)
            return 3
        record("源码启动并拿到真实 HWND", True, json.dumps(info, ensure_ascii=False))
        hwnd = info["hwnd"]

        # Win11 的记事本单进程多窗口，机器上常留着以前开的窗口。必须挑"这次新
        # 出现的"那个：先记下启动前的 hwnd 集合，启动后取差集，再从差集里优先选
        # 已经被系统推到前台的那个。
        before_hwnds = {info["hwnd"] for info in top_level_windows()}
        note = subprocess.Popen(["notepad.exe"])
        note_info = wait_for(
            lambda: notepad_window(hwnd, exclude=before_hwnds),
            timeout=30,
        )
        if note_info is None:
            record("记事本启动", False, "30 秒内没找到新出现的记事本窗口")
            return 4
        record("记事本启动", True, json.dumps(note_info, ensure_ascii=False))
        note_hwnd = note_info["hwnd"]

        focused = force_foreground(note_info["hwnd"])
        fg = user32.GetForegroundWindow()
        record(
            "焦点确实在别的程序（不是 MiniReader）",
            focused and fg != hwnd,
            f"foreground={fg} notepad={note_info['hwnd']} minireader={hwnd}",
        )
        if not (focused and fg != hwnd):
            print("抢不到前台，本脚本无法证明'全局'，不做假结论。", file=sys.stderr)
            return 5

        def visible() -> bool:
            return bool(user32.IsWindowVisible(hwnd))

        def wait_state(target: bool, timeout: float = 3.0) -> bool:
            return bool(wait_for(lambda: visible() is target, timeout, interval=0.02))

        # ---- Ctrl+Shift+` 在记事本聚焦时 → 真隐藏 ----
        before = visible()
        send_chord(VK_OEM_3)
        hidden = wait_state(False)
        record(
            "Ctrl+Shift+` 在记事本聚焦时把窗口真隐藏（全局热键回环）",
            before and hidden,
            f"可见 {before} → {visible()}；进程存活={app.poll() is None}",
        )

        # 第 2 下必须等一会儿：HotkeyManager 有 60ms 去抖，专门用来合并 Windows 对
        # 一次按键的 WM_HOTKEY 双投递。同一拍内连按两下会被合并，这是设计而不是 bug
        # （_manual/hotkey_probe.py 量过：50ms 合并、150ms 起正常）。
        time.sleep(0.6)
        send_chord(VK_OEM_3)
        shown = wait_state(True)
        record("再按 Ctrl+Shift+` 恢复显示", shown, f"可见={visible()}")

        # 恢复时 activateWindow() 会把焦点抢回 MiniReader；重新把记事本拉到前台，
        # 否则下一次按键就不是"在别的程序里按"了。
        force_foreground(note_info["hwnd"])
        record(
            "恢复后焦点回到记事本",
            user32.GetForegroundWindow() != hwnd,
            f"foreground={user32.GetForegroundWindow()}",
        )

        # ---- Ctrl+Shift+L → 防偷窥（真隐藏） ----
        time.sleep(0.3)
        send_chord(VK_L)
        stealth_hidden = wait_state(False)
        record(
            "Ctrl+Shift+L 在记事本聚焦时进入防偷窥（窗口真隐藏）",
            stealth_hidden,
            f"可见={visible()}；进程存活={app.poll() is None}",
        )

        # ---- 再按 Ctrl+Shift+L → 恢复 ----
        time.sleep(0.6)
        send_chord(VK_L)
        stealth_back = wait_state(True)
        record("再按 Ctrl+Shift+L 恢复显示", stealth_back, f"可见={visible()}")

        # ---- 边界 4：老板键已经把窗口藏起来时，Ctrl+Shift+L 应静默忽略 ----
        force_foreground(note_info["hwnd"])
        time.sleep(0.3)
        send_chord(VK_OEM_3)
        wait_state(False)
        time.sleep(0.6)
        send_chord(VK_L)
        time.sleep(1.0)  # 给"错误地 show 出来"留足时间
        record(
            "老板键藏着时 Ctrl+Shift+L 被静默忽略（不偷偷显示）",
            not visible(),
            f"可见={visible()}",
        )

        # ---- 老板键是另一条回家路 ----
        time.sleep(0.3)
        send_chord(VK_OEM_3)
        unlocked = wait_state(True)
        record("老板键可把藏起来的窗口叫回来", unlocked, f"可见={visible()}")

        time.sleep(0.5)
        err.flush()
        stderr_text = open(err.name, encoding="utf-8", errors="replace").read()
        unknown = [line for line in stderr_text.splitlines() if "未知的全局热键名" in line]
        record("stderr 没有'未知的全局热键名'", not unknown, f"命中 {len(unknown)} 行")
        if unknown:
            print("\n".join(unknown), file=sys.stderr)
        noisy = [
            line
            for line in stderr_text.splitlines()
            if line.strip() and "QFont::setPointSize" not in line
        ]
        print("--- stderr（去掉 Qt 字号噪声）---")
        print("\n".join(noisy) if noisy else "<空>")

    finally:
        # 只关掉测试自己开的那个记事本窗口（WM_CLOSE），不 terminate 整个
        # notepad.exe：Win11 记事本是单进程多窗口，terminate 会连用户自己开的
        # 记事本一起关掉。
        if note_hwnd:
            user32.PostMessageW(note_hwnd, WM_CLOSE, 0, 0)
            time.sleep(0.3)
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()

    failed = [item for item in results if not item["ok"]]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} 通过 ===")
    if failed:
        for item in failed:
            print("FAILED:", item["check"], item["detail"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())