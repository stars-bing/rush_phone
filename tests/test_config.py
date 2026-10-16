import tempfile
import unittest
from pathlib import Path

from rush_phone.config import AccountConfig, ConfigError, load_config, render_template


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self) -> None:
        config = load_config(Path(__file__).parent.parent / "config.example.toml")
        self.assertEqual(config.run.max_successful_orders, 1)
        self.assertEqual(len(config.accounts), 2)
        self.assertEqual(sum(step.submit for step in config.steps), 1)

    def test_apple_cn_example_config_is_valid(self) -> None:
        config = load_config(
            Path(__file__).parent.parent / "config.apple-cn.example.toml"
        )
        self.assertEqual(config.target.product_url.rsplit("/", 1)[-1], "iphone-duo")
        self.assertEqual(config.run.browser_channel, "chrome")
        self.assertEqual(sum(step.submit for step in config.steps), 1)

    def test_template_renders_account_and_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account = AccountConfig("甲", Path(directory), data={"phone": "138"})
            value = render_template(
                "${account.name}-${account.data.phone}-${variables.sku}",
                account,
                {"sku": "duo"},
            )
        self.assertEqual(value, "甲-138-duo")

    def test_template_rejects_missing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account = AccountConfig("甲", Path(directory))
            with self.assertRaisesRegex(ConfigError, "模板变量不存在"):
                render_template("${account.data.phone}", account, {})


if __name__ == "__main__":
    unittest.main()
