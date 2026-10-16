"""真实 Chrome/CDP、强制离线的本地模拟商店；不访问 Apple 或使用真实账号。"""

import logging
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from rush_phone.chrome_session import ChromeSessionError
from rush_phone.config import AccountConfig, StepConfig, load_config
from rush_phone.session_checkout import SessionCheckout


@unittest.skipUnless(os.environ.get("RUSH_PHONE_BROWSER_TESTS") == "1", "需显式启用浏览器测试")
class SessionPurchaseBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.directory = tempfile.TemporaryDirectory(prefix="rush-phone-purchase-fixture-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.account = AccountConfig("本地模拟账号", Path(cls.directory.name) / "profile")
        base = load_config(Path(__file__).parent.parent / "config.apple-cn.example.toml")
        cls.config = replace(base, target=replace(base.target, login_url="about:blank"))
        # 无窗口测试浏览器避免 macOS 遮挡引起 rAF 抖动；生产 chrome-login 完全不变。
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.owner = cls.playwright.chromium.launch_persistent_context(
            str(cls.account.profile_dir), channel="chrome", headless=True, args=["--remote-debugging-port=0"],
        )
        cls.addClassCleanup(cls.owner.close)
        cls.owner.set_offline(True)
        port, ws_path = (cls.account.profile_dir / "DevToolsActivePort").read_text().splitlines()[:2]
        cls.browser = cls.playwright.chromium.connect_over_cdp(f"ws://127.0.0.1:{port}{ws_path}", no_defaults=True)

    def fixture(self, context, *, static=False, delivery_status=200, order_status=200, items=0, wrong_cart=False, verification=False):
        state = {"products": 0, "adds": 0, "checkouts": 0, "submits": 0, "items": items}
        config = replace(
            self.config, source_path=Path(self.directory.name) / "fixture.toml",
            run=replace(self.config.run, page_timeout_ms=3000, poll_interval_ms=250, poll_jitter_ms=0,
                        stock_refresh_interval_ms=1000 if static else 5000, challenge_timeout_seconds=3),
            target=replace(self.config.target, product_url="https://www.apple.com.cn/shop/buy-iphone/local-fixture",
                           login_url="https://www.apple.com.cn/shop/account/home"),
            stock=replace(self.config.stock, selector="#continue", mode="enabled", prepare_click_selectors=("#color",)),
            variables={"model": "iPhone 本地测试", "color": "测试色", "capacity": "512GB"},
            steps=(
                StepConfig("继续", "click", "#continue"),
                StepConfig("等待加入购物袋", "wait_visible", "#add"),
                StepConfig("加入购物袋", "click", "#add"),
                StepConfig("等待结账", "wait_visible", "#checkout"),
                StepConfig("结账", "click", "#checkout"),
                StepConfig("等待提交", "wait_visible", "#submit"),
                StepConfig("提交订单", "click", "#submit", submit=True),
                StepConfig("等待待支付", "wait_visible", "#paid", timeout_ms=1000),
            ),
        )

        def respond(route):
            path = urlsplit(route.request.url).path
            if path == "/shop/account/home":
                body = '<h1>你的账户</h1><script>sessionStorage.setItem("session-marker", "fixture-session")</script>'
            elif path == "/shop/fulfillment-messages":
                route.fulfill(status=delivery_status, content_type="application/json", body="{}")
                return
            elif path == "/shop/buy-iphone/local-fixture":
                state["products"] += 1
                available = static and state["products"] >= 2
                toggle = "" if static else 'setTimeout(()=>document.querySelector("#continue").disabled=false,350);'
                auth = ('<input autocomplete="one-time-code"><script>setTimeout(()=>document.querySelector("input[autocomplete]").remove(),300)</script>'
                        if verification else "")
                body = ('<h1>购买 iPhone 本地测试</h1><input id="color" type="radio"><label for="color">测试色</label>'
                        '<button id="continue" ' + ("" if available else "disabled ")
                        + 'onclick="location.href=\'/shop/configuration\'">继续</button>'
                        + '<script>fetch("/shop/fulfillment-messages");' + toggle + '</script>' + auth)
            elif path == "/shop/configuration":
                body = '<button id="add" onclick="location.href=\'/shop/bag/local-add\'">加入购物袋</button>'
            elif path == "/shop/bag/local-add":
                state["adds"] += 1
                state["items"] += 1
                body = '<script>location.replace("/shop/bag")</script>'
            elif path == "/shop/bag":
                if not state["items"]:
                    body = '<h1>你的购物袋中没有商品。</h1>'
                else:
                    name = "其他型号 512GB 测试色" if wrong_cart else "iPhone 本地测试 512GB 测试色"
                    body = ('<ol data-autom="bag-items"><li data-autom="bag-item-1">'
                            f'<span data-autom="bag-item-name">{name}</span>'
                            '<select data-autom="item-quantity-dropdown"><option value="1">1</option></select></li></ol>'
                            '<button id="checkout" onclick="location.href=\'/shop/checkout/review\'">结账</button>')
            elif path == "/shop/checkout/review":
                state["checkouts"] += 1
                body = ('<h1>核对订单</h1><button id="submit" onclick="fetch(\'/shop/checkout/place-order\',{method:\'POST\'})'
                        '.then(r=>{if(r.ok)document.querySelector(\'#paid\').hidden=false})">提交订单</button>'
                        '<h2 id="paid" hidden>待支付</h2>')
            elif path == "/shop/checkout/place-order":
                state["submits"] += 1
                route.fulfill(status=order_status, content_type="application/json", body="{}")
                return
            else:
                route.abort()
                return
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        context.unroute_all()
        context.set_offline(True)  # 即使有漏掉的请求也不允许离开本地模拟环境。
        context.set_default_timeout(3000)
        context.route("**/*", respond)
        context.pages[0].goto(config.target.login_url)
        return config, state

    def exercise(self, *, submit=False, raises=False, **options):
        context = self.browser.contexts[0]
        page = context.pages[0]
        config, state = self.fixture(context, **options)
        driver = SessionCheckout(context, config, self.account, logging.getLogger("offline-purchase-fixture"), watch=True, submit_order=submit)
        if raises:
            with self.assertRaises(ChromeSessionError):
                driver.run()
            result = None
        else:
            result = driver.run()
        self.assertEqual(len(self.browser.contexts), 1)
        self.assertEqual(len(context.pages), 1)
        self.assertIs(context.pages[0], page)
        self.assertFalse(page.is_closed())
        self.assertEqual(page.evaluate('sessionStorage.getItem("session-marker")'), "fixture-session")
        return result, state

    def test_dynamic_button_is_observed_without_waiting_for_refresh(self):
        result, state = self.exercise()
        self.assertTrue(result.ok)
        self.assertIs(result.order_submitted, False)
        self.assertEqual((state["products"], state["adds"], state["checkouts"], state["submits"]), (2, 1, 1, 0))

    def test_static_page_refreshes_same_tab_then_proceeds_once(self):
        result, state = self.exercise(static=True)
        self.assertTrue(result.ok)
        self.assertEqual((state["products"], state["adds"], state["checkouts"], state["submits"]), (3, 1, 1, 0))

    def test_explicit_submit_clicks_once_and_waits_for_confirmation(self):
        result, state = self.exercise(submit=True)
        self.assertTrue(result.ok)
        self.assertIs(result.order_submitted, True)
        self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (1, 1, 1))

    def test_ambiguous_submit_stops_without_retrying(self):
        result, state = self.exercise(submit=True, order_status=541)
        self.assertFalse(result.ok)
        self.assertIsNone(result.order_submitted)
        self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (1, 1, 1))

    def test_existing_matching_bag_skips_add(self):
        result, state = self.exercise(items=1)
        self.assertTrue(result.ok)
        self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (0, 1, 0))

    def test_wrong_bag_and_delivery_failure_stop_before_checkout(self):
        for options in ({"items": 1, "wrong_cart": True}, {"delivery_status": 541}):
            with self.subTest(options=options):
                _, state = self.exercise(raises=True, **options)
                self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (0, 0, 0))

    def test_manual_verification_preserves_original_tab_and_continues(self):
        result, state = self.exercise(verification=True)
        self.assertTrue(result.ok)
        self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (1, 1, 0))

    def test_stop_while_monitoring_leaves_original_tab_without_mutating_cart(self):
        context = self.browser.contexts[0]
        page = context.pages[0]
        config, state = self.fixture(context, static=True)
        driver = SessionCheckout(context, config, self.account, logging.getLogger("offline-purchase-fixture"),
                                 watch=True, submit_order=True, stop_requested=lambda: state["products"] >= 1)
        with self.assertRaises(KeyboardInterrupt):
            driver.run()
        self.assertEqual((state["adds"], state["checkouts"], state["submits"]), (0, 0, 0))
        self.assertIs(context.pages[0], page)
        self.assertFalse(page.is_closed())


if __name__ == "__main__":
    unittest.main()
