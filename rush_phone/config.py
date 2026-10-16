from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigError(ValueError):
    """配置文件无效。"""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    profile_dir: Path
    enabled: bool = True
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunConfig:
    poll_interval_ms: int = 1000
    poll_jitter_ms: int = 200
    page_timeout_ms: int = 12_000
    step_retries: int = 1
    challenge_timeout_seconds: int = 300
    keep_open_after_success_seconds: int = 900
    max_successful_orders: int = 1
    headless: bool = False
    browser_channel: str = "chrome"
    stock_refresh_interval_ms: int = 5000


@dataclass(frozen=True)
class TargetConfig:
    product_url: str
    login_url: str


@dataclass(frozen=True)
class StockConfig:
    selector: str = ""
    mode: str = "text"
    attribute: str = ""
    available_terms: tuple[str, ...] = ()
    unavailable_terms: tuple[str, ...] = ()
    probe_url: str = ""
    probe_available_regex: str = ""
    probe_unavailable_regex: str = ""
    prepare_click_selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChallengeConfig:
    selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepConfig:
    name: str
    action: str
    selector: str = ""
    value: str = ""
    timeout_ms: int | None = None
    after_ms: int = 0
    optional: bool = False
    submit: bool = False


@dataclass(frozen=True)
class AppConfig:
    source_path: Path
    run: RunConfig
    target: TargetConfig
    stock: StockConfig
    challenge: ChallengeConfig
    steps: tuple[StepConfig, ...]
    accounts: tuple[AccountConfig, ...]
    variables: dict[str, Any]


def _tuple_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} 必须是字符串数组")
    return tuple(value)


