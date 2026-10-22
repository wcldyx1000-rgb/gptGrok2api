from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from services.account_health import evaluate_account_health, health_matches_filter
from services.account_scheduler_test import _ServiceCase
from services.account_usage import ImageUsageTracker

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _jwt(exp: datetime) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _account(token: str, **fields) -> dict:
    return {"access_token": token, "status": "正常", "quota": 50, "refresh_token": "rt", **fields}


def _evaluate(accounts, account_usage=None, pool=None, *, cooldown=0.0, concurrency=1, unlimited=lambda _a: False):
    return evaluate_account_health(
        accounts,
        {"accounts": account_usage or {}, "pool": pool or {}},
        cooldown_seconds=cooldown,
        concurrency=concurrency,
        is_unlimited=unlimited,
        now=NOW,
    )


class AccountHealthTest(unittest.TestCase):
    def test_quiet_normal_account_is_healthy(self) -> None:
        by_token, pool = _evaluate([_account("a")])
        self.assertEqual(by_token["a"]["health_level"], "healthy")
        self.assertEqual(by_token["a"]["health_reasons"], [])
        self.assertEqual(pool["level"], "healthy")

    def test_forbidden_is_danger_and_rate_limit_is_warning(self) -> None:
        by_token, _ = _evaluate(
            [_account("a"), _account("b")],
            {"a": {"forbidden_24h": 1}, "b": {"rate_limited_1h": 2}},
        )
        self.assertEqual(by_token["a"]["health_level"], "danger")
        self.assertIn("403", by_token["a"]["health_reasons"][0])
        self.assertEqual(by_token["b"]["health_level"], "warning")
        self.assertIn("429", by_token["b"]["health_reasons"][0])

    def test_consecutive_failures_and_failure_rate(self) -> None:
        by_token, _ = _evaluate(
            [_account("warn"), _account("danger"), _account("rate")],
            {
                "warn": {"consecutive_failures": 2},
                "danger": {"consecutive_failures": 3},
                "rate": {"recent_results": 10, "recent_failures": 3},
            },
        )
        self.assertEqual(by_token["warn"]["health_level"], "warning")
        self.assertEqual(by_token["danger"]["health_level"], "danger")
        self.assertEqual(by_token["rate"]["health_level"], "warning")
        self.assertIn("最近 10 次请求失败 3 次", by_token["rate"]["health_reasons"])

    def test_usage_density_relative_to_pool(self) -> None:
        accounts = [_account(name) for name in ("hot", "warm", "c", "d", "e", "f")]
        usage = {"hot": {"uses_1h": 30}, "warm": {"uses_1h": 8}, "c": {"uses_1h": 1}}
        by_token, _ = _evaluate(accounts, usage)
        # average = 39 / 6 = 6.5 -> hot is 4.6x (danger), warm is 1.2x (fine)
        self.assertEqual(by_token["hot"]["health_level"], "danger")
        self.assertEqual(by_token["warm"]["health_level"], "healthy")
        self.assertEqual(by_token["hot"]["recent_uses_1h"], 30)

    def test_density_needs_minimum_uses(self) -> None:
        by_token, _ = _evaluate([_account("a"), _account("b"), _account("c")], {"a": {"uses_1h": 3}})
        self.assertEqual(by_token["a"]["health_level"], "healthy")

    def test_low_quota_against_typical(self) -> None:
        accounts = [_account("low", quota=5), _account("b", quota=50), _account("c", quota=50)]
        by_token, _ = _evaluate(accounts)
        self.assertEqual(by_token["low"]["health_level"], "warning")
        self.assertIn("剩余额度 5", by_token["low"]["health_reasons"][0])
        unlimited, _ = _evaluate(accounts, unlimited=lambda a: a["access_token"] == "low")
        self.assertEqual(unlimited["low"]["health_level"], "healthy")

    def test_access_token_expiring_without_refresh_token(self) -> None:
        soon = _jwt(NOW + timedelta(hours=2))
        later = _jwt(NOW + timedelta(days=5))
        with_rt = _jwt(NOW + timedelta(hours=3))
        by_token, _ = _evaluate([
            {"access_token": soon, "status": "正常", "quota": 50},
            {"access_token": later, "status": "正常", "quota": 50},
            _account(with_rt),
        ])
        self.assertEqual(by_token[soon]["health_level"], "warning")
        self.assertIn("AT 将在 24 小时内过期", by_token[soon]["health_reasons"][0])
        self.assertEqual(by_token[later]["health_level"], "healthy")
        self.assertEqual(by_token[with_rt]["health_level"], "healthy")

    def test_relogin_needed_counts_refresh_token_as_unusable(self) -> None:
        token = _jwt(NOW + timedelta(hours=1))
        by_token, _ = _evaluate([_account(token, relogin_needed_at=NOW.isoformat(), relogin_attempts=1)])
        reasons = by_token[token]["health_reasons"]
        self.assertEqual(by_token[token]["health_level"], "warning")
        self.assertTrue(any("AT 将在 24 小时内过期" in reason for reason in reasons))
        self.assertTrue(any("等待自动重新登录（已尝试 1 次）" in reason for reason in reasons))

    def test_auto_paused_account_warns_with_reason(self) -> None:
        by_token, _ = _evaluate([
            _account("paused", auto_paused_until=(NOW + timedelta(hours=3)).isoformat(), auto_pause_reason="连续失败"),
            _account("expired", auto_paused_until=(NOW - timedelta(hours=1)).isoformat()),
        ])
        self.assertEqual(by_token["paused"]["health_level"], "warning")
        self.assertIn("连续失败", by_token["paused"]["health_reasons"][0])
        self.assertEqual(by_token["expired"]["health_level"], "healthy")

    def test_out_of_rotation_accounts_are_idle_and_keep_signals(self) -> None:
        by_token, pool = _evaluate(
            [_account("ok"), _account("lim", status="限流"), _account("bad", status="异常"), _account("off", status="禁用")],
            {"bad": {"forbidden_24h": 2}},
        )
        for token, reason in (("lim", "额度"), ("bad", "失效"), ("off", "停用")):
            self.assertEqual(by_token[token]["health_level"], "idle")
            self.assertIn(reason, by_token[token]["health_reasons"][0])
        self.assertIn("403", by_token["bad"]["health_reasons"][1])
        self.assertEqual(pool["metrics"]["danger_accounts"], 0)
        self.assertEqual(pool["metrics"]["ready_accounts"], 1)

    def test_custom_status_category(self) -> None:
        by_token, _ = evaluate_account_health(
            [_account("a")],
            {},
            cooldown_seconds=0,
            concurrency=1,
            is_unlimited=lambda _a: False,
            status_category=lambda _a: "disabled",
            now=NOW,
        )
        self.assertEqual(by_token["a"]["health_level"], "idle")

    def test_health_filter(self) -> None:
        self.assertTrue(health_matches_filter("danger", "all"))
        self.assertTrue(health_matches_filter("warning", "risk"))
        self.assertTrue(health_matches_filter("danger", "risk"))
        self.assertFalse(health_matches_filter("healthy", "risk"))
        self.assertTrue(health_matches_filter("idle", "idle"))
        self.assertFalse(health_matches_filter("idle", "healthy"))


