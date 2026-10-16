import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from rush_phone.chrome_session import ChromeSession, ChromeSessionError
from rush_phone.cli import main
from rush_phone.config import AccountConfig, StepConfig, load_config
from rush_phone.session_checkout import CheckoutDryRun, SessionCheckout
from rush_phone.session_control import control_session
from rush_phone.session_test import SessionTestResult, prepare_product_page, run_session_purchase


class PurchaseCliTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(__file__).parent.parent / "config.apple-cn.example.toml")

    def test_apple_run_reuses_session_and_never_launches_worker_or_login(self):
        with patch("rush_phone.cli.run_session_purchase", return_value=SessionTestResult(True, "fixture")) as run, \
             patch("rush_phone.cli.run_accounts") as old_run, patch("rush_phone.cli.open_chrome_session") as launch, \
             patch("rush_phone.cli.setup_logging"):
            self.assertEqual(main(["run", "--config", self.path, "--submit-order"]), 0)
        self.assertEqual(run.call_args.kwargs, {"submit_order": True, "check_once": False})
        old_run.assert_not_called()
        launch.assert_not_called()

    def test_once_stays_single_and_ambiguous_submission_has_nonzero_status(self):
        for flags, result, code in ((["--once"], SessionTestResult(False, "unavailable"), 1),
                                     (["--submit-order"], SessionTestResult(False, "unknown", order_submitted=None), 3)):
            with self.subTest(flags=flags), patch("rush_phone.cli.setup_logging"), \
                 patch("rush_phone.cli.run_session_purchase", return_value=result) as run:
                self.assertEqual(main(["run", "--config", self.path, *flags]), code)
                self.assertEqual(run.call_args.kwargs["check_once"], "--once" in flags)

    def test_missing_session_stops_without_starting_another_browser(self):
        with patch("rush_phone.cli.setup_logging"), \
             patch("rush_phone.cli.run_session_purchase", side_effect=ChromeSessionError("先运行 chrome-login")), \
             patch("rush_phone.cli.run_accounts") as old_run, patch("rush_phone.cli.open_chrome_session") as launch:
            self.assertEqual(main(["run", "--config", self.path]), 1)
        old_run.assert_not_called()
        launch.assert_not_called()


class SessionPurchaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.account = AccountConfig("fixture", Path(directory.name))
        base = load_config(Path(__file__).parent.parent / "config.apple-cn.example.toml")
        self.config = replace(base, source_path=Path(directory.name) / "fixture.toml",
                              run=replace(base.run, poll_jitter_ms=0),
                              variables={"model": "iPhone Duo", "color": "夜空色", "capacity": "512GB"})
        self.log = Mock(spec=logging.Logger)
        self.context = MagicMock()
        self.page = Mock(url=self.config.target.product_url, main_frame="main")
        self.page.locator.return_value.first.evaluate.return_value = True
        self.page.locator.return_value.first.is_enabled.return_value = True

    def test_connection_uses_original_context_without_cookie_restore_or_close(self):
        session = ChromeSession(12345, 9222, self.account.profile_dir, "/devtools/browser/local-test-1234")
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.connect_over_cdp.return_value
        browser.contexts = [self.context]
        with patch.dict("os.environ", {}, clear=True), \
             patch("rush_phone.session_test.require_chrome_session", return_value=session), \
             patch("playwright.sync_api.sync_playwright", return_value=manager), \
             patch("rush_phone.session_checkout.SessionCheckout") as checkout:
            checkout.return_value.run.return_value = SessionTestResult(True, "fixture")
            self.assertTrue(run_session_purchase(self.config, self.account, self.log, submit_order=True).ok)
        checkout.assert_called_once_with(self.context, self.config, self.account, self.log, watch=True, submit_order=True)
        browser.new_context.assert_not_called()
        browser.close.assert_not_called()
        self.context.new_page.assert_not_called()
        self.context.close.assert_not_called()
        self.context.cookies.assert_not_called()
        self.context.storage_state.assert_not_called()

    def test_same_account_cannot_be_controlled_twice_and_interrupt_releases_lock(self):
        with self.assertRaises(KeyboardInterrupt):
            with control_session(self.account):
                with self.assertRaises(ChromeSessionError):
                    with control_session(self.account):
                        self.fail("must not control the same browser twice")
                raise KeyboardInterrupt
        with control_session(self.account):
            pass

    def monitor(self, states, refresh=5000, pending=False):
        now = [0.0]
        self.page.wait_for_timeout.side_effect = lambda ms: now.__setitem__(0, now[0] + ms / 1000)
        worker = MagicMock()
        worker._visible_challenge.return_value = None
        worker._read_stock_state.side_effect = states
        config = replace(self.config, run=replace(self.config.run, stock_refresh_interval_ms=refresh))
        with patch("rush_phone.session_test.time.monotonic", side_effect=lambda: now[0]), \
             patch("rush_phone.session_test.PurchaseWorker", return_value=worker) as worker_type, \
             patch("rush_phone.session_test.FulfillmentWatch") as watch_type:
            watch_type.return_value.pending = {1} if pending else set()

            def finish_delivery(timeout, guard):
                watch_type.return_value.pending.clear()
                guard()

            watch_type.return_value.wait.side_effect = finish_delivery
            result = prepare_product_page(self.context, self.page, config, self.account, self.log, wait_available=True)
        return result, worker, worker_type, watch_type

    def test_disabled_button_is_observed_until_it_enables_without_reloading_each_poll(self):
        result, worker, _, _ = self.monitor([False, False, True, True])
        self.assertTrue(result.page_ready)
        self.page.goto.assert_called_once()
        worker._execute_flow.assert_not_called()
        self.context.new_page.assert_not_called()

    def test_static_button_refreshes_same_page_and_reapplies_specifications(self):
        result, _, worker_type, watches = self.monitor([False, False, True, True], refresh=1000)
        self.assertTrue(result.page_ready)
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(worker_type._click_control.call_count, 2 * len(self.config.stock.prepare_click_selectors))
        self.assertEqual(watches.call_count, 2)
        self.assertEqual(watches.return_value.detach.call_count, 2)

    def test_refresh_waits_for_pending_delivery_instead_of_cancelling_it(self):
        result, _, _, watches = self.monitor([False, False, False, True, True], refresh=1000, pending=True)
        self.assertTrue(result.page_ready)
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(watches.return_value.wait.call_count, 2)

    def test_delivery_failure_cannot_become_purchase_even_when_button_enabled(self):
        worker = MagicMock()
        worker._visible_challenge.return_value = None
        worker._read_stock_state.return_value = True
        with patch("rush_phone.session_test.PurchaseWorker", return_value=worker), \
             patch("rush_phone.session_test.FulfillmentWatch") as watch:
            watch.return_value.wait.side_effect = ChromeSessionError("HTTP 541")
            with self.assertRaisesRegex(ChromeSessionError, "541"):
                prepare_product_page(self.context, self.page, self.config, self.account, self.log, wait_available=True)
        worker._execute_flow.assert_not_called()


class FinalSubmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = load_config(Path(__file__).parent.parent / "config.apple-cn.example.toml")
        self.config = replace(base, source_path=Path(directory.name) / "fixture.toml")
        self.driver = SessionCheckout(Mock(), self.config, AccountConfig("fixture", Path(directory.name)), Mock(), submit_order=True)
        self.page = Mock(url="https://www.apple.com.cn/shop/checkout/review")
        self.driver.page = self.page
        self.driver.guard = Mock()
        self.page.locator.return_value.count.return_value = 1
        self.page.locator.return_value.first.is_visible.return_value = False
        self.driver.worker._screenshot = Mock(return_value=None)
        self.driver.step = Mock()
        self.final = Mock()

    def test_confirmation_required_after_single_final_click(self):
        result = self.driver.place_order(self.final)
        self.assertTrue(result.ok)
        self.assertIs(result.order_submitted, True)
        self.final.click.assert_called_once()
        self.driver.step.assert_called_once_with(self.config.steps[-1])

    def test_click_timeout_or_interrupt_is_unknown_and_never_retried(self):
        for error in (TimeoutError("private-url"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.driver.submit_attempted = False
                self.final.click.reset_mock()
                self.final.click.side_effect = error
                result = self.driver.place_order(self.final)
                self.assertFalse(result.ok)
                self.assertIsNone(result.order_submitted)
                self.final.click.assert_called_once()
                self.assertNotIn("private-url", result.message)
                with self.assertRaises(ChromeSessionError):
                    self.driver.place_order(self.final)
                self.assertEqual(self.final.click.call_count, 1)

    def test_confirmation_already_visible_stops_before_final_click(self):
        self.page.locator.return_value.first.is_visible.return_value = True
        with self.assertRaisesRegex(ChromeSessionError, "提交前已经满足"):
            self.driver.place_order(self.final)
        self.final.click.assert_not_called()

    def test_single_test_cannot_enable_final_submission(self):
        with self.assertRaises(TypeError):
            CheckoutDryRun(Mock(), self.config, Mock(), Mock(), submit_order=True)

    def test_actions_after_submission_are_rejected_before_connecting_or_clicking(self):
        config = replace(self.config, steps=self.config.steps[:-1] + (StepConfig("unexpected", "click", "#again"),))
        with self.assertRaisesRegex(ChromeSessionError, "等待步骤"):
            SessionCheckout(Mock(), config, Mock(), Mock(), submit_order=True)


if __name__ == "__main__":
    unittest.main()
