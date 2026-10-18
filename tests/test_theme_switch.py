"""① 主题切换（暗夜 / 白昼）的专项测试。

`tests/test_theme.py` 只管**当前**调色板的对比度数学；这里管"切换"这件事本身：
两套调色板各自过 AA、切换是就地改写而不是重新绑定（否则静默失效）、样式表里没有
写死的颜色、切换不碰窗口属性、正文配色的「体贴规则」、两个入口（顶栏 🌙 与设置页
下拉）互相同步、以及落盘/回落。

★ 主题是**进程级可变状态**，所以每个用例都在 setUp 里记下原值、tearDown 里装回去。
不这么做的话，一个把主题切到白昼的用例会污染后面所有模块的样式表断言。
"""

from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from minireader.qt import QApplication, Qt
from minireader.storage import THEME_NAMES as STORAGE_THEME_NAMES, Settings
from minireader.ui import theme
from minireader.ui import reader as reader_module
from minireader.ui import settings_dialog as dialog_module
from minireader.ui import window as window_module
from minireader.ui.settings_dialog import SettingsDialog

from .test_ui_offscreen import WindowTestCase, make_book

_app = QApplication.instance() or QApplication([])

# 样式表输出里允许出现的颜色，只可能是活动调色板、活动 extras，或主按钮那几个与主题
# 无关的橙（`_PRIMARY*`）。多出任何一个 hex 都说明有人在 QSS 源码里写死了颜色。
def _allowed_hexes() -> set[str]:
    allowed = set(theme.PALETTE.values()) | set(theme.ACTIVE_EXTRAS.values())
    allowed |= {
        dialog_module._PRIMARY,
        dialog_module._PRIMARY_HOVER,
        dialog_module._PRIMARY_PRESSED,
        dialog_module._PRIMARY_FG,
    }
    return {colour.lower() for colour in allowed}


# 六个产出样式表的函数：三个模块级常量改成函数的那几个，加上本来就按调用求值的两个。
STYLESHEET_PRODUCERS: tuple[tuple[str, object], ...] = (
    ("settings_dialog._stylesheet", dialog_module._stylesheet),
    ("window._qss", window_module._qss),
    ("window.reader_menu_qss", window_module.reader_menu_qss),
    ("window.tooltip_qss", window_module.tooltip_qss),
    ("window.pdf_ask_qss", window_module.pdf_ask_qss),
    ("reader._scrollbar_qss", reader_module._scrollbar_qss),
)


class ThemeSwitchCase(unittest.TestCase):
    """把主题状态原样装回去，免得污染其它模块。"""

    def setUp(self) -> None:
        self._theme_before = theme.CURRENT
        theme.apply_theme("dark")

    def tearDown(self) -> None:
        theme.apply_theme(self._theme_before)


