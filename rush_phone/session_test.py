from __future__ import annotations

import logging
import os
import re
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .chrome_session import ChromeSessionError, require_chrome_session
from .config import AccountConfig, AppConfig, render_template
from .limiter import OrderLimiter
from .login_diagnostics import LoginNetworkDiagnostics
from .login_status import LoginState, inspect_apple_login, is_apple_store_url
from .worker import PurchaseWorker
from .session_control import control_session


@dataclass(frozen=True)
class SessionTestResult:
    ok: bool
    message: str
    page_ready: bool | None = None
    screenshot: Path | None = None
    order_submitted: bool | None = False  # None 表示已尝试最终提交，但结果无法确认。


class FulfillmentWatch:
    """只追踪当前商品页的配送状态，不读取参数、请求体或 Cookie。"""

    def __init__(self, page: Any, product_url: str) -> None:
        self.page = page
        target = urlsplit(product_url)
        self.origin = target.scheme, target.netloc
        self.pending: set[int] = set()
        self.last_status: int | None = None
        self.failure: str | None = None
        self.last_activity = time.monotonic()
        self.handlers = {
            "request": self.started, "response": self.responded,
            "requestfinished": self.finished, "requestfailed": self.failed,
        }

    def relevant(self, request: Any) -> bool:
        parts = urlsplit(request.url)
        return ((parts.scheme, parts.netloc) == self.origin
                and parts.path.rstrip("/") == "/shop/fulfillment-messages"
                and request.resource_type in {"fetch", "xhr"}
                and request.frame == self.page.main_frame)

    def attach(self) -> None:
        for event, handler in self.handlers.items():
            self.page.on(event, handler)

    def detach(self) -> None:
        for event, handler in self.handlers.items():
            self.page.remove_listener(event, handler)

    def started(self, request: Any) -> None:
        if self.relevant(request):
            self.pending.add(id(request))
            self.last_activity = time.monotonic()

    def responded(self, response: Any) -> None:
        if self.relevant(response.request) and id(response.request) in self.pending:
            self.last_status = response.status
            self.last_activity = time.monotonic()
            if response.status >= 400:
                self.failure = f"配送查询返回 HTTP {response.status}；本次停止，不自动重试，也不是缺货结论"

    def finished(self, request: Any) -> None:
        if self.relevant(request):
            self.pending.discard(id(request))
            self.last_activity = time.monotonic()

    def failed(self, request: Any) -> None:
        if self.relevant(request) and id(request) in self.pending:
            self.finished(request)
            match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
            code = match.group(0) if match else "网络请求失败"
            self.failure = f"配送查询未完成：{code}；本次停止，不自动重试"

    def check(self) -> None:
        if self.failure:
            raise ChromeSessionError(self.failure)

    def wait(self, timeout_ms: int, guard: Any) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        # 规格切换可能延迟触发查询，不能把旧的 200 或启用按钮当作最终结果。
        earliest = time.monotonic() + 1
        while time.monotonic() < deadline:
            guard()
            self.check()
            if (self.last_status is not None and 200 <= self.last_status < 300
                    and not self.pending and time.monotonic() >= earliest
                    and time.monotonic() - self.last_activity >= 0.75):
                return
            self.page.wait_for_timeout(100)
        self.check()
        raise ChromeSessionError("未确认当前页面配送查询完成；不把购物袋按钮可点击当作成功，不进入结账")


def check_product_only(context: Any, config: AppConfig, account: AccountConfig, logger: logging.Logger) -> SessionTestResult:
    """同一已登录 context 中仅选单选规格、读取配送及页面状态，绝不执行 flow.steps。"""
    inspection = inspect_apple_login(context)
    if inspection.state != LoginState.SIGNED_IN:
        return SessionTestResult(False, f"尚未确认商店登录（{inspection.state.value}）；请在专用 Chrome 人工登录后再运行 session-test")
    logger.info("已核对商店登录；复用原浏览器 context 和已登录标签页，不恢复或导出 Cookie。")
    return prepare_product_page(context, inspection.page, config, account, logger)