class PoolHealthTest(unittest.TestCase):
    def test_no_ready_account_is_danger(self) -> None:
        _, pool = _evaluate([_account("a", status="异常")])
        self.assertEqual(pool["level"], "danger")
        self.assertEqual(pool["warnings"][0]["code"], "no_ready_account")

    def test_high_utilization_suggests_more_accounts(self) -> None:
        # concurrency 1, hold 60s -> 60 requests/hour/account; 2 accounts -> capacity 120.
        _, pool = _evaluate(
            [_account("a", quota=0, image_quota_unknown=True), _account("b", quota=0, image_quota_unknown=True)],
            pool={"attempts_1h": 100, "avg_hold_seconds": 60},
        )
        codes = [warning["code"] for warning in pool["warnings"]]
        self.assertIn("high_utilization", codes)
        # needed = ceil(100 / (60 * 0.7)) = 3 -> 1 more account.
        self.assertEqual(pool["suggested_additional_accounts"], 1)
        self.assertEqual(pool["metrics"]["capacity_1h"], 120)
        self.assertEqual(pool["level"], "warning")

    def test_cooldown_shrinks_capacity(self) -> None:
        _, pool = _evaluate([_account("a")], pool={"attempts_1h": 30, "avg_hold_seconds": 60}, cooldown=60, concurrency=3)
        # one request per 120s -> 30/hour; 30 demand needs ceil(30/21)=2 accounts.
        self.assertEqual(pool["metrics"]["capacity_1h"], 30)
        self.assertEqual(pool["suggested_additional_accounts"], 1)

    def test_timeouts_waits_and_unavailable(self) -> None:
        _, pool = _evaluate(
            [_account("a")],
            pool={"attempts_1h": 1, "timeouts_1h": 2, "waits_1h": 4, "avg_wait_seconds": 15, "unavailable_1h": 1},
        )
        codes = {warning["code"] for warning in pool["warnings"]}
        self.assertTrue({"selection_timeout", "selection_wait", "selection_unavailable"} <= codes)
        self.assertEqual(pool["level"], "danger")

    def test_short_waits_do_not_warn(self) -> None:
        _, pool = _evaluate([_account("a")], pool={"waits_1h": 4, "avg_wait_seconds": 2})
        self.assertNotIn("selection_wait", {warning["code"] for warning in pool["warnings"]})

    def test_quota_running_out(self) -> None:
        _, pool = _evaluate(
            [_account("a", quota=10), _account("b", quota=10)],
            pool={"attempts_1h": 10, "avg_hold_seconds": 1},
        )
        self.assertIn("quota_running_out", {warning["code"] for warning in pool["warnings"]})
        _, unlimited_pool = _evaluate(
            [_account("a", quota=10)],
            pool={"attempts_1h": 10, "avg_hold_seconds": 1},
            unlimited=lambda _a: True,
        )
        self.assertNotIn("quota_running_out", {warning["code"] for warning in unlimited_pool["warnings"]})

    def test_batch_invalidation(self) -> None:
        recent = (NOW - timedelta(hours=2)).isoformat()
        old = (NOW - timedelta(days=3)).isoformat()
        accounts = [_account(f"ok{i}") for i in range(8)] + [
            _account("x", status="异常", last_invalid_at=recent),
            _account("y", status="异常", last_invalid_at=recent),
            _account("z", status="异常", last_invalid_at=old),
        ]
        _, pool = _evaluate(accounts)
        warning = next(item for item in pool["warnings"] if item["code"] == "batch_invalidated")
        self.assertIn("2 个账号失效", warning["message"])

    def test_risky_accounts_roll_up(self) -> None:
        _, pool = _evaluate([_account("a"), _account("b")], {"a": {"consecutive_failures": 2}})
        self.assertEqual(pool["warnings"][-1]["code"], "warning_accounts")
        self.assertEqual(pool["level"], "warning")
        _, pool = _evaluate([_account("a"), _account("b")], {"a": {"forbidden_24h": 1}})
        self.assertEqual(pool["warnings"][-1]["code"], "danger_accounts")
        self.assertEqual(pool["level"], "danger")


