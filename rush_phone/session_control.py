from contextlib import contextmanager
import sys

from .chrome_session import ChromeSessionError
from .config import AccountConfig
from .private_files import protect_private_path


@contextmanager
def control_session(account: AccountConfig):
    """操作现有浏览器前加账号锁，防止多个脚本同时加购或提交。"""
    path = account.profile_dir / "session-control.lock"
    with path.open("a+b") as stream:
        protect_private_path(path)
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ChromeSessionError("该账号已有购买或测试脚本正在运行；请先停止它，避免重复订单") from None
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
