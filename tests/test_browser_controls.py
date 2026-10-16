"""RUSH_PHONE_BROWSER_TESTS=1 时在本机 Chrome 中验证真实控件交互。"""

import os
import logging
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from rush_phone.config import AccountConfig, StepConfig, load_config
from rush_phone.auto_login import AutoLoginResult, submit_credentials_once
from rush_phone.credentials import LoginCredentials
from rush_phone.interactive_test import TerminalAction, run_interactive_test
from rush_phone.limiter import OrderLimiter
from rush_phone.login_diagnostics import LoginNetworkDiagnostics
from rush_phone.login_status import LoginState, inspect_apple_login, wait_for_apple_login
from rush_phone.session import restore_auth_state, save_auth_state
from rush_phone.worker import Outcome, PurchaseWorker


@unittest.skipUnless(os.environ.get("RUSH_PHONE_BROWSER_TESTS") == "1", "需显式启用浏览器测试")
class BrowserControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.page = self.browser.new_page()

    def tearDown(self) -> None:
        self.page.close()

    def test_styled_radio_with_label_overlay_can_be_selected(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        self.page.set_content("""
            <style>
              input {position:absolute; width:30px; height:30px; margin:0}
              label {position:relative; display:block; width:30px; height:30px}
              label span {display:block; width:30px; height:30px; background:black}
            </style>
            <input id="night" type="radio" name="color">
            <label for="night"><span></span></label>
        """)
        radio = self.page.locator("#night")
        with self.assertRaises(PlaywrightTimeoutError):
            radio.click(trial=True, timeout=250)
        PurchaseWorker._click_control(self.page, radio, timeout=1500)
        self.assertTrue(radio.is_checked())

    def test_disabled_radio_is_not_reported_as_selected(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        self.page.set_content('<input id="capacity" type="radio" disabled><label for="capacity">256GB</label>')
        with self.assertRaises(PlaywrightTimeoutError):
            PurchaseWorker._click_control(self.page, self.page.locator("#capacity"), timeout=500)
        self.assertFalse(self.page.locator("#capacity").is_checked())

    def test_radio_overlaying_its_label_can_be_selected(self) -> None:
        self.page.set_content("""
            <style>
              .card {position:relative; width:200px; height:60px}
              input {position:absolute; inset:0; width:200px; height:60px; margin:0; z-index:2}
              label {display:block; width:200px; height:60px}
            </style>
            <div class="card">
              <input id="no-trade" type="radio" name="trade">
              <label for="no-trade">不折抵换购</label>
            </div>
        """)
        radio = self.page.locator("#no-trade")
        PurchaseWorker._click_control(self.page, radio, timeout=1500)
        self.assertTrue(radio.is_checked())

    def test_regular_button_still_clicks(self) -> None:
        self.page.set_content('<button onclick="this.textContent=\'已点击\'">继续</button>')
        PurchaseWorker._click_control(self.page, self.page.locator("button"), timeout=1500)
        self.assertEqual(self.page.locator("button").inner_text(), "已点击")

    def test_session_cookie_is_restored_in_a_new_browser_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account = AccountConfig("测试", Path(directory))
            context = self.browser.new_context()
            context.add_cookies([
                {"name": "test_session", "value": "example-only", "domain": "example.test", "path": "/"}
            ])
            save_auth_state(context, account)
            context.close()
            fresh = self.browser.new_context()
            try:
                self.assertEqual(restore_auth_state(fresh, account), 1)
                restored = fresh.cookies()
                self.assertEqual(restored[0]["value"], "example-only")
            finally:
                fresh.close()

    def test_login_in_another_tab_is_detected_with_real_browser_events(self) -> None:
        # 所有页面由本地 fixture 拦截，不向 Apple 发送请求。
        context = self.browser.new_context()
        context.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body=("<h1>你的账户</h1>" if "/shop/account/home" in route.request.url else
                  '<iframe srcdoc=\'<input id="password_text_field" type="password">\'></iframe>'),
        ))
        try:
            first = context.new_page()
            first.goto("https://www.apple.com.cn/shop/signIn/account")
            self.assertEqual(inspect_apple_login(context).state, LoginState.SIGN_IN_REQUIRED)
            first.evaluate("setTimeout(() => window.open('https://www.apple.com.cn/shop/account/home'), 150)")
            completed = wait_for_apple_login(context, 3)
            self.assertIsNot(completed, first)
            self.assertEqual(completed.url, "https://www.apple.com.cn/shop/account/home")
        finally:
            context.close()

    def test_dry_run_reaches_submit_without_clicking_it(self) -> None:
        from playwright.sync_api import TimeoutError as BrowserTimeout

        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(__file__).parent.parent / "config.example.toml")
            config = replace(
                config,
                target=replace(config.target, product_url="https://shop.example.test/product"),
                stock=replace(config.stock, selector="#add", mode="enabled"),
                steps=(
                    StepConfig("加入购物袋", "click", "#add"),
                    StepConfig("等待提交按钮", "wait_visible", "#submit"),
                    StepConfig("最终提交", "click", "#submit", submit=True),
                    StepConfig("等待确认", "wait_visible", "#paid"),
                ),
            )
            context = self.browser.new_context()
            context.route("**/*", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body="""<script>window.orderSubmitted=false</script>
                    <button id="add" onclick="document.querySelector('#submit').hidden=false">加入购物袋</button>
                    <button id="submit" hidden onclick="window.orderSubmitted=true">提交订单</button>
                    <p id="paid" hidden>待支付</p>""",
            ))
            try:
                page = context.new_page()
                worker = PurchaseWorker(
                    config, AccountConfig("测试", Path(directory)), logging.getLogger("test"),
                    OrderLimiter(1), threading.Event(), False, Path(directory), threading.Event(), True,
                )
                result = worker._run_in_page(page, BrowserTimeout)
                self.assertEqual(result.outcome, Outcome.DRY_RUN_READY)
                self.assertFalse(page.evaluate("window.orderSubmitted"))
                self.assertIsNotNone(result.screenshot)
            finally:
                context.close()

    def test_store_login_handoff_541_stops_waiting_in_a_real_browser(self) -> None:
        context = self.browser.new_context()
        messages = []
        diagnostics = LoginNetworkDiagnostics(messages.append)
        diagnostics.attach(context)
        context.route("**/*", lambda route: route.fulfill(
            status=541 if "/shop/signIn/idms/authx" in route.request.url else 200,
            content_type="text/html; charset=utf-8",
            body=("local fixture error" if "/shop/signIn/idms/authx" in route.request.url else
                  "<p>处理中</p><script>fetch('/shop/signIn/idms/authx', {method:'POST', body:'local-fixture-only'})</script>"),
        ))
        try:
            page = context.new_page()
            page.goto("https://www.apple.com.cn/shop/signIn/account")
            with self.assertRaisesRegex(RuntimeError, "登录交接失败：HTTP 541"):
                wait_for_apple_login(context, 3, on_tick=diagnostics.check_status)
        finally:
            diagnostics.detach(context)
            context.close()

    def test_two_stage_workflow_uses_one_window_and_holds_success_or_failure(self) -> None:
        for product_status in (200, 541):
            with self.subTest(product_status=product_status), tempfile.TemporaryDirectory() as directory:
                account = AccountConfig("本地测试", Path(directory) / "profile")
                base = load_config(Path(__file__).parent.parent / "config.example.toml")
                config = replace(
                    base,
                    source_path=Path(directory) / "fixture.toml",
                    target=replace(base.target,
                        login_url="https://www.apple.com.cn/shop/account/home",
                        product_url="https://www.apple.com.cn/shop/product-fixture"),
                    stock=replace(base.stock, selector="#add", mode="enabled"),
                    steps=(
                        StepConfig("加入购物袋", "click", "#add"),
                        StepConfig("等待提交按钮", "wait_visible", "#submit"),
                        StepConfig("最终提交", "click", "#submit", submit=True),
                        StepConfig("等待确认", "wait_visible", "#paid"),
                    ),
                    accounts=(account,),
                )
                context = self.browser.new_context()
                visits = []

                def serve(route):
                    is_product = "/shop/product-fixture" in route.request.url
                    visits.append(route.request.url)
                    route.fulfill(
                        status=product_status if is_product else 200,
                        content_type="text/html; charset=utf-8",
                        body=("""<script>window.orderSubmitted=false</script>
                          <button id="add" onclick="document.querySelector('#submit').hidden=false">加入购物袋</button>
                          <button id="submit" hidden onclick="window.orderSubmitted=true">提交订单</button>
                          <p id="paid" hidden>待支付</p>""" if is_product else "<h1>你的账户</h1>"),
                    )

                context.route("**/*", serve)
                inputs = Mock()
                actions_seen = []
                pages_seen = []

                def next_action():
                    page = context.pages[0]
                    self.assertFalse(page.is_closed())
                    pages_seen.append(page)
                    index = len(actions_seen)
                    actions_seen.append(index)
                    if index < 2:
                        self.assertEqual(page.url, config.target.login_url)
                        self.assertFalse(any("/shop/product-fixture" in url for url in visits))
                        return None if index == 0 else TerminalAction.CONFIRM
                    self.assertEqual(page.url, config.target.product_url)
                    self.assertFalse(page.evaluate("window.orderSubmitted"))
                    return None if index == 2 else TerminalAction.QUIT

                inputs.next_action.side_effect = next_action
                launch = Mock(return_value=context)
                manager = MagicMock()
                manager.__enter__.return_value = SimpleNamespace(
                    chromium=SimpleNamespace(launch_persistent_context=launch),
                )
                try:
                    with patch("playwright.sync_api.sync_playwright", return_value=manager), \
                         patch("rush_phone.interactive_test.TerminalInput", return_value=inputs):
                        result = run_interactive_test(config, account, logging.getLogger("test"))
                    expected = Outcome.DRY_RUN_READY if product_status == 200 else Outcome.FAILED
                    self.assertEqual(result.outcome, expected)
                    self.assertEqual(len(actions_seen), 4)
                    self.assertTrue(all(page is pages_seen[0] for page in pages_seen))
                    launch.assert_called_once()
                    self.assertTrue((account.profile_dir / "auth-state.json").exists())
                    inputs.stop.assert_called_once()
                finally:
                    context.close()

    def test_auto_login_fills_only_local_fixture_credentials_and_clicks_once(self) -> None:
        context = self.browser.new_context()
        context.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body=("""<script>window.submissions=0</script>
              <input id="account_name_text_field">
              <input id="password_text_field" type="password">
              <button id="sign-in" onclick="window.submissions++">继续</button>"""
              if "idmsa.apple.com.cn" in route.request.url else
              '<iframe src="https://idmsa.apple.com.cn/appleauth/auth/authorize/signin"></iframe>'),
        ))
        try:
            page = context.new_page()
            page.goto("https://www.apple.com.cn/shop/signIn/account")
            credentials = LoginCredentials("fixture@example.test", "fixture-password-not-real")
            result = submit_credentials_once(context, credentials)
            self.assertEqual(result, AutoLoginResult.SUBMITTED)
            frame = page.frames[1]
            self.assertEqual(frame.locator("#account_name_text_field").input_value(), credentials.username)
            self.assertEqual(frame.locator("#password_text_field").input_value(), credentials.password)
            self.assertEqual(frame.evaluate("window.submissions"), 1)
            self.assertEqual(inspect_apple_login(context).state, LoginState.SIGN_IN_REQUIRED)
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