def prepare_product_page(context: Any, page: Any, config: AppConfig, account: AccountConfig, logger: logging.Logger,
                         *, wait_available: bool = False, on_guard: Callable[[], Any] | None = None) -> SessionTestResult:
    """在调用方已核对登录的同一标签页选择规格并等待配送响应。"""
    if (not is_apple_store_url(config.target.product_url)
            or not urlsplit(config.target.product_url).path.startswith("/shop/buy-")
            or config.stock.probe_url):
        raise ChromeSessionError("会话测试仅支持 Apple 官方商品页，不执行自定义 HTTP 库存接口")
    diagnostics = LoginNetworkDiagnostics(logger.info)
    diagnostics.attach(context)
    watch = None
    document_handler = None
    try:
        diagnostics.check_status()
        # no_defaults=True 保留普通 Chrome 的后台标签页行为。原生点击前置于
        # 前台，避免后台页面的 actionability 等待超时；不强制点击或修改指纹。
        page.bring_to_front()
        worker = PurchaseWorker(
            config, account, logger, OrderLimiter(1), threading.Event(), False,
            config.source_path.parent / "artifacts", threading.Event(), True,
        )
        document_handler = lambda response: worker._record_document_response(page, response)
        page.on("response", document_handler)
        def guard() -> None:
            if on_guard:
                on_guard()
            try:
                diagnostics.check_status()
            except RuntimeError:
                raise ChromeSessionError("商店登录交接出现错误；本次检查已停，不重新提交登录") from None
            watch.check()
            if worker._document_response and worker._document_response[0] >= 400:
                raise ChromeSessionError(f"商品页面返回 HTTP {worker._document_response[0]}；本次检查已停")
            if worker._critical_response:
                raise ChromeSessionError(f"配送查询返回 HTTP {worker._critical_response[0]}；本次检查已停")
            if worker._visible_challenge(page):
                raise ChromeSessionError("检测到人工验证；本次测试已停，请在保留的 Chrome 页面处理后再检查")

        from playwright.sync_api import TimeoutError as BrowserTimeout

        def load_product() -> bool:
            nonlocal watch
            if watch:
                watch.detach()
            watch = FulfillmentWatch(page, config.target.product_url)
            watch.attach()
            worker._document_response = worker._critical_response = None
            response = page.goto(config.target.product_url, wait_until="domcontentloaded", timeout=config.run.page_timeout_ms)
            if response is not None:
                worker._record_document_response(page, response)
            guard()
            if wait_available and urlsplit(page.url).path != urlsplit(config.target.product_url).path:
                return False  # 人工认证回到账户页时，下一轮重新进入商品页，不在账户页寻找规格。
            for template in config.stock.prepare_click_selectors:
                guard()
                page.bring_to_front()
                selector = render_template(template, account, config.variables)
                locator = page.locator(selector).first
                if not locator.evaluate("element => element.tagName === 'INPUT' && element.type === 'radio'"):
                    raise ChromeSessionError("会话检查仅允许单选规格；已拒绝点击按钮、链接或其他控件")
                if wait_available and not locator.is_enabled():
                    return False
                PurchaseWorker._click_control(page, locator, timeout=min(config.run.page_timeout_ms, 3000))
            return True

        prepared = load_product()
        if wait_available:
            logger.info("持续检查购买按钮（间隔 %d ms），每 %d ms 刷新原标签页并恢复规格；按 Ctrl+C 停止。",
                        config.run.poll_interval_ms, config.run.stock_refresh_interval_ms)
            refresh_at = time.monotonic() + config.run.stock_refresh_interval_ms / 1000
            report_at = time.monotonic()
            while True:
                guard()
                if prepared and worker._read_stock_state(page, BrowserTimeout):
                    watch.wait(config.run.page_timeout_ms, guard)
                    guard()
                    if worker._read_stock_state(page, BrowserTimeout):
                        logger.info("购买按钮已可操作且配送查询成功，继续核对购物袋。")
                        return SessionTestResult(True, "当前商品可继续购买", True)
                now = time.monotonic()
                if now >= report_at:
                    logger.info("购买按钮尚不可操作，继续等待；未加购、未结账。")
                    report_at = now + 30
                if now >= refresh_at or urlsplit(page.url).path != urlsplit(config.target.product_url).path:
                    if watch.pending:
                        # 不用刷新中断尚在进行的配送查询，否则可能把主动取消误当成网络错误。
                        watch.wait(config.run.page_timeout_ms, guard)
                        continue
                    logger.debug("刷新当前商品标签页并重新选择相同规格。")
                    prepared = load_product()
                    refresh_at = time.monotonic() + config.run.stock_refresh_interval_ms / 1000
                    continue
                delay = max(250, config.run.poll_interval_ms + random.randint(
                    -config.run.poll_jitter_ms, config.run.poll_jitter_ms))
                page.wait_for_timeout(min(delay, max(1, (refresh_at - now) * 1000)))
        watch.wait(config.run.page_timeout_ms, guard)
        ready = worker._read_stock_state(page, BrowserTimeout)
        guard()
        logger.info("配送查询已收到成功响应；页面可操作状态：%s（不代表库存锁定）。", "可继续" if ready else "尚未就绪")
        return SessionTestResult(True, "会话与公开配送检查完成；未加入购物袋、未结账、未提交订单", ready)
    finally:
        if watch:
            watch.detach()
        if page is not None and document_handler is not None:
            page.remove_listener("response", document_handler)
        diagnostics.detach(context)
        # 保留当前标签页；不得 close 默认 context 或浏览器。


