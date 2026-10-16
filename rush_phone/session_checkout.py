from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from fnmatch import fnmatchcase
from typing import Any, Callable
from urllib.parse import urlsplit

from .chrome_session import ChromeSessionError
from .config import AccountConfig, AppConfig, StepConfig, render_template
from .limiter import OrderLimiter
from .login_diagnostics import LoginNetworkDiagnostics
from .login_status import LoginState, _inspect_page, inspect_apple_login, is_apple_store_url, wait_for_apple_login
from .session_test import SessionTestResult, prepare_product_page
from .worker import PurchaseWorker


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


class SessionCheckout:
    """复用专用标签页；监控只在加购前执行，最终提交必须显式授权。"""

    def __init__(self, context: Any, config: AppConfig, account: AccountConfig, logger: logging.Logger,
                 *, watch: bool = False, submit_order: bool = False,
                 stop_requested: Callable[[], bool] | None = None):
        self.context, self.config, self.account, self.log = context, config, account, logger
        self.page: Any = None
        self.failure: str | None = None
        self.expected: tuple[str, ...] = ()
        self.checkout_started = False
        self.checkout_resumed = False
        self.auth_resumed = False
        self.watch, self.submit_order = watch, submit_order
        self.stop_requested = stop_requested
        self.submit_attempted = False
        self.worker = PurchaseWorker(config, account, logger, OrderLimiter(1), threading.Event(),
                                     False, config.source_path.parent / "artifacts", threading.Event(), True)
        self.diagnostics = LoginNetworkDiagnostics(logger.info)
        submit = [i for i, step in enumerate(config.steps) if step.submit]
        if len(submit) != 1:
            raise ChromeSessionError("会话演练必须配置唯一的 submit=true 最终提交步骤")
        self.submit_index = submit[0]
        self.final_step = config.steps[self.submit_index]
        if submit_order and (any(step.action not in {"wait_url", "wait_visible", "wait_hidden"}
                                  for step in config.steps[self.submit_index + 1:])
                             or not any(step.action in {"wait_visible", "wait_url"}
                                        for step in config.steps[self.submit_index + 1:])):
            raise ChromeSessionError("最终提交后必须只配置等待步骤，且至少有一项订单成功或待支付的正向确认条件")
        adds = [i for i, step in enumerate(config.steps[:self.submit_index])
                if step.action == "click" and re.search(r"购物袋|add.?to.?bag", step.name + step.selector, re.I)]
        checkouts = [i for i, step in enumerate(config.steps[:self.submit_index])
                     if step.action == "click" and re.search(r"结账|checkout", step.name + step.selector, re.I)]
        if len(adds) != 1 or len(checkouts) != 1 or adds[0] >= checkouts[0]:
            raise ChromeSessionError("flow.steps 须包含唯一的“加入购物袋”和“结账”点击步骤，并位于最终提交之前")
        self.add_index, self.checkout_index = adds[0], checkouts[0]
        self.checkout_step = config.steps[self.checkout_index]
        parts = urlsplit(config.target.product_url)
        if not is_apple_store_url(config.target.product_url) or config.stock.probe_url:
            raise ChromeSessionError("同会话结账演练仅支持官方 Apple Store 商品页")
        self.bag_url = f"{parts.scheme}://{parts.netloc}/shop/bag"

    def relevant(self, request: Any) -> bool:
        parts = urlsplit(request.url)
        if self.page is None or request.frame != self.page.main_frame:
            return False
        return request.resource_type == "document" or (
            request.resource_type in {"fetch", "xhr"} and is_apple_store_url(request.url)
            and (parts.path.startswith(("/shop/bagx/", "/shop/checkout", "/shop/signIn/idms/"))
                 or parts.path.rstrip("/") == "/shop/fulfillment-messages"))

    def record_response(self, response: Any) -> None:
        if self.relevant(response.request) and response.status >= 400:
            self.failure = f"商店页面或结账请求返回 HTTP {response.status}；本次停止，不自动重试"

    def record_failed(self, request: Any) -> None:
        if not self.relevant(request):
            return
        match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
        code = match.group(0) if match else "网络请求失败"
        if request.resource_type == "document" and code == "net::ERR_ABORTED":
            return  # 正常的页面跳转可能取消旧文档，不能据此认定结账失败。
        self.failure = f"商店或结账请求未完成：{code}；本次停止，不自动重试"

    def check_stop(self) -> None:
        if self.stop_requested is not None and self.stop_requested():
            # 与 Ctrl+C 共用处理：提交后中断必须报告结果未知，不能重新下单。
            raise KeyboardInterrupt

    def guard(self) -> bool:
        """返回是否刚完成一次人工登录/验证，供等待步骤重置时限。"""
        self.check_stop()
        if self.failure:
            raise ChromeSessionError(self.failure)
        self.diagnostics.check_status()
        if self.page.is_closed():
            raise ChromeSessionError("专用标签页已关闭；本次演练停止")
        if not is_apple_store_url(self.page.url):
            raise ChromeSessionError("当前标签页已离开 Apple Store；本次演练停止")
        states = {LoginState.SIGN_IN_REQUIRED, LoginState.VERIFICATION_REQUIRED, LoginState.LOADING}
        if _inspect_page(self.page) not in states and not self.worker._visible_challenge(self.page):
            return False
        self.log.warning("需要人工登录或验证，请在当前专用标签页完成；不要手动提交订单，脚本等待后继续。")
        deadline = time.monotonic() + self.config.run.challenge_timeout_seconds
        while time.monotonic() < deadline:
            self.check_stop()
            if self.failure:
                raise ChromeSessionError(self.failure)
            self.diagnostics.check_status()
            if self.page.is_closed():
                raise ChromeSessionError("人工处理期间标签页已关闭；本次停止")
            if not is_apple_store_url(self.page.url):
                raise ChromeSessionError("人工处理期间离开 Apple Store；本次停止")
            if _inspect_page(self.page) not in states and not self.worker._visible_challenge(self.page):
                self.log.info("人工登录/验证页面已结束，继续核对当前页面。")
                self.auth_resumed = True
                return True
            self.page.wait_for_timeout(250)
        raise ChromeSessionError("等待人工登录或验证超时；窗口保留，未提交订单")

    def check_network(self) -> None:
        self.check_stop()
        if self.failure:
            raise ChromeSessionError(self.failure)
        self.diagnostics.check_status()

    def cart_state(self) -> str:
        deadline = time.monotonic() + self.config.run.page_timeout_ms / 1000
        while time.monotonic() < deadline:
            self.guard()
            rows = self.page.locator('ol[data-autom="bag-items"] > li[data-autom^="bag-item-"]')
            if rows.count():
                if rows.count() != 1:
                    raise ChromeSessionError("购物袋不是单件商品；请人工整理为目标机型 1 台后重新演练")
                row = rows.first
                names = row.locator('[data-autom="bag-item-name"]')
                quantities = row.locator('[data-autom="item-quantity-dropdown"]')
                if names.count() != 1 or quantities.count() != 1:
                    raise ChromeSessionError("购物袋商品或数量控件不明确；本次停止，不修改购物袋")
                name = _normalized(names.inner_text())
                if not all(_normalized(token) in name for token in self.expected):
                    raise ChromeSessionError("购物袋已有商品与目标机型、颜色或容量不符；请人工整理后再运行")
                if quantities.input_value() != "1":
                    raise ChromeSessionError("购物袋目标商品数量不是 1；请人工核对后再运行")
                return "target"
            empty = self.page.get_by_role("heading", name=re.compile(r"你的购物袋中没有商品|Your bag is empty", re.I))
            if empty.count() and empty.first.is_visible():
                return "empty"
            self.page.wait_for_timeout(100)
        raise ChromeSessionError("未能确认购物袋内容；不重复加购，不进入结账")

    def open_bag(self) -> str:
        self.guard()
        self.page.bring_to_front()
        self.page.goto(self.bag_url, wait_until="domcontentloaded", timeout=self.config.run.page_timeout_ms)
        return self.cart_state()

    def step(self, step: StepConfig, *, final_wait: bool = False) -> None:
        if step.submit:
            raise ChromeSessionError("会话演练拒绝执行最终提交步骤")
        self.guard()
        self.page.bring_to_front()
        timeout = step.timeout_ms or self.config.run.page_timeout_ms
        selector = render_template(step.selector, self.account, self.config.variables)
        self.log.info("同会话演练步骤：%s", step.name)
        if step.action in {"wait_visible", "wait_hidden", "wait_url"}:
            if final_wait:
                timeout = max(timeout, self.config.run.challenge_timeout_seconds * 1000)
                self.log.info("等待订单确认页；如需选择配送、补填地址或付款方式，请在此页人工完成，停在最终提交前。")
            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                resumed = self.guard() or self.auth_resumed
                self.auth_resumed = False
                if resumed:
                    deadline = time.monotonic() + timeout / 1000
                    # Apple 二次认证可能回到购物袋。只有人工认证结束且重新核对
                    # 单件商品后才允许再次进入结账；不盲目重放加购或结账。
                    if final_wait and self.checkout_started and re.match(r"^/shop/(bag|account)(/|$)", urlsplit(self.page.url).path):
                        if self.submit_attempted:
                            raise ChromeSessionError("最终提交后回到购物袋，订单结果待确认；不再次进入结账")
                        state = self.cart_state() if urlsplit(self.page.url).path == "/shop/bag" else self.open_bag()
                        if self.checkout_resumed or state != "target":
                            raise ChromeSessionError("结账登录后未能继续；窗口保留，请人工核对")
                        self.checkout_resumed = True
                        self.step(self.checkout_step)
                if step.action == "wait_url":
                    value = render_template(step.value, self.account, self.config.variables)
                    from playwright.sync_api import TimeoutError as BrowserTimeout
                    try:
                        self.page.wait_for_url(value, timeout=100, wait_until="domcontentloaded")
                        ready = True
                    except BrowserTimeout:
                        ready = False
                else:
                    locator = self.page.locator(selector).first
                    ready = locator.is_visible() if step.action == "wait_visible" else not locator.is_visible()
                if ready:
                    return
                self.page.wait_for_timeout(100)
            raise ChromeSessionError(f"等待步骤“{step.name}”未完成；窗口保留，未提交订单")
        if step.action == "goto" and not is_apple_store_url(render_template(step.value, self.account, self.config.variables)):
            raise ChromeSessionError("会话演练拒绝跳转到 Apple Store 以外的地址")
        if step.action == "click":
            locator = self.page.locator(selector).first
            label = ((locator.text_content() or "") + (locator.get_attribute("aria-label") or "")).strip()
            if re.search(r"提交订单|确认下单|立即下单|^下单$|Place\s+(?:your\s+)?order|Pay\s+now", label, re.I):
                raise ChromeSessionError("已拒绝点击可能提交订单的按钮；请将其配置为 submit=true")
        # 不调用 worker 的重试入口；加购、结账和填写各只执行一次。
        self.check_stop()
        self.worker._execute_step(self.page, step)
        if step.after_ms:
            self.page.wait_for_timeout(step.after_ms)
        self.guard()

    def run(self) -> SessionTestResult:
        self.context.on("response", self.record_response)
        self.context.on("requestfailed", self.record_failed)
        self.diagnostics.attach(self.context)
        try:
            self.check_stop()
            inspection = inspect_apple_login(self.context)
            self.page = inspection.page
            if inspection.state != LoginState.SIGNED_IN:
                if self.page is None:
                    raise ChromeSessionError("没有可复用的 Apple Store 标签页；请先运行 chrome-login 并完成登录")
                self.page.bring_to_front()
                self.page.goto(self.config.target.login_url, wait_until="domcontentloaded", timeout=self.config.run.page_timeout_ms)
                self.log.info("正在核对商店登录；如出现登录或验证，请在专用标签页人工完成。")
                self.page = wait_for_apple_login(self.context, self.config.run.challenge_timeout_seconds,
                                                 on_tick=self.check_network)
            self.guard()
            while True:
                kwargs = {"wait_available": True, "on_guard": self.guard} if self.watch else {}
                if self.stop_requested is not None and not self.watch:
                    kwargs["on_guard"] = self.check_stop
                product = prepare_product_page(self.context, self.page, self.config, self.account, self.log, **kwargs)
                if not product.page_ready:
                    return SessionTestResult(False, "当前商品不可继续购买；未加购或进入结账", False)
                model = str(self.config.variables.get("model", ""))
                if not model:
                    model = re.sub(r"^(购买|选购|Buy)\s*", "", self.page.locator("h1").first.inner_text(), flags=re.I).strip()
                self.expected = (model, str(self.config.variables.get("color", "")), str(self.config.variables.get("capacity", "")))
                if not all(self.expected):
                    raise ChromeSessionError("无法确认目标规格；请配置 variables.model、color 和 capacity")
                state = self.open_bag()
                if state == "target":
                    self.log.info("购物袋已有目标机型 1 台，已跳过重复加购。")
                    break
                options = {"on_guard": self.check_stop} if self.stop_requested is not None else {}
                product = prepare_product_page(self.context, self.page, self.config, self.account, self.log, **options)
                if not product.page_ready:
                    if self.watch:
                        continue  # 尚未修改购物袋，可以重新等待；加购之后永远不回到监控。
                    return SessionTestResult(False, "复核时商品已不可购买；未加入购物袋", False)
                for step in self.config.steps[:self.add_index + 1]:
                    self.step(step)
                if self.open_bag() != "target":
                    raise ChromeSessionError("加购结果未确认；本次停止，不自动重试；再次运行会先核对购物袋")
                break
            self.log.info("已确认购物袋只有目标机型 1 台，继续结账。")
            for index in range(self.add_index + 1, self.submit_index):
                step = self.config.steps[index]
                is_final_wait = step.action == "wait_visible" and index > self.checkout_index
                if index == self.checkout_index:
                    self.checkout_started = True
                self.step(step, final_wait=is_final_wait)
            # 即使配置遗漏等待步骤，也必须实际看到可用的最终按钮，才算演练成功。
            final = self.page.locator(render_template(self.final_step.selector, self.account, self.config.variables)).first
            self.guard()
            if not urlsplit(self.page.url).path.startswith("/shop/checkout"):
                raise ChromeSessionError("当前页面不是结账确认页面；未提交订单，不报告演练成功")
            if not final.is_visible() or not final.is_enabled():
                raise ChromeSessionError("最终提交按钮尚未就绪；不把流程执行完当作成功")
            if self.submit_order:
                return self.place_order(final)
            self.worker.artifacts_dir.mkdir(parents=True, exist_ok=True)
            screenshot = self.worker._screenshot(self.page, "session-dry-run-ready")
            self.log.warning("已到最终提交订单页面；未点击最终提交，专用 Chrome 保留供核对。")
            return SessionTestResult(True, "已到最终提交订单前；未提交订单、未付款", True, screenshot)
        finally:
            # Ctrl+C 可能已经终止 Playwright 事件循环；清理异常不能覆盖中断原因。
            for event, handler in (("response", self.record_response), ("requestfailed", self.record_failed)):
                try:
                    self.context.remove_listener(event, handler)
                except Exception:
                    pass
            try:
                self.diagnostics.detach(self.context)
            except Exception:
                pass


    def place_order(self, final: Any) -> SessionTestResult:
        if self.submit_attempted:
            raise ChromeSessionError("本次已经尝试过最终提交；不重复点击，请人工核对订单")
        confirmations = [step for step in self.config.steps[self.submit_index + 1:]
                         if step.action in {"wait_visible", "wait_url"}]
        # 已经出现在结账页的标题/URL 不能证明一次提交成功。
        def ready(step: StepConfig) -> bool:
            if step.action == "wait_url":
                return fnmatchcase(self.page.url, render_template(step.value, self.account, self.config.variables))
            return self.page.locator(render_template(step.selector, self.account, self.config.variables)).first.is_visible()

        if all(ready(step) for step in confirmations):
            raise ChromeSessionError("订单确认条件在提交前已经满足，无法确认提交结果；请先校准确认选择器，未提交订单")
        selector = render_template(self.final_step.selector, self.account, self.config.variables)
        if self.page.locator(selector).count() != 1:
            raise ChromeSessionError("最终提交按钮不唯一；未提交订单")
        self.guard()
        if not urlsplit(self.page.url).path.startswith("/shop/checkout") or not final.is_enabled():
            raise ChromeSessionError("提交前结账页面已变化；未提交订单")
        self.worker.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.log.warning("已明确允许提交订单，将只点击一次最终提交；付款仍由人工完成。")
        self.check_stop()
        self.submit_attempted = True  # 点击 API 超时也可能已送达，绝不能自动重试。
        try:
            final.click(timeout=self.final_step.timeout_ms or self.config.run.page_timeout_ms)
            if self.final_step.after_ms:
                self.page.wait_for_timeout(self.final_step.after_ms)
            self.guard()
            for step in self.config.steps[self.submit_index + 1:]:
                self.step(step)
            screenshot = self.worker._screenshot(self.page, "session-order-submitted")
            self.log.info("已确认订单提交或进入待支付页面；原 Chrome 保持打开，请手动支付。")
            return SessionTestResult(True, "已确认订单提交；未自动付款，原 Chrome 保持打开", True, screenshot, True)
        except (Exception, KeyboardInterrupt) as exc:
            screenshot = self.worker._screenshot(self.page, "session-submit-unknown")
            self.log.error("最终提交结果无法确认（%s）；立即停止，不重复点击，请在原窗口核对订单。", type(exc).__name__)
            return SessionTestResult(False, "已尝试最终提交，但订单结果待确认；请人工核对，不要直接重新下单", True, screenshot, None)


class CheckoutDryRun(SessionCheckout):
    """单次测试入口永远不允许最终提交，也不持续监控。"""

    def __init__(self, context: Any, config: AppConfig, account: AccountConfig, logger: logging.Logger):
        super().__init__(context, config, account, logger)


def check_until_submit(context: Any, config: AppConfig, account: AccountConfig, logger: logging.Logger) -> SessionTestResult:
    return CheckoutDryRun(context, config, account, logger).run()
