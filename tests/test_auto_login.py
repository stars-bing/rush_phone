import unittest
from unittest.mock import Mock, patch

from rush_phone.auto_login import AutoLoginError, AutoLoginResult, submit_credentials_once
from rush_phone.credentials import LoginCredentials
from rush_phone.login_status import LoginInspection, LoginState


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        self.credentials = LoginCredentials("account@example.test", "private-password")
        self.page = Mock(url="https://secure7.www.apple.com.cn/shop/signIn/account")
        self.page.is_closed.return_value = False
        self.frame = Mock(url="https://idmsa.apple.com.cn/appleauth/auth/authorize/signin", page=self.page)
        self.username = Mock()
        self.username.is_visible.return_value = True
        self.password = Mock()
        self.password.is_visible.return_value = True
        self.button = Mock()
        self.frame.locator.side_effect = lambda selector: (
            self.username if selector == "#account_name_text_field" else
            self.password if selector == "#password_text_field" else self.button
        )
        self.page.frames = [self.frame]
        self.context = Mock(pages=[self.page])
        self.inspector = patch("rush_phone.auto_login.inspect_apple_login", return_value=LoginInspection(LoginState.SIGN_IN_REQUIRED, self.page, self.page.url))
        self.inspector.start()
        self.addCleanup(self.inspector.stop)

    def test_password_is_submitted_once_only_in_trusted_frame(self):
        self.assertEqual(submit_credentials_once(self.context, self.credentials), AutoLoginResult.SUBMITTED)
        self.username.fill.assert_called_once_with(self.credentials.username, timeout=5000)
        self.password.fill.assert_called_once_with(self.credentials.password, timeout=5000)
        self.button.filter.return_value.first.click.assert_called_once_with(timeout=8000)

    def test_two_step_login_does_not_resubmit_password(self):
        self.password.is_visible.return_value = False
        submit_credentials_once(self.context, self.credentials)
        self.assertEqual(self.button.filter.return_value.first.click.call_count, 2)
        self.password.fill.assert_called_once()

    def test_api_exception_cannot_disclose_password(self):
        self.password.fill.side_effect = ValueError("fill contains private-password")
        with self.assertRaises(AutoLoginError) as captured:
            submit_credentials_once(self.context, self.credentials)
        self.assertNotIn("private-password", str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)
        self.assertTrue(captured.exception.__suppress_context__)
        self.button.filter.return_value.first.click.assert_not_called()

    def test_untrusted_auth_frame_is_never_filled(self):
        self.frame.url = "https://idmsa.apple.com.cn.attacker.test/appleauth/auth/authorize/signin"
        with patch("rush_phone.auto_login.time.monotonic", side_effect=[0, 0, 21]):
            with self.assertRaises(AutoLoginError):
                submit_credentials_once(self.context, self.credentials)
        self.username.fill.assert_not_called()
        self.password.fill.assert_not_called()

    def test_otp_requires_human_and_does_not_submit_credentials(self):
        with patch("rush_phone.auto_login.inspect_apple_login", return_value=LoginInspection(LoginState.VERIFICATION_REQUIRED, self.page, self.page.url)):
            self.assertEqual(submit_credentials_once(self.context, self.credentials), AutoLoginResult.NEEDS_VERIFICATION)
        self.username.fill.assert_not_called()
        self.password.fill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
