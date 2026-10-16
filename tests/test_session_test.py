import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from rush_phone.chrome_session import ChromeSession, ChromeSessionError
from rush_phone.config import AccountConfig, load_config
from rush_phone.login_status import LoginInspection, LoginState
from rush_phone.session_test import FulfillmentWatch, SessionTestResult, check_product_only, run_session_test


class PrivateRequest:
    url = "https://www.apple.com.cn/shop/fulfillment-messages?token=private-token"
    resource_type = "fetch"
    frame = "main"
    failure = "net::ERR_TIMED_OUT at https://example.test/?password=private-token"

    @property
    def headers(self):
        raise AssertionError("不得读取请求头")

    @property
    def post_data(self):
        raise AssertionError("不得读取请求体")


class FulfillmentWatchTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock(main_frame="main")
        self.watch = FulfillmentWatch(self.page, "https://www.apple.com.cn/shop/buy-iphone/example")

    def test_541_stops_without_reading_or_logging_secrets(self):
        request = PrivateRequest()
        self.watch.started(request)
        self.watch.responded(Mock(request=request, status=541))
        with self.assertRaisesRegex(ChromeSessionError, "HTTP 541") as captured:
            self.watch.check()
        self.assertNotIn("private-token", str(captured.exception))

    def test_failed_request_keeps_only_network_code(self):
        request = PrivateRequest()
        self.watch.started(request)
        self.watch.failed(request)
        self.assertEqual(self.watch.pending, set())
        with self.assertRaisesRegex(ChromeSessionError, "net::ERR_TIMED_OUT") as captured:
            self.watch.check()
        self.assertNotIn("private-token", str(captured.exception))

    def test_no_response_does_not_become_success(self):
        with self.assertRaisesRegex(ChromeSessionError, "未确认"):
            self.watch.wait(1, lambda: None)

    def test_success_waits_until_response_body_finished_and_quiet(self):
        now = [0.0]
        with patch("rush_phone.session_test.time.monotonic", side_effect=lambda: now[0]):
            watch = FulfillmentWatch(self.page, "https://www.apple.com.cn/shop/buy-iphone/example")
            request = PrivateRequest()
            watch.started(request)
            watch.responded(Mock(request=request, status=200))

            def tick(milliseconds):
                now[0] += milliseconds / 1000
                if now[0] > 0.3 and watch.pending:
                    watch.finished(request)

            self.page.wait_for_timeout.side_effect = tick
            watch.wait(3000, lambda: None)
            self.assertGreaterEqual(now[0], 1)
            self.assertFalse(watch.pending)

    def test_unrelated_frame_or_origin_cannot_satisfy_check(self):
        for url, frame in (("https://evil.test/shop/fulfillment-messages", "main"), (PrivateRequest.url, "iframe")):
            request = SimpleNamespace(url=url, frame=frame, resource_type="fetch")
            self.watch.responded(Mock(request=request, status=200))
        self.assertIsNone(self.watch.last_status)

    def test_response_from_request_before_current_watch_cannot_satisfy_check(self):
        request = PrivateRequest()
        self.watch.responded(Mock(request=request, status=200))
        self.watch.failed(request)
        self.assertIsNone(self.watch.last_status)
        self.assertIsNone(self.watch.failure)


class SessionProductTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.account = AccountConfig("测试", Path(self.directory.name))
        config = load_config(Path(__file__).parent.parent / "config.example.toml")
        self.config = replace(config, target=replace(config.target, product_url="https://www.apple.com.cn/shop/buy-iphone/example"), stock=replace(config.stock, probe_url="", selector="#add", mode="enabled", prepare_click_selectors=("#color",)))
        self.logger = Mock(spec=logging.Logger)
        self.context = MagicMock()
        self.page = self.context.new_page.return_value
        self.page.locator.return_value.first.evaluate.return_value = True
        self.worker = MagicMock()
        self.worker._visible_challenge.return_value = None
        self.worker._document_response = None
        self.worker._critical_response = None
        self.worker._read_stock_state.return_value = True

    def inspection(self, state=LoginState.SIGNED_IN):
        return LoginInspection(state, self.page, "https://www.apple.com.cn/shop/account/home")

    def test_not_logged_in_never_opens_product_or_reads_cookies(self):
        with patch("rush_phone.session_test.inspect_apple_login", return_value=self.inspection(LoginState.SIGN_IN_REQUIRED)):
            result = check_product_only(self.context, self.config, self.account, self.logger)
        self.assertFalse(result.ok)
        self.context.new_page.assert_not_called()
        self.context.cookies.assert_not_called()
        self.context.storage_state.assert_not_called()

    def test_success_only_selects_radio_and_never_runs_order_flow(self):
        with patch("rush_phone.session_test.inspect_apple_login", return_value=self.inspection()), \
             patch("rush_phone.session_test.PurchaseWorker", return_value=self.worker) as worker_type, \
             patch("rush_phone.session_test.FulfillmentWatch") as watch_type:
            watch_type.return_value.wait.side_effect = lambda timeout, guard: guard()
            result = check_product_only(self.context, self.config, self.account, self.logger)
        self.assertTrue(result.ok)
        self.assertIs(worker_type.call_args.args[5], False)
        self.worker._execute_flow.assert_not_called()
        self.worker._run_in_page.assert_not_called()
        self.context.close.assert_not_called()
        self.context.new_page.assert_not_called()
        self.context.cookies.assert_not_called()
        self.context.storage_state.assert_not_called()
        self.page.close.assert_not_called()
        self.page.bring_to_front.assert_called()

    def test_original_tab_is_foregrounded_before_navigation_and_radio_actions(self):
        events = []
        self.page.bring_to_front.side_effect = lambda: events.append("front")
        self.page.goto.side_effect = lambda *a, **kw: events.append("navigate")
        with patch("rush_phone.session_test.inspect_apple_login", return_value=self.inspection()), \
             patch("rush_phone.session_test.PurchaseWorker", return_value=self.worker) as worker_type, \
             patch("rush_phone.session_test.FulfillmentWatch") as watch_type:
            worker_type._click_control.side_effect = lambda *a, **kw: events.append("radio")
            watch_type.return_value.wait.side_effect = lambda timeout, guard: guard()
            result = check_product_only(self.context, self.config, self.account, self.logger)
        self.assertTrue(result.ok)
        self.assertEqual(events, ["front", "navigate", "front", "radio"])

    def test_button_in_preselection_is_rejected_before_click(self):
        self.page.locator.return_value.first.evaluate.return_value = False
        with patch("rush_phone.session_test.inspect_apple_login", return_value=self.inspection()), \
             patch("rush_phone.session_test.PurchaseWorker", return_value=self.worker) as worker_type:
            with self.assertRaisesRegex(ChromeSessionError, "拒绝点击"):
                check_product_only(self.context, self.config, self.account, self.logger)
        worker_type._click_control.assert_not_called()

    def test_failed_delivery_never_reads_button_as_success(self):
        with patch("rush_phone.session_test.inspect_apple_login", return_value=self.inspection()), \
             patch("rush_phone.session_test.PurchaseWorker", return_value=self.worker), \
             patch("rush_phone.session_test.FulfillmentWatch") as watch_type:
            watch_type.return_value.wait.side_effect = ChromeSessionError("HTTP 541")
            with self.assertRaisesRegex(ChromeSessionError, "HTTP 541"):
                check_product_only(self.context, self.config, self.account, self.logger)
        self.worker._read_stock_state.assert_not_called()

    def test_connection_reuses_default_context_without_closing_browser(self):
        session = ChromeSession(12345, 9222, self.account.profile_dir, "/devtools/browser/local-test-1234")
        manager = MagicMock()
        playwright = manager.__enter__.return_value
        browser = playwright.chromium.connect_over_cdp.return_value
        browser.contexts = [self.context]
        with patch.dict("os.environ", {}, clear=True), \
             patch("rush_phone.session_test.require_chrome_session", return_value=session), \
             patch("playwright.sync_api.sync_playwright", return_value=manager), \
             patch("rush_phone.session_test.check_product_only", return_value=SessionTestResult(True, "测试完成")) as check:
            result = run_session_test(self.config, self.account, self.logger, product_only=True)
        self.assertTrue(result.ok)
        check.assert_called_once_with(self.context, self.config, self.account, self.logger)
        browser.new_context.assert_not_called()
        browser.close.assert_not_called()
        self.context.close.assert_not_called()
        self.assertTrue(playwright.chromium.connect_over_cdp.call_args.kwargs["no_defaults"])

    def test_browser_exception_does_not_echo_sensitive_details(self):
        session = ChromeSession(12345, 9222, self.account.profile_dir, "/devtools/browser/local-test-1234")
        manager = MagicMock()
        manager.__enter__.return_value.chromium.connect_over_cdp.side_effect = ValueError("secret-cookie-value")
        with patch.dict("os.environ", {}, clear=True), \
             patch("rush_phone.session_test.require_chrome_session", return_value=session), \
             patch("playwright.sync_api.sync_playwright", return_value=manager):
            with self.assertRaises(ChromeSessionError) as captured:
                run_session_test(self.config, self.account, self.logger)
        self.assertNotIn("secret-cookie-value", str(captured.exception))

    def test_debug_logging_is_rejected_before_session_is_contacted(self):
        with patch.dict("os.environ", {"DEBUG": "pw:api"}), \
             patch("rush_phone.session_test.require_chrome_session") as require:
            with self.assertRaisesRegex(ChromeSessionError, "DEBUG"):
                run_session_test(self.config, self.account, self.logger)
        require.assert_not_called()


if __name__ == "__main__":
    unittest.main()
