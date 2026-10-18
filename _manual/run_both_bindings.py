r"""收尾 2：同一个测试包在两个绑定下各跑一遍。

`minireader.qt` 的绑定选择是"先试 PySide6，失败再退 PyQt5"，所以装了 PySide6 之后
默认就是 PySide6——想验 PyQt5 必须**屏蔽** PySide6 的 import，不能靠环境变量
（项目里没有那个开关，加一个只为了测试是多余的生产代码）。

这个脚本用 `-c` 起一个子进程，在 `import minireader` **之前**把 `PySide6` 的
import 打成 ImportError，于是 `minireader.qt` 自然回落到 PyQt5，然后跑 discover。

跑法： py -3.12 _manual\run_both_bindings.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 屏蔽 PySide6 的引导代码。必须在任何 minireader import 之前执行。
_BLOCK_PYSIDE6 = """
import builtins, sys
_real = builtins.__import__
def _fake(name, *a, **k):
    if name == "PySide6" or name.startswith("PySide6."):
        raise ImportError("blocked on purpose: force the PyQt5 path")
    return _real(name, *a, **k)
builtins.__import__ = _fake
for _m in [m for m in sys.modules if m.startswith("PySide6")]:
    del sys.modules[_m]
"""

_RUNNER = """
import os, sys, unittest
sys.path.insert(0, {root!r})
from minireader.qt import BINDING, QT_VERSION
print("@@BINDING@@" + BINDING + " " + QT_VERSION)
loader = unittest.TestLoader()
suite = loader.discover({tests!r}, top_level_dir={root!r})
result = unittest.TextTestRunner(verbosity=0).run(suite)
print("@@RESULT@@%d %d %d" % (result.testsRun, len(result.failures), len(result.errors)))
"""


def run(label: str, block_pyside6: bool) -> tuple[str, int, int, int]:
    code = (_BLOCK_PYSIDE6 if block_pyside6 else "") + _RUNNER.format(
        root=ROOT, tests=os.path.join(ROOT, "tests")
    )
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, env=env,
    )
    binding = "?"
    for line in proc.stdout.splitlines():
        if line.startswith("@@BINDING@@"):
            binding = line[len("@@BINDING@@"):]
    match = re.search(r"@@RESULT@@(\d+) (\d+) (\d+)", proc.stdout)
    if match is None:
        print(f"--- {label}: 没拿到结果，输出尾部 ---")
        print((proc.stdout or "")[-1500:])
        print((proc.stderr or "")[-1500:])
        return (binding, -1, -1, -1)
    ran, failures, errors = (int(g) for g in match.groups())
    print(f"--- {label}")
    print(f"    绑定：{binding}")
    print(f"    跑 {ran} 条，失败 {failures}，错误 {errors}")
    return (binding, ran, failures, errors)


def main() -> None:
    print(f"Python {sys.version.split()[0]}\n")
    results = []
    for label, block in (("PySide6（默认路径）", False), ("PyQt5（屏蔽 PySide6 强制走）", True)):
        results.append(run(label, block))
        print()

    print("=" * 60)
    ok = True
    for (binding, ran, failures, errors), label in zip(
        results, ("PySide6", "PyQt5")
    ):
        good = ran > 0 and failures == 0 and errors == 0
        ok = ok and good
        print(f"{'OK  ' if good else 'FAIL'} {label:<8} {binding:<22} "
              f"{ran} 条，失败 {failures}，错误 {errors}")
    print("=" * 60)
    print("两个绑定都全绿" if ok else "有绑定未通过")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
