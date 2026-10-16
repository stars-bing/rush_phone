import unittest
from unittest.mock import Mock

from rush_phone.login_diagnostics import LoginNetworkDiagnostics
from rush_phone.login_status import safe_page_url


class MetadataOnlyRequest:
    url = "https://idmsa.apple.com.cn/appleauth/auth/signin?password=private&code=123456#secret"
    method = "POST"
    resource_type = "xhr"
    failure = "net::ERR_TIMED_OUT at https://example.test/?password=private"

    @property
    def post_data(self):
        raise AssertionError("不得读取请求体")

    @property
    def headers(self):
        raise AssertionError("不得读取认证头或 Cookie")


class LoginDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.messages = []
        self.diagnostics = LoginNetworkDiagnostics(self.messages.append, lambda: self.now)

    def assert_private_data_absent(self) -> None:
        output = "\n".join(self.messages)
        for value in ("private", "123456", "secret", "password=", "code="):
            self.assertNotIn(value, output)

    def test_http_failure_logs_only_sanitized_request_metadata(self) -> None:
        request = MetadataOnlyRequest()
        self.diagnostics._started(request)
        self.diagnostics._response(Mock(request=request, status=541))
        self.assertIn("HTTP 541", self.messages[0])
        self.assertIn("POST", self.messages[0])
        self.assert_private_data_absent()
        self.assertNotIn("private", repr(self.diagnostics.pending))

    def test_network_error_does_not_log_raw_failure_message(self) -> None:
        request = MetadataOnlyRequest()
        self.diagnostics._started(request)
        self.diagnostics._failed(request)
        self.assertIn("net::ERR_TIMED_OUT", self.messages[0])
        self.assertEqual(self.diagnostics.pending, {})
        self.assert_private_data_absent()

    def test_successful_http_response_is_not_claimed_as_successful_login(self) -> None:
        self.diagnostics._response(Mock(request=MetadataOnlyRequest(), status=200))
        self.assertIn("HTTP 200", self.messages[0])
        self.assertIn("不等于登录成功", self.messages[0])
        self.assert_private_data_absent()

    def test_store_handoff_http_failure_stops_login_without_retry(self) -> None:
        request = MetadataOnlyRequest()
        request.url = "https://secure7.www.apple.com.cn/shop/signIn/idms/authx?token=private#secret"
        self.diagnostics._response(Mock(request=request, status=541))
        with self.assertRaisesRegex(RuntimeError, "登录交接失败：HTTP 541") as captured:
            self.diagnostics.check_status()
        self.assertNotIn("private", str(captured.exception))
        self.assertIn("不会重试登录", str(captured.exception))

    def test_store_handoff_network_failure_also_stops_waiting(self) -> None:
        request = MetadataOnlyRequest()
        request.url = "https://secure7.www.apple.com.cn/shop/signIn/idms/authx?token=private"
        self.diagnostics._failed(request)
        with self.assertRaisesRegex(RuntimeError, "net::ERR_TIMED_OUT"):
            self.diagnostics.check_status()

    def test_unrelated_search_failure_does_not_abort_login(self) -> None:
        request = MetadataOnlyRequest()
        request.url = "https://secure7.www.apple.com.cn/search-services/suggestions/defaultlinks/?token=private"
        self.diagnostics._response(Mock(request=request, status=403))
        self.diagnostics.check_status()

    def test_successful_manual_handoff_clears_previous_failure(self) -> None:
        request = MetadataOnlyRequest()
        request.url = "https://secure7.www.apple.com.cn/shop/signIn/idms/authx"
        self.diagnostics._response(Mock(request=request, status=541))
        self.diagnostics._response(Mock(request=request, status=200))
        self.diagnostics.check_status()

    def test_identity_response_does_not_clear_store_failure(self) -> None:
        failed = MetadataOnlyRequest()
        failed.url = "https://secure7.www.apple.com.cn/shop/signIn/idms/authx"
        self.diagnostics._response(Mock(request=failed, status=541))
        self.diagnostics._response(Mock(request=MetadataOnlyRequest(), status=200))
        with self.assertRaisesRegex(RuntimeError, "HTTP 541"):
            self.diagnostics.check_status()

    def test_successful_top_level_account_document_allows_rechecking_login(self) -> None:
        failed = MetadataOnlyRequest()
        failed.url = "https://secure7.www.apple.com.cn/shop/signIn/idms/authx"
        self.diagnostics._response(Mock(request=failed, status=541))
        account = Mock(url="https://secure7.www.apple.com.cn/shop/account/home", method="GET", resource_type="document")
        account.frame.parent_frame = None
        self.diagnostics._response(Mock(request=account, status=200))
        self.diagnostics.check_status()

    def test_slow_request_is_reported_and_throttled_then_removed(self) -> None:
        request = MetadataOnlyRequest()
        self.diagnostics._started(request)
        self.now = 14
        self.diagnostics.report_pending()
        self.assertEqual(self.messages, [])
        self.now = 16
        self.diagnostics.report_pending()
        self.assertEqual(len(self.messages), 1)
        self.now = 20
        self.diagnostics.report_pending()
        self.assertEqual(len(self.messages), 1)
        self.now = 47
        self.diagnostics.report_pending()
        self.assertEqual(len(self.messages), 2)
        self.diagnostics._finished(request)
        self.now = 80
        self.diagnostics.report_pending()
        self.assertEqual(len(self.messages), 2)
        self.assert_private_data_absent()

    def test_non_apple_requests_are_not_collected(self) -> None:
        request = Mock(url="https://apple.com.cn.example.test/auth?private=secret", method="POST", resource_type="xhr")
        self.diagnostics._started(request)
        self.diagnostics._response(Mock(request=request, status=500))
        self.assertEqual(self.diagnostics.pending, {})
        self.assertEqual(self.messages, [])

    def test_listeners_can_be_attached_and_removed(self) -> None:
        context = Mock()
        self.diagnostics.attach(context)
        self.diagnostics.detach(context)
        self.assertEqual(context.on.call_count, 4)
        self.assertEqual(context.on.call_args_list, context.remove_listener.call_args_list)

    def test_url_userinfo_is_never_logged(self) -> None:
        self.assertEqual(
            safe_page_url("https://user:private@www.apple.com.cn/shop/signIn?token=secret#private"),
            "https://www.apple.com.cn/shop/signIn",
        )


if __name__ == "__main__":
    unittest.main()
