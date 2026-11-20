import time
import unittest
from datetime import datetime, timedelta, timezone

from services.account_memory_service import MEMORY_RETRY_SECS, AccountMemoryService, memory_due


class _FakeAccounts:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = {item["access_token"]: dict(item) for item in accounts}

    def list_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts.values()]

    def update_account(self, token: str, updates: dict, quiet: bool = False) -> dict:
        self.accounts[token].update(updates)
        return dict(self.accounts[token])


class MemoryDueTests(unittest.TestCase):
    def test_due_rules(self) -> None:
        self.assertTrue(memory_due({"access_token": "t", "status": "正常"}))
        self.assertTrue(memory_due({"access_token": "t", "status": "限流"}))
        self.assertFalse(memory_due({"access_token": "t", "memory_disabled_at": "2026-11-20T00:00:00+00:00"}))
        self.assertFalse(memory_due({"access_token": "t", "status": "禁用"}))
        self.assertFalse(memory_due({"access_token": "t", "status": "异常"}))
        self.assertFalse(memory_due({"access_token": ""}))

    def test_failed_account_backs_off(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(seconds=MEMORY_RETRY_SECS + 60)).isoformat()
        self.assertFalse(memory_due({"access_token": "t", "memory_check_error_at": recent}, time.time()))
        self.assertTrue(memory_due({"access_token": "t", "memory_check_error_at": old}, time.time()))


class MemoryServiceTests(unittest.TestCase):
    def test_run_due_records_success_and_failure(self) -> None:
        accounts = _FakeAccounts([
            {"access_token": "ok", "status": "正常"},
            {"access_token": "bad", "status": "正常"},
            {"access_token": "done", "status": "正常", "memory_disabled_at": "2026-11-01T00:00:00+00:00"},
        ])
        calls: list[str] = []

        def disable(token: str) -> dict:
            calls.append(token)
            if token == "bad":
                raise RuntimeError("HTTP 401")
            return {"was_enabled": True, "cleared": 2}

        service = AccountMemoryService(disable_fn=disable, accounts=accounts, sleep_fn=lambda _s: None)
        self.assertEqual(service.run_due(), 1)
        self.assertEqual(sorted(calls), ["bad", "ok"])
        ok = accounts.accounts["ok"]
        self.assertTrue(ok["memory_disabled_at"])
        self.assertEqual(ok["memory_cleared"], 2)
        self.assertIsNone(ok["memory_check_error"])
        bad = accounts.accounts["bad"]
        self.assertNotIn("memory_disabled_at", bad)
        self.assertIn("401", bad["memory_check_error"])
        # 失败的账号在退避期内不会马上重试，成功的不再处理。
        calls.clear()
        self.assertEqual(service.run_due(), 0)
        self.assertEqual(calls, [])

    def test_batch_limit(self) -> None:
        accounts = _FakeAccounts([{"access_token": f"t{i}", "status": "正常"} for i in range(5)])
        service = AccountMemoryService(
            disable_fn=lambda _t: {"cleared": 0}, accounts=accounts, sleep_fn=lambda _s: None,
        )
        self.assertEqual(service.run_due(limit=2), 2)
        self.assertEqual(service.run_due(limit=10), 3)


if __name__ == "__main__":
    unittest.main()
