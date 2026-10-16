from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from enum import Enum
from typing import Any, TextIO

from .config import AccountConfig, AppConfig
from .auto_login import AutoLoginError, submit_credentials_once
from .credentials import load_credentials
from .limiter import OrderLimiter
from .login_diagnostics import LoginNetworkDiagnostics
from .login_status import LoginState, inspect_apple_login, is_apple_store_url, safe_page_url
from .session import restore_auth_state, save_auth_state
from .worker import Outcome, PurchaseWorker, WorkerResult


class TerminalAction(Enum):
    CONFIRM = "确认"
    QUIT = "退出"
    EOF = "输入已关闭"
    INVALID = "无效命令"


class TerminalInput:
    """只在线程中读终端，不阻塞主线程处理 Playwright 事件。"""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.actions: queue.Queue[TerminalAction] = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._read, name="演练终端输入", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def _read(self) -> None:
        while not self.stopped.is_set():
            try:
                line = self.stream.readline()
            except (OSError, ValueError):
                line = ""
            if line == "":
                self.actions.put(TerminalAction.EOF)
                return
            command = line.strip().lower()
            action = (TerminalAction.CONFIRM if not command else
                      TerminalAction.QUIT if command in {"q", "quit", "exit"} else TerminalAction.INVALID)
            # 队列仅保留命令类型，不保留用户输入的原文。
            self.actions.put(action)
            if action == TerminalAction.QUIT:
                return

    def next_action(self) -> TerminalAction | None:
        try:
            return self.actions.get_nowait()
        except queue.Empty:
            return None


def _pump_browser(context: Any) -> bool:
    pages = [page for page in context.pages if not page.is_closed()]
    if not pages:
        return False
    try:
        pages[0].wait_for_timeout(250)
    except Exception:
        if not pages[0].is_closed():
            raise
    return any(not page.is_closed() for page in context.pages)


def wait_for_manual_login(
    context: Any,
    original_page: Any,
    inputs: Any,
    diagnostics: LoginNetworkDiagnostics,
    logger: logging.Logger,
    login_url: str,
    warning_after_seconds: float,
    auto_confirm: bool = False,
) -> Any | None:
    """登录错误/超时只保留窗口提示；确认有效会话且收到 Enter 才返回。"""
    previous = None
    previous_error = None
    warned = False
    started = time.monotonic()
    while any(not page.is_closed() for page in context.pages):
        inspection = inspect_apple_login(context)
        marker = inspection.state, inspection.url
        if marker != previous:
            logger.info("登录状态：%s；%s", inspection.state.value, inspection.url)
            if inspection.state == LoginState.SIGNED_IN and not auto_confirm:
                logger.info("已检测到商店登录。准备好后，请回终端按 Enter，才会打开商品页选择规格。")
            previous = marker
        error = None
        try:
            diagnostics.check_status()
        except RuntimeError as exc:
            error = str(exc)
            if error != previous_error:
                logger.warning("%s；窗口保持打开，等待你人工处理；输入 q 退出。", error)
        previous_error = error
        if auto_confirm and error is None and inspection.state == LoginState.SIGNED_IN:
            logger.info("自动登录已确认有效商店会话，按授权继续单次演练；不提交订单。")
            return inspection.page
        if not warned and time.monotonic() - started >= warning_after_seconds:
            logger.warning("登录等待已超过配置时限；交互演练不会自动关闭窗口，也不会自动重试登录。")
            warned = True

        action = inputs.next_action()
        if action in {TerminalAction.QUIT, TerminalAction.EOF}:
            if action == TerminalAction.EOF:
                logger.warning("终端输入已关闭，无法等待手动确认；请在可输入的终端运行 test 命令。")
            return None
        if action == TerminalAction.INVALID:
            logger.info("终端只需按 Enter 确认，或输入 q 退出；不要在终端输入密码或验证码。")
        if action == TerminalAction.CONFIRM:
            if error:
                logger.warning("商店登录交接仍有错误，暂不进入商品页。请先在当前窗口完成人工登录。")
            elif is_apple_store_url(login_url) and inspection.state != LoginState.SIGNED_IN:
                logger.warning("尚未确认商店登录成功，继续保留登录页；成功后再按 Enter。")
            else:
                return inspection.page or original_page
        if not _pump_browser(context):
            break
    logger.info("浏览器已关闭，结束交互演练。")
    return None


