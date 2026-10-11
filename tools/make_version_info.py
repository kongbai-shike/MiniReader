"""生成 PyInstaller 的 --version-file。

为什么用脚本生成，而不是手写一个 build/version_info.txt 提交进仓库：

版本号散在三个地方最烦人——pyproject/包里的 __version__、exe 的属性页、还有安装包
的名字。这里让版本号只有一个来源（minireader.__version__），其余全部派生。跑一次
打包脚本就自动重新生成，不会出现「exe 属性里写着 0.1.0 但代码已经是 0.2.0」。

用法：
    py -3.12 tools/make_version_info.py [输出路径]

默认输出 build/version_info.txt。注意这个文件是**生成物**，不要手工编辑，
也不建议提交进仓库（见 .gitignore）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# 产品信息。改这里就改了 exe 属性页上显示的内容。
COMPANY = "MiniReader"
PRODUCT = "MiniReader"
DESCRIPTION = "MiniReader —— 置顶悬浮窗小说阅读器"
COPYRIGHT = "Copyright (C) 2026 MiniReader contributors"
ORIGINAL_FILENAME = "MiniReader.exe"
INTERNAL_NAME = "MiniReader"
# 0x0804 = 简体中文(中国)，1200 = Unicode 代码页。
# 这两个值必须和下面 StringTable 的键 '080404B0' 对得上，否则属性页会显示成方块。
LANG_ID = 0x0804
CODEPAGE = 1200


def read_version(package_dir: Path) -> str:
    """从包里读 __version__。

    用 ast 解析而不是 import：import minireader 会把整个包拉起来（连带 Qt），
    为了拿一个字符串不值得，而且打包脚本不该依赖运行环境能起 Qt。
    """
    src = (package_dir / "__init__.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return ast.literal_eval(node.value)
    raise SystemExit("没有在 minireader/__init__.py 里找到 __version__")


def as_quad(version: str) -> tuple[int, int, int, int]:
    """'0.1.0' -> (0, 1, 0, 0)。Windows 的固定版本字段必须正好四段数字。

    '0.1.0-beta' 这种带后缀的也收：负数/非数字一律当 0，只取前四段。
    """
    parts: list[int] = []
    for chunk in version.replace("-", ".").replace("+", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    parts = (parts + [0, 0, 0, 0])[:4]
    return tuple(parts)  # type: ignore[return-value]


def render(version: str) -> str:
    quad = as_quad(version)
    quad_s = ", ".join(str(n) for n in quad)
    table_key = f"{LANG_ID:04X}{CODEPAGE:04X}"
    strings = [
        ("CompanyName", COMPANY),
        ("FileDescription", DESCRIPTION),
        ("FileVersion", version),
        ("InternalName", INTERNAL_NAME),
        ("LegalCopyright", COPYRIGHT),
        ("OriginalFilename", ORIGINAL_FILENAME),
        ("ProductName", PRODUCT),
        ("ProductVersion", version),
    ]
    body = "\n".join(
        f"          StringStruct({name!r}, {value!r})," for name, value in strings
    )
    return f"""# 由 tools/make_version_info.py 生成，不要手改。
# 版本号来源：minireader/__init__.py 的 __version__ = {version!r}
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad_s}),
    prodvers=({quad_s}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        {table_key!r},
        [
{body}
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [{LANG_ID}, {CODEPAGE}])])
  ]
)
"""


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    version = read_version(root / "minireader")
    out = Path(argv[1]).resolve() if len(argv) > 1 else root / "build" / "version_info.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(version), encoding="utf-8")
    print(f"版本    : {version} -> 固定字段 {as_quad(version)}")
    print(f"已写出  : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
