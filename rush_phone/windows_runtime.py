"""Windows 启动器的配置、后台任务与日志；不改变现有命令行入口。"""
from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue

from .chrome_session import ChromeSessionError, open_chrome_session
from .config import AccountConfig, AppConfig, ConfigError, load_config
from .private_files import PrivateFileError, protect_private_path
from .session_test import run_session_purchase


PRESETS = {"duo": "duo.toml", "pro-max": "pro-max.toml"}


def window_message(message: str) -> str:
    if "未找到系统 Google Chrome" in message:
        return "未找到系统 Google Chrome；请安装 Google Chrome 后再点击“登录”。"
    if "缺少 Windows" in message and "pip install" in message:
        return "发布包缺少 Windows 运行依赖；请重新下载并完整解压发布包。"
    return (message.replace("chrome-login 命令", "“登录”按钮")
            .replace("重新运行 chrome-login", "重新点击“登录”")
            .replace("运行 chrome-login", "点击“登录”")
            .replace("按 Ctrl+C 停止", "点击“停止”结束"))


def user_data_dir() -> Path:
    base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local"
    return base / "RushPhone"


def initialize_data(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    protect_private_path(data_dir, directory=True)
    defaults = Path(__file__).with_name("windows_defaults")
    for filename in PRESETS.values():
        content = (defaults / filename).read_bytes()
        try:
            with (data_dir / filename).open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            pass  # 升级或重复启动绝不覆盖用户配置。


def load_preset(data_dir: Path, preset: str) -> tuple[AppConfig, AccountConfig]:
    if preset not in PRESETS:
        raise ConfigError("请选择 Duo 或 Pro Max")
    configs = {key: load_config(data_dir / filename) for key, filename in PRESETS.items()}
    accounts = {}
    for key, config in configs.items():
        enabled = [account for account in config.accounts if account.enabled]
        if len(enabled) != 1:
            raise ConfigError("Windows 启动器的每份配置必须只启用一个账号")
        accounts[key] = enabled[0]
    if len({(account.name, account.profile_dir.resolve()) for account in accounts.values()}) != 1:
        raise ConfigError("Duo 和 Pro Max 的启用账号名称与 profile_dir 必须一致，才能复用登录窗口")
    return configs[preset], accounts[preset]


class LauncherLock:
    """同一用户只运行一个启动器；进程结束由系统释放文件锁。"""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "launcher.lock"
        self.stream = None

    def acquire(self) -> None:
        stream = self.path.open("a+b")
        try:
            protect_private_path(self.path)
            if self.path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # 允许在 Mac 上运行不启动浏览器的单元测试。
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("启动器已在运行，请使用原窗口") from None
        except BaseException:
            stream.close()
            raise
        self.stream = stream

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


@contextmanager
def system_chrome_environment():
    """打包版启动系统 Chrome 时恢复系统 DLL 搜索路径，随后还原。"""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel.GetDllDirectoryW
    get_directory.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    get_directory.restype = wintypes.DWORD
    set_directory = kernel.SetDllDirectoryW
    set_directory.argtypes = [wintypes.LPCWSTR]
    set_directory.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(get_directory(0, None) + 1)
    get_directory(len(buffer), buffer)
    old_path = os.environ.get("PATH")
    bundle = Path(sys._MEIPASS).resolve()
    if not set_directory(None):
        raise RuntimeError("无法准备系统 Chrome 的启动环境")
    try:
        if old_path is not None:
            os.environ["PATH"] = os.pathsep.join(
                part for part in old_path.split(os.pathsep)
                if part and not Path(part).resolve().is_relative_to(bundle)
            )
        yield
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path
        if not set_directory(buffer.value or None):
            raise RuntimeError("无法恢复启动器运行环境；请关闭后重新启动")


@dataclass(frozen=True)
class TaskResult:
    state: str
    message: str


class EventLogHandler(logging.Handler):
    def __init__(self, events: Queue):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", window_message(self.format(record))))


class LauncherController:
    def __init__(self, data_dir: Path, events: Queue):
        self.data_dir, self.events = data_dir, events
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.review_required = False
        self.logger = logging.Logger("rush_phone.windows", logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        log_dir = data_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        for handler in (EventLogHandler(events), RotatingFileHandler(
                log_dir / "rush_phone.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")):
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, action: str, *, submit_order: bool = False) -> None:
        if self.busy:
            raise RuntimeError("已有任务正在运行，请先停止它")
        if self.review_required and action != "login":
            raise RuntimeError("上次订单结果待确认；请先在 Chrome 核对订单，核对后再重新启动本程序")
        config, account = load_preset(self.data_dir, "duo" if action == "login" else action)
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._work, args=(action, config, account, submit_order),
                                       name="购买助手", daemon=False)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _work(self, action: str, config: AppConfig, account: AccountConfig, submit_order: bool) -> None:
        try:
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            if action == "login":
                with system_chrome_environment():
                    _, reused = open_chrome_session(config, account)
                if self.stop_event.is_set():
                    raise KeyboardInterrupt
                result = TaskResult("login", f"{'已有' if reused else '新的'}专用 Chrome 已打开，请手动完成登录和验证，并保持窗口打开。")
            else:
                self.logger.info("%s：%s / %s / %s", "真实购买" if submit_order else "演练到最终提交前",
                                 config.variables.get("model", ""), config.variables.get("capacity", ""),
                                 config.variables.get("color", ""))
                purchase = run_session_purchase(config, account, self.logger, submit_order=submit_order,
                                                stop_requested=self.stop_event.is_set)
                if purchase.screenshot:
                    self.logger.info("现场截图：%s", purchase.screenshot)
                if purchase.order_submitted is None:
                    self.review_required = True
                    result = TaskResult("unknown", purchase.message)
                else:
                    result = TaskResult("success" if purchase.ok else "error", purchase.message)
        except KeyboardInterrupt:
            result = TaskResult("stopped", "已停止脚本，专用 Chrome 保持打开；请核对页面和订单状态。")
        except (ConfigError, ChromeSessionError, PrivateFileError) as exc:
            result = TaskResult("error", window_message(str(exc)))
        except Exception as exc:
            result = TaskResult("error", f"操作未完成（{type(exc).__name__}）；请核对 Chrome 页面和本地日志。")
        self.logger.info(result.message)
        self.events.put(("finished", result))

    def close(self) -> None:
        if self.busy:
            raise RuntimeError("请等待当前任务停止后再关闭启动器")
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
