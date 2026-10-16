from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import AccountConfig, AppConfig, ConfigError, StepConfig, render_template
from .limiter import OrderLimiter
from .session import auth_state_path, restore_auth_state, save_auth_state


class Outcome(str, Enum):
    SUCCESS = "下单成功"
    SUBMIT_UNKNOWN = "提交结果待确认"
    DRY_RUN_READY = "演练到达提交步骤"
    UNAVAILABLE = "当前不可购买"
    FAILED = "失败"
    CANCELLED = "已取消"


@dataclass(frozen=True)
class WorkerResult:
    account: str
    outcome: Outcome
    message: str
    screenshot: Path | None = None


class PurchaseWorker:
    def __init__(
        self,
        config: AppConfig,
        account: AccountConfig,
        logger: logging.Logger,
        limiter: OrderLimiter,
        stop_event: threading.Event,
        submit_order: bool,
        artifacts_dir: Path,
        shutdown_event: threading.Event,
        check_once: bool = False,
    ) -> None:
        self.config = config
        self.account = account
        self.log = logger
        self.limiter = limiter
        self.stop_event = stop_event
        self.submit_order = submit_order
        self.artifacts_dir = artifacts_dir
        self.shutdown_event = shutdown_event
        self.check_once = check_once
        self._slot_held = False
        self._submit_clicked = False
        self._document_response: tuple[int, str] | None = None
        self._critical_response: tuple[int, str] | None = None

    def run(self) -> WorkerResult:
        threading.current_thread().name = self.account.name
        self.account.profile_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return WorkerResult(
                self.account.name,
                Outcome.FAILED,
                "缺少 Playwright，请先执行 pip install -e .；未使用系统 Chrome 时还需执行 playwright install chromium",
            )

        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.account.profile_dir),
                    channel=self.config.run.browser_channel or None,
                    args=["--profile-directory=Default"],
                    handle_sigint=False,
                    headless=self.config.run.headless,
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    timeout=30_000,
                )
                context.set_default_timeout(self.config.run.page_timeout_ms)
                restored = restore_auth_state(context, self.account)
                if restored:
                    self.log.info("已恢复 %d 项登录会话信息", restored)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    result = self._run_in_page(
                        page, PlaywrightTimeoutError=PlaywrightTimeoutError
                    )
                    if result.outcome in {Outcome.SUCCESS, Outcome.SUBMIT_UNKNOWN}:
                        self._keep_open_after_success()
                    return result
                finally:
                    if auth_state_path(self.account).exists():
                        try:
                            save_auth_state(context, self.account)
                        except Exception as exc:
                            self.log.warning("无法更新登录状态快照：%s", type(exc).__name__)
                    try:
                        context.close()
                    except Exception:
                        if not self.shutdown_event.is_set():
                            raise
        except PlaywrightError as exc:
            if self._slot_held:
                self.limiter.release(self.account.name)
            self.log.error("浏览器执行失败：%s", exc)
            return WorkerResult(self.account.name, Outcome.FAILED, str(exc))
        except Exception as exc:  # 保证单账号异常不会拖垮其他账号
            if self._slot_held:
                self.limiter.release(self.account.name)
            self.log.exception("账号任务出现未预期错误")
            return WorkerResult(self.account.name, Outcome.FAILED, str(exc))

    def _run_in_page(self, page: Any, PlaywrightTimeoutError: type[Exception]) -> WorkerResult:
        self._document_response = None
        self._critical_response = None
        page.on("response", lambda response: self._record_document_response(page, response))
        self.log.info("开始监控库存：%s", self.config.target.product_url)
        consecutive_errors = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                available = self._check_stock(page, PlaywrightTimeoutError)
                consecutive_errors = 0
            except PlaywrightTimeoutError as exc:
                if self.check_once:
                    return self._flow_failure(page, "单次库存检查", exc)
                consecutive_errors += 1
                self.log.warning("库存检查超时（连续 %d 次）", consecutive_errors)
                available = False
            except Exception as exc:
                if self.check_once:
                    return self._flow_failure(page, "单次库存检查", exc)
                consecutive_errors += 1
                self.log.warning("库存检查异常（连续 %d 次）：%s", consecutive_errors, exc)
                available = False

            if self.check_once:
                self.log.info(
                    "单次检查完成：%s；耗时 %.2f 秒",
                    "检测到可购买状态" if available else "未检测到可购买状态",
                    time.monotonic() - started,
                )
                if not available:
                    return WorkerResult(
                        self.account.name,
                        Outcome.UNAVAILABLE,
                        "当前页面未检测到可购买状态，单次试跑结束",
                        self._screenshot(page, "stock-check"),
                    )

            if available:
                self.log.info("检测到可购买状态，开始执行下单流程")
                if self.config.stock.probe_url:
                    try:
                        page.goto(
                            self.config.target.product_url,
                            wait_until="domcontentloaded",
                            timeout=self.config.run.page_timeout_ms,
                        )
                    except Exception as exc:
                        return self._flow_failure(page, "打开商品页", exc)
                return self._execute_flow(page, PlaywrightTimeoutError)

            base = self.config.run.poll_interval_ms
            jitter = self.config.run.poll_jitter_ms
            delay_ms = max(250, base + random.randint(-jitter, jitter))
            if consecutive_errors:
                delay_ms = max(delay_ms, min(30_000, 1000 * (2 ** min(consecutive_errors, 5))))
            self.stop_event.wait(delay_ms / 1000)

        return WorkerResult(self.account.name, Outcome.CANCELLED, "其他账号已达到订单上限")

    def _check_stock(self, page: Any, timeout_type: type[Exception]) -> bool:
        stock = self.config.stock
        if stock.probe_url:
            probe_url = render_template(
                stock.probe_url, self.account, self.config.variables
            )
            response = page.request.get(probe_url, timeout=self.config.run.page_timeout_ms)
            if response.status in {403, 429}:
                raise RuntimeError(f"库存接口返回 {response.status}，已自动退避")
            if not response.ok:
                raise RuntimeError(f"库存接口返回 HTTP {response.status}")
            body = response.text()
            if re.search(stock.probe_available_regex, body):
                return True
            if stock.probe_unavailable_regex and re.search(stock.probe_unavailable_regex, body):
                return False
            self.log.debug("库存接口响应未匹配任何状态")
            return False

        self._document_response = None
        self._critical_response = None
        response = page.goto(
            self.config.target.product_url,
            wait_until="domcontentloaded",
            timeout=self.config.run.page_timeout_ms,
        )
        if response is not None:
            self._record_document_response(page, response)
            self._ensure_document_ok()
        self._handle_challenge(page)
        for selector_template in stock.prepare_click_selectors:
            selector = render_template(
                selector_template, self.account, self.config.variables
            )
            option = page.locator(selector).first
            try:
                self._click_control(
                    page, option,
                    timeout=min(self.config.run.page_timeout_ms, 3000),
                )
            except timeout_type:
                if option.count() and not option.is_enabled():
                    self._ensure_document_ok()
                    self.log.debug("商品配置选项尚未启用：%s", selector)
                    return False
                raise
            self.log.debug("库存检查的预选操作已完成：%s", selector)
            self._ensure_document_ok()
        return self._read_stock_state(page, timeout_type)

    def _read_stock_state(self, page: Any, timeout_type: type[Exception]) -> bool:
        """只读取当前页面状态，不导航、不点击；供同会话诊断复用。"""
        stock = self.config.stock
        stock_selector = render_template(
            stock.selector, self.account, self.config.variables
        )
        locator = page.locator(stock_selector).first
        try:
            locator.wait_for(state="attached", timeout=min(self.config.run.page_timeout_ms, 5000))
        except timeout_type:
            self._ensure_document_ok()
            self.log.debug("未找到库存元素：%s", stock_selector)
            return False
        self._ensure_document_ok()
        if stock.mode == "visible":
            return locator.is_visible()
        if stock.mode == "enabled":
            return locator.is_visible() and locator.is_enabled()
        if stock.mode == "attribute":
            observed = locator.get_attribute(stock.attribute) or ""
        else:
            observed = locator.text_content() or ""
        if any(term in observed for term in stock.unavailable_terms):
            return False
        if stock.available_terms:
            return any(term in observed for term in stock.available_terms)
        return bool(observed.strip())

    def _execute_flow(self, page: Any, timeout_type: type[Exception]) -> WorkerResult:
        for step in self.config.steps:
            if self.stop_event.is_set() and not self._slot_held:
                return WorkerResult(self.account.name, Outcome.CANCELLED, "订单上限已达到")
            try:
                self._ensure_document_ok()
                self._handle_challenge(page)
            except Exception as exc:
                return self._flow_failure(page, f"步骤“{step.name}”前的页面检查", exc)
            if step.submit:
                if not self.submit_order:
                    screenshot = self._screenshot(page, "dry-run-ready")
                    self.log.warning("演练模式已到最终提交步骤；未点击“%s”", step.name)
                    return WorkerResult(
                        self.account.name,
                        Outcome.DRY_RUN_READY,
                        "页面已准备到最终提交前；加 --submit-order 才会真正提交",
                        screenshot,
                    )
                self.log.info("等待最终下单名额")
                if not self.limiter.acquire(self.account.name, self.stop_event):
                    return WorkerResult(self.account.name, Outcome.CANCELLED, "订单上限已达到")
                self._slot_held = True

            try:
                self._execute_step_with_retry(page, step, timeout_type)
                self._ensure_document_ok()
            except Exception as exc:
                if step.optional:
                    self.log.warning("可选步骤“%s”失败，继续执行：%s", step.name, exc)
                    continue
                if self._slot_held and step.submit:
                    self._submit_clicked = True
                return self._flow_failure(page, f"步骤“{step.name}”", exc)
            if step.submit:
                self._submit_clicked = True
            try:
                self._handle_challenge(page)
            except Exception as exc:
                return self._flow_failure(page, f"步骤“{step.name}”后的人工验证", exc)

        if not self._slot_held:
            return WorkerResult(
                self.account.name,
                Outcome.FAILED,
                "流程结束但未执行最终提交步骤",
            )
        screenshot = self._screenshot(page, "order-submitted")
        limit_reached = self.limiter.commit(self.account.name)
        self._slot_held = False
        if limit_reached:
            self.stop_event.set()
        self.log.info("下单流程完成，浏览器将保留供手动支付；当前页面：%s", page.url)
        return WorkerResult(
            self.account.name,
            Outcome.SUCCESS,
            f"已完成配置的下单流程：{page.url}",
            screenshot,
        )

    def _record_document_response(self, page: Any, response: Any) -> None:
        try:
            request = response.request
            parts = urlsplit(response.url)
            safe_url = parts._replace(query="", fragment="").geturl()
            if request.resource_type == "document" and request.frame == page.main_frame:
                self._document_response = response.status, safe_url
            elif (
                request.resource_type in {"fetch", "xhr"}
                and request.frame == page.main_frame
                and response.status >= 400
                and (parts.scheme, parts.netloc) == (
                    urlsplit(self.config.target.product_url).scheme,
                    urlsplit(self.config.target.product_url).netloc,
                )
                and parts.path.rstrip("/") == "/shop/fulfillment-messages"
            ):
                # Apple 配送查询失败不等于缺货；也不能仅凭已启用的购物袋按钮判定可购买。
                self._critical_response = response.status, safe_url
        except Exception:
            pass

    def _ensure_document_ok(self) -> None:
        if self._document_response and self._document_response[0] >= 400:
            status, safe_url = self._document_response
            raise RuntimeError(f"页面请求返回 HTTP {status}：{safe_url}，已停止当前流程")
        if self._critical_response:
            status, safe_url = self._critical_response
            raise RuntimeError(f"配送查询返回 HTTP {status}：{safe_url}，已停止当前流程（不是缺货结论）")

    def _flow_failure(self, page: Any, stage: str, exc: Exception) -> WorkerResult:
        try:
            safe_url = urlsplit(page.url)._replace(query="", fragment="").geturl()
            self.log.error("失败现场：%s；页面标题：%s", safe_url, page.title())
        except Exception:
            pass
        if self._slot_held and self._submit_clicked:
            screenshot = self._screenshot(page, "submit-unknown")
            self.stop_event.set()
            self.log.error(
                "%s失败，最终提交结果无法确认；为避免重复下单，已停止其他账号：%s",
                stage,
                exc,
            )
            return WorkerResult(
                self.account.name,
                Outcome.SUBMIT_UNKNOWN,
                "最终提交结果不确定，请立即在保留的浏览器中人工检查订单状态",
                screenshot,
            )
        if self._slot_held:
            self.limiter.release(self.account.name)
            self._slot_held = False
        screenshot = self._screenshot(page, "step-failed")
        self.log.error("%s失败：%s", stage, exc)
        return WorkerResult(
            self.account.name,
            Outcome.FAILED,
            f"{stage}失败：{exc}",
            screenshot,
        )

    def _execute_step_with_retry(
        self, page: Any, step: StepConfig, timeout_type: type[Exception]
    ) -> None:
        last_error: Exception | None = None
        retries = 0 if step.submit else self.config.run.step_retries
        for attempt in range(retries + 1):
            try:
                self.log.info("执行步骤：%s", step.name)
                self._execute_step(page, step)
                if step.after_ms:
                    page.wait_for_timeout(step.after_ms)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    break
                self.log.warning(
                    "步骤“%s”失败，准备第 %d 次重试：%s",
                    step.name,
                    attempt + 1,
                    exc,
                )
                self._handle_challenge(page)
        assert last_error is not None
        raise last_error

    def _execute_step(self, page: Any, step: StepConfig) -> None:
        timeout = step.timeout_ms or self.config.run.page_timeout_ms
        selector = render_template(step.selector, self.account, self.config.variables)
        value = render_template(step.value, self.account, self.config.variables)
        if step.action == "goto":
            page.goto(value, wait_until="domcontentloaded", timeout=timeout)
            return
        if step.action == "sleep":
            try:
                milliseconds = int(value)
            except ValueError as exc:
                raise ConfigError(f"sleep 的 value 必须是毫秒整数：{value!r}") from exc
            page.wait_for_timeout(milliseconds)
            return
        if step.action == "wait_url":
            page.wait_for_url(value, timeout=timeout)
            return

        locator = page.locator(selector).first
        if step.action == "click":
            if step.submit:
                locator.click(timeout=timeout)
            else:
                self._click_control(page, locator, timeout=timeout)
        elif step.action == "fill":
            locator.fill(value, timeout=timeout)
        elif step.action == "select":
            locator.select_option(value, timeout=timeout)
        elif step.action == "check":
            locator.check(timeout=timeout)
        elif step.action == "uncheck":
            locator.uncheck(timeout=timeout)
        elif step.action == "press":
            locator.press(value, timeout=timeout)
        elif step.action == "wait_visible":
            locator.wait_for(state="visible", timeout=timeout)
        elif step.action == "wait_hidden":
            locator.wait_for(state="hidden", timeout=timeout)
        else:  # validate_config 已拦截，仅为类型完整性
            raise ConfigError(f"不支持的步骤：{step.action}")

    @staticmethod
    def _click_control(page: Any, locator: Any, timeout: int) -> None:
        """样式化单选框通过可见 label 点击，避免 input 被色块覆盖而超时。"""
        control = locator.evaluate(
            "element => ({tag: element.tagName, type: element.type, id: element.id})",
            timeout=timeout,
        )
        if control["tag"] == "INPUT" and control["type"] in {"radio", "checkbox"}:
            if control["type"] == "radio" and locator.is_checked():
                return
            locator.evaluate("element => element.scrollIntoView({block: 'center', inline: 'center'})")
            input_receives_click = locator.evaluate("""element => {
                const box = element.getBoundingClientRect();
                const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
                return hit === element || element.contains(hit);
            }""")
            if input_receives_click:
                locator.click(timeout=timeout)
                return
            label = locator.locator("xpath=ancestor::label[1]")
            if not label.count() and control["id"]:
                label = page.locator(f'label[for={json.dumps(control["id"])}]')
            if label.count() and label.first.is_visible():
                label.first.click(timeout=timeout)
                if control["type"] == "radio" and not locator.is_checked():
                    raise RuntimeError("点击标签后单选框未选中，请检查选项是否仍被禁用")
                return
        locator.click(timeout=timeout)

    def _visible_challenge(self, page: Any) -> str | None:
        for selector in self.config.challenge.selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=250):
                    return selector
            except Exception:
                continue
        return None

    def _handle_challenge(self, page: Any) -> None:
        selector = self._visible_challenge(page)
        if not selector:
            return
        screenshot = self._screenshot(page, "verification")
        self.log.warning(
            "检测到验证（%s），请在账号“%s”的可视浏览器窗口中人工完成；截图：%s",
            selector,
            self.account.name,
            screenshot,
        )
        deadline = time.monotonic() + self.config.run.challenge_timeout_seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            page.wait_for_timeout(500)
            if not self._visible_challenge(page):
                self.log.info("人工验证已完成，继续执行")
                return
        raise RuntimeError("等待人工验证超时")

    def _screenshot(self, page: Any, label: str) -> Path | None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_account = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", self.account.name)
        path = self.artifacts_dir / f"{safe_account}-{label}-{timestamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:
            self.log.warning("保存截图失败：%s", exc)
            return None

    def _keep_open_after_success(self) -> None:
        seconds = self.config.run.keep_open_after_success_seconds
        if seconds <= 0:
            return
        self.log.info("浏览器将继续保留 %d 秒，便于手动支付（Ctrl+C 可结束）", seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.shutdown_event.is_set():
            self.shutdown_event.wait(min(1, deadline - time.monotonic()))
