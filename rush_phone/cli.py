from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .interactive_test import run_interactive_test
from .logging_cn import setup_logging
from .login_status import is_apple_store_url, wait_for_apple_login
from .login_diagnostics import LoginNetworkDiagnostics
from .runner import run_accounts
from .session import restore_auth_state, save_auth_state
from .worker import Outcome
from .chrome_session import ChromeSessionError, open_chrome_session
from .session_test import run_session_test, run_session_purchase


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多账号并发、人工验证接管的抢购助手")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="生成一份可编辑的配置文件")
    init.add_argument("--output", default="config.toml", help="输出路径")
    init.add_argument(
        "--preset",
        choices=("apple-cn", "generic"),
        default="apple-cn",
        help="配置模板，默认 Apple 中国大陆官方商店",
    )

    for name, help_text in (
        ("doctor", "检查配置与浏览器环境"),
        ("login", "逐个账号打开浏览器，人工预登录"),
        ("test", "先人工登录，终端确认后在同一窗口演练，不提交订单"),
        ("run", "Apple：复用 chrome-login 窗口持续等开售，--submit-order 允许最终下单"),
        ("chrome-login", "打开独立 Chrome，先人工登录，不连接自动化或导出 Cookie"),
        ("session-test", "复用专用 Chrome 演练到最终提交前；不提交订单，--product-only 只查商品"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--config", default="config.toml", help="配置文件路径")
        item.add_argument("--account", action="append", help="只运行指定账号，可重复传入")
    run = sub.choices["run"]
    sub.choices["chrome-login"].add_argument("--port", type=int, help="专用本机端口；默认自动选择空闲端口")
    sub.choices["chrome-login"].add_argument("--chrome-path", help="系统 Chrome 可执行文件路径；默认自动查找，Windows 为 chrome.exe")
    sub.choices["session-test"].add_argument(
        "--product-only", action="store_true",
        help="仅检查登录、规格和配送，不加入购物袋或进入结账",
    )
    sub.choices["test"].add_argument(
        "--auto-login", action="store_true",
        help="使用 accounts 登录名与配置目录 .env 的密码，仅提交一次；验证仍需人工完成",
    )
    sub.choices["login"].add_argument(
        "--then-test",
        action="store_true",
        help="等同于 test：先人工登录，按 Enter 确认后在同一窗口单次演练",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="只检查一次库存并保存截图；可购买时演练到最终提交前，随后退出",
    )
    run.add_argument(
        "--submit-order",
        action="store_true",
        help="允许点击标有 submit=true 的最终下单按钮",
    )
    return parser


def _select_accounts(config: AppConfig, names: list[str] | None):
    selected = [
        account
        for account in config.accounts
        if account.enabled and (not names or account.name in set(names))
    ]
    missing = set(names or ()) - {account.name for account in config.accounts}
    if missing:
        raise ConfigError(f"找不到账号：{', '.join(sorted(missing))}")
    if not selected:
        raise ConfigError("没有匹配的已启用账号")
    return selected


def _doctor(config: AppConfig) -> int:
    print(f"配置文件：{config.source_path}")
    print(f"账号数量：{len([a for a in config.accounts if a.enabled])}")
    print(f"流程步骤：{len(config.steps)}")
    print(f"轮询间隔：{config.run.poll_interval_ms}ms ± {config.run.poll_jitter_ms}ms")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[失败] 尚未安装 Playwright：请执行 pip install -e .")
        return 2
    try:
        with sync_playwright() as playwright:
            if config.run.browser_channel:
                browser = playwright.chromium.launch(
                    channel=config.run.browser_channel, headless=True
                )
                browser.close()
                print(f"[通过] 浏览器通道：{config.run.browser_channel}")
            else:
                executable = Path(playwright.chromium.executable_path)
                if not executable.exists():
                    print("[失败] 尚未安装 Chromium：请执行 playwright install chromium")
                    return 2
                print(f"[通过] Chromium：{executable}")
    except Exception as exc:
        print(f"[失败] 无法检查 Chromium：{exc}")
        return 2
    for account in config.accounts:
        print(f"[通过] {account.name} 独立资料目录：{account.profile_dir}")
    print("环境检查通过")
    return 0


def _interactive_test(config: AppConfig, names: list[str] | None, verbose: bool = False, auto_login: bool = False) -> int:
    accounts = _select_accounts(config, names)
    if len(accounts) != 1:
        raise ConfigError("交互演练一次只运行一个账号，请使用 --account 指定账号")
    logger = setup_logging(config.source_path.parent, verbose=verbose)
    result = run_interactive_test(config, accounts[0], logger, auto_login=auto_login)
    return 0 if result.outcome == Outcome.DRY_RUN_READY else 1


def _login(config: AppConfig, names: list[str] | None) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 Playwright，请先执行 pip install -e .", file=sys.stderr)
        return 2
    for account in _select_accounts(config, names):
        account.profile_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n正在打开账号“{account.name}”的登录页……")
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
            diagnostics = LoginNetworkDiagnostics(lambda message: print(message, flush=True))
            diagnostics.attach(context)
            try:
                restore_auth_state(context, account)
                page = context.pages[0] if context.pages else context.new_page()
                response = page.goto(config.target.login_url, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    raise RuntimeError(f"登录入口返回 HTTP {response.status}")
                page.bring_to_front()
                if is_apple_store_url(config.target.login_url):
                    print("请在专用窗口完成登录和验证；成功后自动保存并继续，无需回终端按 Enter。")
                    page = wait_for_apple_login(
                        context,
                        config.run.challenge_timeout_seconds,
                        on_change=lambda status: print(f"登录状态：{status.state.value}；{status.url}", flush=True),
                        on_tick=diagnostics.check_status,
                    )
                else:
                    input(f"请在浏览器中完成“{account.name}”的登录和验证，然后回到终端按 Enter 保存：")
                state_path = save_auth_state(context, account)
                print(f"账号“{account.name}”的登录状态已保存：{state_path}")
            finally:
                diagnostics.detach(context)
                context.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run" and args.once and args.submit_order:
        parser.error("--once 是单次演练选项，不能和 --submit-order 同时使用")
    if args.command == "init":
        filename = (
            "config.apple-cn.example.toml"
            if args.preset == "apple-cn"
            else "config.example.toml"
        )
        source = Path(__file__).resolve().parent.parent / filename
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            print(f"拒绝覆盖已有文件：{output}", file=sys.stderr)
            return 2
        shutil.copyfile(source, output)
        print(f"已生成配置：{output}（模板：{args.preset}）")
        if args.preset == "apple-cn":
            print("请先修改颜色、容量和账号；预购开放后务必用演练模式校准结账页选择器。")
        return 0

    try:
        config = load_config(args.config)
        _select_accounts(config, args.account)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return _doctor(config)
    if args.command in {"chrome-login", "session-test"}:
        try:
            accounts = _select_accounts(config, args.account)
            if len(accounts) != 1:
                raise ConfigError("会话连接测试一次只运行一个账号，请使用 --account 指定账号")
            logger = setup_logging(config.source_path.parent, verbose=args.verbose)
            if args.command == "chrome-login":
                session, reused = open_chrome_session(config, accounts[0], args.port, args.chrome_path)
                logger.info("%s专用 Chrome 已就绪（仅本机端口 %d）；请在此窗口人工登录并保持打开。", "已有" if reused else "新的", session.port)
                logger.info("此阶段没有连接 Playwright，也没有读取 .env 或 Cookie；完成登录后运行 run 购买，或 session-test 单次演练。")
                return 0
            result = run_session_test(config, accounts[0], logger, product_only=args.product_only)
            logger.info("会话测试结果：%s", result.message)
            if result.screenshot:
                logger.info("现场截图：%s", result.screenshot)
            return 0 if result.ok else 1
        except (ConfigError, ChromeSessionError) as exc:
            print(f"会话操作未完成：{exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("已断开会话演练；未提交订单，专用 Chrome 保持打开。", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"会话操作未完成（{type(exc).__name__}）；未提交订单，未清除账号资料。", file=sys.stderr)
            return 1
    if args.command in {"login", "test"}:
        interactive = args.command == "test" or args.then_test
        try:
            if interactive:
                return _interactive_test(config, args.account, verbose=args.verbose, auto_login=getattr(args, "auto_login", False))
            return _login(config, args.account)
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("已停止交互操作；未执行最终提交订单。", file=sys.stderr)
            return 130
        except Exception as exc:
            detail = str(exc) if type(exc) in {RuntimeError, TimeoutError} else "浏览器启动或页面检查失败；账号资料未清除"
            print(f"{'演练' if interactive else '登录'}未完成：{type(exc).__name__}；{detail}", file=sys.stderr)
            return 1

    logger = setup_logging(config.source_path.parent, verbose=args.verbose)
    if is_apple_store_url(config.target.product_url):
        try:
            accounts = _select_accounts(config, args.account)
            if len(accounts) != 1:
                raise ConfigError("复用专用 Chrome 一次只运行一个账号，请使用 --account 指定账号")
            result = run_session_purchase(config, accounts[0], logger, submit_order=args.submit_order, check_once=args.once)
            logger.info("同会话购买结果：%s", result.message)
            if result.screenshot:
                logger.info("现场截图：%s", result.screenshot)
            return 0 if result.ok else (3 if result.order_submitted is None else 1)
        except (ConfigError, ChromeSessionError) as exc:
            logger.error("同会话购买已停止：%s", exc)
            return 1
        except KeyboardInterrupt:
            logger.warning("已断开购买脚本；专用 Chrome 保持打开，请核对当前页面和订单状态。")
            return 130
        except Exception as exc:
            logger.error("同会话购买未完成（%s）；请核对原 Chrome 页面与订单状态。", type(exc).__name__)
            return 1
    try:
        results = run_accounts(
            config,
            logger,
            submit_order=args.submit_order,
            account_names=set(args.account) if args.account else None,
            check_once=args.once,
        )
    except (ConfigError, ValueError) as exc:
        logger.error("无法启动：%s", exc)
        return 2
    return (
        0
        if any(
            r.outcome in {Outcome.SUCCESS, Outcome.DRY_RUN_READY, Outcome.SUBMIT_UNKNOWN}
            for r in results
        )
        else 1
    )
