import logging
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from rush_phone.config import AccountConfig, load_config
from rush_phone.limiter import OrderLimiter
from rush_phone.worker import PurchaseWorker


class HttpErrorTests(unittest.TestCase):
    def make_worker(self, directory: str) -> PurchaseWorker:
        config = load_config(Path(__file__).parent.parent / "config.example.toml")
        config = replace(config, target=replace(config.target, product_url="https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro"))
        return PurchaseWorker(
            config, AccountConfig("测试", Path(directory)), logging.getLogger("test"),
            OrderLimiter(1), threading.Event(), False, Path(directory), threading.Event(),
        )

    def test_navigation_http_error_is_explicit_and_does_not_log_query_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(__file__).parent.parent / "config.example.toml")
            worker = PurchaseWorker(
                config, AccountConfig("测试", Path(directory)), logging.getLogger("test"),
                OrderLimiter(1), threading.Event(), False, Path(directory), threading.Event(),
            )
            page = Mock()
            response = Mock()
            response.status = 541
            response.url = "https://example.test/product?atbtoken=private-token#detail"
            response.request.resource_type = "document"
            response.request.frame = page.main_frame
            worker._record_document_response(page, response)
            with self.assertRaisesRegex(RuntimeError, "HTTP 541") as captured:
                worker._ensure_document_ok()
            self.assertNotIn("private-token", str(captured.exception))
            self.assertNotIn("atbtoken", str(captured.exception))

    def test_fulfillment_error_is_not_reported_as_out_of_stock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = self.make_worker(directory)
            page = Mock()
            response = Mock()
            response.status = 541
            response.url = "https://www.apple.com.cn/shop/fulfillment-messages?parts.0=example&token=private"
            response.request.resource_type = "fetch"
            response.request.frame = page.main_frame
            worker._record_document_response(page, response)
            with self.assertRaisesRegex(RuntimeError, "配送查询返回 HTTP 541") as captured:
                worker._ensure_document_ok()
            self.assertNotIn("private", str(captured.exception))
            self.assertIn("不是缺货", str(captured.exception))

    def test_unrelated_asset_and_third_party_errors_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = self.make_worker(directory)
            page = Mock()
            for url, resource_type in (
                ("https://www.apple.com.cn/shop/image.png", "image"),
                ("https://example.test/shop/fulfillment-messages", "fetch"),
                ("https://www.apple.com.cn/shop/analytics", "xhr"),
            ):
                response = Mock()
                response.status, response.url = 541, url
                response.request.resource_type, response.request.frame = resource_type, page.main_frame
                worker._record_document_response(page, response)
                worker._ensure_document_ok()

    def test_a_new_inventory_check_does_not_reuse_an_old_response_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = self.make_worker(directory)
            worker._critical_response = 541, "https://www.apple.com.cn/shop/fulfillment-messages"
            page = Mock()
            response = page.goto.return_value
            response.status = 200
            response.url = worker.config.target.product_url
            response.request.resource_type, response.request.frame = "document", page.main_frame
            worker._handle_challenge = Mock()
            page.locator.return_value.first.text_content.return_value = "立即购买"
            self.assertTrue(worker._check_stock(page, TimeoutError))
            self.assertIsNone(worker._critical_response)


if __name__ == "__main__":
    unittest.main()