class TestPaletteSwitching(ThemeSwitchCase):
    def test_the_two_themes_share_the_same_keys(self) -> None:
        """按下标取值的代码到处都是，缺一个键就是 KeyError（或更糟：取到旧值）。"""
        keys = {name: set(table) for name, table in theme.THEMES.items()}
        first = keys[theme.THEME_NAMES[0]]
        for name, table in keys.items():
            self.assertEqual(table, first, name)
        self.assertEqual(len(first), 11, "调色板是 11 键")
        self.assertEqual(set(theme.EXTRAS), set(theme.THEMES))

    def test_the_storage_whitelist_matches_the_ui_themes(self) -> None:
        """storage.py 故意不 import ui.theme（那是反依赖），所以两份名单要钉在一起。"""
        self.assertEqual(tuple(STORAGE_THEME_NAMES), tuple(theme.THEMES))
        self.assertEqual(set(theme.THEME_LABELS), set(theme.THEME_NAMES))

    def test_every_theme_passes_the_contrast_audit(self) -> None:
        """白昼那 11 对必须和深色一样过 AA 4.5，最紧的一对也要过。"""
        for name in theme.THEME_NAMES:
            theme.apply_theme(name)
            self.assertEqual(len(theme.audit()), len(theme.TEXT_PAIRS), name)
            for role, ratio, passes in theme.audit():
                self.assertTrue(passes, f"{name}: {role} is {ratio:.2f}:1, below AA")
                self.assertGreaterEqual(round(ratio, 2), theme.AA_NORMAL, f"{name}: {role}")

    def test_switching_rewrites_the_same_dict_rather_than_rebinding_it(self) -> None:
        """★最关键的一条。五个模块都是 `from .theme import PALETTE` 拿到的旧引用，
        重新绑定名字会让切换**静默失效且不报错**。"""
        before = id(theme.PALETTE)
        extras_before = id(theme.ACTIVE_EXTRAS)
        dark_bg = theme.PALETTE["bg"]
        theme.apply_theme("light")
        self.assertEqual(id(theme.PALETTE), before, "PALETTE 必须是就地改写")
        self.assertEqual(id(theme.ACTIVE_EXTRAS), extras_before, "EXTRAS 同理")
        self.assertEqual(theme.PALETTE["bg"], theme.THEMES["light"]["bg"])
        self.assertNotEqual(theme.PALETTE["bg"], dark_bg)

    def test_an_alias_held_by_another_module_sees_the_switch(self) -> None:
        """模拟 `from .theme import PALETTE`：那个名字必须指向**活**的调色板。"""
        from minireader.ui.theme import PALETTE as alias

        theme.apply_theme("light")
        self.assertEqual(alias["fg"], theme.THEMES["light"]["fg"])
        theme.apply_theme("dark")
        self.assertEqual(alias["fg"], theme.THEMES["dark"]["fg"])

    def test_an_unknown_name_falls_back_to_dark(self) -> None:
        """设置文件可以被手改坏，一个坏字段不该让窗口打不开。"""
        for bad in ("", "Light", "solarized", None, 42):
            self.assertEqual(theme.apply_theme(bad), "dark", repr(bad))
            self.assertEqual(theme.PALETTE, theme.THEMES["dark"])

    def test_a_round_trip_is_lossless(self) -> None:
        theme.apply_theme("dark")
        dark = dict(theme.PALETTE)
        dark_extra = dict(theme.ACTIVE_EXTRAS)
        theme.apply_theme("light")
        theme.apply_theme("dark")
        self.assertEqual(theme.PALETTE, dark, "11 键逐键相等")
        self.assertEqual(theme.ACTIVE_EXTRAS, dark_extra)
        self.assertEqual(theme.CURRENT, "dark")

    def test_next_theme_cycles_through_the_whole_list(self) -> None:
        """点满一圈必须把每一档都走到，且最后一档的下一档回到起点。"""
        names = list(theme.THEME_NAMES)
        seen = []
        name = theme.DEFAULT_THEME
        for _ in range(len(names)):
            name = theme.next_theme(name)
            seen.append(name)
        self.assertEqual(seen, names[1:] + names[:1], "从出厂档起算的整圈轮转")
        self.assertEqual(theme.next_theme(seen[-1]), seen[0], "环要闭合")
        self.assertEqual(theme.next_theme("garbage"), names[1], "非法名从当前档起算")


class TestStylesheetsFollowTheTheme(ThemeSwitchCase):
    def test_no_stylesheet_invents_a_colour_of_its_own(self) -> None:
        """★白昼主题下"白底白字"那类 bug 只有真桌面撞得见，所以在这里堵住：
        样式表里出现的每一个 6 位 hex 都必须来自活动调色板/活动 extras/主按钮橙。"""
        for name in theme.THEME_NAMES:
            theme.apply_theme(name)
            allowed = _allowed_hexes()
            for label, producer in STYLESHEET_PRODUCERS:
                found = {hit.lower() for hit in re.findall(r"#[0-9a-fA-F]{6}", producer())}
                stray = found - allowed
                self.assertFalse(stray, f"{name} / {label} 里混进了写死的颜色：{sorted(stray)}")

    def test_the_three_converted_constants_differ_between_themes(self) -> None:
        """模块级常量在 import 时就插值定死了，切主题不会重算——所以它们必须是函数。
        两套主题下产出不同，正是"它真的按调用求值"的证据。"""
        for label, producer in STYLESHEET_PRODUCERS:
            theme.apply_theme("dark")
            dark = producer()
            theme.apply_theme("light")
            light = producer()
            self.assertNotEqual(dark, light, f"{label} 必须随主题变")

    def test_the_reader_scrollbar_flips_direction(self) -> None:
        """深色用白色 rgba 提亮，白昼必须反过来——否则白底上两根手柄完全看不见。"""
        theme.apply_theme("dark")
        self.assertIn("rgba(255,255,255", reader_module._scrollbar_qss())
        theme.apply_theme("light")
        light = reader_module._scrollbar_qss()
        self.assertIn("rgba(0,0,0", light)
        self.assertNotIn("rgba(255,255,255", light)


