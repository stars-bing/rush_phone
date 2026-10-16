import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rush_phone.cli import main
from rush_phone.interactive_test import TerminalAction, TerminalInput, wait_for_manual_login
from rush_phone.login_status import LoginInspection, LoginState


class TerminalInputTests(unittest.TestCase):
    def read_actions(self, text):
        reader = TerminalInput(io.StringIO(text))
        reader.start()
        reader.thread.join(timeout=1)
        self.assertFalse(reader.thread.is_alive())
        result = []
        while (action := reader.next_action()) is not None:
            result.append(action)
        return result

    def test_only_action_types_are_queued_not_input_text(self):
        self.assertEqual(
            self.read_actions("\nprivate-password\nq\n"),
            [TerminalAction.CONFIRM, TerminalAction.INVALID, TerminalAction.QUIT],
        )

    def test_eof_is_not_treated_as_enter(self):
        self.assertEqual(self.read_actions(""), [TerminalAction.EOF])


class ManualLoginTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.context = Mock(pages=[self.page])
        self.inputs = Mock()
        self.diagnostics = Mock()
        self.logger = Mock(spec=logging.Logger)
        self.url = "https://secure7.www.apple.com.cn/shop/account/home"

    def wait(self):
        return wait_for_manual_login(
            self.context, self.page, self.inputs, self.diagnostics, self.logger, self.url, 0,
        )

    def inspection(self, state):
        return LoginInspection(state, self.page, self.url)

    def test_signed_in_page_does_not_start_until_user_confirms(self):
        self.inputs.next_action.side_effect = [None, TerminalAction.CONFIRM]
        with patch("rush_phone.interactive_test.inspect_apple_login", return_value=self.inspection(LoginState.SIGNED_IN)), \
             patch("rush_phone.interactive_test._pump_browser", return_value=True) as pump:
            self.assertIs(self.wait(), self.page)
            pump.assert_called_once()
        self.context.close.assert_not_called()

    def test_enter_before_login_cannot_automatically_continue_afterwards(self):
        self.inputs.next_action.side_effect = [TerminalAction.CONFIRM, None, TerminalAction.QUIT]
        with patch("rush_phone.interactive_test.inspect_apple_login", side_effect=[
            self.inspection(LoginState.SIGN_IN_REQUIRED), self.inspection(LoginState.SIGNED_IN), self.inspection(LoginState.SIGNED_IN),
        ]), patch("rush_phone.interactive_test._pump_browser", return_value=True):
            self.assertIsNone(self.wait())
        self.page.goto.assert_not_called()

    def test_handoff_error_keeps_window_open_and_prevents_confirm(self):
        self.inputs.next_action.side_effect = [TerminalAction.CONFIRM, TerminalAction.QUIT]
        self.diagnostics.check_status.side_effect = RuntimeError("HTTP 541")
        with patch("rush_phone.interactive_test.inspect_apple_login", return_value=self.inspection(LoginState.SIGNED_IN)), \
             patch("rush_phone.interactive_test._pump_browser", return_value=True) as pump:
            self.assertIsNone(self.wait())
            pump.assert_called_once()
        self.context.close.assert_not_called()
        self.page.goto.assert_not_called()

    def test_eof_does_not_start_product_flow(self):
        self.inputs.next_action.return_value = TerminalAction.EOF
        with patch("rush_phone.interactive_test.inspect_apple_login", return_value=self.inspection(LoginState.SIGNED_IN)):
            self.assertIsNone(self.wait())
        self.context.close.assert_not_called()

    def test_auto_mode_requires_actual_signed_in_state_before_continuing(self):
        self.inputs.next_action.side_effect = [None, TerminalAction.QUIT]
        with patch("rush_phone.interactive_test.inspect_apple_login", side_effect=[
            self.inspection(LoginState.SIGN_IN_REQUIRED), self.inspection(LoginState.SIGNED_IN),
        ]), patch("rush_phone.interactive_test._pump_browser", return_value=True):
            result = wait_for_manual_login(
                self.context, self.page, self.inputs, self.diagnostics, self.logger, self.url, 30, auto_confirm=True,
            )
        self.assertIs(result, self.page)
        self.assertEqual(self.inputs.next_action.call_count, 1)

    def test_auto_mode_cannot_ignore_a_failed_store_handoff(self):
        self.inputs.next_action.return_value = TerminalAction.QUIT
        self.diagnostics.check_status.side_effect = RuntimeError("HTTP 541")
        with patch("rush_phone.interactive_test.inspect_apple_login", return_value=self.inspection(LoginState.SIGNED_IN)):
            result = wait_for_manual_login(
                self.context, self.page, self.inputs, self.diagnostics, self.logger, self.url, 30, auto_confirm=True,
            )
        self.assertIsNone(result)


class InteractiveCliTests(unittest.TestCase):
    def setUp(self):
        self.config_path = str(Path(__file__).parent.parent / "config.example.toml")

    def test_test_command_dispatches_to_two_stage_workflow(self):
        with patch("rush_phone.cli._interactive_test", return_value=0) as interactive:
            self.assertEqual(main(["--verbose", "test", "--config", self.config_path]), 0)
            self.assertTrue(interactive.call_args.kwargs["verbose"])

    def test_old_then_test_command_uses_new_workflow(self):
        with patch("rush_phone.cli._interactive_test", return_value=0) as interactive, \
             patch("rush_phone.cli._login") as login:
            self.assertEqual(main(["login", "--config", self.config_path, "--then-test"]), 0)
            interactive.assert_called_once()
            login.assert_not_called()

    def test_interactive_test_never_accepts_submit_order_flag(self):
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as captured:
                main(["test", "--config", self.config_path, "--submit-order"])
        self.assertEqual(captured.exception.code, 2)

    def test_auto_login_flag_is_explicit_and_forwarded(self):
        with patch("rush_phone.cli._interactive_test", return_value=0) as interactive:
            self.assertEqual(main(["test", "--config", self.config_path, "--auto-login"]), 0)
            self.assertTrue(interactive.call_args.kwargs["auto_login"])


if __name__ == "__main__":
    unittest.main()