def run_session_test(config: AppConfig, account: AccountConfig, logger: logging.Logger, *, product_only: bool = False) -> SessionTestResult:
    return _run_existing_session(config, account, logger, product_only=product_only)


def run_session_purchase(config: AppConfig, account: AccountConfig, logger: logging.Logger,
                         *, submit_order: bool = False, check_once: bool = False,
                         stop_requested: Callable[[], bool] | None = None) -> SessionTestResult:
    return _run_existing_session(config, account, logger, purchase=True, submit_order=submit_order,
                                 check_once=check_once, stop_requested=stop_requested)


def _run_existing_session(config: AppConfig, account: AccountConfig, logger: logging.Logger, *, product_only: bool = False,
                           purchase: bool = False, submit_order: bool = False, check_once: bool = False,
                           stop_requested: Callable[[], bool] | None = None) -> SessionTestResult:
    if stop_requested and stop_requested():
        raise KeyboardInterrupt
    if os.environ.get("DEBUG") or os.environ.get("PWDEBUG"):
        raise ChromeSessionError("请先取消 DEBUG/PWDEBUG，避免浏览器 API 调试日志泄露连接信息；项目 --verbose 可以使用")
    session = require_chrome_session(account)
    from playwright.sync_api import sync_playwright
    try:
        with control_session(account), sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                session.endpoint, timeout=15000, is_local=True, no_defaults=True,
            )
            if not browser.contexts:
                raise ChromeSessionError("专用 Chrome 没有可复用的现有 context；未创建替代会话")
            context = browser.contexts[0]
            context.set_default_timeout(config.run.page_timeout_ms)
            if product_only:
                return check_product_only(context, config, account, logger)
            if purchase:
                from .session_checkout import SessionCheckout
                options = {"stop_requested": stop_requested} if stop_requested is not None else {}
                return SessionCheckout(context, config, account, logger, watch=not check_once,
                                       submit_order=submit_order, **options).run()
            from .session_checkout import check_until_submit
            return check_until_submit(context, config, account, logger)
    except ChromeSessionError:
        raise
    except Exception as exc:
        if purchase:
            raise ChromeSessionError(f"同会话购买未完成（{type(exc).__name__}）；请核对保留的 Chrome 和订单状态，不自动重新下单") from None
        if product_only:
            raise ChromeSessionError(f"商品检查未完成（{type(exc).__name__}）；未加购或进入结账，专用 Chrome 保持打开") from None
        raise ChromeSessionError(f"会话演练未完成（{type(exc).__name__}）；未提交订单，专用 Chrome 保持打开；重新运行会先核对购物袋") from None
    finally:
        logger.info("本次只断开脚本连接；专用 Chrome 保持打开，不自动重试登录或订单。")
