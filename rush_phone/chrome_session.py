from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .config import AccountConfig, AppConfig
from .login_status import is_apple_store_url
from .private_files import PrivateFileError, protect_private_path


class ChromeSessionError(RuntimeError):
    """仅包含安全的操作提示，不包含浏览器原始异常或会话内容。"""


@dataclass(frozen=True)
class ChromeSession:
    pid: int
    port: int
    profile_dir: Path
    websocket_path: str = field(repr=False)
    _process: subprocess.Popen | None = field(default=None, repr=False, compare=False)

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.websocket_path}"


def session_profile_dir(account: AccountConfig) -> Path:
    return (account.profile_dir / "chrome-session").resolve()


def session_record_path(account: AccountConfig) -> Path:
    return account.profile_dir / "chrome-session.json"


def _check_profile(account: AccountConfig) -> Path:
    profile = session_profile_dir(account)
    parent = account.profile_dir.resolve()
    daily_roots = [Path.home() / "Library/Application Support/Google/Chrome",
                   Path.home() / "AppData/Local/Google/Chrome/User Data"]
    if os.environ.get("LOCALAPPDATA"):
        daily_roots.append(Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data")
    if profile.parent != parent or any(root.resolve() == profile or root.resolve() in profile.parents
                                       for root in daily_roots):
        raise ChromeSessionError("拒绝使用日常 Chrome 资料目录或指向其他位置的会话目录")
    return profile


def _chrome_executable(explicit: str | Path | None = None) -> Path:
    if sys.platform not in {"darwin", "win32"}:
        raise ChromeSessionError("专用 Chrome 启动模式目前支持 macOS 和 Windows 原生环境")
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
        expected = "chrome.exe" if sys.platform == "win32" else "Google Chrome"
        if candidate.name.casefold() != expected.casefold() or not candidate.is_file():
            raise ChromeSessionError("--chrome-path 必须指向已安装 Google Chrome 的可执行文件，不接受脚本或目录")
        return candidate
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                      Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        candidates = [local / "Google/Chrome/Application/chrome.exe"]
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name]) / "Google/Chrome/Application/chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ChromeSessionError("未找到系统 Google Chrome；请先安装，或使用 --chrome-path 指定可执行文件")


def _protect_session_path(path: Path, *, directory: bool = False) -> None:
    try:
        protect_private_path(path, directory=directory)
    except PrivateFileError as exc:
        raise ChromeSessionError(str(exc)) from None


def _load_record(account: AccountConfig) -> ChromeSession | None:
    path = session_record_path(account)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 8192:
            raise ValueError
        raw = json.loads(path.read_text(encoding="utf-8"))
        if set(raw) != {"version", "pid", "port", "profile_dir", "websocket_path"} or raw["version"] != 1:
            raise ValueError
        pid, port = raw["pid"], raw["port"]
        if type(pid) is not int or pid <= 1 or type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError
        profile = _check_profile(account)
        if Path(raw["profile_dir"]).resolve() != profile:
            raise ValueError
        ws_path = raw["websocket_path"]
        if not isinstance(ws_path, str) or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{8,100}", ws_path):
            raise ValueError
        return ChromeSession(pid, port, profile, ws_path)
    except ChromeSessionError:
        raise
    except Exception:
        raise ChromeSessionError("专用 Chrome 连接记录无效；未连接任何浏览器，请检查 chrome-session.json") from None


def _save_record(account: AccountConfig, session: ChromeSession) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".chrome-session-", dir=account.profile_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _protect_session_path(temporary)
            json.dump({
                "version": 1, "pid": session.pid, "port": session.port,
                "profile_dir": str(session.profile_dir), "websocket_path": session.websocket_path,
            }, stream)
        temporary.replace(session_record_path(account))
    finally:
        temporary.unlink(missing_ok=True)


def _listeners_are_owned(output: str, pid: int, port: int) -> bool:
    """要求所有匹配监听都属于本次 Chrome，且仅绑定回环地址。"""
    owner = None
    found = False
    try:
        for line in output.splitlines():
            if line.startswith("p"):
                owner = int(line[1:])
            elif line.startswith("n"):
                host, _, observed_port = line[1:].rpartition(":")
                if owner != pid or int(observed_port) != port:
                    return False
                if not ipaddress.ip_address(host.strip("[]")).is_loopback:
                    return False
                found = True
        return found
    except (ValueError, TypeError):
        return False


def _listener_owned_by(pid: int, port: int) -> bool:
    try:
        if sys.platform == "win32":
            return _windows_listener_owned_by(pid, port)
        if sys.platform != "darwin":
            raise ChromeSessionError("无法在此系统核对专用 Chrome 监听；目前支持 macOS 和 Windows")
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.returncode == 0 and _listeners_are_owned(result.stdout, pid, port)
    except ChromeSessionError:
        raise
    except Exception:
        raise ChromeSessionError("无法核对专用 Chrome 的本机监听端口；为安全起见没有连接") from None