def _url(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or urlparse(value).scheme not in {"http", "https"}:
        raise ConfigError(f"{field_name} 必须是 http/https URL")
    return value


def _resolve_dir(config_dir: Path, value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} 必须是非空路径")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_dir / path).resolve()


def load_config(path: str | Path) -> AppConfig:
    source = Path(path).expanduser().resolve()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件：{source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 格式错误：{exc}") from exc

    run_raw = raw.get("run", {})
    target_raw = raw.get("target", {})
    stock_raw = raw.get("stock", {})
    challenge_raw = raw.get("challenge", {})

    run = RunConfig(
        poll_interval_ms=int(run_raw.get("poll_interval_ms", 1000)),
        poll_jitter_ms=int(run_raw.get("poll_jitter_ms", 200)),
        page_timeout_ms=int(run_raw.get("page_timeout_ms", 12_000)),
        step_retries=int(run_raw.get("step_retries", 1)),
        challenge_timeout_seconds=int(run_raw.get("challenge_timeout_seconds", 300)),
        keep_open_after_success_seconds=int(
            run_raw.get("keep_open_after_success_seconds", 900)
        ),
        max_successful_orders=int(run_raw.get("max_successful_orders", 1)),
        headless=bool(run_raw.get("headless", False)),
        browser_channel=str(run_raw.get("browser_channel", "chrome")),
        stock_refresh_interval_ms=int(run_raw.get("stock_refresh_interval_ms", 5000)),
    )
    target = TargetConfig(
        product_url=_url(target_raw.get("product_url"), "target.product_url"),
        login_url=_url(target_raw.get("login_url"), "target.login_url"),
    )
    stock = StockConfig(
        selector=str(stock_raw.get("selector", "")),
        mode=str(stock_raw.get("mode", "text")),
        attribute=str(stock_raw.get("attribute", "")),
        available_terms=_tuple_strings(stock_raw.get("available_terms"), "stock.available_terms"),
        unavailable_terms=_tuple_strings(
            stock_raw.get("unavailable_terms"), "stock.unavailable_terms"
        ),
        probe_url=str(stock_raw.get("probe_url", "")),
        probe_available_regex=str(stock_raw.get("probe_available_regex", "")),
        probe_unavailable_regex=str(stock_raw.get("probe_unavailable_regex", "")),
        prepare_click_selectors=_tuple_strings(
            stock_raw.get("prepare_click_selectors"), "stock.prepare_click_selectors"
        ),
    )
    challenge = ChallengeConfig(
        selectors=_tuple_strings(challenge_raw.get("selectors"), "challenge.selectors")
    )

    steps: list[StepConfig] = []
    for index, item in enumerate(raw.get("flow", {}).get("steps", []), 1):
        if not isinstance(item, dict):
            raise ConfigError(f"flow.steps 第 {index} 项必须是表")
        action = str(item.get("action", "")).strip()
        if not action:
            raise ConfigError(f"flow.steps 第 {index} 项缺少 action")
        steps.append(
            StepConfig(
                name=str(item.get("name", f"步骤 {index}")),
                action=action,
                selector=str(item.get("selector", "")),
                value=str(item.get("value", "")),
                timeout_ms=int(item["timeout_ms"]) if "timeout_ms" in item else None,
                after_ms=int(item.get("after_ms", 0)),
                optional=bool(item.get("optional", False)),
                submit=bool(item.get("submit", False)),
            )
        )

    accounts: list[AccountConfig] = []
    for index, item in enumerate(raw.get("accounts", []), 1):
        if not isinstance(item, dict):
            raise ConfigError(f"accounts 第 {index} 项必须是表")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ConfigError(f"accounts 第 {index} 项缺少 name")
        accounts.append(
            AccountConfig(
                name=name,
                profile_dir=_resolve_dir(
                    source.parent, item.get("profile_dir"), f"accounts.{name}.profile_dir"
                ),
                enabled=bool(item.get("enabled", True)),
                data=dict(item.get("data", {})),
            )
        )

    config = AppConfig(
        source_path=source,
        run=run,
        target=target,
        stock=stock,
        challenge=challenge,
        steps=tuple(steps),
        accounts=tuple(accounts),
        variables=dict(raw.get("variables", {})),
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if config.run.stock_refresh_interval_ms < 1000:
        raise ConfigError("run.stock_refresh_interval_ms 不能低于 1000ms")
    if config.run.poll_interval_ms < 250:
        raise ConfigError("run.poll_interval_ms 不能低于 250ms，请避免触发站点限流")
    if config.run.poll_jitter_ms < 0:
        raise ConfigError("run.poll_jitter_ms 不能为负数")
    if config.run.page_timeout_ms < 1000:
        raise ConfigError("run.page_timeout_ms 不能低于 1000ms")
    if config.run.step_retries < 0:
        raise ConfigError("run.step_retries 不能为负数")
    if config.run.max_successful_orders < 1:
        raise ConfigError("run.max_successful_orders 必须至少为 1")
    if not config.accounts:
        raise ConfigError("至少需要配置一个 accounts 项")
    enabled = [account for account in config.accounts if account.enabled]
    if not enabled:
        raise ConfigError("至少需要启用一个账号")
    if len({account.name for account in config.accounts}) != len(config.accounts):
        raise ConfigError("账号 name 不能重复")
    if len({account.profile_dir for account in config.accounts}) != len(config.accounts):
        raise ConfigError("每个账号必须使用不同的 profile_dir")
    if config.stock.mode not in {"text", "enabled", "visible", "attribute"}:
        raise ConfigError("stock.mode 只支持 text/enabled/visible/attribute")
    if config.stock.mode == "attribute" and not config.stock.attribute:
        raise ConfigError("stock.mode=attribute 时必须填写 stock.attribute")
    if config.stock.probe_url:
        _url(config.stock.probe_url, "stock.probe_url")
        for field_name, pattern in (
            ("stock.probe_available_regex", config.stock.probe_available_regex),
            ("stock.probe_unavailable_regex", config.stock.probe_unavailable_regex),
        ):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigError(f"{field_name} 不是有效正则：{exc}") from exc
        if not config.stock.probe_available_regex:
            raise ConfigError("使用 probe_url 时必须配置 probe_available_regex")
    elif not config.stock.selector:
        raise ConfigError("stock.selector 和 stock.probe_url 至少填写一个")
    if not config.steps:
        raise ConfigError("flow.steps 不能为空")
    supported = {
        "goto",
        "click",
        "fill",
        "select",
        "check",
        "uncheck",
        "press",
        "wait_visible",
        "wait_hidden",
        "wait_url",
        "sleep",
    }
    submit_count = 0
    submit_index = -1
    for index, step in enumerate(config.steps):
        if step.action not in supported:
            raise ConfigError(f"步骤“{step.name}”的 action={step.action!r} 不受支持")
        if step.after_ms < 0:
            raise ConfigError(f"步骤“{step.name}”的 after_ms 不能为负数")
        if step.submit:
            submit_count += 1
            submit_index = index
            if step.action != "click":
                raise ConfigError(f"步骤“{step.name}”标记 submit 时 action 必须是 click")
            if step.optional:
                raise ConfigError(f"步骤“{step.name}”不能同时设置 submit=true 和 optional=true")
        if step.action in {
            "click",
            "fill",
            "select",
            "check",
            "uncheck",
            "press",
            "wait_visible",
            "wait_hidden",
        } and not step.selector:
            raise ConfigError(f"步骤“{step.name}”缺少 selector")
    if submit_count != 1:
        raise ConfigError("flow.steps 中必须恰好有一个 submit=true 的最终提交步骤")
    if submit_index == len(config.steps) - 1:
        raise ConfigError("submit=true 后必须至少有一个确认订单成功/待支付页的等待步骤")


_TEMPLATE_PATTERN = re.compile(r"\$\{([a-zA-Z0-9_.-]+)}")


def render_template(value: str, account: AccountConfig, variables: dict[str, Any]) -> str:
    root: dict[str, Any] = {
        "account": {"name": account.name, "data": account.data},
        "variables": variables,
    }

    def replace(match: re.Match[str]) -> str:
        current: Any = root
        path = match.group(1)
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise ConfigError(f"模板变量不存在：{path}")
            current = current[part]
        return str(current)

    return _TEMPLATE_PATTERN.sub(replace, value)
