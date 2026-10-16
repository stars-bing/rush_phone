from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import AccountConfig, ConfigError


@dataclass(frozen=True)
class LoginCredentials:
    username: str
    password: str = field(repr=False)


def load_credentials(account: AccountConfig, env_file: Path) -> LoginCredentials:
    """只读取指定变量的字面值，不执行 shell，也不展开环境变量。"""
    if os.environ.get("DEBUG") or os.environ.get("PWDEBUG"):
        raise ConfigError("自动登录前请取消 DEBUG/PWDEBUG，避免浏览器 API 调试日志输出密码")
    username = account.data.get("username", account.name)
    key = account.data.get("password_env", "PASSWORD")
    if not isinstance(username, str) or not username.strip():
        raise ConfigError("accounts.name 或 accounts.data.username 需要有效的登录名")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ConfigError("accounts.data.password_env 需要有效的环境变量名")
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        raise ConfigError("无法读取 .env；未尝试自动登录") from None
    pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=(.*)$")
    password = None
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if value[:1] in {"'", '"'}:
            end = value.rfind(value[0])
            if end == 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                raise ConfigError(".env 的密码变量引号不匹配；未尝试自动登录")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        password = value
    if not password:
        raise ConfigError(f".env 中未找到非空的 {key}；未尝试自动登录")
    return LoginCredentials(username.strip(), password)
