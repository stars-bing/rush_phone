import ctypes
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from rush_phone.chrome_session import ChromeSessionError
from rush_phone.config import ConfigError
from rush_phone.session_checkout import SessionCheckout
from rush_phone.session_test import SessionTestResult, run_session_purchase
from rush_phone.windows_app import main
from rush_phone.windows_runtime import (
    LauncherController, LauncherLock, initialize_data, load_preset, system_chrome_environment, user_data_dir,
)


class WindowsDataTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name) / "中文用户 with spaces"
        initialize_data(self.data_dir)

    def test_public_presets_use_requested_specs_and_share_one_relative_profile(self):
        duo, first = load_preset(self.data_dir, "duo")
        pro, second = load_preset(self.data_dir, "pro-max")
        self.assertEqual(duo.variables, {"model": "iPhone Duo", "color": "夜空色", "capacity": "512GB"})
        self.assertEqual(pro.variables, {"model": "iPhone 18 Pro Max", "color": "勃艮第酒红色", "capacity": "512GB"})
        self.assertEqual(first, second)
        self.assertEqual(first.profile_dir, (self.data_dir / ".profiles/apple-cn-account-1").resolve())
        for config in (duo, pro):
            self.assertEqual(len(config.accounts), 1)
            self.assertEqual(config.accounts[0].data, {})
            text = config.source_path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text)
            self.assertNotIn("password", text)
            self.assertNotIn("@", config.accounts[0].name)
            SessionCheckout(Mock(), config, first, Mock(), submit_order=True)

    def test_upgrade_does_not_overwrite_user_config_or_login_data(self):
        path = self.data_dir / "duo.toml"
        changed = path.read_text(encoding="utf-8").replace('capacity = "512GB"', 'capacity = "1TB"')
        path.write_text(changed, encoding="utf-8")
        profile = self.data_dir / ".profiles/apple-cn-account-1/chrome-session"
        profile.mkdir(parents=True)
        marker = profile / "local-login-fixture"
        marker.write_bytes(b"existing-profile")
        initialize_data(self.data_dir)
        self.assertEqual(path.read_text(encoding="utf-8"), changed)
        self.assertEqual(marker.read_bytes(), b"existing-profile")

    def test_different_account_profile_is_rejected_before_browser_access(self):
        path = self.data_dir / "pro-max.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("apple-cn-account-1", "other-account"), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "必须一致"):
            load_preset(self.data_dir, "pro-max")

    def test_user_directory_does_not_depend_on_cwd_or_executable_location(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.data_dir)}):
            self.assertEqual(user_data_dir(), self.data_dir / "RushPhone")

    def test_second_launcher_is_blocked_and_lock_is_released_on_close(self):
        first, second = LauncherLock(self.data_dir), LauncherLock(self.data_dir)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.acquire()
        with self.assertRaisesRegex(RuntimeError, "已在运行"):
            second.acquire()
        first.close()
        second.acquire()

    @unittest.skipIf(os.name == "nt", "Mac 入口回归")
    def test_windows_entry_does_not_replace_mac_cli(self):
        with patch("rush_phone.windows_app.initialize_data") as initialize:
            self.assertEqual(main([]), 2)
            initialize.assert_not_called()


class WindowsControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        initialize_data(self.data_dir)
        self.events = Queue()
        self.controller = LauncherController(self.data_dir, self.events)
        self.addCleanup(self.controller.close)

    def finish(self):
        self.controller.thread.join(3)
        self.assertFalse(self.controller.busy)
        finished = []
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "finished":
                finished.append(value)
        self.assertEqual(len(finished), 1)
        return finished[0]

    def test_login_only_opens_ordinary_chrome_without_running_purchase(self):
        with patch("rush_phone.windows_runtime.open_chrome_session", return_value=(Mock(), False)) as launch, \
             patch("rush_phone.windows_runtime.run_session_purchase") as purchase:
            self.controller.start("login", submit_order=True)
            result = self.finish()
        self.assertEqual(result.state, "login")
        launch.assert_called_once()
        purchase.assert_not_called()

    def test_purchase_reuses_session_and_final_submit_requires_explicit_choice(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed), \
                 patch("rush_phone.windows_runtime.run_session_purchase", return_value=SessionTestResult(True, "fixture")) as purchase, \
                 patch("rush_phone.windows_runtime.open_chrome_session") as launch:
                self.controller.start("pro-max", submit_order=allowed)
                self.assertEqual(self.finish().state, "success")
                self.assertEqual(purchase.call_args.args[0].variables["model"], "iPhone 18 Pro Max")
                self.assertIs(purchase.call_args.kwargs["submit_order"], allowed)
                self.assertEqual(purchase.call_args.kwargs["stop_requested"], self.controller.stop_event.is_set)
                launch.assert_not_called()

    def test_stop_is_cooperative_and_an_active_task_blocks_a_second_purchase(self):
        entered = threading.Event()

        def purchase(*args, stop_requested, **kwargs):
            entered.set()
            if not self.controller.stop_event.wait(2):
                raise AssertionError("stop not received")
            if stop_requested():
                raise KeyboardInterrupt

        with patch("rush_phone.windows_runtime.run_session_purchase", side_effect=purchase):
            self.controller.start("duo")
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(RuntimeError, "已有任务"):
                    self.controller.start("pro-max")
            finally:
                self.controller.stop()
                result = self.finish()
        self.assertEqual(result.state, "stopped")

    def test_unknown_order_blocks_further_purchase_and_never_retries(self):
        with patch("rush_phone.windows_runtime.run_session_purchase", return_value=SessionTestResult(False, "订单待确认", order_submitted=None)) as purchase:
            self.controller.start("duo", submit_order=True)
            self.assertEqual(self.finish().state, "unknown")
            for action in ("duo", "pro-max"):
                with self.assertRaisesRegex(RuntimeError, "核对订单"):
                    self.controller.start(action, submit_order=True)
            purchase.assert_called_once()

    def test_missing_session_stops_without_launching_a_fallback_browser(self):
        with patch("rush_phone.windows_runtime.run_session_purchase", side_effect=ChromeSessionError("尚未打开专用 Chrome，请先运行 chrome-login 命令")) as purchase, \
             patch("rush_phone.windows_runtime.open_chrome_session") as launch:
            self.controller.start("duo")
            result = self.finish()
        self.assertEqual(result.state, "error")
        self.assertIn("登录", result.message)
        self.assertNotIn("chrome-login", result.message)
        launch.assert_not_called()
        purchase.assert_called_once()

    def test_missing_chrome_has_an_actionable_message_without_terminal_commands(self):
        with patch("rush_phone.windows_runtime.open_chrome_session", side_effect=ChromeSessionError(
                "未找到系统 Google Chrome；请先安装，或使用 --chrome-path 指定可执行文件")):
            self.controller.start("login")
            result = self.finish()
        self.assertEqual(result.state, "error")
        self.assertIn("安装 Google Chrome", result.message)
        self.assertNotIn("--chrome-path", result.message)


class CooperativeStopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        initialize_data(self.data_dir)
        self.config, self.account = load_preset(self.data_dir, "duo")
        self.stop = threading.Event()
        self.driver = SessionCheckout(Mock(), self.config, self.account, Mock(spec=logging.Logger),
                                      watch=True, submit_order=True, stop_requested=self.stop.is_set)

    def test_stop_before_connect_does_not_access_a_browser(self):
        self.stop.set()
        with patch("rush_phone.session_test.require_chrome_session") as require:
            with self.assertRaises(KeyboardInterrupt):
                run_session_purchase(self.config, self.account, Mock(), stop_requested=self.stop.is_set)
            require.assert_not_called()

    def test_stop_interrupts_manual_verification_wait(self):
        self.driver.page = Mock(url="https://www.apple.com.cn/shop/signIn")
        self.driver.page.is_closed.return_value = False
        self.driver.page.wait_for_timeout.side_effect = lambda _: self.stop.set()
        from rush_phone.login_status import LoginState
        with patch("rush_phone.session_checkout._inspect_page", return_value=LoginState.VERIFICATION_REQUIRED):
            with self.assertRaises(KeyboardInterrupt):
                self.driver.guard()
        self.driver.page.close.assert_not_called()

    def test_stop_before_final_click_does_not_submit(self):
        self.driver.page = Mock(url="https://www.apple.com.cn/shop/checkout/review")
        self.driver.page.locator.return_value.first.is_visible.return_value = False
        self.driver.page.locator.return_value.count.return_value = 1
        self.stop.set()
        final = Mock()
        with self.assertRaises(KeyboardInterrupt):
            self.driver.place_order(final)
        final.click.assert_not_called()
        self.assertFalse(self.driver.submit_attempted)

    def test_stop_after_final_click_reports_unknown_without_retry_or_close(self):
        self.driver.page = Mock(url="https://www.apple.com.cn/shop/checkout/review")
        self.driver.page.locator.return_value.first.is_visible.return_value = False
        self.driver.page.locator.return_value.count.return_value = 1
        self.driver.guard = self.driver.check_stop
        self.driver.worker._screenshot = Mock(return_value=None)
        final = Mock()
        final.click.side_effect = lambda **kwargs: self.stop.set()
        result = self.driver.place_order(final)
        self.assertIsNone(result.order_submitted)
        final.click.assert_called_once()
        self.driver.context.close.assert_not_called()
        self.driver.page.close.assert_not_called()


class FrozenChromeEnvironmentTests(unittest.TestCase):
    def test_system_dll_and_path_are_restored_even_if_launch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle"
            normal = Path(directory) / "system"
            original_path = os.pathsep.join((str(bundle), str(bundle / "libs"), str(normal)))
            kernel = Mock()

            def get_directory(size, buffer):
                if buffer is not None:
                    buffer.value = str(bundle)
                return len(str(bundle))

            kernel.GetDllDirectoryW.side_effect = get_directory
            kernel.SetDllDirectoryW.return_value = True
            with patch("rush_phone.windows_runtime.sys.platform", "win32"), \
                 patch("rush_phone.windows_runtime.sys.frozen", True, create=True), \
                 patch("rush_phone.windows_runtime.sys._MEIPASS", str(bundle), create=True), \
                 patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                 patch.dict(os.environ, {"PATH": original_path}):
                with self.assertRaisesRegex(RuntimeError, "fixture"):
                    with system_chrome_environment():
                        self.assertEqual(os.environ["PATH"], str(normal))
                        raise RuntimeError("fixture")
                self.assertEqual(os.environ["PATH"], original_path)
            self.assertEqual([call.args for call in kernel.SetDllDirectoryW.call_args_list], [(None,), (str(bundle),)])


if __name__ == "__main__":
    unittest.main()
