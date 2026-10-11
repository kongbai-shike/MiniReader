"""PyInstaller 的入口脚本——只是一个转发。

为什么不直接把 minireader/__main__.py 当入口：

PyInstaller 会把入口脚本当作顶层 __main__ 来执行。而 minireader/__main__.py 里用的是
相对 import（`from . import __version__`），一旦按顶层脚本执行就会报
"attempted relative import with no known parent package"。

所以这里放一个转发脚本，真正的逻辑仍然只有 minireader/__main__.py 那一份。别把逻辑
写到这里来：从源码跑（py -3.12 -m minireader）和从 exe 跑必须走同一条路，
否则打包版本的行为就成了没测过的东西。
"""

import sys

from minireader.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
