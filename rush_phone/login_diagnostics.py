from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .login_status import is_apple_store_url, safe_page_url


class LoginNetworkDiagnostics:
    """只记录状态码、网络错误码和脱敏路径，不采集请求体、Cookie 或认证头。"""

    def __init__(
        self,
        emit: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self.clock = clock
        self.pending: dict[int, tuple[float, str, str]] = {}
        self._last_pending_report = float("-inf")
        self._handoff_failure: tuple[str, str] | None = None
        self._handlers = {
            "request": self._started,
            "response": self._response,
            "requestfinished": self._finished,
            "requestfailed": self._failed,
        }

    def attach(self, context: Any) -> None:
        for event, handler in self._handlers.items():
            context.on(event, handler)

    def detach(self, context: Any) -> None:
        for event, handler in self._handlers.items():
            context.remove_listener(event, handler)

    @staticmethod
    def _relevant(request: Any) -> bool:
        parts = urlsplit(request.url)
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and any(host == domain or host.endswith("." + domain)
                    for domain in ("apple.com", "apple.com.cn", "cdn-apple.com"))
            and request.resource_type in {"document", "fetch", "xhr", "script"}
        )

    def _started(self, request: Any) -> None:
        if self._relevant(request):
            self.pending[id(request)] = self.clock(), request.method, safe_page_url(request.url)

    def _response(self, response: Any) -> None:
        request = response.request
        if not self._relevant(request):
            return
        if 200 <= response.status < 300 and self._handoff_failure:
            account_document = (
                request.resource_type == "document"
                and is_apple_store_url(request.url)
                and urlsplit(request.url).path.rstrip("/").lower() == "/shop/account/home"
                and request.frame.parent_frame is None
            )
            if self._is_store_handoff(request) or account_document:
                self._handoff_failure = None
                self.emit("已收到新的商店登录交接或账户页响应，重新检查当前登录状态。")
        if response.status >= 400:
            if self._is_store_handoff(request):
                self._handoff_failure = f"HTTP {response.status}", safe_page_url(request.url)
            self.emit(
                f"登录相关请求返回 HTTP {response.status}：{request.method} {safe_page_url(request.url)}"
                "（状态码仅供诊断，不自动重试或判断密码错误）"
            )
        elif request.method == "POST" or request.resource_type == "document":
            self.emit(
                f"登录相关请求已收到 HTTP {response.status} 响应：{request.method} {safe_page_url(request.url)}"
                "（收到响应不等于登录成功）"
            )

    def _finished(self, request: Any) -> None:
        self.pending.pop(id(request), None)

    def _failed(self, request: Any) -> None:
        self._finished(request)
        if self._relevant(request):
            match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
            code = match.group(0) if match else "网络请求失败"
            if self._is_store_handoff(request):
                self._handoff_failure = code, safe_page_url(request.url)
            self.emit(f"登录相关请求未完成：{code}；{request.method} {safe_page_url(request.url)}")

    @staticmethod
    def _is_store_handoff(request: Any) -> bool:
        return (
            is_apple_store_url(request.url)
            and urlsplit(request.url).path.lower().startswith("/shop/signin/idms/")
        )

    def check_status(self) -> None:
        if self._handoff_failure:
            error, url = self._handoff_failure
            raise RuntimeError(
                f"Apple Store 登录交接失败：{error}；{url}。"
                "已阻止后续演练；脚本不会重试登录，不将该错误直接解释为密码错误或反自动化限制"
            )
        self.report_pending()

    def report_pending(self) -> None:
        now = self.clock()
        stalled = [row for row in self.pending.values() if now - row[0] >= 15]
        if stalled and now - self._last_pending_report >= 30:
            self._last_pending_report = now
            for started, method, url in stalled[:5]:
                self.emit(f"登录请求仍在等待（{now - started:.0f} 秒）：{method} {url}")