def hold_test_window(context: Any, inputs: Any, logger: logging.Logger) -> None:
    logger.info("本次演练已停止，窗口保持打开供检查；不会自动重复运行。输入 q、关闭窗口或按 Ctrl+C 结束。")
    while any(not page.is_closed() for page in context.pages):
        action = inputs.next_action()
        if action in {TerminalAction.QUIT, TerminalAction.EOF}:
            return
        if action is not None:
            logger.info("本次演练不会重复执行；输入 q 退出，或继续在浏览器中检查。")
        if not _pump_browser(context):
            return


def run_interactive_test(config: AppConfig, account: AccountConfig, logger: logging.Logger, auto_login: bool = False) -> WorkerResult:
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from playwright.sync_api import sync_playwright

    credentials = load_credentials(account, config.source_path.parent / ".env") if auto_login else None
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    inputs = TerminalInput(sys.stdin)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(account.profile_dir),
            channel=config.run.browser_channel or None,
            args=["--profile-directory=Default"],
            handle_sigint=False,
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timeout=30_000,
        )
        context.set_default_timeout(config.run.page_timeout_ms)
        diagnostics = LoginNetworkDiagnostics(logger.info)
        diagnostics.attach(context)
        verified = False
        try:
            try:
                restore_auth_state(context, account)
            except Exception as exc:
                logger.warning("无法恢复会话快照（%s）；不清除原资料，请在当前窗口人工登录。", type(exc).__name__)
            page = context.pages[0] if context.pages else context.new_page()
            logger.info("阶段 1/2：先%s登录；此时不会打开商品页或选择任何规格。", "单次自动" if auto_login else "人工")
            if auto_login:
                logger.info("仅从 .env 读取密码并提交一次；有效登录后自动继续。验证码和双重认证仍需人工完成，输入 q 退出。")
            else:
                logger.info("请只在浏览器输入账号密码和验证码；登录完成后在终端按 Enter，输入 q 退出。")
            try:
                response = page.goto(config.target.login_url, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    logger.warning("登录入口返回 HTTP %d；保留窗口，供人工检查。", response.status)
            except Exception as exc:
                logger.warning("登录入口加载未完成（%s）；保留窗口，可人工访问：%s", type(exc).__name__, safe_page_url(config.target.login_url))
            if not page.is_closed():
                page.bring_to_front()
            if credentials is not None:
                try:
                    result = submit_credentials_once(context, credentials)
                    logger.info("自动登录状态：%s；不自动重试。", result.value)
                except AutoLoginError as exc:
                    logger.warning("%s；保留窗口等待人工处理。", exc)
                finally:
                    credentials = None
            inputs.start()
            page = wait_for_manual_login(
                context, page, inputs, diagnostics, logger,
                config.target.login_url, config.run.challenge_timeout_seconds,
                auto_confirm=auto_login,
            )
            if page is None:
                return WorkerResult(account.name, Outcome.CANCELLED, "人工登录阶段已结束，未进入商品页")
            verified = True
            try:
                state_path = save_auth_state(context, account)
                logger.info("已保存当前登录会话：%s", state_path)
            except Exception as exc:
                logger.warning("无法保存会话快照（%s）；继续使用当前窗口的已登录会话。", type(exc).__name__)
            diagnostics.detach(context)
            logger.info("阶段 2/2：已确认登录，在同一窗口开始选择规格和单次演练；不提交订单。")
            worker = PurchaseWorker(
                config, account, logger, OrderLimiter(config.run.max_successful_orders),
                threading.Event(), False, config.source_path.parent / "artifacts", threading.Event(), True,
            )
            worker.artifacts_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = worker._run_in_page(page, BrowserTimeout)
            except Exception as exc:
                logger.error("演练出现异常（%s），保留窗口供检查；不会自动重复运行。", type(exc).__name__)
                result = WorkerResult(account.name, Outcome.FAILED, f"演练异常：{type(exc).__name__}")
            logger.info("演练结果：%s；%s", result.outcome.value, result.message)
            if result.screenshot:
                logger.info("现场截图：%s", result.screenshot)
            hold_test_window(context, inputs, logger)
            return result
        finally:
            inputs.stop()
            diagnostics.detach(context)
            if verified:
                try:
                    save_auth_state(context, account)
                except Exception:
                    pass
            context.close()
