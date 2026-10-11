"""app_paths：源码/打包两种形态下的资源路径与可写目录策略。

以及根目录 main.py 里单实例握手（QLocalServer / QLocalSocket）的往返测试。
两者放一起是因为 main.py 唯一依赖的"路径外基础设施"就是 QtNetwork，而这个平台有
没有 QtNetwork 直接决定这些用例能不能跑——跳过而不是假装通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader import app_paths  # noqa: E402
from minireader.qt import QLocalServer, QLocalSocket  # noqa: E402


class AppPathsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._appdata = os.environ.get("APPDATA")
        self.tmp = Path(tempfile.mkdtemp(prefix="mr-paths-"))
        os.environ["APPDATA"] = str(self.tmp)
        self._frozen = getattr(sys, "frozen", None)
        self._meipass = getattr(sys, "_MEIPASS", None)

    def tearDown(self) -> None:
        if self._appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._appdata
        if self._frozen is None:
            if hasattr(sys, "frozen"):
                delattr(sys, "frozen")
        else:
            sys.frozen = self._frozen
        if self._meipass is None:
            if hasattr(sys, "_MEIPASS"):
                delattr(sys, "_MEIPASS")
        else:
            sys._MEIPASS = self._meipass

    def test_is_frozen_is_false_in_a_source_checkout(self) -> None:
        self.assertFalse(app_paths.is_frozen())

    def test_resource_path_points_at_the_project_root_in_source(self) -> None:
        icon = Path(app_paths.resource_path(os.path.join("assets", "minireader.ico")))
        # 源码形态：资源就在仓库根的 assets/ 下，文件真的在。
        self.assertTrue(icon.is_file(), icon)

    def test_resource_path_uses_meipass_when_frozen(self) -> None:
        sys.frozen = True
        sys._MEIPASS = str(self.tmp / "unpacked")
        self.assertEqual(
            Path(app_paths.resource_path("assets/x.ico")),
            self.tmp / "unpacked" / "assets" / "x.ico",
        )

    def test_user_data_dir_is_appdata_appname_and_is_created(self) -> None:
        target = self.tmp / app_paths.APP_NAME
        self.assertFalse(target.exists())
        self.assertEqual(app_paths.user_data_dir(), target)
        self.assertTrue(target.is_dir(), "目录不存在时必须自己建好")

    def test_log_dir_lives_under_the_user_data_dir(self) -> None:
        logs = app_paths.log_dir()
        self.assertEqual(logs, app_paths.user_data_dir() / "logs")
        self.assertTrue(logs.is_dir())

    def test_a_failed_mkdir_degrades_to_a_home_dotdir(self) -> None:
        """首选目录建不出来（只读配置目录）时退到 ~/.minireader，绝不抛异常。"""
        original = Path.mkdir
        blocked = self.tmp

        def refuse(self, *args, **kwargs):
            if str(self).startswith(str(blocked)):
                raise OSError("read-only")
            return original(self, *args, **kwargs)

        Path.mkdir = refuse
        try:
            path = app_paths.user_data_dir()
        finally:
            Path.mkdir = original
        self.assertEqual(path, Path.home() / f".{app_paths.APP_NAME.lower()}")
        self.assertTrue(path.is_dir())


@unittest.skipIf(
    QLocalServer is None or QLocalSocket is None,
    "此 Qt 绑定不含 QtNetwork，单实例功能整体降级",
)
class SingleInstanceCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from minireader.qt import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.server_name = f"MiniReader.Test.{uuid.uuid4().hex}"

    def test_no_existing_instance_reports_false(self) -> None:
        import main as entry

        # 全新的随机名，不可能有监听者；超时给短一点，别让用例干等 300ms。
        self.assertFalse(
            entry.signal_existing_instance(self.server_name, timeout_ms=50)
        )

    def test_the_second_instance_makes_the_first_present_its_window(self) -> None:
        import main as entry

        calls = []
        server = entry.SingleInstanceServer(lambda: calls.append("show"), self.server_name)
        try:
            self.assertTrue(entry.signal_existing_instance(self.server_name, timeout_ms=500))
            # 命名管道的到达是异步的：推几圈事件循环让 newConnection 槽跑完。
            for _ in range(10):
                self.app.processEvents()
                if calls:
                    break
            self.assertEqual(calls, ["show"])
        finally:
            server.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
