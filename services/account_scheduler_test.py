import random
import threading
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.account_scheduler import WeightedFairScheduler
from services.account_service import AccountService, ImageAccountSelectionError
from services.config import config


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _iso(seconds_from_now: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


def _account(token: str, quota: int = 10, **extra) -> dict:
    item = {
        "access_token": token,
        "status": "正常",
        "quota": quota,
        "image_quota_unknown": False,
        "email": f"{token}@example.test",
    }
    item.update(extra)
    return item


def _max_run(picks: list[str]) -> int:
    longest = run = 0
    previous = None
    for pick in picks:
        run = run + 1 if pick == previous else 1
        longest = max(longest, run)
        previous = pick
    return longest


class _ServiceCase(unittest.TestCase):
    concurrency = 3

    def setUp(self) -> None:
        patcher = patch.dict(config.data, {
            "image_account_cooldown_secs": 0,
            "image_preflight_fresh_secs": 0,
            "image_account_concurrency": self.concurrency,
            "account_auto_pause_strikes": 3,
            "account_auto_pause_hours": 6,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        log_patch = patch("services.account_service.log_service")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        warm_patch = patch.object(AccountService, "_warm_preferred_tokens", staticmethod(lambda tokens: set()))
        warm_patch.start()
        self.addCleanup(warm_patch.stop)

    def make_service(self, accounts: list[dict]) -> AccountService:
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            service = AccountService(_MemoryStorage(accounts))
        service.refresh_access_token = lambda token, **_kwargs: token
        service.remote_calls = []

        def fetch_remote_info(token, event="fetch_remote_info", remove_invalid=None):
            service.remote_calls.append(token)
            return service.get_account(token)

        service.fetch_remote_info = fetch_remote_info
        return service

    @staticmethod
    def lease(service: AccountService, **kwargs) -> str:
        token = service.get_available_access_token(**kwargs)
        service.release_image_slot(token)
        return token


class WeightedSchedulingScenarioTest(_ServiceCase):
    def test_equal_accounts_rotate_without_bursts(self) -> None:
        service = self.make_service([_account(f"tok-{i}") for i in range(4)])
        picks = [self.lease(service) for _ in range(20)]
        self.assertEqual(set(Counter(picks).values()), {5})
        self.assertEqual(_max_run(picks), 1)

    def test_bulk_import_spreads_over_new_accounts(self) -> None:
        service = self.make_service([_account(f"old-{i}") for i in range(5)])
        for _ in range(50):
            self.lease(service)
        service.add_account_items([_account(f"new-{i}") for i in range(20)], False)
        picks = [self.lease(service) for _ in range(50)]
        self.assertEqual(set(Counter(picks).values()), {2})
        self.assertEqual(_max_run(picks), 1)

    def test_weight_follows_remaining_quota(self) -> None:
        service = self.make_service([_account("big", 30), _account("small", 10)])
        counts = Counter(self.lease(service) for _ in range(40))
        self.assertEqual(counts, {"big": 30, "small": 10})

    def test_one_huge_quota_cannot_absorb_the_pool(self) -> None:
        service = self.make_service([_account("huge", 1000)] + [_account(f"tok-{i}", 10) for i in range(9)])
        counts = Counter(self.lease(service) for _ in range(260))
        self.assertAlmostEqual(counts["huge"] / 260, 4 / 13, delta=0.02)

    def test_drained_account_ranks_last(self) -> None:
        service = self.make_service([
            _account("drained", 0, image_quota_unknown=True, last_quota_estimated_empty_at=_iso()),
            _account("tok-a"),
            _account("tok-b"),
        ])
        picks = [self.lease(service) for _ in range(21)]
        self.assertLessEqual(picks.count("drained"), 1)

    def test_limited_account_returns_without_burst(self) -> None:
        service = self.make_service([_account(f"tok-{i}") for i in range(5)])
        for _ in range(10):
            self.lease(service)
        service.update_account("tok-0", {"status": "限流"})
        for _ in range(40):
            self.assertNotEqual(self.lease(service), "tok-0")
        service.update_account("tok-0", {"status": "正常", "quota": 10, "image_quota_unknown": False})
        picks = [self.lease(service) for _ in range(10)]
        self.assertEqual(picks.count("tok-0"), 2)
        self.assertEqual(_max_run(picks), 1)

    def test_delete_account_prunes_scheduler(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        leased = service.get_available_access_token()
        service.delete_accounts([leased], return_items=False)
        service.release_image_slot(leased)
        remaining = {"tok-a", "tok-b"} - {leased}
        self.assertEqual({self.lease(service) for _ in range(4)}, remaining)
        self.assertNotIn(leased, service._image_inflight)
        self.assertLessEqual(len(service._image_scheduler), 1)

    def test_delete_all_accounts_reports_unavailable(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        self.lease(service)
        service.delete_accounts(["tok-a", "tok-b"], return_items=False)
        with self.assertRaises(ImageAccountSelectionError) as info:
            service.get_available_access_token()
        self.assertEqual(info.exception.kind, "unavailable")
        self.assertEqual(service.get_text_access_token(), "")

    def test_token_rotation_keeps_rotation_place(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        self.assertEqual(self.lease(service), "tok-a")
        service._apply_refreshed_tokens("tok-a", {"access_token": "tok-a2"}, "test")
        self.assertEqual(self.lease(service), "tok-b")
        self.assertEqual(self.lease(service), "tok-a2")


class ExclusionAndPenaltyTest(_ServiceCase):
    def test_excluded_tokens_are_never_picked_and_keep_credit(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b"), _account("tok-c")])
        for _ in range(9):
            self.assertNotEqual(self.lease(service, excluded_tokens={"tok-a"}), "tok-a")
        self.assertEqual(self.lease(service), "tok-a")

    def test_only_excluded_accounts_left_raises(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        with self.assertRaises(ImageAccountSelectionError) as info:
            service.get_available_access_token(excluded_tokens={"tok-a", "tok-b"})
        self.assertEqual(info.exception.kind, "unavailable")
        self.assertEqual(service.remote_calls, [])

    def test_only_excluded_left_when_rest_limited_reports_quota(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b", status="限流")])
        with self.assertRaises(ImageAccountSelectionError) as info:
            service.get_available_access_token(excluded_tokens={"tok-a"})
        self.assertEqual(info.exception.kind, "quota_exhausted")

    def test_penalize_defers_account_without_counting_failure(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b"), _account("tok-c")])
        for _ in range(3):
            self.lease(service)
        service.penalize_image_account("tok-a", reason="upstream_error_reply")
        picks = [self.lease(service) for _ in range(4)]
        self.assertNotIn("tok-a", picks[:2])
        account = service.get_account("tok-a")
        self.assertEqual(int(account.get("fail") or 0), 0)
        self.assertNotIn("last_image_failure_at", account)

    def test_failed_image_is_deprioritized(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b"), _account("tok-c")])
        token = service.get_available_access_token()
        service.mark_image_result(token, False)
        picks = [self.lease(service) for _ in range(2)]
        self.assertNotIn(token, picks)


class AutoPauseTest(_ServiceCase):
    def fail_run(self, service: AccountService, token: str, times: int = 3) -> None:
        for _ in range(times):
            service._image_inflight[token] = service._image_inflight.get(token, 0) + 1
            service.mark_image_result(token, False)

    def test_three_failure_runs_pause_the_account(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        self.fail_run(service, "tok-a")
        self.fail_run(service, "tok-a")
        self.assertFalse(service.get_account("tok-a").get("auto_paused_until"))
        self.assertEqual(len(service.get_account("tok-a")["failure_strikes"]), 2)
        self.fail_run(service, "tok-a")
        account = service.get_account("tok-a")
        self.assertTrue(account["auto_paused_until"])
        self.assertIn("自动暂停", account["auto_pause_reason"])
        self.assertEqual({self.lease(service) for _ in range(6)}, {"tok-b"})

    def test_success_resets_streak(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        self.fail_run(service, "tok-a", 2)
        service._image_inflight["tok-a"] = 1
        service.mark_image_result("tok-a", True)
        self.fail_run(service, "tok-a", 2)
        self.assertFalse(service.get_account("tok-a").get("failure_strikes"))

    def test_paused_account_still_serves_when_nothing_else_can(self) -> None:
        service = self.make_service([_account("tok-a", auto_paused_until=_iso(3600)), _account("tok-b", status="禁用")])
        self.assertEqual(self.lease(service), "tok-a")

    def test_success_while_paused_clears_pause(self) -> None:
        service = self.make_service([_account("tok-a", auto_paused_until=_iso(3600), failure_strikes=[_iso()])])
        token = service.get_available_access_token()
        service.mark_image_result(token, True)
        account = service.get_account("tok-a")
        self.assertNotIn("auto_paused_until", account)
        self.assertNotIn("failure_strikes", account)

    def test_expired_pause_is_ignored(self) -> None:
        service = self.make_service([_account("tok-a", auto_paused_until=_iso(-60)), _account("tok-b")])
        self.assertEqual(Counter(self.lease(service) for _ in range(4)), {"tok-a": 2, "tok-b": 2})

    def test_disabled_setting_never_pauses(self) -> None:
        config.data["account_auto_pause_strikes"] = 0
        service = self.make_service([_account("tok-a")])
        for _ in range(4):
            self.fail_run(service, "tok-a")
        self.assertFalse(service.get_account("tok-a").get("auto_paused_until"))

    def test_text_selection_prefers_unpaused(self) -> None:
        service = self.make_service([_account("tok-a", auto_paused_until=_iso(3600)), _account("tok-b")])
        self.assertEqual({service.get_text_access_token() for _ in range(4)}, {"tok-b"})


class TextImageSeparationTest(_ServiceCase):
    def test_text_rotation_is_independent_of_image_rotation(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        for _ in range(3):
            self.lease(service)
        text_picks = [service.get_text_access_token() for _ in range(4)]
        self.assertEqual(text_picks, ["tok-a", "tok-b", "tok-a", "tok-b"])

    def test_text_excluded_tokens(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        self.assertEqual({service.get_text_access_token({"tok-a"}) for _ in range(3)}, {"tok-b"})

    def test_text_uses_limited_but_not_abnormal_accounts(self) -> None:
        service = self.make_service([_account("tok-a", status="限流"), _account("tok-b", status="异常")])
        self.assertEqual(service.get_text_access_token(), "tok-a")


class ConcurrencyTest(_ServiceCase):
    concurrency = 1

    def test_high_concurrency_respects_cap_and_stays_fair(self) -> None:
        service = self.make_service([_account(f"tok-{i}") for i in range(8)])
        active: Counter = Counter()
        peak: Counter = Counter()
        guard = threading.Lock()
        served: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(16)

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            barrier.wait()
            for _ in range(10):
                try:
                    token = service.get_available_access_token()
                except BaseException as exc:  # pragma: no cover - failure detail
                    errors.append(exc)
                    return
                with guard:
                    active[token] += 1
                    peak[token] = max(peak[token], active[token])
                    served.append(token)
                time.sleep(rng.uniform(0, 0.002))
                with guard:
                    active[token] -= 1
                service.release_image_slot(token)

        threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(served), 160)
        self.assertEqual(max(peak.values()), 1)
        self.assertEqual(service._image_inflight, {})
        counts = Counter(served)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 12)

    def test_idle_image_tokens_excludes_busy_and_abnormal(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b"), _account("tok-c", status="限流")])
        busy = service.get_available_access_token()
        idle = service.idle_image_tokens()
        self.assertNotIn(busy, idle)
        self.assertNotIn("tok-c", idle)
        self.assertEqual(len(idle), 1)


class WarmPreferenceTest(unittest.TestCase):
    def test_preferred_wins_near_ties_only(self) -> None:
        scheduler = WeightedFairScheduler()
        members = ["a", "b", "c"]
        first = scheduler.pick([(key, 1.0, 0) for key in members], members=members, preferred={"c"})
        self.assertEqual(first, "c")
        # c is now one stride ahead; the bonus only brings it level, so a/b go first.
        second = scheduler.pick([(key, 1.0, 0) for key in members], members=members, preferred={"c"})
        self.assertEqual(second, "a")

    def test_long_run_keeps_proportions(self) -> None:
        scheduler = WeightedFairScheduler()
        weights = {"a": 40.0, "b": 10.0, "c": 3.0, "d": 0.5}
        picks: Counter = Counter()
        rounds = 50_000
        for _ in range(rounds):
            picks[scheduler.pick([(key, weight, 0) for key, weight in weights.items()], members=weights)] += 1
        total = sum(weights.values())
        for key, weight in weights.items():
            self.assertAlmostEqual(picks[key] / rounds, weight / total, delta=weight / total * 0.02)


class ReloginInterfaceTest(_ServiceCase):
    def test_terminal_refresh_error_flags_relogin(self) -> None:
        service = self.make_service([_account("tok-a", refresh_token="rt-a")])
        del service.refresh_access_token
        with patch.object(AccountService, "_token_needs_refresh", return_value=True), \
                patch.object(AccountService, "_request_access_token_refresh",
                             side_effect=RuntimeError("oauth_refresh_http_400: invalid_grant")):
            service.refresh_access_token("tok-a", force=True)
        self.assertEqual([item["access_token"] for item in service.list_relogin_needed_accounts()], ["tok-a"])

    def test_transient_refresh_error_does_not_flag(self) -> None:
        service = self.make_service([_account("tok-a", refresh_token="rt-a")])
        del service.refresh_access_token
        with patch.object(AccountService, "_token_needs_refresh", return_value=True), \
                patch.object(AccountService, "_request_access_token_refresh",
                             side_effect=RuntimeError("oauth_refresh_http_502")):
            service.refresh_access_token("tok-a", force=True)
        self.assertEqual(service.list_relogin_needed_accounts(), [])

    def test_apply_relogin_tokens_restores_account(self) -> None:
        service = self.make_service([_account("tok-a", status="异常", auto_paused_until=_iso(3600))])
        service.mark_account_relogin_needed("tok-a", "invalid_grant")
        service.record_relogin_failure("tok-a", "otp timeout")
        self.assertEqual(service.get_account("tok-a")["relogin_attempts"], 1)
        new_token = service.apply_relogin_tokens("tok-a", {"access_token": "tok-new", "refresh_token": "rt-new"})
        self.assertEqual(new_token, "tok-new")
        account = service.get_account("tok-new")
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["refresh_token"], "rt-new")
        self.assertNotIn("relogin_needed_at", account)
        self.assertNotIn("auto_paused_until", account)
        self.assertEqual(account["relogin_attempts"], 0)
        self.assertEqual(service.list_relogin_needed_accounts(), [])
        self.assertEqual(service.resolve_access_token("tok-a"), "tok-new")

    def test_terminal_refresh_error_wakes_relogin_service(self) -> None:
        service = self.make_service([_account("tok-a", refresh_token="rt-a")])
        del service.refresh_access_token
        with patch.object(AccountService, "_token_needs_refresh", return_value=True), \
                patch.object(AccountService, "_request_access_token_refresh",
                             side_effect=RuntimeError("invalid_grant")), \
                patch("services.account_relogin_service.account_relogin_service.wake") as wake:
            service.refresh_access_token("tok-a", force=True)
        wake.assert_called_once()

    def test_invalid_token_kept_when_relogin_holds(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        holds = lambda account: account.get("access_token") == "tok-a"
        with patch("services.account_relogin_service.relogin_holds", side_effect=holds), \
                patch("services.account_relogin_service.account_relogin_service.wake") as wake:
            self.assertFalse(service.handle_invalid_token("tok-a", "test", remove=True))
            self.assertTrue(service.handle_invalid_token("tok-b", "test", remove=True))
        self.assertEqual(service.get_account("tok-a")["status"], "异常")
        self.assertIsNone(service.get_account("tok-b"))
        wake.assert_called_once()

    def test_auto_remove_cleanup_skips_relogin_candidates(self) -> None:
        service = self.make_service([_account("tok-a", status="异常"), _account("tok-b", status="异常")])
        holds = lambda account: account.get("access_token") == "tok-a"
        with patch("services.account_relogin_service.relogin_holds", side_effect=holds):
            preview = service.preview_auto_remove_accounts(remove_invalid=True, remove_rate_limited=False)
            service.cleanup_auto_remove_accounts(remove_invalid=True, remove_rate_limited=False)
        self.assertIsNotNone(service.get_account("tok-a"))
        self.assertIsNone(service.get_account("tok-b"))
        self.assertEqual(preview["invalid"], 1)


if __name__ == "__main__":
    unittest.main()
