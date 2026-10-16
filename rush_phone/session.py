from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import AccountConfig
from .private_files import protect_private_path


def auth_state_path(account: AccountConfig) -> Path:
    return account.profile_dir / "auth-state.json"


def save_auth_state(context: Any, account: AccountConfig) -> Path:
    """额外保存会话 Cookie；仅依赖 Chrome 目录可能丢失关闭时的会话 Cookie。"""
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    destination = auth_state_path(account)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".auth-state-", suffix=".json", dir=account.profile_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        protect_private_path(temporary)
        context.storage_state(path=str(temporary))
        protect_private_path(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def restore_auth_state(context: Any, account: AccountConfig) -> int:
    """补回缺失且未过期的 Cookie；保留浏览器中已有的较新登录状态。"""
    source = auth_state_path(account)
    if not source.exists():
        return 0
    state = json.loads(source.read_text(encoding="utf-8"))
    existing = {
        (cookie["name"], cookie["domain"], cookie["path"])
        for cookie in context.cookies()
    }
    now = time.time()
    missing = [
        cookie
        for cookie in state.get("cookies", [])
        if (cookie["name"], cookie["domain"], cookie["path"]) not in existing
        and (cookie.get("expires", -1) == -1 or cookie["expires"] > now)
    ]
    if missing:
        context.add_cookies(missing)
    return len(missing)