class ImageUsageTrackerTest(unittest.TestCase):
    def test_windows_and_consecutive_failures(self) -> None:
        now = [1000.0]
        tracker = ImageUsageTracker(clock=lambda: now[0])
        tracker.record_lease("a")
        now[0] += 30
        held = tracker.record_release("a")
        tracker.record_attempt("a", success=False, account_failure=True, status_code=403, held_seconds=held)
        tracker.record_attempt("a", success=False, account_failure=True, failure_code="upstream_rate_limited")
        tracker.record_attempt("a", success=False, account_failure=False, failure_code="upstream_error_reply")
        stats = tracker.account_stats("a")
        self.assertEqual(held, 30)
        self.assertEqual(stats["uses_1h"], 3)
        self.assertEqual(stats["consecutive_failures"], 2)
        self.assertEqual(stats["forbidden_24h"], 1)
        self.assertEqual(stats["rate_limited_1h"], 1)
        tracker.record_attempt("a", success=True, account_failure=False)
        self.assertEqual(tracker.account_stats("a")["consecutive_failures"], 0)
        now[0] += 3601
        stats = tracker.account_stats("a")
        self.assertEqual((stats["uses_1h"], stats["uses_24h"], stats["forbidden_24h"]), (0, 4, 1))
        self.assertEqual(tracker.pool_stats()["attempts_1h"], 0)


class AccountServiceUsageTest(_ServiceCase):
    def test_mark_image_result_records_usage_and_snapshot(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        token = service._acquire_next_candidate_token()
        service.mark_image_result(token, False, failure_code="upstream_rate_limited", status_code=429)
        token = service._acquire_next_candidate_token()
        service.mark_image_result(token, True)
        service.penalize_image_account("tok-a", reason="upstream_error_reply")
        snapshot = service.image_usage_snapshot()
        self.assertEqual(set(snapshot["accounts"]), {"tok-a", "tok-b"})
        total_uses = sum(item["uses_1h"] for item in snapshot["accounts"].values())
        self.assertEqual(total_uses, 3)
        self.assertEqual(sum(item["rate_limited_1h"] for item in snapshot["accounts"].values()), 1)
        self.assertEqual(snapshot["pool"]["attempts_1h"], 3)
        self.assertIsNotNone(snapshot["pool"]["avg_hold_seconds"])

    def test_unavailable_selection_is_counted(self) -> None:
        from services.account_service import ImageAccountSelectionError

        service = self.make_service([_account("tok-a", status="异常")])
        with self.assertRaises(ImageAccountSelectionError):
            service._acquire_next_candidate_token()
        self.assertEqual(service.image_usage_snapshot()["pool"]["unavailable_1h"], 1)


if __name__ == "__main__":
    unittest.main()
