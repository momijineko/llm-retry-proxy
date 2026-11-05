import asyncio
import unittest
from unittest.mock import patch

from retry_proxy.sync_adapters.base import (DEFAULT_WRITE_DELAY, MAX_WRITE_DELAY,
                                            WriteThrottle, is_rate_limited, write_delay)


class RateLimitDetectionTests(unittest.TestCase):
    def test_common_upstream_limit_messages_are_recognized(self):
        for message in ("HTTP 429", "rate limit exceeded", "Too Many Requests",
                        "requests too frequent", "frequency limit", "上游限流"):
            with self.subTest(message=message):
                self.assertTrue(is_rate_limited(RuntimeError(message)))

    def test_other_errors_are_not_mistaken_for_limits(self):
        for message in ("HTTP 500", "connection reset", "invalid key", ""):
            with self.subTest(message=message):
                self.assertFalse(is_rate_limited(RuntimeError(message)))


class WriteDelayTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        self.assertEqual(write_delay({}), DEFAULT_WRITE_DELAY)
        self.assertEqual(write_delay(None), DEFAULT_WRITE_DELAY)
        self.assertEqual(write_delay({}, default=3.0), 3.0)
        self.assertEqual(write_delay({"delay_seconds": 2.5}), 2.5)
        # options 优先于 default
        self.assertEqual(write_delay({"delay_seconds": 0.5}, default=9.0), 0.5)

    def test_invalid_and_out_of_range_values_are_clamped(self):
        self.assertEqual(write_delay({"delay_seconds": -5}), 0.0)
        self.assertEqual(write_delay({"delay_seconds": 9999}), MAX_WRITE_DELAY)
        self.assertEqual(write_delay({"delay_seconds": "bad"}), DEFAULT_WRITE_DELAY)
        self.assertEqual(write_delay({"delay_seconds": None}, default=2.0), 2.0)


class WriteThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_request_is_not_delayed(self):
        throttle = WriteThrottle(1.5)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", fake_sleep):
            await throttle.before()
            self.assertEqual(sleeps, [])
            await throttle.before()
            self.assertEqual(sleeps, [1.5])
            await throttle.before()
            self.assertEqual(sleeps, [1.5, 1.5])

    async def test_zero_delay_never_sleeps(self):
        throttle = WriteThrottle(0)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", fake_sleep):
            for _ in range(5):
                await throttle.before()
        self.assertEqual(sleeps, [])

    async def test_rate_limit_backs_off_exponentially(self):
        throttle = WriteThrottle(1.0)
        self.assertEqual(throttle.delay, 1.0)

        throttle.on_rate_limited()
        self.assertEqual(throttle.delay, 2.0)
        throttle.on_rate_limited()
        self.assertEqual(throttle.delay, 4.0)
        self.assertEqual(throttle.rate_limited, 2)

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", fake_sleep):
            await throttle.before()   # 第一次不等待
            await throttle.before()
        self.assertEqual(sleeps, [4.0])

    async def test_backoff_is_capped(self):
        throttle = WriteThrottle(10.0, max_delay=15.0)
        throttle.on_rate_limited()
        self.assertEqual(throttle.delay, 15.0)
        throttle.on_rate_limited()
        self.assertEqual(throttle.delay, 15.0)

    async def test_success_relaxes_back_toward_base(self):
        throttle = WriteThrottle(1.0)
        throttle.on_rate_limited()   # 2.0
        throttle.on_rate_limited()   # 4.0
        throttle.on_success()
        self.assertEqual(throttle.delay, 2.0)
        throttle.on_success()
        self.assertEqual(throttle.delay, 1.0)
        # 不会降到 base 以下
        throttle.on_success()
        self.assertEqual(throttle.delay, 1.0)

    async def test_backoff_base_zero_still_grows_from_zero(self):
        # delay=0 时命中限流也应能退避（用默认基准）
        throttle = WriteThrottle(0.0)
        throttle.on_rate_limited()
        self.assertGreater(throttle.delay, 0.0)


if __name__ == "__main__":
    unittest.main()
