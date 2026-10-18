"""Run tests/test_mobi.py as if the optional `mobi` package were not installed.

The point of the stage-6 design is that the container judgement (sniff, DRM,
corrupt, phase gate, dispatch) does not need the GPL library at all. That claim
is worth proving rather than asserting.

**How the absence is simulated matters.** An earlier version of this probe put a
meta-path finder in front that *raised* `ModuleNotFoundError`, and that is not
what a clean machine looks like: when no finder claims a name, `find_spec()`
returns `None` and only `import` raises. The probe was therefore stricter than
reality and broke a test that asserts `is_available() == (find_spec(...) is not
None)` — a false alarm caused by the probe, not by the code.

So this version removes the directory that actually holds `mobi` from
`sys.path`. Afterwards `find_spec("mobi")` returns None by itself, exactly as on
a machine where it was never installed.

Expected: the unpacking cases skip with MOBI_SKIP, everything else passes.
"""

from __future__ import annotations

import os
import sys
import unittest
from importlib.util import find_spec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def hide_mobi() -> str:
    """Drop the site-packages directory that contains `mobi` from `sys.path`."""
    spec = find_spec("mobi")
    if spec is None:
        return "(already absent)"
    origin = spec.origin or ""
    # .../site-packages/mobi/__init__.py -> .../site-packages
    package_dir = os.path.dirname(os.path.dirname(origin))
    removed = []
    for entry in list(sys.path):
        if os.path.abspath(entry) == os.path.abspath(package_dir):
            sys.path.remove(entry)
            removed.append(entry)
    for name in [n for n in sys.modules if n == "mobi" or n.startswith("mobi.")]:
        del sys.modules[name]
    return removed[0] if removed else f"(not on sys.path: {package_dir})"


def main() -> int:
    where = hide_mobi()

    from minireader.core import mobidoc

    available = mobidoc.is_available()
    print(f"hid {where}")
    print(f"mobidoc.is_available() -> {available}")
    if available:
        print("FAIL: the library is still visible, the probe proves nothing")
        return 1

    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_mobi")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(
        f"\nran={result.testsRun} skipped={len(result.skipped)} "
        f"failures={len(result.failures)} errors={len(result.errors)}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
