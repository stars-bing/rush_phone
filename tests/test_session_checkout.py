import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rush_phone.config import AccountConfig, load_config
from rush_phone.login_status import LoginInspection, LoginState
from rush_phone.session_checkout import CheckoutDryRun


class CheckoutGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = load_config(Path(__file__).parent.parent / "config.apple-cn.example.toml")
        self.context = Mock()
        self.page = Mock(main_frame="main")
        self.driver = CheckoutDryRun(self.context, config, AccountConfig("测试", Path(directory.name)), Mock(spec=logging.Logger))
        self.driver.page = self.page

    def test_failed_checkout_request_does_not_echo_private_url_or_body(self):
        class PrivateRequest:
            url = "https://secure.www.apple.com.cn/shop/checkoutx/shipping?token=private-value"
            resource_type = "xhr"
            frame = "main"
            failure = "net::ERR_TIMED_OUT at ?password=private-value"

            @property
            def post_data(self):
                raise AssertionError("不得读取请求体")

            @property
            def headers(self):
                raise AssertionError("不得读取认证头")

        self.driver.record_failed(PrivateRequest())
        self.assertIn("net::ERR_TIMED_OUT", self.driver.failure)
        self.assertNotIn("private-value", self.driver.failure)

    def test_third_party_and_normal_document_abort_do_not_stop_checkout(self):
        for url, resource in (("https://example.test/shop/checkoutx/shipping", "fetch"),
                              ("https://www.apple.com.cn/shop/checkout", "document")):
            request = SimpleNamespace(url=url, resource_type=resource, frame="main", failure="net::ERR_ABORTED")
            self.driver.record_failed(request)
        self.assertIsNone(self.driver.failure)

    def test_listener_cleanup_does_not_mask_ctrl_c_or_close_browser(self):
        self.context.remove_listener.side_effect = RuntimeError("event loop stopped")
        self.driver.guard = Mock()
        inspection = LoginInspection(LoginState.SIGNED_IN, self.page, "https://www.apple.com.cn/shop/account/home")
        with patch("rush_phone.session_checkout.inspect_apple_login", return_value=inspection), \
             patch("rush_phone.session_checkout.prepare_product_page", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.driver.run()
        self.context.close.assert_not_called()
        self.page.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
