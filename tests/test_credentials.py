import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rush_phone.config import AccountConfig, ConfigError
from rush_phone.credentials import load_credentials


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.account = AccountConfig("account@example.test", self.root / "profile")
        self.env = self.root / ".env"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_reads_literal_password_and_excludes_it_from_repr(self):
        self.env.write_text("PASSWORD=literal$secret#value\n", encoding="utf-8")
        credentials = load_credentials(self.account, self.env)
        self.assertEqual(credentials.username, "account@example.test")
        self.assertEqual(credentials.password, "literal$secret#value")
        self.assertNotIn(credentials.password, repr(credentials))

    def test_quoted_password_and_custom_account_mapping(self):
        self.env.write_text('export APPLE_PASSWORD="a b # $literal" # comment\n', encoding="utf-8")
        account = AccountConfig("账号1", self.root, data={"username":"account@example.test", "password_env":"APPLE_PASSWORD"})
        credentials = load_credentials(account, self.env)
        self.assertEqual(credentials.username, "account@example.test")
        self.assertEqual(credentials.password, "a b # $literal")

    def test_invalid_or_empty_password_does_not_disclose_value(self):
        for contents in ("PASSWORD=\n", "OTHER=private-value\n", 'PASSWORD="private-value\n'):
            with self.subTest(contents=bool(contents)):
                self.env.write_text(contents, encoding="utf-8")
                with self.assertRaises(ConfigError) as captured:
                    load_credentials(self.account, self.env)
                self.assertNotIn("private-value", str(captured.exception))

    def test_missing_file_is_a_safe_configuration_error(self):
        with self.assertRaisesRegex(ConfigError, "无法读取 .env"):
            load_credentials(self.account, self.env)

    def test_browser_debug_logging_is_rejected_before_credentials_are_read(self):
        with patch.dict(os.environ, {"DEBUG":"pw:api"}):
            with self.assertRaisesRegex(ConfigError, "DEBUG/PWDEBUG"):
                load_credentials(self.account, self.env)


if __name__ == "__main__":
    unittest.main()
