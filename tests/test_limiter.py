import threading
import unittest

from rush_phone.limiter import OrderLimiter


class LimiterTests(unittest.TestCase):
    def test_limiter_releases_failed_slot_to_next_account(self) -> None:
        limiter = OrderLimiter(1)
        stop = threading.Event()
        self.assertTrue(limiter.acquire("账号1", stop))
        limiter.release("账号1")
        self.assertTrue(limiter.acquire("账号2", stop))
        self.assertTrue(limiter.commit("账号2"))
        self.assertEqual(limiter.successes, ("账号2",))

    def test_limiter_rejects_after_limit_is_reached(self) -> None:
        limiter = OrderLimiter(1)
        stop = threading.Event()
        self.assertTrue(limiter.acquire("账号1", stop))
        limiter.commit("账号1")
        self.assertFalse(limiter.acquire("账号2", stop))


if __name__ == "__main__":
    unittest.main()
