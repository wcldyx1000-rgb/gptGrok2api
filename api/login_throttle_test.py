from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system
from services.login_throttle import LoginThrottle, login_throttle


class LoginThrottleUnitTest(unittest.TestCase):
    def test_blocks_after_max_failures_and_expires_with_window(self) -> None:
        now = [1000.0]
        throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=lambda: now[0])
        for _ in range(2):
            throttle.record_failure("1.1.1.1")
        self.assertEqual(throttle.retry_after("1.1.1.1"), 0)
        throttle.record_failure("1.1.1.1")
        self.assertGreater(throttle.retry_after("1.1.1.1"), 0)
        self.assertLessEqual(throttle.retry_after("1.1.1.1"), 61)
        self.assertEqual(throttle.retry_after("2.2.2.2"), 0)
        now[0] += 61
        self.assertEqual(throttle.retry_after("1.1.1.1"), 0)

    def test_clear_resets_counter_and_memory_is_bounded(self) -> None:
        throttle = LoginThrottle(max_failures=2, window_seconds=60)
        throttle.record_failure("a")
        throttle.clear("a")
        throttle.record_failure("a")
        self.assertEqual(throttle.retry_after("a"), 0)
        for index in range(5000):
            throttle.record_failure(f"ip-{index}")
        self.assertLessEqual(len(throttle._failures), 4096)


class LoginRouteThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        login_throttle.reset()
        self.addCleanup(login_throttle.reset)
        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        self.client = TestClient(app)

    def _patch_identity(self):
        def fake(authorization):
            if authorization == "Bearer good":
                return {"id": "admin", "name": "管理员", "role": "admin"}
            raise system.HTTPException(status_code=401, detail={"error": "bad"})

        return patch.object(system, "require_identity", side_effect=fake)

    def test_repeated_bad_keys_get_429_with_retry_after_and_success_clears(self) -> None:
        with self._patch_identity(), patch.dict("os.environ", {"CHATGPT2API_LOGIN_MAX_FAILURES": "3"}):
            for _ in range(3):
                self.assertEqual(
                    self.client.post("/auth/login", headers={"Authorization": "Bearer bad"}).status_code, 401
                )
            blocked = self.client.post("/auth/login", headers={"Authorization": "Bearer good"})
            self.assertEqual(blocked.status_code, 429)
            self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
            self.assertEqual(blocked.json()["detail"]["code"], "too_many_login_attempts")

    def test_success_resets_failure_count(self) -> None:
        with self._patch_identity(), patch.dict("os.environ", {"CHATGPT2API_LOGIN_MAX_FAILURES": "3"}):
            for _ in range(2):
                self.client.post("/auth/login", headers={"Authorization": "Bearer bad"})
            self.assertEqual(self.client.post("/auth/login", headers={"Authorization": "Bearer good"}).status_code, 200)
            for _ in range(2):
                self.assertEqual(self.client.post("/auth/login", headers={"Authorization": "Bearer bad"}).status_code, 401)

    def test_anonymous_status_probe_does_not_count_and_returns_unauthenticated(self) -> None:
        with self._patch_identity(), patch.dict("os.environ", {"CHATGPT2API_LOGIN_MAX_FAILURES": "2"}):
            for _ in range(5):
                body = self.client.get("/auth/status").json()
                self.assertFalse(body["authenticated"])
            self.assertEqual(self.client.get("/auth/status", headers={"Authorization": "Bearer good"}).status_code, 200)

    def test_bad_key_status_probe_counts_towards_throttle(self) -> None:
        with self._patch_identity(), patch.dict("os.environ", {"CHATGPT2API_LOGIN_MAX_FAILURES": "2"}):
            for _ in range(2):
                self.client.get("/auth/status", headers={"Authorization": "Bearer bad"})
            self.assertEqual(
                self.client.get("/auth/status", headers={"Authorization": "Bearer bad"}).status_code, 429
            )


if __name__ == "__main__":
    unittest.main()
