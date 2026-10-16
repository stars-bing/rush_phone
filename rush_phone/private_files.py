from __future__ import annotations

import sys
from pathlib import Path


class PrivateFileError(RuntimeError):
    """不包含路径、凭据或系统原始错误的权限设置失败。"""


def protect_private_path(path: Path, *, directory: bool = False) -> None:
    """仅保护指定对象；Windows 目录的当前用户 ACE 可供新子项继承。"""
    try:
        if path.is_symlink() or not path.exists() or path.is_dir() != directory:
            raise PrivateFileError("无法保护专用会话文件或目录；未写入登录数据")
        if sys.platform != "win32":
            path.chmod(0o700 if directory else 0o600)
            return
        _protect_windows_path(path, directory=directory)
    except PrivateFileError:
        raise
    except Exception:
        raise PrivateFileError("无法设置专用会话访问权限；请使用当前用户可写的本地 NTFS 目录") from None


def _protect_windows_path(path: Path, *, directory: bool) -> None:
    # chmod(600) 在 Windows 只调整只读标记，不能限制其他用户读取。
    # 使用当前进程的用户 SID，不依赖英文用户名、管理员终端或 shell。
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security
    except ImportError:
        raise PrivateFileError("缺少 Windows 权限依赖，请重新运行 python -m pip install -e .") from None
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    inheritance = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if directory else 0
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inheritance, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )
