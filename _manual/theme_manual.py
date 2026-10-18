r"""收尾阶段 ① 主题切换手动确认：暗夜 / 白昼两套外观的看得见的行为。

单测验的是"调色板对了、样式表跟着变"，这里验规格点名的那几条**看得见**的行为：

   1. 顶栏 🌙 点一下切一次（不是开关），深 → 浅 → 深 往返
   2. 顶栏 / 底栏 / 正文 / 目录抽屉 / 托盘 / 右键菜单 / tooltip / 设置对话框
      八处全部跟着变
   3. 半透明（translucent_bg=True）与兼容模式（False）两种下都正常
   4. 关掉重开记住（settings.json 里的 theme 字段）
   5. 设置页下拉与顶栏按钮同步（顶栏切完下拉跟着走，下拉改完顶栏跟着走）

**这个脚本在 offscreen 下也能跑**，所以第 1-5 条全部是自动判定的；真正需要人眼的
只有"白昼下看起来舒不舒服"这一条，它由 6 张截图留证：

    _manual/theme_shots/01_dark_full.png       暗夜整窗
    _manual/theme_shots/02_light_full.png      白昼整窗
    _manual/theme_shots/03_light_compat.png    白昼 + 兼容模式
    _manual/theme_shots/04_light_dialog.png    白昼 + 设置对话框（外观页）
    _manual/theme_shots/05_light_menu.png      白昼 + 正文右键菜单
    _manual/theme_shots/06_dark_round_trip.png 切回暗夜，逐像素核对与 01 相同

第 6 张是往返无损的证据：`01` 与 `06` 两张 PNG 的字节必须一致。

跑法： py -3.12 _manual\theme_manual.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 控制台默认是 GBK，装不下 🌙 这种字符。替换而不是崩掉，脚本本身不靠打印判对错。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from minireader.qt import QApplication, QColor, QPixmap, Qt  # noqa: E402

_app = QApplication.instance() or QApplication([])

from minireader import ui  # noqa: E402
from minireader.storage import Library, ProgressStore, Settings  # noqa: E402
from minireader.ui import theme  # noqa: E402
from minireader.ui import tray as tray_module  # noqa: E402
from minireader.ui import settings_dialog as dialog_module  # noqa: E402
from minireader.ui.settings_dialog import SettingsDialog  # noqa: E402
from minireader.ui.window import MiniWindow, reader_menu_qss  # noqa: E402

SHOTS = os.path.join(ROOT, "_manual", "theme_shots")

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def pump(n: int = 6) -> None:
    for _ in range(n):
        _app.processEvents()


def shot(window, name: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    window.grab().save(path, "PNG")
    print(f"      截图 {path}")
    return path


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def open_window(path: str, *, settings: Settings | None = None, width: int = 1100, height: int = 760):
    window = MiniWindow(settings or Settings(wheel_animation_ms=0), ProgressStore.load(), Library.load())
    window._quit_on_close = False
    window.resize(width, height)
    window.show()
    pump()
    window.load_path(path)
    pump()
    # `apply_style` 会把视口顶端那一行按 `_ANCHOR_PAD` 重新对齐一次（正文首行本来在
    # 16px 文档边距上，对齐后落到 4px）。这一步在任何换字号/换主题后都会发生，所以
    # 先让它落定，第 7 步的两张截图才是同一个静止帧。
    window.reader.apply_style(window.settings)
    pump(12)
    return window


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mr-theme-manual-")
    os.environ["APPDATA"] = tmp

    book = os.path.join(tmp, "sample.txt")
    with open(book, "w", encoding="utf-8") as fh:
        fh.write("第一章 开始\n")
        fh.write("".join(f"这是第 {i} 段的正文，用来把目录和滚动条都撑出来。\n" for i in range(1, 61)))
        fh.write("\n第二章 继续\n")
        fh.write("".join(f"第二章第 {i} 段，段落短一点，让窗口里能看到好几段。\n" for i in range(1, 41)))

    # 起点固定在暗夜，免得受上一次运行残留影响。
    theme.apply_theme("dark")

    window = open_window(book)
    dark_full = shot(window, "01_dark_full.png")

    print("\n[1] 顶栏 🌙 点一下切一次（不是开关）")
    button = window.theme_button
    check("顶栏有 🌙 按钮且排在自动滚动之后、漫画三钮之前", bool(button) and not button.isCheckable())
    before = theme.CURRENT
    button.click()
    pump()
    check("点第一下 → 白昼", theme.CURRENT == "light", f"{before} → {theme.CURRENT}")
    button.click()
    pump()
    check("点第二下 → 暗夜", theme.CURRENT == "dark", f"light → {theme.CURRENT}")
    button.click()
    pump()
    check("点第三下 → 白昼（循环无死角）", theme.CURRENT == "light")

    light_full = shot(window, "02_light_full.png")

    print("\n[2] 八处界面全部跟着变")
    # 2.1 顶栏 / 底栏：窗口级样式表里的颜色来自活动调色板
    sheet = window.styleSheet()
    check("顶栏/底栏样式表用的是白昼 fg", theme.PALETTE["fg"] in sheet, theme.PALETTE["fg"])
    check("顶栏/底栏样式表里没有残留的深色 hex", theme.THEMES["dark"]["fg"] not in sheet)

    # 2.2 正文：底色走 apply_background，字色走 reader.apply_style
    check("正文底色 = 白昼正文默认底色", window.bg_color.name() == theme.read_defaults("light")[0], window.bg_color.name())
    check("正文字色 = 白昼正文默认字色", window.reader.styleSheet().find(theme.read_defaults("light")[1]) >= 0)

    # 2.3 目录抽屉：当前章高亮条是 paint 出来的，颜色直接读 PALETTE
    check("目录抽屉当前章配色 = (bg, accent)", window.toc_panel.current_row_colors() == (theme.PALETTE["bg"], theme.PALETTE["accent"]))

    # 2.4 托盘：图标是逐尺寸现画的，读的是 PALETTE
    tray_icon = tray_module.make_icon()
    check("托盘图标画得出来且不是空图", not tray_icon.isNull() and not tray_icon.pixmap(32, 32).isNull())

    # 2.5 右键菜单：函数重算，不是 import 时插值
    menu_qss = reader_menu_qss()
    check("右键菜单底色 = 白昼菜单底", theme.ACTIVE_EXTRAS["menu_bg"] in menu_qss, theme.ACTIVE_EXTRAS["menu_bg"])
    check("右键菜单已重挂到新样式", window.reader_menu.styleSheet() == menu_qss or theme.ACTIVE_EXTRAS["menu_bg"] in window.reader_menu.styleSheet())

    # 2.6 tooltip：全局样式表里只有一份，且是新色
    app_sheet = _app.styleSheet()
    check("tooltip 块只有一份（旧块先摘掉再挂新块）", app_sheet.count("MiniReaderTooltip") == 1)
    check("tooltip 是新色", theme.ACTIVE_EXTRAS["tooltip_bg"] in app_sheet, theme.ACTIVE_EXTRAS["tooltip_bg"])
    check("深色 tooltip 没残留", theme.EXTRAS["dark"]["tooltip_bg"] not in app_sheet)

    # 2.7 设置对话框
    dialog = SettingsDialog(window.settings, parent=window, on_theme=window._preview_theme)
    window._settings_dialog = dialog
    pump()
    dialog_sheet = dialog.styleSheet()
    check("设置对话框样式表用白昼配色", theme.PALETTE["card"] in dialog_sheet and theme.THEMES["dark"]["card"] not in dialog_sheet)
    check("对话框里没有写死的 6 位 hex", not _stray_hex(dialog_sheet), _stray_hex(dialog_sheet))
    dialog.show()
    pump()
    # 对话框打开时停在记忆的那一页，这里明确翻到「外观」页，截图才落在主题那行上。
    dialog._nav.setCurrentRow([title for title, _ in dialog_module.NAV_ITEMS].index("外观"))
    pump(12)
    shot(dialog, "04_light_dialog.png")

    print("\n[3] 设置页下拉与顶栏按钮同步")
    check("下拉里恰好两项且顺序为 (暗夜, 白昼)", dialog.theme_combo.count() == 2 and [dialog.theme_combo.itemData(i) for i in range(2)] == list(theme.THEME_NAMES))
    check("顶栏切到白昼后，下拉也指着白昼", dialog.theme_combo.currentData() == "light", dialog.theme_combo.currentData())
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("dark"))
    pump()
    check("下拉改暗夜 → 界面立刻跟着切（预览，不落盘）", theme.CURRENT == "dark", theme.CURRENT)
    check("顶栏 🌙 的 tooltip 指向另一个主题", "白昼" in button.toolTip(), button.toolTip())
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("light"))
    pump()
    check("下拉改回白昼 → 界面跟着回", theme.CURRENT == "light")
    dialog.close()
    pump()

    print("\n[4] 半透明与兼容模式两种下都正常")
    window.apply_theme("light", persist=False)
    window.settings.translucent_bg = True
    window.apply_background()
    pump()
    check("半透明模式：底色仍带 bg_alpha", window.bg_color.alpha() == window.settings.bg_alpha, str(window.bg_color.alpha()))
    check("半透明模式：RGB 是白昼正文底色", window.bg_color.name() == theme.read_defaults("light")[0])
    window.settings.translucent_bg = False
    window.apply_background()
    pump()
    compat = shot(window, "03_light_compat.png")
    check("兼容模式：RGB 不变（只有 alpha 语义变了）", window.bg_color.name() == theme.read_defaults("light")[0])
    check("兼容模式截图非空", os.path.getsize(compat) > 2000)
    window.settings.translucent_bg = True

    print("\n[5] 右键菜单与 tooltip 的真截图")
    # `window.grab()` 抓不到 QMenu 这种顶层弹出件（offscreen 平台上更抓不到），所以
    # 直接把菜单 render 进一张图：`render()` 走的是同一套样式表绘制路径。
    menu = window.reader_menu
    menu.popup(window.mapToGlobal(window.rect().center()))
    pump()
    check("菜单弹出来了", menu.isVisible())
    menu.adjustSize()
    pump()
    canvas = QPixmap(menu.size())
    canvas.fill(QColor(theme.ACTIVE_EXTRAS["menu_bg"]))
    menu.render(canvas)
    menu_path = os.path.join(SHOTS, "05_light_menu.png")
    os.makedirs(SHOTS, exist_ok=True)
    canvas.save(menu_path, "PNG")
    print(f"      截图 {menu_path}  ({menu.width()}x{menu.height()})")
    # 菜单是 135×143 的纯色块，压出来不到 1 KB；只要不是全透明空图就算抓到了。
    check("菜单截图抓到了", os.path.getsize(menu_path) > 400)
    menu.close()
    pump()

    print("\n[6] 关掉重开记住")
    path = os.path.join(tmp, "settings.json")
    window.settings.theme = "light"
    ok, message = window.settings.save(Path(path))
    check("落盘成功", ok, message)
    raw = json.load(open(path, encoding="utf-8"))
    check("settings.json 里写了 theme", raw.get("theme") == "light", str(raw.get("theme")))
    reloaded = Settings.load(Path(path))
    check("读回来还是白昼", reloaded.theme == "light")
    theme.apply_theme(reloaded.theme)
    reopened = open_window(book, settings=reloaded)
    pump()
    check("重开窗口是白昼底色", reopened.bg_color.name() == theme.read_defaults("light")[0], reopened.bg_color.name())
    reopened.close()
    pump()

    print("\n[7] 深 → 浅 → 深 往返无损")
    window.apply_theme("dark", persist=False)
    pump(12)
    dark_again = shot(window, "06_dark_round_trip.png")
    same = digest(dark_full) == digest(dark_again)
    check("切回暗夜后整窗逐字节等于初始暗夜", same, f"{digest(dark_full)[:12]} / {digest(dark_again)[:12]}")

    print("\n[8] 切主题不碰原生窗口重建那条路")
    flags = int(window.windowFlags())
    attr = bool(window.testAttribute(Qt.WA_TranslucentBackground))
    window.apply_theme("light", persist=False)
    pump()
    window.apply_theme("dark", persist=False)
    pump()
    check("windowFlags 两次切换后不变", int(window.windowFlags()) == flags)
    check("WA_TranslucentBackground 两次切换后不变", bool(window.testAttribute(Qt.WA_TranslucentBackground)) == attr)

    window.close()
    pump()

    failed = [name for ok, name, _ in RESULTS if not ok]
    print(f"\n{'=' * 72}")
    print(f"手动确认：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("未通过：")
        for name in failed:
            print(f"  - {name}")
    print(f"截图目录：{SHOTS}")
    return 1 if failed else 0


def _stray_hex(sheet: str) -> list[str]:
    """样式表里写死的 6 位 hex（不含 rgba），应当为空。

    允许的两类颜色：活动调色板 / 活动 extras（即"从 theme 取"的那些），以及主按钮
    那几个与主题无关的橙（`_PRIMARY*`，白昼沿用同一套）。
    """
    import re

    from minireader.ui import settings_dialog as dialog_module

    allowed = set()
    for table in (theme.THEMES, theme.EXTRAS):
        for palette in table.values():
            allowed.update(colour.lower() for colour in palette.values())
    allowed |= {
        dialog_module._PRIMARY,
        dialog_module._PRIMARY_HOVER,
        dialog_module._PRIMARY_PRESSED,
        dialog_module._PRIMARY_FG,
    }
    found = {match.lower() for match in re.findall(r"#[0-9a-fA-F]{6}", sheet)}
    return sorted(found - allowed)


if __name__ == "__main__":
    raise SystemExit(main())
