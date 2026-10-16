import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rush_phone.chrome_session import (
    ChromeSession, ChromeSessionError, _check_profile, _choose_port, _chrome_executable,
    _listener_owned_by, _listeners_are_owned, _load_record, _read_websocket_path,
    _save_record, open_chrome_session, require_chrome_session,
    session_profile_dir, session_record_path,
)
from rush_phone.cli import main
from rush_phone.config import AccountConfig, load_config


class ChromeSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.account = AccountConfig("本地测试", Path(self.directory.name))
        self.session = ChromeSession(12345, 9222, session_profile_dir(self.account), "/devtools/browser/private-id-1234")

    def test_record_contains_only_connection_metadata_and_is_private(self):
        _save_record(self.account, self.session)
        self.assertEqual(_load_record(self.account), self.session)
        path = session_record_path(self.account)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(path.read_text())), {"version", "pid", "port", "profile_dir", "websocket_path"})
        self.assertNotIn("private-id", repr(self.session))

    def test_corrupt_record_errors_do_not_echo_contents(self):
        session_record_path(self.account).write_text('{"password":"secret-value"}')
        with self.assertRaises(ChromeSessionError) as captured:
            _load_record(self.account)
        self.assertNotIn("secret-value", str(captured.exception))

    def test_profile_escape_and_daily_profile_are_rejected(self):
        if os.name != "nt":  # Windows 创建符号链接可能需要开发者模式；另测日常目录。
            with tempfile.TemporaryDirectory() as outside:
                (self.account.profile_dir / "chrome-session").symlink_to(outside)
                with self.assertRaises(ChromeSessionError):
                    _check_profile(self.account)
        daily = Path.home() / "Library/Application Support/Google/Chrome"
        with self.assertRaises(ChromeSessionError):
            _check_profile(AccountConfig("不允许", daily))

    def test_only_owned_loopback_listeners_are_accepted(self):
        self.assertTrue(_listeners_are_owned("p12345\nn127.0.0.1:9222\nn[::1]:9222\n", 12345, 9222))
        for output in ("p999\nn127.0.0.1:9222", "p12345\nn*:9222", "p12345\nn0.0.0.0:9222", "p12345\nn192.0.2.1:9222", "", "p12345\nn127.0.0.1:9223"):
            self.assertFalse(_listeners_are_owned(output, 12345, 9222), output)

    def test_missing_record_does_not_probe_arbitrary_ports(self):
        with patch("rush_phone.chrome_session._listener_owned_by") as listener:
            with self.assertRaisesRegex(ChromeSessionError, "chrome-login"):
                require_chrome_session(self.account)
            listener.assert_not_called()

    def test_record_identity_must_match_live_browser(self):
        _save_record(self.account, self.session)
        with patch("rush_phone.chrome_session._listener_owned_by", return_value=True), \
             patch("rush_phone.chrome_session._read_websocket_path", return_value="/devtools/browser/other-id-1234"):
            with self.assertRaisesRegex(ChromeSessionError, "拒绝接管"):
                require_chrome_session(self.account)

    def test_port_owner_is_checked_before_contacting_debug_endpoint(self):
        _save_record(self.account, self.session)
        with patch("rush_phone.chrome_session._listener_owned_by", return_value=False), \
             patch("rush_phone.chrome_session._read_websocket_path") as read:
            with self.assertRaises(ChromeSessionError):
                require_chrome_session(self.account)
            read.assert_not_called()

    def test_invalid_ports_are_rejected(self):
        for port in (0, 80, 65536, True):
            with self.assertRaises(ChromeSessionError):
                _choose_port(port)

    def test_version_endpoint_cannot_redirect_connection_to_remote_host(self):
        for endpoint in ("ws://example.test:9222/devtools/browser/private-id-1234", "ws://user:secret@127.0.0.1:9222/devtools/browser/private-id-1234", "ws://127.0.0.1:9222/devtools/browser/private-id-1234?token=secret"):
            response = Mock(status=200)
            response.read.return_value = json.dumps({"Browser": "Chrome/155", "webSocketDebuggerUrl": endpoint}).encode()
            connection = Mock()
            connection.getresponse.return_value = response
            with patch("rush_phone.chrome_session.http.client.HTTPConnection", return_value=connection):
                with self.assertRaises(ChromeSessionError) as captured:
                    _read_websocket_path(9222)
                self.assertNotIn("secret", str(captured.exception))
                connection.close.assert_called_once()

    def test_windows_daily_profile_and_children_are_rejected(self):
        local = Path(self.directory.name) / "Local AppData"
        daily = local / "Google/Chrome/User Data"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            for account_dir in (daily, daily / "Default"):
                with self.assertRaises(ChromeSessionError):
                    _check_profile(AccountConfig("不允许", account_dir))

    def test_windows_finds_user_and_system_chrome_with_spaces(self):
        roots = {name: str(Path(self.directory.name) / name) for name in
                 ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")}
        with patch("rush_phone.chrome_session.sys.platform", "win32"), patch.dict(os.environ, roots, clear=True):
            for root in roots.values():
                executable = Path(root) / "Google/Chrome/Application/chrome.exe"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
                self.assertEqual(_chrome_executable(), executable.resolve())
                executable.unlink()

    def test_custom_chrome_path_rejects_scripts_and_missing_files(self):
        executable = Path(self.directory.name) / "自定义 Chrome/chrome.exe"
        executable.parent.mkdir()
        executable.touch()
        script = executable.with_suffix(".cmd")
        script.touch()
        with patch("rush_phone.chrome_session.sys.platform", "win32"):
            self.assertEqual(_chrome_executable(executable), executable.resolve())
            for bad in (script, executable.parent, executable.parent / "missing.exe"):
                with self.assertRaises(ChromeSessionError):
                    _chrome_executable(bad)

    def test_unsupported_platform_does_not_launch_or_probe(self):
        with patch("rush_phone.chrome_session.sys.platform", "linux"), \
             patch("rush_phone.chrome_session.subprocess.run") as run:
            with self.assertRaises(ChromeSessionError):
                _chrome_executable()
            with self.assertRaises(ChromeSessionError):
                _listener_owned_by(12345, 9222)
            run.assert_not_called()

    @staticmethod
    def connection(host="127.0.0.1", port=9222, pid=12345, status="LISTEN"):
        return SimpleNamespace(laddr=SimpleNamespace(ip=host, port=port), pid=pid, status=status)

    def test_windows_requires_all_ipv4_ipv6_listeners_to_be_owned_loopback(self):
        psutil = Mock(CONN_LISTEN="LISTEN")
        cases = [
            ([self.connection(), self.connection("::1")], True),
            ([], False),
            ([self.connection(pid=None)], False),
            ([self.connection(pid=999)], False),
            ([self.connection(), self.connection("::1", pid=999)], False),
            ([self.connection("0.0.0.0")], False),
            ([self.connection("::")], False),
            ([self.connection("192.0.2.1")], False),
            ([self.connection(port=9333)], False),
            ([self.connection(status="ESTABLISHED")], False),
        ]
        with patch("rush_phone.chrome_session.sys.platform", "win32"), \
             patch.dict("sys.modules", {"psutil": psutil}), \
             patch("rush_phone.chrome_session.subprocess.run") as lsof:
            for connections, expected in cases:
                psutil.net_connections.return_value = connections
                self.assertEqual(_listener_owned_by(12345, 9222), expected)
            psutil.net_connections.assert_called_with(kind="tcp")
            lsof.assert_not_called()

    def test_windows_listener_failure_is_closed_without_sensitive_error(self):
        psutil = Mock(CONN_LISTEN="LISTEN")
        psutil.net_connections.side_effect = PermissionError("private-connection-data")
        with patch("rush_phone.chrome_session.sys.platform", "win32"), patch.dict("sys.modules", {"psutil": psutil}):
            with self.assertRaises(ChromeSessionError) as captured:
                _listener_owned_by(12345, 9222)
        self.assertNotIn("private-connection-data", str(captured.exception))

    def test_windows_launcher_is_detached_and_preserves_security_flags(self):
        config = load_config(Path(__file__).parent.parent / "config.example.toml")
        config = replace(config, run=replace(config.run, browser_channel="chrome"),
                         target=replace(config.target, login_url="https://www.apple.com.cn/shop/account/home"))
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch("rush_phone.chrome_session.sys.platform", "win32"), \
             patch("rush_phone.chrome_session.subprocess.DETACHED_PROCESS", 8, create=True), \
             patch("rush_phone.chrome_session.subprocess.CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch("rush_phone.chrome_session._chrome_executable", return_value=Path("Chrome/chrome.exe")), \
             patch("rush_phone.chrome_session._choose_port", return_value=9222), \
             patch("rush_phone.chrome_session._protect_session_path") as protect, \
             patch("rush_phone.chrome_session._listener_owned_by", return_value=True), \
             patch("rush_phone.chrome_session._read_websocket_path", return_value=self.session.websocket_path), \
             patch("rush_phone.chrome_session._save_record"), \
             patch("rush_phone.chrome_session.subprocess.Popen", return_value=process) as launch:
            session, reused = open_chrome_session(config, self.account)
        self.assertFalse(reused)
        self.assertEqual(session.pid, 12345)
        protect.assert_called_once_with(session_profile_dir(self.account), directory=True)
        self.assertEqual(launch.call_args.kwargs["creationflags"], 520)
        self.assertNotIn("start_new_session", launch.call_args.kwargs)
        args = launch.call_args.args[0]
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertNotIn("--no-sandbox", args)
        self.assertNotIn("--enable-automation", args)
        process.terminate.assert_not_called()

    def test_metadata_permissions_are_set_before_payload_is_written(self):
        from rush_phone.private_files import PrivateFileError
        with patch("rush_phone.chrome_session.protect_private_path", side_effect=PrivateFileError("权限失败")):
            with self.assertRaises(ChromeSessionError):
                _save_record(self.account, self.session)
        self.assertFalse(session_record_path(self.account).exists())
        self.assertEqual(list(self.account.profile_dir.glob(".chrome-session-*")), [])


class SessionCliTests(unittest.TestCase):
    def setUp(self):
        self.config = str(Path(__file__).parent.parent / "config.example.toml")

    def test_chrome_login_only_opens_manual_session(self):
        session = ChromeSession(12345, 9222, Path("/tmp/example-profile"), "/devtools/browser/local-test-1234")
        with patch("rush_phone.cli.open_chrome_session", return_value=(session, False)) as launch, \
             patch("rush_phone.cli.run_session_test") as check, patch("rush_phone.cli.setup_logging", return_value=Mock()):
            self.assertEqual(main(["chrome-login", "--config", self.config]), 0)
            launch.assert_called_once()
            check.assert_not_called()

    def test_session_test_has_no_submit_or_auto_password_flags(self):
        for flag in ("--submit-order", "--auto-login"):
            with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as captured:
                main(["session-test", "--config", self.config, flag])
            self.assertEqual(captured.exception.code, 2)

    def test_product_only_flag_is_forwarded_and_default_runs_checkout(self):
        from rush_phone.session_test import SessionTestResult
        for flags, expected in (([], False), (["--product-only"], True)):
            with self.subTest(flags=flags), \
                 patch("rush_phone.cli.run_session_test", return_value=SessionTestResult(True, "演练完成")) as check, \
                 patch("rush_phone.cli.setup_logging", return_value=Mock()):
                self.assertEqual(main(["session-test", "--config", self.config, *flags]), 0)
                self.assertIs(check.call_args.kwargs["product_only"], expected)

    def test_chrome_path_is_passed_as_one_argument_without_shell_parsing(self):
        session = ChromeSession(12345, 9222, Path("example-profile"), "/devtools/browser/local-test-1234")
        path = r"D:\Apps\Google Chrome\chrome.exe"
        with patch("rush_phone.cli.open_chrome_session", return_value=(session, False)) as launch, \
             patch("rush_phone.cli.setup_logging", return_value=Mock()):
            self.assertEqual(main(["chrome-login", "--config", self.config, "--chrome-path", path]), 0)
            self.assertEqual(launch.call_args.args[3], path)


if __name__ == "__main__":
    unittest.main()
