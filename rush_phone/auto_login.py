from __future__ import annotations

import time
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from .credentials import LoginCredentials
from .login_status import LoginState, inspect_apple_login, is_apple_store_url


class AutoLoginResult(str, Enum):
    SUBMITTED = "已提交一次凭据"
    SIGNED_IN = "已有有效商店会话"
    NEEDS_VERIFICATION = "需要人工验证"


class AutoLoginError(RuntimeError):
    pass


def _trusted_auth_frame(frame: Any) -> bool:
    parts = urlsplit(frame.url)
    return (
        parts.scheme == "https"
        and parts.hostname in {"idmsa.apple.com.cn", "idmsa.apple.com"}
        and parts.path.startswith("/appleauth/auth/")
    )


def _require_trusted_frame(frame: Any) -> None:
    if not _trusted_auth_frame(frame) or not is_apple_store_url(frame.page.url):
        raise AutoLoginError("认证页面地址已变化；未继续发送凭据")


def submit_credentials_once(context: Any, credentials: LoginCredentials) -> AutoLoginResult:
    """仅在官方认证框进行一轮登录；不接触验证码，不读取或记录请求体。"""
    stage = "定位官方认证框"
    try:
        deadline = time.monotonic() + 20
        selected = None
        while time.monotonic() < deadline:
            state = inspect_apple_login(context)
            if state.state == LoginState.SIGNED_IN:
                return AutoLoginResult.SIGNED_IN
            if state.state == LoginState.VERIFICATION_REQUIRED:
                return AutoLoginResult.NEEDS_VERIFICATION
            pages = [page for page in context.pages if not page.is_closed()]
            if not pages:
                raise AutoLoginError("专用浏览器已关闭；未尝试登录")
            for page in pages:
                if not is_apple_store_url(page.url):
                    continue
                for frame in page.frames:
                    if _trusted_auth_frame(frame) and frame.locator("#account_name_text_field").is_visible():
                        selected = frame
                        break
                if selected is not None:
                    break
            if selected is not None:
                break
            pages[0].wait_for_timeout(250)
        if selected is None:
            raise AutoLoginError("未找到可信的 Apple 账号输入框；未发送凭据")
        _require_trusted_frame(selected)
        stage = "填写账号"
        selected.locator("#account_name_text_field").fill(credentials.username, timeout=5000)
        button = selected.locator('#sign-in, button[aria-label="继续"], button[aria-label="Continue"]').filter(visible=True).first
        password = selected.locator("#password_text_field")
        if not password.is_visible():
            stage = "进入密码输入页"
            button.click(timeout=8000)
        password.wait_for(state="visible", timeout=10000)
        _require_trusted_frame(selected)
        stage = "填写密码"
        password.fill(credentials.password, timeout=5000)
        _require_trusted_frame(selected)
        stage = "单次提交凭据"
        button.click(timeout=8000)
        return AutoLoginResult.SUBMITTED
    except AutoLoginError:
        raise
    except Exception as exc:
        # Playwright 的原始异常可能含 fill 的明文值，禁止直接输出或串联原始 traceback。
        raise AutoLoginError(f"自动登录在“{stage}”停止（{type(exc).__name__}）；未记录原始异常，不自动重试") from None
