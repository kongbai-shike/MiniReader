"""MiniReader - a floating mini window for reading local TXT novels.

A standalone Windows app that reuses the reading kernel of the DSH
`dsh-novel-wallpaper` plugin: the encoding sniffer, the chapter splitter and the
wheel-scroll model are ports, not rewrites, and are verified against the frozen
JavaScript kernel by the test suite.

Layout rule that keeps the tests honest:
    minireader.core.*   pure logic, never imports Qt, runs headless
    minireader.qt       the only place a Qt binding is imported
    minireader.ui.*     widgets
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
