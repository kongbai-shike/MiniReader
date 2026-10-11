"""MiniReader 的桌面应用入口（双击启动 / PyInstaller 打包都走这里）。

源码运行：
    py -3.12 main.py [小说.txt] [--smoke 秒]

这个外壳只负责"把脚本变成桌面应用"的四件事，业务逻辑全部还在 minireader 包里：

1. **日志落盘** —— logging 写 ``%APPDATA%\\MiniReader\\logs\\app.log``，不用 print：
   ``--noconsole`` 打包后 sys.stdout 可能是 None，print 会直接抛异常。
2. **异常兜底** —— sys.excepthook 把未处理异常写日志并弹 QMessageBox，窗口化程序
   没有控制台，不兜底就是"双击没反应"。
3. **单实例** —— QLocalServer/QLocalSocket 走本地命名管道/socket：第二个实例把
   b"SHOW" 发给第一个实例让它把窗口唤到前台，然后自己退出。
4. **常驻托盘** —— setQuitOnLastWindowClosed(False)，✕ 只藏不退；窗口、托盘、单
   实例服务器都由 TrayApp 持有强引用（局部变量会被 GC，托盘图标随之消失）。

打包（第 4 步再做，记录在此免得忘）：
    pip install pyinstaller
    pyinstaller --noconsole --onedir --name MiniReader --icon=assets/minireader.ico \
        --add-data "assets/minireader.ico;assets" main.py
    # --noconsole 去 cmd 黑框；--onedir 启动快，换单文件用 --onefile；
    # --add-data 在 Windows 用 ; 分隔，macOS/Linux 用 :。
    # 注意：单实例用到 QtNetwork，打包时不要在 excludes 里排 PyQt5.QtNetwork。
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

# 双击 / PyInstaller 两种情况下脚本目录通常已在 sys.path 上，这里再兜底一次，保证
# `from minireader... import` 无论从哪个 cwd 启动都成立。
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from minireader.app_paths import log_dir  # noqa: E402

# 本地命名管道（Windows）/ 本地 domain socket（其它平台）的服务名。
SERVER_NAME = "MiniReader.SingleInstance"
_SHOW_MESSAGE = b"SHOW"

logger = logging.getLogger("minireader")


# ---------------------------------------------------------------------------
# 日志与异常兜底
# ---------------------------------------------------------------------------

def configure_logging() -> logging.Logger:
    """文件日志为主（--noconsole 下唯一可靠的出口），控制台有就挂一份。"""
    log = logging.getLogger("minireader")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        file_handler = logging.FileHandler(log_dir() / "app.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError:
        # 日志目录都建不起来时不能拖垮启动：放弃文件日志即可。
        pass

    # --noconsole 下 sys.stdout/sys.stderr 可能为 None；有一个能用就同步一份，方便
    # 源码运行时在终端里看。
    for stream in (sys.stderr, sys.stdout):
        if stream is not None:
            try:
                console = logging.StreamHandler(stream)
                console.setFormatter(formatter)
                log.addHandler(console)
                break
            except Exception:  # noqa: BLE001 - 挂不上控制台日志无所谓
                continue
    return log


def install_excepthook(log: logging.Logger) -> None:
    """未处理异常：先落日志，再弹框，最后交回默认钩子（保退出码/调试器行为）。"""

    def hook(exc_type, exc, tb) -> None:
        log.critical("未处理异常", exc_info=(exc_type, exc, tb))
        # KeyboardInterrupt / SystemExit 是 BaseException，不是程序错误，不弹窗。
        if issubclass(exc_type, Exception):
            try:
                from minireader.qt import QApplication, QMessageBox

                if QApplication.instance() is not None:
                    detail = "".join(traceback.format_exception(exc_type, exc, tb))
                    QMessageBox.critical(
                        None,
                        "MiniReader 遇到错误",
                        "程序遇到未处理的错误（详情已写入 logs/app.log）：\n\n"
                        + detail[-3000:],
                    )
            except Exception:  # noqa: BLE001 - 兜底本身不能再炸
                log.exception("异常弹窗也失败了")
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------

def signal_existing_instance(server_name: str = SERVER_NAME, timeout_ms: int = 300) -> bool:
    """尝试让已在运行的实例把窗口叫出来。连上了返回 True（调用方应退出自己）。"""
    from minireader.qt import QLocalSocket

    if QLocalSocket is None:
        return False
    socket = QLocalSocket()
    try:
        socket.connectToServer(server_name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(_SHOW_MESSAGE)
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True
    except Exception:  # noqa: BLE001 - 连接阶段的任何异常都按"没有老实例"处理
        logger.exception("联系已有实例失败，按新实例继续启动")
        return False
    finally:
        try:
            socket.deleteLater()
        except Exception:  # noqa: BLE001
            pass


class SingleInstanceServer:
    """第一个实例的监听端：收到 SHOW 就把主窗口请到前台。"""

    def __init__(self, present, server_name: str = SERVER_NAME) -> None:
        from minireader.qt import QLocalServer

        self._present = present
        self._server_name = server_name
        self._server = QLocalServer()
        # 必须在 listen 之前清掉同名残留：上次进程被强杀时命名管道不会自己注销，
        # 不清的话 listen 直接失败，表现为单实例永久失效。
        QLocalServer.removeServer(server_name)
        if not self._server.listen(server_name):
            logger.warning("单实例服务监听失败：%s", self._server.errorString())
        self._server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        connection = self._server.nextPendingConnection()
        if connection is None:
            return
        try:
            connection.waitForReadyRead(100)
            data = bytes(connection.readAll())
        except Exception:  # noqa: BLE001
            data = b""
        finally:
            try:
                connection.disconnectFromServer()
                connection.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        # 目前协议只有一条消息（SHOW）；内容对不上也照样唤窗，保持宽容。
        logger.info("收到第二个实例的请求：%r，唤出主窗口", data or b"<empty>")
        try:
            self._present()
        except Exception:  # noqa: BLE001 - IPC 槽里的异常绝不能炸掉主进程
            logger.exception("响应第二个实例时唤窗失败")

    def close(self) -> None:
        from minireader.qt import QLocalServer

        try:
            self._server.close()
            QLocalServer.removeServer(self._server_name)
        except Exception:  # noqa: BLE001
            pass


class TrayApp:
    """桌面外壳状态的强引用持有者。

    必须在 main() 里以模块级容器持有：QSystemTrayIcon/QLocalServer 都是 QObject，
    一旦 Python 侧引用被回收，托盘图标就消失、第二个实例也唤不醒窗口。
    """

    def __init__(self, app, window, tray, server) -> None:
        self.app = app
        self.window = window
        self.tray = tray
        self.server = server

    def real_quit(self) -> None:
        """唯一的"真退出"序列：置标志 → 关窗 → 隐托盘 → 退进程。

        托盘菜单的"退出"走 window.quit_app()，效果等价（closeEvent 的真关闭分支会
        shutdown 托盘并调度 app.quit）；这个方法留给外壳需要主动结束时调用。
        """
        window = self.window
        window._really_quit = True
        window.close()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    log = configure_logging()
    args = list(sys.argv[1:] if argv is None else argv)

    # CLI 工具模式不需要单实例、托盘和窗口外壳，直接交给包内原有入口处理。
    if "--help" in args or "-h" in args or "--check" in args:
        from minireader.__main__ import main as cli_main

        return int(cli_main(args))

    install_excepthook(log)

    from minireader.qt import QApplication, enable_high_dpi

    # 单实例握手必须在主窗口（甚至任何重活）之前：第二个实例的全部职责就是发消息
    # 然后退出，不该去加载书库、注册热键。
    if signal_existing_instance():
        log.info("已有实例在运行，已通知它显示窗口，本实例退出")
        return 0

    # AA_EnableHighDpiScaling 这类属性必须在 QApplication 之前设。
    enable_high_dpi()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("MiniReader")
    app.setApplicationDisplayName("MiniReader")
    # 关窗口不退出：✕ 只 hide()，退出只认托盘菜单（见 window.closeEvent）。
    app.setQuitOnLastWindowClosed(False)

    # 强引用持有区：TrayApp / QLocalServer 都是 QObject，一旦被 GC，托盘图标和单实例
    # 监听会一起消失。holders 是 main() 的局部变量，生命周期 = 整个事件循环
    # （run_gui 阻塞到退出），正好覆盖应用的一生。
    holders: list[TrayApp] = []

    from minireader.__main__ import main as run_gui

    def on_ready(window, tray, _app) -> None:
        server = None
        try:
            server = SingleInstanceServer(window.present_window)
            log.info("单实例服务已启动：%s", SERVER_NAME)
        except Exception:  # noqa: BLE001 - 单实例失效不影响正常使用
            log.exception("单实例服务启动失败，继续运行")
        holders.append(TrayApp(app, window, tray, server))

    try:
        return int(run_gui(args, app=app, on_ready=on_ready))
    except Exception:
        log.critical("启动失败", exc_info=True)
        try:
            from minireader.qt import QMessageBox

            QMessageBox.critical(
                None,
                "MiniReader 启动失败",
                "启动失败，详情见 logs/app.log：\n\n"
                + traceback.format_exc()[-3000:],
            )
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        for holder in tuple(holders):
            if holder.server is not None:
                holder.server.close()


if __name__ == "__main__":
    raise SystemExit(main())
