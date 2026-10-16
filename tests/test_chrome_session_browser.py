"""真实 Chrome/CDP 集成；仅访问拦截的本地 fixture，不登录 Apple。"""

import logging
import os
import signal
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rush_phone.chrome_session import ChromeSessionError, open_chrome_session, require_chrome_session
from rush_phone.config import AccountConfig, StepConfig, load_config
from rush_phone.session_test import check_product_only
from rush_phone.session_checkout import check_until_submit


@unittest.skipUnless(os.environ.get("RUSH_PHONE_BROWSER_TESTS") == "1", "需显式启用浏览器测试")
class ChromeSessionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="rush-phone-cdp-fixture-")
        cls.account = AccountConfig("本地 CDP 测试", Path(cls.directory.name) / "account")
        base = load_config(Path(__file__).parent.parent / "config.example.toml")
        cls.config = replace(
            base,
            run=replace(base.run, browser_channel="chrome", page_timeout_ms=3000),
            target=replace(base.target, login_url="about:blank", product_url="https://www.apple.com.cn/shop/buy-iphone/local-fixture"),
            stock=replace(base.stock, probe_url="", selector="#add", mode="enabled", prepare_click_selectors=("#color",)),
            challenge=replace(base.challenge, selectors=()),
        )
        # 仅绕过启动器对 fixture about:blank 的 URL 检查，不改变真实站点行为。
        with patch("rush_phone.chrome_session.is_apple_store_url", return_value=True):
            cls.session, _ = open_chrome_session(cls.config, cls.account)

    @classmethod
    def tearDownClass(cls):
        # 只结束本组测试新建且仍通过身份核对的 Chrome，不影响用户窗口。
        try:
            require_chrome_session(cls.account)
            os.kill(cls.session.pid, signal.SIGTERM)
        finally:
            if cls.session._process is not None:
                cls.session._process.wait(timeout=5)
            cls.directory.cleanup()

    def fixture(self, context, delivery_status=200):
        def respond(route):
            url = route.request.url
            if "/shop/fulfillment-messages" in url:
                route.fulfill(status=delivery_status, content_type="application/json", body="{}")
            elif "/shop/account/home" in url:
                route.fulfill(content_type="text/html; charset=utf-8", body="""
                    <h1>你的账户</h1>
                    <script>
                      localStorage.setItem('local-test', 'local-fixture-only');
                      sessionStorage.setItem('tab-test', 'local-fixture-only');
                      document.cookie='test_session=local-fixture-only; path=/; Secure';
                    </script>""")
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body="""
                    <script>window.orderClicks=0; fetch('/shop/fulfillment-messages')</script>
                    <input id="color" type="radio"><label for="color">本地颜色</label>
                    <button id="add" onclick="window.orderClicks++">加入购物袋</button>""")
        context.route("**/*", respond)

    def test_01_disconnect_keeps_same_browser_tab_and_storage_alive(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            self.fixture(context)
            page = context.pages[0]
            page.goto("https://www.apple.com.cn/shop/account/home")
            self.assertEqual(page.evaluate("sessionStorage.getItem('tab-test')"), "local-fixture-only")
        self.assertEqual(require_chrome_session(self.account), self.session)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            page = browser.contexts[0].pages[0]
            self.assertEqual(page.url, "https://www.apple.com.cn/shop/account/home")
            self.assertEqual(page.evaluate("localStorage.getItem('local-test')"), "local-fixture-only")
            self.assertEqual(page.evaluate("sessionStorage.getItem('tab-test')"), "local-fixture-only")

    def test_02_product_probe_never_clicks_add_or_creates_new_context(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            self.fixture(context)
            page = context.pages[0]
            page.goto("https://www.apple.com.cn/shop/account/home")
            page_count = len(context.pages)
            result = check_product_only(context, self.config, self.account, logging.getLogger("cdp-fixture"))
            self.assertTrue(result.ok)
            self.assertTrue(result.page_ready)
            self.assertEqual(len(context.pages), page_count)
            self.assertEqual(page.evaluate("sessionStorage.getItem('tab-test')"), "local-fixture-only")
            self.assertEqual(page.evaluate("window.orderClicks"), 0)
        self.assertEqual(require_chrome_session(self.account), self.session)

    def test_03_delivery_541_stops_without_add_or_order(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            self.fixture(context, delivery_status=541)
            page = context.pages[0]
            page.goto("https://www.apple.com.cn/shop/account/home")
            with self.assertRaisesRegex(ChromeSessionError, "HTTP 541"):
                check_product_only(context, self.config, self.account, logging.getLogger("cdp-fixture"))
            self.assertEqual(page.evaluate("window.orderClicks"), 0)
        self.assertEqual(require_chrome_session(self.account), self.session)

    def checkout_fixture(self, context, *, items=0, quantity="1", name=None, checkout_status=200, disabled_final=False, manual_login=False):
        from urllib.parse import urlsplit
        state = {"items": items, "adds": 0, "checkouts": 0, "submits": 0}
        config = replace(
            self.config,
            source_path=Path(self.directory.name) / "fixture.toml",
            run=replace(self.config.run, challenge_timeout_seconds=3),
            target=replace(self.config.target, login_url="https://www.apple.com.cn/shop/account/home"),
            variables={"model": "iPhone 本地测试", "color": "测试色", "capacity": "512GB"},
            steps=(
                StepConfig("选择颜色", "click", "#color"),
                StepConfig("加入购物袋", "click", "#add"),
                StepConfig("等待结账", "wait_visible", "#checkout"),
                StepConfig("结账", "click", "#checkout"),
                StepConfig("等待最终按钮", "wait_visible", "#submit"),
                StepConfig("提交订单", "click", "#submit", submit=True),
                StepConfig("等待确认", "wait_visible", "#paid"),
            ),
        )

        def respond(route):
            path = urlsplit(route.request.url).path
            status = 200
            if path == "/shop/fulfillment-messages":
                route.fulfill(content_type="application/json", body="{}")
                return
            if path == "/shop/account/home":
                body = "<h1>你的账户</h1>"
            elif path == "/shop/bag/local-add":
                state["adds"] += 1
                state["items"] += 1
                route.fulfill(status=302, headers={"Location": "/shop/bag"})
                return
            elif path == "/shop/bag":
                if not state["items"]:
                    body = "<h1>你的购物袋中没有商品。</h1>"
                else:
                    title = name or "iPhone 本地测试 512GB 测试色"
                    body = '<ol data-autom="bag-items">' + "".join(
                        f'<li data-autom="bag-item-{i}"><a data-autom="bag-item-name">{title}</a>'
                        f'<select data-autom="item-quantity-dropdown"><option value="{quantity}">{quantity}</option></select></li>'
                        for i in range(state["items"])) + "</ol>"
                    body += '<button id="checkout" data-autom="checkout" onclick="location.href=\'/shop/checkout/start\'">结账</button>'
            elif path.startswith("/shop/checkout/"):
                if path == "/shop/checkout/place-order":
                    state["submits"] += 1
                if path == "/shop/checkout/start":
                    state["checkouts"] += 1
                    status = checkout_status
                if manual_login and path == "/shop/checkout/start" and state["checkouts"] == 1:
                    destination = "/shop/bag" if manual_login == "bag" else "/shop/checkout/review"
                    body = f'<input id="password_text_field" type="password"><script>setTimeout(()=>location.href="{destination}",300)</script>'
                else:
                    body = ('<h1>核对订单</h1><button id="submit" ' + ("disabled " if disabled_final else "")
                            + 'onclick="fetch(\'/shop/checkout/place-order\',{method:\'POST\'})">提交订单</button><p id="paid" hidden>待支付</p>')
            else:
                body = '''<h1>购买 iPhone 本地测试</h1>
                    <script>fetch('/shop/fulfillment-messages')</script>
                    <input id="color" type="radio"><label for="color">测试色</label>
                    <button id="add" onclick="location.href='/shop/bag/local-add'">加入购物袋</button>'''
            route.fulfill(status=status, content_type="text/html; charset=utf-8", body=body)

        context.unroute_all()
        context.route("**/*", respond)
        context.pages[0].goto("https://www.apple.com.cn/shop/account/home")
        return config, state

    def test_04_checkout_reaches_final_and_second_run_does_not_add_again(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            page = context.pages[0]
            config, state = self.checkout_fixture(context)
            for _ in range(2):
                result = check_until_submit(context, config, self.account, logging.getLogger("cdp-fixture"))
                self.assertTrue(result.ok)
                self.assertIsNotNone(result.screenshot)
                self.assertTrue(result.screenshot.exists())
                self.assertIs(context.pages[0], page)
                self.assertEqual(len(context.pages), 1)
                self.assertTrue(page.locator("#submit").is_visible())
                self.assertEqual(state["adds"], 1)
                self.assertEqual(state["submits"], 0)
            self.assertEqual(state["checkouts"], 2)
        self.assertEqual(require_chrome_session(self.account), self.session)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            self.assertTrue(browser.contexts[0].pages[0].url.endswith("/shop/checkout/start"))

    def test_05_mismatched_or_multiple_items_stop_before_checkout(self):
        from playwright.sync_api import sync_playwright
        cases = ({"items": 1, "quantity": "2"}, {"items": 2}, {"items": 1, "name": "其他型号 512GB 测试色"})
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            for options in cases:
                with self.subTest(options=options):
                    config, state = self.checkout_fixture(context, **options)
                    with self.assertRaises(ChromeSessionError):
                        check_until_submit(context, config, self.account, logging.getLogger("cdp-fixture"))
                    self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (0, 0, 0))

    def test_06_checkout_541_stops_without_retry_or_submit(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            config, state = self.checkout_fixture(context, checkout_status=541)
            with self.assertRaisesRegex(ChromeSessionError, "HTTP 541"):
                check_until_submit(context, config, self.account, logging.getLogger("cdp-fixture"))
            self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (1, 1, 0))

    def test_07_disabled_final_is_not_claimed_as_ready(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            config, state = self.checkout_fixture(context, items=1, disabled_final=True)
            with self.assertRaisesRegex(ChromeSessionError, "尚未就绪"):
                check_until_submit(context, config, self.account, logging.getLogger("cdp-fixture"))
            self.assertEqual(state["adds"], 0)
            self.assertEqual(state["submits"], 0)

    def test_08_checkout_manual_authentication_uses_same_tab_and_continues(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self.session.endpoint, no_defaults=True, timeout=5000)
            context = browser.contexts[0]
            for mode in (True, "bag"):
                with self.subTest(mode=mode):
                    config, state = self.checkout_fixture(context, items=1, manual_login=mode)
                    page = context.pages[0]
                    result = check_until_submit(context, config, self.account, logging.getLogger("cdp-fixture"))
                    self.assertTrue(result.ok)
                    self.assertIs(context.pages[0], page)
                    expected_path = "/shop/checkout/start" if mode == "bag" else "/shop/checkout/review"
                    self.assertTrue(page.url.endswith(expected_path))
                    self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (0, 2 if mode == "bag" else 1, 0))


if __name__ == "__main__":
    unittest.main()
