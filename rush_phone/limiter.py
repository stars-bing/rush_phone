from __future__ import annotations

import threading


class OrderLimiter:
    """限制最终提交中的订单数，防止多个账号同时重复下单。"""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("maximum 必须至少为 1")
        self.maximum = maximum
        self._in_flight: set[str] = set()
        self._successes: set[str] = set()
        self._condition = threading.Condition()

    def acquire(self, account: str, stop_event: threading.Event) -> bool:
        with self._condition:
            while True:
                if account in self._in_flight:
                    return True
                if len(self._successes) >= self.maximum or stop_event.is_set():
                    return False
                free_slots = self.maximum - len(self._successes) - len(self._in_flight)
                if free_slots > 0:
                    self._in_flight.add(account)
                    return True
                self._condition.wait(timeout=0.2)

    def commit(self, account: str) -> bool:
        with self._condition:
            self._in_flight.discard(account)
            self._successes.add(account)
            full = len(self._successes) >= self.maximum
            self._condition.notify_all()
            return full

    def release(self, account: str) -> None:
        with self._condition:
            self._in_flight.discard(account)
            self._condition.notify_all()

    @property
    def successes(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(sorted(self._successes))

