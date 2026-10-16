from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import AppConfig
from .limiter import OrderLimiter
from .worker import PurchaseWorker, WorkerResult


def run_accounts(
    config: AppConfig,
    logger: logging.Logger,
    submit_order: bool,
    account_names: set[str] | None = None,
    check_once: bool = False,
) -> list[WorkerResult]:
    accounts = [
        account
        for account in config.accounts
        if account.enabled and (not account_names or account.name in account_names)
    ]
    if not accounts:
        raise ValueError("没有匹配的已启用账号")
    stop_event = threading.Event()
    shutdown_event = threading.Event()
    limiter = OrderLimiter(config.run.max_successful_orders)
    artifacts_dir = config.source_path.parent / "artifacts"

    old_sigint = signal.getsignal(signal.SIGINT)

    def handle_sigint(_signum: int, _frame: object) -> None:
        logger.warning("收到 Ctrl+C，正在停止所有账号……")
        stop_event.set()
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_sigint)
    results: list[WorkerResult] = []
    logger.info(
        "启动 %d 个账号；模式：%s；订单上限：%d",
        len(accounts),
        "真实提交" if submit_order else "演练（不点最终提交）",
        config.run.max_successful_orders,
    )
    try:
        with ThreadPoolExecutor(max_workers=len(accounts), thread_name_prefix="账号") as executor:
            futures = {
                executor.submit(
                    PurchaseWorker(
                        config=config,
                        account=account,
                        logger=logger,
                        limiter=limiter,
                        stop_event=stop_event,
                        submit_order=submit_order,
                        artifacts_dir=artifacts_dir,
                        shutdown_event=shutdown_event,
                        check_once=check_once,
                    ).run
                ): account.name
                for account in accounts
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                logger.info("账号“%s”结束：%s；%s", result.account, result.outcome.value, result.message)
                if result.screenshot:
                    logger.info("现场截图：%s", result.screenshot)
    finally:
        stop_event.set()
        shutdown_event.set()
        signal.signal(signal.SIGINT, old_sigint)
    return results
