"""打包产物基准：启动耗时 + 内存占用。临时工具，用完即删。

用法：
    py -3.12 _pack_bench.py

两个坑都踩过了，所以这里不用 stdout 计时：

1. 被管道接住时 Python 的 stdout 是块缓冲的，读到第一行的时间等于「进程退出」的时间。
   第一版就此测出「启动 6.8 秒」——而程序里那段报告其实是在进事件循环**之前**打的。
   加 PYTHONUNBUFFERED=1 也无效：PyInstaller 打出来的 exe 不认这个环境变量
   （PYTHONIOENCODING 同样不认，这就是之前 --check 和 --smoke 编码不一致的根源）。
   所以改成用 EnumWindows 等真正的窗口出现。

2. 单文件模式下有两个 MiniReader.exe 进程（引导器 + 真正的程序），拿 Popen 的句柄去
   查内存只会量到引导器那 8 MB。所以按进程名把整棵进程树都抓出来。
"""

from __future__ import annotations

import ctypes
import os
import statistics
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS = {
    "目录模式": ROOT / "dist" / "MiniReader" / "MiniReader.exe",
    "单文件模式": ROOT / "dist-onefile" / "MiniReader.exe",
}
RUNS = 3
SMOKE = "6"
EXE_NAME = "miniReader.exe".lower()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


kernel32.K32GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD
]
kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL


def our_pids() -> list[int]:
    """当前所有 MiniReader.exe 进程的 pid。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    pids = []
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == EXE_NAME:
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def read_mem() -> dict[int, dict[str, float]]:
    out = {}
    for pid in our_pids():
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if kernel32.K32GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                out[pid] = {
                    "working": counters.WorkingSetSize / 1048576.0,
                    "peak": counters.PeakWorkingSetSize / 1048576.0,
                    "private": counters.PrivateUsage / 1048576.0,
                }
        finally:
            kernel32.CloseHandle(handle)
    return out


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]


def visible_pids() -> set[int]:
    """有可见顶层窗口的 pid 集合。"""
    result: set[int] = set()

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            result.add(int(pid.value))
        return True

    user32.EnumWindows(_ENUM_PROC(callback), 0)
    return result


def kill_stale() -> None:
    for pid in our_pids():
        handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)


def run_once(exe: Path) -> tuple[float, float, dict[str, float], int]:
    """返回 (启动->窗口可见, 启动->退出, 内存峰值, 进程数)。"""
    kill_stale()
    time.sleep(0.5)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QT_QPA_PLATFORM", None)

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [str(exe), "--smoke", SMOKE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(exe.parent),
    )

    shown = float("nan")
    peak = {"working": 0.0, "peak": 0.0, "private": 0.0}
    procs_seen = 0
    while proc.poll() is None:
        if shown != shown:  # 还是 nan：窗口还没出现
            if visible_pids() & set(our_pids()):
                shown = time.perf_counter() - t0
        mem = read_mem()
        if mem:
            procs_seen = max(procs_seen, len(mem))
            # 「已打开」之后窗口才稳定；这里用窗口可见之后采样到的最大值近似稳态。
            if shown == shown:
                peak["working"] = max(peak["working"], sum(m["working"] for m in mem.values()))
                peak["peak"] = max(peak["peak"], sum(m["peak"] for m in mem.values()))
                peak["private"] = max(peak["private"], sum(m["private"] for m in mem.values()))
        time.sleep(0.04)
    total = time.perf_counter() - t0
    return shown, total, peak, procs_seen


def main() -> int:
    print("=" * 74)
    print(f"打包产物基准（每次全新进程；smoke={SMOKE}s，取 {RUNS} 次中位数）")
    print("启动 = 进程创建 -> 第一个可见顶层窗口出现")
    print("=" * 74)
    for label, exe in TARGETS.items():
        if not exe.exists():
            print(f"\n[{label}] 不存在：{exe}")
            continue
        if (exe.parent / "_internal").exists():
            total_mb = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file()) / 1048576
            detail = f"exe {exe.stat().st_size / 1048576:.2f} MB，目录共 {total_mb:.1f} MB"
        else:
            detail = f"exe {exe.stat().st_size / 1048576:.2f} MB"
        print(f"\n[{label}]  {detail}")

        starts, works, peaks, privs, counts = [], [], [], [], []
        for i in range(RUNS):
            shown, total, mem, n = run_once(exe)
            starts.append(shown)
            works.append(mem["working"])
            peaks.append(mem["peak"])
            privs.append(mem["private"])
            counts.append(n)
            print(f"      第 {i + 1} 次: 窗口 {shown * 1000:5.0f} ms | 进程 {n} 个 | "
                  f"工作集 {mem['working']:5.1f} MB  峰值 {mem['peak']:5.1f} MB  "
                  f"提交 {mem['private']:5.1f} MB | 全程 {total:.1f} s")
        print(f"      中位数: 窗口 {statistics.median(starts) * 1000:.0f} ms | "
              f"进程 {max(counts)} 个 | 工作集 {statistics.median(works):.1f} MB  "
              f"峰值 {statistics.median(peaks):.1f} MB  提交 {statistics.median(privs):.1f} MB")

    print("\n" + "=" * 74)
    print("--check（进程创建 -> 解释器 -> Qt -> 本项目 -> 退出，不开窗口）")
    print("=" * 74)
    for label, exe in TARGETS.items():
        if not exe.exists():
            continue
        times = []
        for _ in range(RUNS + 2):
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            t0 = time.perf_counter()
            subprocess.run([str(exe), "--check"], capture_output=True, env=env,
                           cwd=str(exe.parent), timeout=180)
            times.append(time.perf_counter() - t0)
        warm = times[1:]
        print(f"  {label:<10} 冷 {times[0] * 1000:6.0f} ms | 热 中位数 "
              f"{statistics.median(warm) * 1000:6.0f} ms  "
              f"（{', '.join(f'{t * 1000:.0f}' for t in warm)} ms）")
    kill_stale()
    return 0


if __name__ == "__main__":
    sys.exit(main())
