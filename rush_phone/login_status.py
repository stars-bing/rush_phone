from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable
from urllib.parse import urlsplit


class LoginState(str, Enum):
    SIGNED_IN = "已确认商店登录"
    SIGN_IN_REQUIRED = "等待账号登录"
    VERIFICATION_REQUIRED = "等待人工验证"
    LOADING = "登录页面处理中"
    UNKNOWN = "尚未确认登录"


@dataclass(frozen=True)
class LoginInspection:
    state: LoginState
    page: Any | None
    url: str


def safe_page_url(url: str) -> str:
    """诊断信息不包含认证参数或 URL 片段。"""
    parts = urlsplit(url)
    return parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1], query="", fragment="").geturl()


def is_apple_store_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and any(host == domain or host.endswith("." + domain) for domain in ("apple.com.cn", "apple.com"))
        and parts.path.startswith("/shop/")
    )


def _visible(locator: Any) -> bool:
    return any(locator.nth(index).is_visible() for index in range(locator.count()))


def _inspect_page(page: Any) -> LoginState:
    # 只检查本工具专用窗口中的 Apple Store 页面；不读取任何输入值。
    if not is_apple_store_url(page.url):
        return LoginState.UNKNOWN
    for frame in page.frames:
        if _visible(frame.locator(
            'input[autocomplete="one-time-code"], input[id^="char"][maxlength="1"], '
            'input[id^="security-code"]'
        )):
            return LoginState.VERIFICATION_REQUIRED
    for frame in page.frames:
        if _visible(frame.locator("#account_name_text_field, #password_text_field")):
            return LoginState.SIGN_IN_REQUIRED
    if re.match(r"^/shop/signin(?:/|$)", urlsplit(page.url).path.lower()):
        return LoginState.LOADING
    sign_out = re.compile(r"^(退出登录|登出|Sign\s*out)$", re.IGNORECASE)
    if any(_visible(page.get_by_role(role, name=sign_out)) for role in ("link", "button")):
        return LoginState.SIGNED_IN
    account_path = re.match(r"^/shop/account(?:/|$)", urlsplit(page.url).path)
    account_heading = re.compile(r"^(你的|我的|Your\s+)?(账户|帐户|Account)(首页|设置)?$", re.IGNORECASE)
    if account_path and _visible(page.get_by_role("heading", name=account_heading)):
        return LoginState.SIGNED_IN
    return LoginState.UNKNOWN


def inspect_apple_login(context: Any) -> LoginInspection:
    """检查所有标签页，不把 Cookie 存在或跳转出登录页当作登录成功。"""
    observations = []
    for page in reversed(context.pages):
        try:
            if page.is_closed():
                continue
            state = _inspect_page(page)
            inspection = LoginInspection(state, page, safe_page_url(page.url))
            if state == LoginState.SIGNED_IN:
                return inspection
            if is_apple_store_url(page.url):
                observations.append(inspection)
        except Exception:
            # 用户切换标签页或导航时，下一轮再检查。
            continue
    for state in (LoginState.VERIFICATION_REQUIRED, LoginState.SIGN_IN_REQUIRED, LoginState.LOADING, LoginState.UNKNOWN):
        for inspection in observations:
            if inspection.state == state:
                return inspection
    return LoginInspection(LoginState.UNKNOWN, None, "")


def wait_for_apple_login(
    context: Any,
    timeout_seconds: float,
    on_change: Callable[[LoginInspection], None] | None = None,
    on_tick: Callable[[], None] | None = None,
) -> Any:
    """持续处理浏览器事件，人工登录成功后自动继续，无需阻塞在 input()。"""
    deadline = time.monotonic() + timeout_seconds
    previous = None
    while time.monotonic() < deadline:
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            raise RuntimeError("专用浏览器窗口已关闭，登录尚未确认")
        inspection = inspect_apple_login(context)
        if on_tick:
            on_tick()
        marker = inspection.state, inspection.url
        if marker != previous and on_change:
            on_change(inspection)
        previous = marker
        if inspection.state == LoginState.SIGNED_IN:
            return inspection.page
        # Playwright 的同步事件依靠 API 调用驱动；不要用阻塞 input/sleep 等待人工。
        try:
            pages[0].wait_for_timeout(500)
        except Exception:
            if pages[0].is_closed() and any(not page.is_closed() for page in context.pages):
                continue
            raise
    raise TimeoutError("等待 Apple Store 登录超时；未确认成功，不启动购买演练")
