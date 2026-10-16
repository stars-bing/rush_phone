import unittest
from unittest.mock import Mock, patch

from rush_phone.login_status import (
    LoginState, inspect_apple_login, is_apple_store_url, wait_for_apple_login,
)


def locator(visible: bool = False) -> Mock:
    result = Mock()
    result.count.return_value = 1 if visible else 0
    result.nth.return_value.is_visible.return_value = visible
    return result


def page(url: str, *, signed_in=False, password=False, otp=False) -> Mock:
    result = Mock()
    result.url = url
    result.is_closed.return_value = False
    frame = Mock()
    frame.locator.side_effect = lambda selector: locator(
        password if selector.startswith("#account_name") else otp
    )
    result.frames = [frame]
    result.get_by_role.side_effect = lambda role, **kwargs: locator(signed_in and role == "heading")
    return result


class LoginStatusTests(unittest.TestCase):
    def test_only_actual_https_apple_store_hosts_are_trusted(self) -> None:
        self.assertTrue(is_apple_store_url("https://secure6.www.apple.com.cn/shop/account/home"))
        self.assertFalse(is_apple_store_url("https://apple.com.cn.attacker.test/shop/account/home"))
        self.assertFalse(is_apple_store_url("https://notapple.com/shop/account/home"))
        self.assertFalse(is_apple_store_url("http://www.apple.com.cn/shop/account/home"))
        self.assertFalse(is_apple_store_url("https://idmsa.apple.com.cn/auth/authorize"))

    def test_success_in_a_new_tab_is_detected(self) -> None:
        initial = page("https://secure6.www.apple.com.cn/shop/signIn/account", password=True)
        completed = page("https://secure6.www.apple.com.cn/shop/account/home", signed_in=True)
        context = Mock(pages=[initial, completed])
        inspection = inspect_apple_login(context)
        self.assertEqual(inspection.state, LoginState.SIGNED_IN)
        self.assertIs(inspection.page, completed)

    def test_visible_password_prevents_false_success(self) -> None:
        pending = page("https://secure6.www.apple.com.cn/shop/account/home", signed_in=True, password=True)
        self.assertEqual(inspect_apple_login(Mock(pages=[pending])).state, LoginState.SIGN_IN_REQUIRED)

    def test_otp_is_reported_without_reading_input_values(self) -> None:
        pending = page("https://secure6.www.apple.com.cn/shop/signIn/account?token=private#secret", password=True, otp=True)
        inspection = inspect_apple_login(Mock(pages=[pending]))
        self.assertEqual(inspection.state, LoginState.VERIFICATION_REQUIRED)
        self.assertNotIn("private", inspection.url)
        self.assertNotIn("secret", inspection.url)
        pending.frames[0].input_value.assert_not_called()

    def test_account_url_alone_is_not_login_success(self) -> None:
        pending = page("https://secure6.www.apple.com.cn/shop/account/home")
        self.assertEqual(inspect_apple_login(Mock(pages=[pending])).state, LoginState.UNKNOWN)

    def test_signin_with_or_without_trailing_path_is_loading_when_form_is_hidden(self) -> None:
        for url in ("https://secure6.www.apple.com.cn/shop/signIn", "https://secure6.www.apple.com.cn/shop/signIn/account"):
            with self.subTest(url=url):
                self.assertEqual(inspect_apple_login(Mock(pages=[page(url)])).state, LoginState.LOADING)

    def test_unrelated_page_cannot_claim_login_success(self) -> None:
        unrelated = page("https://example.test/shop/account/home", signed_in=True)
        self.assertEqual(inspect_apple_login(Mock(pages=[unrelated])).state, LoginState.UNKNOWN)

    def test_wait_pumps_browser_events_instead_of_blocking_on_input(self) -> None:
        pending = page("https://secure6.www.apple.com.cn/shop/signIn/account", password=True)
        completed = page("https://secure6.www.apple.com.cn/shop/account/home", signed_in=True)
        context = Mock(pages=[pending])
        pending.wait_for_timeout.side_effect = lambda _: setattr(context, "pages", [pending, completed])
        tick = Mock()
        self.assertIs(wait_for_apple_login(context, 2, on_tick=tick), completed)
        pending.wait_for_timeout.assert_called_once_with(500)
        self.assertEqual(tick.call_count, 2)

    def test_failed_handoff_stops_before_accepting_an_account_page(self) -> None:
        completed = page("https://secure6.www.apple.com.cn/shop/account/home", signed_in=True)
        tick = Mock(side_effect=RuntimeError("HTTP 541"))
        with self.assertRaisesRegex(RuntimeError, "HTTP 541"):
            wait_for_apple_login(Mock(pages=[completed]), 2, on_tick=tick)

    def test_closed_window_stops_waiting(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            wait_for_apple_login(Mock(pages=[]), 2)

    def test_unconfirmed_login_times_out(self) -> None:
        pending = page("https://secure6.www.apple.com.cn/shop/signIn/account", password=True)
        with patch("rush_phone.login_status.time.monotonic", side_effect=[0, 0, 3]):
            with self.assertRaisesRegex(TimeoutError, "不启动购买演练"):
                wait_for_apple_login(Mock(pages=[pending]), 2)


if __name__ == "__main__":
    unittest.main()
