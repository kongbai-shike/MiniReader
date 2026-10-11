"""检查打包产物有没有「缺依赖」——裁剪 Qt DLL 之后最该跑的一步。

用法：
    py -3.12 tools/check_bundle.py [dist\\MiniReader\\_internal]

做什么：
    遍历产物里每个 .dll / .pyd，用 pefile 读出它的导入表，逐个检查每个被导入的
    DLL 能不能找到：
      * 产物目录里（递归按文件名找）
      * 或者系统目录里（C:\\Windows\\System32）
    两边都找不到的就是「悬空依赖」——那种包在本机能跑（因为开发者机器上装了
    各种运行库），拷到别的机器上就双击没反应。

为什么需要它：
    裁 Qt5\\bin 的时候很容易删掉一个看起来没用的 DLL，实际上 Qt5Gui.dll 静态
    依赖它。这类错误不会在打包时报出来，PyInstaller 也不检查导入表。它是本项目
    裁剪部分唯一能自动化的正确性保证。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import pefile
except ImportError:  # pragma: no cover - 只在没装 PyInstaller 时发生
    raise SystemExit("需要 pefile（随 PyInstaller 一起装）：py -3.12 -m pip install pyinstaller")

# 这些前缀是 Windows 的 API Set（虚拟 DLL 名）和系统 DLL，永远由系统提供。
_SYSTEM_PREFIXES = ("api-ms-win-", "ext-ms-win-")
_ALWAYS_SYSTEM = {
    "kernel32.dll", "user32.dll", "gdi32.dll", "advapi32.dll", "shell32.dll",
    "ole32.dll", "oleaut32.dll", "comdlg32.dll", "comctl32.dll", "winmm.dll",
    "ws2_32.dll", "opengl32.dll", "glu32.dll", "dwmapi.dll", "uxtheme.dll",
    "imm32.dll", "version.dll", "netapi32.dll", "userenv.dll", "ntdll.dll",
    "msvcrt.dll", "shlwapi.dll", "d3d9.dll", "d3d11.dll", "dxgi.dll",
    "crypt32.dll", "secur32.dll", "bcrypt.dll", "ncrypt.dll", "wintrust.dll",
    "setupapi.dll", "winspool.drv", "mpr.dll", "wtsapi32.dll", "psapi.dll",
    "rpcrt4.dll", "iphlpapi.dll", "authz.dll", "dnsapi.dll", "mswsock.dll",
    "powrprof.dll", "propsys.dll", "dwrite.dll", "usp10.dll", "msimg32.dll",
    "wininet.dll", "urlmon.dll", "mscoree.dll", "dbghelp.dll", "pdh.dll",
    "odbc32.dll", "wldap32.dll", "normaliz.dll", "imagehlp.dll",
}


def system_dir() -> Path:
    import os

    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path("dist/MiniReader/_internal").resolve()
    if not root.exists():
        raise SystemExit(f"找不到目录：{root}")

    # 产物里所有文件的文件名 -> 路径（用于解析同目录依赖）
    present: dict[str, Path] = {}
    for f in root.rglob("*"):
        if f.is_file():
            present[f.name.lower()] = f
    sysdir = system_dir()

    binaries = [f for f in root.rglob("*") if f.suffix.lower() in (".dll", ".pyd", ".exe")]
    # exe 在 _internal 外面，顺手也收进来
    for extra in sorted(root.parent.glob("*.exe")):
        binaries.append(extra)

    print(f"检查目录 : {root}")
    print(f"文件总数 : {len(present)}")
    print(f"待检二进制: {len(binaries)}")
    print()

    missing: dict[str, list[str]] = {}
    checked = 0
    for binary in sorted(binaries):
        try:
            pe = pefile.PE(str(binary), fast_load=True)
            pe.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
            )
        except Exception as exc:  # noqa: BLE001 - 读不了就报出来，别静默跳过
            print(f"  ! 读不出导入表 {binary.name}: {exc}")
            continue
        imports = getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []
        for entry in imports:
            name = entry.dll.decode("ascii", "replace").lower()
            checked += 1
            if name.startswith(_SYSTEM_PREFIXES) or name in _ALWAYS_SYSTEM:
                continue
            if name in present:
                continue
            if (sysdir / name).exists():
                continue
            missing.setdefault(name, []).append(binary.name)
        pe.close()

    print(f"检查了 {checked} 条导入记录。")
    if not missing:
        print("✓ 没有悬空依赖：每个被导入的 DLL 要么在产物里，要么在系统目录里。")
        return 0

    print(f"✗ 发现 {len(missing)} 个找不到的依赖：")
    for name, users in sorted(missing.items()):
        print(f"    {name}")
        for user in sorted(set(users)):
            print(f"        <- {user}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
