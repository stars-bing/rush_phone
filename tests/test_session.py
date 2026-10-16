import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rush_phone.config import AccountConfig
from rush_phone.session import auth_state_path, restore_auth_state, save_auth_state


class SessionTests(unittest.TestCase):
    def test_missing_state_does_not_change_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = Mock()
            account = AccountConfig("测试", Path(directory))
            self.assertEqual(restore_auth_state(context, account), 0)
            context.add_cookies.assert_not_called()

    def test_restore_preserves_current_cookies_and_skips_expired_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account = AccountConfig("测试", Path(directory))
            base = {"domain": "example.test", "path": "/", "value": "test", "expires": -1}
            current = dict(base, name="current", value="newer")
            saved = [
                dict(base, name="current", value="older"),
                dict(base, name="session"),
                dict(base, name="expired", expires=time.time() - 100),
                dict(base, name="valid", expires=time.time() + 3600),
            ]
            auth_state_path(account).write_text(json.dumps({"cookies": saved}), encoding="utf-8")
            context = Mock()
            context.cookies.return_value = [current]
            self.assertEqual(restore_auth_state(context, account), 2)
            restored = context.add_cookies.call_args.args[0]
            self.assertEqual({cookie["name"] for cookie in restored}, {"session", "valid"})

    def test_saved_state_is_private_and_atomically_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account = AccountConfig("测试", Path(directory))
            context = Mock()

            def write_state(*, path: str) -> None:
                Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")

            context.storage_state.side_effect = write_state
            destination = save_auth_state(context, account)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(json.loads(destination.read_text())['cookies'], [])
            self.assertEqual(list(Path(directory).glob('.auth-state-*.json')), [])

    def test_windows_permission_failure_prevents_writing_credentials(self):
        from rush_phone.private_files import PrivateFileError
        with tempfile.TemporaryDirectory() as directory:
            account = AccountConfig("测试", Path(directory))
            context = Mock()
            with patch("rush_phone.session.protect_private_path", side_effect=PrivateFileError("权限失败")):
                with self.assertRaises(PrivateFileError):
                    save_auth_state(context, account)
            context.storage_state.assert_not_called()
            self.assertFalse(auth_state_path(account).exists())
            self.assertEqual(list(Path(directory).glob('.auth-state-*.json')), [])


if __name__ == "__main__":
    unittest.main()