class TestWindowFollowsTheTheme(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._theme_before = theme.CURRENT
        theme.apply_theme("dark")

    def tearDown(self) -> None:
        theme.apply_theme(self._theme_before)
        super().tearDown()

    def test_switching_only_moves_the_background_colour(self) -> None:
        """切主题只走 apply_background() 那条通道：windowFlags 与半透明属性一个字不碰
        （那三样在无边框 + 半透明顶层窗上都会触发原生窗口重建，是崩溃老巢）。"""
        window = self.make_window(path=self.book_path)
        flags_before = int(window.windowFlags())
        translucent_before = bool(window.testAttribute(Qt.WA_TranslucentBackground))
        dark_bg_color = window.bg_color.name()

        resolved = window.apply_theme("light", persist=False)

        self.assertEqual(resolved, "light")
        self.assertEqual(int(window.windowFlags()), flags_before, "windowFlags 不许变")
        self.assertEqual(
            bool(window.testAttribute(Qt.WA_TranslucentBackground)),
            translucent_before,
            "半透明属性不许变",
        )
        # 窗口底色画的是**正文底色**（settings.bg），不是 panel：深色下两者恰好都是
        # #1b1b1b，白昼下窗口是 #ffffff 而面板是 #f7f7f8。所以这里对齐「体贴规则」
        # 换过去的那一档，而不是 PALETTE["bg"]。
        self.assertEqual(window.bg_color.name(), theme.read_defaults("light")[0])
        self.assertNotEqual(window.bg_color.name(), dark_bg_color, "底色真的换了")

    def test_the_tooltip_block_is_replaced_rather_than_stacked(self) -> None:
        """幂等标记的坑：原来是 `if "MiniReaderTooltip" not in app.styleSheet()`，
        切主题后标记还在，新样式永远挂不进去（主窗变白、tooltip 仍黑）。"""
        window = self.make_window(path=self.book_path)
        window.apply_theme("light", persist=False)
        sheet = _app.styleSheet()
        self.assertEqual(sheet.count("MiniReaderTooltip"), 1, "旧块必须先摘掉")
        self.assertIn(theme.ACTIVE_EXTRAS["tooltip_bg"], sheet)
        self.assertNotIn(theme.EXTRAS["dark"]["tooltip_bg"], sheet, "深色 tooltip 不许残留")
        window.apply_theme("dark", persist=False)
        sheet = _app.styleSheet()
        self.assertEqual(sheet.count("MiniReaderTooltip"), 1)
        self.assertIn(theme.ACTIVE_EXTRAS["tooltip_bg"], sheet)

    def test_the_body_colours_follow_when_the_user_never_touched_them(self) -> None:
        """「体贴规则」：正文配色仍等于**原主题**的默认值 ⇒ 用户没动过 ⇒ 跟着切。"""
        window = self.make_window(path=self.book_path)
        self.assertEqual((window.settings.bg, window.settings.fg), theme.READ_DEFAULTS["dark"])
        window.apply_theme("light", persist=False)
        self.assertEqual((window.settings.bg, window.settings.fg), theme.READ_DEFAULTS["light"])
        self.assertIn(theme.READ_DEFAULTS["light"][1], window.reader.styleSheet())

    def test_body_colours_the_user_picked_are_kept(self) -> None:
        """用户自己挑过的配色一律保留——切主题不该把"护眼米黄"顶掉。"""
        window = self.make_window(path=self.book_path)
        window.settings.bg = "#f5efe0"
        window.settings.fg = "#3a352c"
        window.reader.apply_style(window.settings)
        window.apply_theme("light", persist=False)
        self.assertEqual((window.settings.bg, window.settings.fg), ("#f5efe0", "#3a352c"))

    def test_the_top_bar_button_and_the_dialog_combo_agree(self) -> None:
        """两个入口必须一致：🌙 切了，设置页那行下拉也要跟着走。"""
        window = self.make_window(path=self.book_path)
        self.assertTrue(hasattr(window, "theme_button"), "顶栏要有 🌙 按钮")
        dialog = SettingsDialog(window.settings, None)
        try:
            window._settings_dialog = dialog
            window._sync_theme_controls()
            self.assertEqual(dialog.theme_combo.currentData(), "dark")

            window.toggle_theme()      # 深 → 浅
            self.assertEqual(theme.CURRENT, "light")
            self.assertEqual(dialog.theme_combo.currentData(), "light", "下拉必须同步")
            self.assertIn("白昼", window.theme_button.toolTip())

            window.toggle_theme()      # 浅 → 深
            self.assertEqual(dialog.theme_combo.currentData(), "dark")
            self.assertIn("暗夜", window.theme_button.toolTip())
        finally:
            window._settings_dialog = None
            dialog.deleteLater()

    def test_the_theme_button_is_not_a_toggle(self) -> None:
        """点一下切一次，不是开关——所以不能是可勾选按钮（否则会留下"按下"的假状态）。"""
        window = self.make_window(path=self.book_path)
        self.assertFalse(window.theme_button.isCheckable())

    def test_the_reader_menu_is_reskinned(self) -> None:
        """右键菜单是长生命周期对象，样式表只在 `_build_reader_menu` 里挂一次。
        切主题必须重挂，否则白昼主窗配一张黑菜单。

        ★这条是回归测试：`_apply_theme` 一开始按 `getattr(self, "_reader_menu", None)`
        找菜单，而属性其实叫 `reader_menu`——`getattr` 的默认值把拼写错误吞了，
        菜单永远重挂不上，而且不报错。
        """
        window = self.make_window(path=self.book_path)
        window.apply_theme("light", persist=False)
        self.assertEqual(window.reader_menu.styleSheet(), window_module.reader_menu_qss())
        self.assertIn(theme.ACTIVE_EXTRAS["menu_bg"], window.reader_menu.styleSheet())
        window.apply_theme("dark", persist=False)
        self.assertEqual(window.reader_menu.styleSheet(), window_module.reader_menu_qss())
        self.assertIn(theme.ACTIVE_EXTRAS["menu_bg"], window.reader_menu.styleSheet())

    def test_switching_persists_and_reloads(self) -> None:
        window = self.make_window(path=self.book_path)
        window.apply_theme("light")
        self.assertEqual(window.settings.theme, "light")
        reloaded = Settings.load(window._settings_path)
        self.assertEqual(reloaded.theme, "light")
        # 启动时 __main__ 就是这么把它装回去的。
        theme.apply_theme(reloaded.theme)
        self.assertEqual(theme.PALETTE["bg"], theme.THEMES["light"]["bg"])


class TestDialogThemeRow(ThemeSwitchCase):
    def _dialog(self, settings=None, **kw):
        return SettingsDialog(settings or Settings(), None, **kw)

    def test_the_dropdown_offers_every_theme_in_order(self) -> None:
        dialog = self._dialog()
        try:
            offered = [
                dialog.theme_combo.itemData(i) for i in range(dialog.theme_combo.count())
            ]
            self.assertEqual(tuple(offered), tuple(theme.THEME_NAMES))
            self.assertEqual(dialog.theme_combo.currentData(), "dark")
        finally:
            dialog.deleteLater()

    def test_picking_a_theme_previews_without_saving(self) -> None:
        seen = []
        settings = Settings()
        dialog = self._dialog(settings, on_theme=seen.append)
        try:
            index = dialog.theme_combo.findData("light")
            dialog.theme_combo.setCurrentIndex(index)
            self.assertEqual(seen, ["light"], "预览回调要收到主题名")
            self.assertEqual(settings.theme, "dark", "预览不写设置")
        finally:
            dialog.deleteLater()

    def test_apply_to_writes_the_theme_and_clamp_backs_it(self) -> None:
        from dataclasses import replace

        dialog = self._dialog()
        try:
            dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("light"))
            written = dialog.apply_to(replace(Settings()))
            self.assertEqual(written.theme, "light")
        finally:
            dialog.deleteLater()
        # clamp() 是兜底：手改坏的字段回落 dark，而不是让窗口打不开。
        broken = Settings(theme="solarized")
        broken.clamp()
        self.assertEqual(broken.theme, "dark")
        for good in theme.THEME_NAMES:
            kept = Settings(theme=good)
            kept.clamp()
            self.assertEqual(kept.theme, good, "合法值不许被 clamp 改掉")

    def test_sync_theme_does_not_fire_the_preview_callback(self) -> None:
        """顶栏 🌙 改主题时回灌下拉：主题已经是那个了，再切一次是白跑一趟。"""
        seen = []
        dialog = self._dialog(on_theme=seen.append)
        try:
            dialog.sync_theme("light")
            self.assertEqual(dialog.theme_combo.currentData(), "light")
            self.assertEqual(seen, [], "回灌不得触发预览")
        finally:
            dialog.deleteLater()

    def test_restore_defaults_puts_the_theme_back_without_previewing(self) -> None:
        seen = []
        dialog = self._dialog(on_theme=seen.append)
        try:
            dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("light"))
            seen.clear()
            dialog._restore_defaults()
            self.assertEqual(dialog.theme_combo.currentData(), Settings().theme)
            self.assertEqual(seen, [], "恢复默认要等按确定才真变")
        finally:
            dialog.deleteLater()


class TestStartupWiring(unittest.TestCase):
    def test_the_theme_is_installed_before_any_window_exists(self) -> None:
        """晚一步就会先画一帧深色再跳成浅色。这是源码级断言：接线必须在 MiniWindow
        之前，且必须在 Settings.load() 之后。"""
        source = (
            Path(__file__).resolve().parent.parent / "minireader" / "__main__.py"
        ).read_text(encoding="utf-8")
        load_at = source.index("settings = Settings.load()")
        apply_at = source.index("apply_theme(settings.theme)")
        window_at = source.index("MiniWindow(")
        self.assertLess(load_at, apply_at, "先读设置再装主题")
        self.assertLess(apply_at, window_at, "装主题必须在建窗之前")


if __name__ == "__main__":
    unittest.main()