def _windows_listener_owned_by(pid: int, port: int) -> bool:
    try:
        import psutil
    except ImportError:
        raise ChromeSessionError("缺少 Windows 端口核验依赖，请重新运行 python -m pip install -e .") from None
    # 同时核验 IPv4/IPv6 的全部监听者，不能只查 Chrome 自身的连接列表。
    # 不读取其他进程的命令行、浏览器资料、网络报文或 Cookie。
    found = False
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        if connection.laddr.port != port:
            continue
        if connection.pid != pid or not ipaddress.ip_address(connection.laddr.ip).is_loopback:
            return False
        found = True
    return found


def _read_websocket_path(port: int) -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        payload = response.read(65537)
        if response.status != 200 or len(payload) > 65536:
            raise ValueError
        data = json.loads(payload)
        if not isinstance(data.get("Browser"), str) or not data["Browser"].startswith("Chrome/"):
            raise ValueError
        endpoint = urlsplit(data["webSocketDebuggerUrl"])
        if (endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
                or endpoint.port != port or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment
                or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{8,100}", endpoint.path)):
            raise ValueError
        return endpoint.path
    except Exception:
        raise ChromeSessionError("专用 Chrome 调试接口尚未就绪或身份不匹配；未读取登录数据") from None
    finally:
        connection.close()


def require_chrome_session(account: AccountConfig) -> ChromeSession:
    session = _load_record(account)
    if session is None:
        raise ChromeSessionError("尚未打开专用 Chrome，请先运行 chrome-login 命令")
    if not _listener_owned_by(session.pid, session.port):
        raise ChromeSessionError("专用 Chrome 已关闭或端口身份不匹配；请重新运行 chrome-login")
    if _read_websocket_path(session.port) != session.websocket_path:
        raise ChromeSessionError("浏览器会话已变化；拒绝接管其他窗口，请重新运行 chrome-login")
    return session


def _choose_port(requested: int | None) -> int:
    if requested is not None and (type(requested) is not int or not 1024 <= requested <= 65535):
        raise ChromeSessionError("本机端口必须是 1024 到 65535 之间的整数")
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", requested or 0))
            return listener.getsockname()[1]
    except OSError:
        raise ChromeSessionError("本机端口已被占用；不会连接占用该端口的其他浏览器") from None


def open_chrome_session(
    config: AppConfig, account: AccountConfig, port: int | None = None,
    chrome_path: str | Path | None = None,
) -> tuple[ChromeSession, bool]:
    """普通 Chrome 先人工登录；此阶段不启动 Playwright，不读取 Cookie。"""
    if config.run.browser_channel != "chrome" or not is_apple_store_url(config.target.login_url):
        raise ChromeSessionError("会话复用模式目前仅支持系统 Chrome 与官方 Apple Store 登录页")
    if port is not None and (type(port) is not int or not 1024 <= port <= 65535):
        raise ChromeSessionError("本机端口必须是 1024 到 65535 之间的整数")
    profile = _check_profile(account)
    previous = _load_record(account)
    if previous is not None:
        try:
            active = require_chrome_session(account)
        except ChromeSessionError:
            # 不删除 Chrome 锁文件或用户资料，不强行接管未核实的实例。
            pass
        else:
            if port is not None and port != active.port:
                raise ChromeSessionError("该专用 Chrome 已使用其他本机端口；请不要另开相同资料目录")
            return active, True
    if any((profile / name).exists() or (profile / name).is_symlink()
           for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")):
        raise ChromeSessionError("专用资料目录仍被占用；请先关闭该专用窗口，不要删除锁文件或日常 Chrome 资料")
    executable = _chrome_executable(chrome_path)
    selected_port = _choose_port(port)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    _protect_session_path(profile, directory=True)
    launch_options = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32" else {"start_new_session": True}
    )
    process = subprocess.Popen(
        [str(executable), f"--user-data-dir={profile}", "--profile-directory=Default",
         "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={selected_port}",
         "--no-first-run", "--no-default-browser-check", config.target.login_url],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **launch_options,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ChromeSessionError("专用 Chrome 启动未完成；资料未清除，请检查是否有同目录窗口正在运行")
            if _listener_owned_by(process.pid, selected_port):
                try:
                    ws_path = _read_websocket_path(selected_port)
                except ChromeSessionError:
                    time.sleep(0.2)
                    continue
                session = ChromeSession(process.pid, selected_port, profile, ws_path, process)
                _save_record(account, session)
                return session, False
            time.sleep(0.2)
        raise ChromeSessionError("等待专用 Chrome 本机接口超时；未读取或迁移 Cookie")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise
