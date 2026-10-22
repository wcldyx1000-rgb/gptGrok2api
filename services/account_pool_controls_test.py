"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

账号池调度控制：新号冷静期/放量期、对话并发槽位、上游失败冷却、AT 失效待核验、设备 ID、操作进度。
"""
from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.account_service import AccountService
from services.config import config


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _account(token: str, **extra) -> dict:
    item = {
        "access_token": token,
        "status": "正常",
        "quota": 10,
        "image_quota_unknown": False,
        "email": f"{token}@example.test",
    }
    item.update(extra)
    return item


def _jwt(exp_offset: int) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + exp_offset})}.sig"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(config.data, {
            "image_account_cooldown_secs": 0,
            "image_preflight_fresh_secs": 0,
            "image_account_concurrency": 3,
            "text_account_concurrency": 2,
            "account_auto_pause_strikes": 3,
            "account_auto_pause_hours": 6,
            "account_failure_cooldown_secs": 300,
            "new_account_cooldown_hours": 12,
            "new_account_ramp_days": 3,
            "new_account_ramp_daily_images": 2,
            "auto_remove_invalid_accounts": False,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        log = patch("services.account_service.log_service")
        log.start()
        self.addCleanup(log.stop)

    def make_service(self, accounts: list[dict]) -> AccountService:
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            service = AccountService(_MemoryStorage(accounts))
        service.refresh_access_token = lambda token, **_kwargs: token
        return service


class SettingsDefaultsTests(unittest.TestCase):
    def test_defaults_and_clamping(self) -> None:
        with patch.dict(config.data, {}, clear=False):
            for key in ("new_account_cooldown_hours", "new_account_ramp_days", "new_account_ramp_daily_images",
                        "text_account_concurrency", "account_failure_cooldown_secs"):
                config.data.pop(key, None)
            self.assertEqual(config.new_account_cooldown_hours, 12)
            self.assertEqual(config.new_account_ramp_days, 3)
            self.assertEqual(config.new_account_ramp_daily_images, 5)
            self.assertEqual(config.text_account_concurrency, 3)
            self.assertEqual(config.account_failure_cooldown_secs, 300)
        with patch.dict(config.data, {
            "new_account_cooldown_hours": 9999, "new_account_ramp_days": -3, "new_account_ramp_daily_images": 0,
            "text_account_concurrency": 999, "account_failure_cooldown_secs": "bad",
        }):
            self.assertEqual(config.new_account_cooldown_hours, 168)
            self.assertEqual(config.new_account_ramp_days, 0)
            self.assertEqual(config.new_account_ramp_daily_images, 1)
            self.assertEqual(config.text_account_concurrency, 20)
            self.assertEqual(config.account_failure_cooldown_secs, 300)


class NewAccountStageTests(_Base):
    def test_legacy_account_without_field_is_never_new(self) -> None:
        service = self.make_service([_account("old")])
        account = service.get_account("old")
        self.assertIsNone(service.new_account_stage(account))
        self.assertTrue(service._is_image_account_available(account))
        self.assertEqual(service.get_text_access_token(), "old")

    def test_stage_boundaries(self) -> None:
        joined = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        account = {"new_account_joined_at": joined.isoformat()}
        stage = AccountService.new_account_stage
        self.assertEqual(stage(account, joined)["stage"], "cooldown")
        self.assertEqual(stage(account, joined + timedelta(hours=12) - timedelta(seconds=1))["stage"], "cooldown")
        self.assertEqual(stage(account, joined + timedelta(hours=12))["stage"], "ramp")
        ramp_end = joined + timedelta(hours=12, days=3)
        self.assertEqual(stage(account, ramp_end - timedelta(seconds=1))["stage"], "ramp")
        self.assertIsNone(stage(account, ramp_end))

    def test_zero_settings_disable_stages(self) -> None:
        account = {"new_account_joined_at": _iso(timedelta(minutes=-1))}
        with patch.dict(config.data, {"new_account_cooldown_hours": 0, "new_account_ramp_days": 0}):
            self.assertIsNone(AccountService.new_account_stage(account))

    def test_cooldown_blocks_image_and_text(self) -> None:
        service = self.make_service([
            _account("new", new_account_joined_at=_iso(timedelta(hours=-1))),
            _account("old"),
        ])
        self.assertFalse(service._is_image_account_available(service.get_account("new")))
        self.assertEqual({service.get_text_access_token() for _ in range(4)}, {"old"})
        self.assertEqual({service._acquire_next_candidate_token() for _ in range(3)}, {"old"})

    def test_ramp_daily_limit_blocks_then_resets_next_day(self) -> None:
        joined = _iso(timedelta(hours=-13))  # 已过 12 小时冷静期，处于放量期
        service = self.make_service([_account("new", new_account_joined_at=joined)])
        self.assertTrue(service._is_image_account_available(service.get_account("new")))
        for _ in range(2):  # 每日上限 2
            service.mark_image_result(service._acquire_next_candidate_token(), True)
        account = service.get_account("new")
        self.assertEqual(account["ramp_image_count"], 2)
        self.assertTrue(service.new_account_stage(account)["exhausted"])
        self.assertFalse(service._is_image_account_available(account))
        # 对话不受放量期每日图片上限影响
        self.assertEqual(service.get_text_access_token(), "new")
        # 计数日期不是今天时视为 0
        service.update_account("new", {"ramp_image_day": "2000-01-01"}, quiet=True)
        self.assertTrue(service._is_image_account_available(service.get_account("new")))

    def test_ramp_does_not_count_for_graduated_account(self) -> None:
        service = self.make_service([_account("grown", new_account_joined_at=_iso(timedelta(days=-30)))])
        service.mark_image_result(service._acquire_next_candidate_token(), True)
        self.assertNotIn("ramp_image_count", service.get_account("grown"))

    def test_registration_stamp_survives_normalization(self) -> None:
        service = self.make_service([])
        stamp = _iso()
        service.add_account_items([{"access_token": "reg", "new_account_joined_at": stamp}])
        self.assertEqual(service.get_account("reg")["new_account_joined_at"], stamp)


class TextSlotTests(_Base):
    def test_slots_cap_per_account_and_release_wakes_waiter(self) -> None:
        service = self.make_service([_account("a")])
        first = service.acquire_text_access_token(timeout_secs=0.1)
        second = service.acquire_text_access_token(timeout_secs=0.1)
        self.assertEqual((first, second), ("a", "a"))
        self.assertEqual(service.acquire_text_access_token(timeout_secs=0.05), "")  # 并发 2 已满

        got: list[str] = []
        waiter = threading.Thread(target=lambda: got.append(service.acquire_text_access_token(timeout_secs=3)))
        waiter.start()
        time.sleep(0.1)
        service.release_text_slot("a")
        waiter.join(timeout=3)
        self.assertEqual(got, ["a"])

    def test_release_is_never_negative_and_spreads_across_accounts(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        service.release_text_slot("a")
        service.release_text_slot("a")
        self.assertEqual(service._text_inflight, {})
        picks = [service.acquire_text_access_token(timeout_secs=0.1) for _ in range(4)]
        self.assertEqual(sorted(picks), ["a", "a", "b", "b"])

    def test_preferred_token_is_honoured_when_free(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        self.assertEqual(service.acquire_text_access_token("b", timeout_secs=0.1), "b")

    def test_text_slot_releases_on_exception(self) -> None:
        service = self.make_service([_account("a")])
        with self.assertRaises(RuntimeError):
            with service.text_slot() as token:
                self.assertEqual(token, "a")
                self.assertEqual(service._text_inflight, {"a": 1})
                raise RuntimeError("boom")
        self.assertEqual(service._text_inflight, {})

    def test_refresh_failure_during_acquire_releases_slot(self) -> None:
        service = self.make_service([_account("a")])

        def boom(*_a, **_k):
            raise ValueError("refresh exploded")

        service.refresh_access_token = boom
        with self.assertRaises(ValueError):
            service.acquire_text_access_token(timeout_secs=0.1)
        self.assertEqual(service._text_inflight, {})

    def test_rotation_moves_slot_to_new_token(self) -> None:
        service = self.make_service([_account("a")])
        service.refresh_access_token = lambda token, **_k: "a2"
        service._accounts["a2"] = dict(service._accounts.pop("a"), access_token="a2")
        token = service.acquire_text_access_token(preferred_token="a2", timeout_secs=0.1)
        self.assertEqual(token, "a2")
        self.assertEqual(service._text_inflight, {"a2": 1})
        service.release_text_slot("a2")
        self.assertEqual(service._text_inflight, {})

    def test_no_accounts_returns_empty_without_waiting(self) -> None:
        service = self.make_service([])
        started = time.monotonic()
        self.assertEqual(service.acquire_text_access_token(timeout_secs=5), "")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_get_text_access_token_does_not_take_a_slot(self) -> None:
        service = self.make_service([_account("a")])
        self.assertEqual(service.get_text_access_token(), "a")
        self.assertEqual(service._text_inflight, {})


class StreamTextSlotTests(_Base):
    """stream_text_deltas 必须在正常结束、抛错、调用方中途丢弃生成器时都归还槽位。"""

    def _run(self, service: AccountService, events, *, take: int | None = None, exc_expected=None):
        from services.protocol import conversation

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token

            def close(self) -> None:
                pass

        request = conversation.ConversationRequest(model="auto", messages=[{"role": "user", "content": "hi"}])
        with patch.object(conversation, "account_service", service), \
                patch.object(conversation, "OpenAIBackendAPI", FakeBackend), \
                patch.object(conversation, "conversation_events", events):
            gen = conversation.stream_text_deltas(FakeBackend("a"), request)
            collected = []
            if exc_expected:
                with self.assertRaises(exc_expected):
                    for delta in gen:
                        collected.append(delta)
            elif take is None:
                collected = list(gen)
            else:
                for _ in range(take):
                    collected.append(next(gen))
                gen.close()  # 客户端断开 -> GeneratorExit
            return collected

    def test_slot_released_after_normal_completion(self) -> None:
        service = self.make_service([_account("a")])
        deltas = self._run(service, lambda *a, **k: iter([
            {"type": "conversation.delta", "delta": "x"}, {"type": "conversation.delta", "delta": "y"},
        ]))
        self.assertEqual(deltas, ["x", "y"])
        self.assertEqual(service._text_inflight, {})

    def test_slot_released_when_stream_raises(self) -> None:
        service = self.make_service([_account("a")])

        def events(*_a, **_k):
            yield {"type": "conversation.delta", "delta": "x"}
            raise RuntimeError("upstream broke")

        self._run(service, events, exc_expected=RuntimeError)
        self.assertEqual(service._text_inflight, {})

    def test_slot_released_when_client_drops_stream(self) -> None:
        service = self.make_service([_account("a")])

        def events(*_a, **_k):
            while True:
                yield {"type": "conversation.delta", "delta": "x"}

        self._run(service, events, take=2)
        self.assertEqual(service._text_inflight, {})

    def test_all_slots_busy_fails_fast_instead_of_leaking(self) -> None:
        service = self.make_service([_account("a")])
        with patch.dict(config.data, {"text_account_concurrency": 1}):
            service._text_inflight["a"] = 1
            with patch.object(service, "acquire_text_access_token", wraps=lambda **k: ""):
                self._run(service, lambda *a, **k: iter(()), exc_expected=RuntimeError)
        self.assertEqual(service._text_inflight, {"a": 1})  # 别人的占用没被误释放

    def test_auth_rejection_queues_verification_and_switches_account(self) -> None:
        from services.protocol import conversation

        service = self.make_service([_account("a"), _account("b")])
        calls = []

        def events(backend, **_k):
            calls.append(backend.access_token)
            if backend.access_token == "a":
                raise RuntimeError("401 Unauthorized: token_invalidated")
            yield {"type": "conversation.delta", "delta": "ok"}

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token

            def close(self) -> None:
                pass

        request = conversation.ConversationRequest(model="auto", messages=[{"role": "user", "content": "hi"}])
        with patch.object(conversation, "account_service", service), \
                patch.object(conversation, "OpenAIBackendAPI", FakeBackend), \
                patch.object(conversation, "conversation_events", events), \
                patch.object(service, "_start_auth_verification", return_value=True):
            deltas = list(conversation.stream_text_deltas(FakeBackend("a"), request))
        self.assertEqual(deltas, ["ok"])
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(service.get_account("a")["last_remote_check_result"], "pending")
        self.assertEqual(service._text_inflight, {})


class FailureCooldownTests(_Base):
    def test_three_text_failures_cool_down_and_prefer_others(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        self.assertFalse(service.record_upstream_failure("a"))
        self.assertFalse(service.record_upstream_failure("a"))
        self.assertTrue(service.record_upstream_failure("a"))
        picks = {service.get_text_access_token() for _ in range(6)}
        self.assertEqual(picks, {"b"})
        self.assertGreater(service.scheduling_view(service.get_account("a"))["failure_cooldown_until"], time.time())

    def test_all_cooling_still_serves(self) -> None:
        service = self.make_service([_account("a")])
        for _ in range(3):
            service.record_upstream_failure("a")
        self.assertEqual(service.get_text_access_token(), "a")

    def test_success_clears_streak_and_cooldown(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        for _ in range(3):
            service.record_upstream_failure("a")
        service.mark_text_used("a")
        self.assertEqual({service.get_text_access_token() for _ in range(6)}, {"a", "b"})
        service.record_upstream_failure("b")
        service.record_upstream_failure("b")
        service.mark_text_used("b")  # 连续被打断，不应累计到 3
        self.assertFalse(service.record_upstream_failure("b"))

    def test_cooldown_expires(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        for _ in range(3):
            service.record_upstream_failure("a")
        service._upstream_cooldown_until["a"] = time.monotonic() - 1
        self.assertEqual({service.get_text_access_token() for _ in range(6)}, {"a", "b"})

    def test_zero_secs_disables_cooldown(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        with patch.dict(config.data, {"account_failure_cooldown_secs": 0}):
            for _ in range(3):
                self.assertFalse(service.record_upstream_failure("a"))
        self.assertEqual({service.get_text_access_token() for _ in range(6)}, {"a", "b"})

    def test_image_failure_run_cools_down_before_auto_pause(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        for _ in range(3):
            token = service._acquire_next_candidate_token(excluded_tokens={"b"})
            service.mark_image_result(token, False)
        a = service.get_account("a")
        self.assertFalse(service._auto_pause_active(a))  # 才 1 次计次，未达 3 次自动暂停
        self.assertEqual(len(a.get("failure_strikes") or []), 1)
        self.assertGreater(service.scheduling_view(a)["failure_cooldown_until"], time.time())
        picks = [service._acquire_next_candidate_token() for _ in range(2)]
        self.assertEqual(picks, ["b", "b"])

    def test_strikes_escalate_to_auto_pause_and_pause_ranks_below_cooldown(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        for _ in range(9):  # 3 轮 x 3 次连续失败
            token = service._acquire_next_candidate_token(excluded_tokens={"b"})
            service.mark_image_result(token, False)
        a = service.get_account("a")
        self.assertTrue(service._auto_pause_active(a))
        view = service.scheduling_view(a)
        self.assertTrue(view["auto_paused_until"])
        # b 仅处于冷却：排在“冷却 < 暂停”分层里应优先于 a
        service._upstream_cooldown_until["b"] = time.monotonic() + 100
        with service._lock:
            self.assertEqual(service._prefer_serviceable_locked(["a", "b"]), ["b"])

    def test_clear_auto_pause_also_clears_cooldown(self) -> None:
        service = self.make_service([_account("a")])
        for _ in range(3):
            service.record_upstream_failure("a")
        service.clear_auto_pause("a")
        self.assertIsNone(service.scheduling_view(service.get_account("a"))["failure_cooldown_until"])

    def test_image_success_clears_cooldown(self) -> None:
        service = self.make_service([_account("a")])
        for _ in range(3):
            service.mark_image_result(service._acquire_next_candidate_token(), False)
        self.assertTrue(service._upstream_cooldown_until)
        service.mark_image_result(service._acquire_next_candidate_token(), True)
        self.assertFalse(service._upstream_cooldown_until)


class DeviceIdTests(_Base):
    def test_concurrent_first_assignment_persists_exactly_one(self) -> None:
        service = self.make_service([_account("a")])
        results: list[str] = []
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            results.append(service.ensure_device_id("a", f"device-{index}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(service.get_account("a")["oai-device-id"], results[0])
        self.assertEqual(service.ensure_device_id("a", "another"), results[0])

    def test_unknown_account_is_not_created_and_stored_fp_id_wins(self) -> None:
        service = self.make_service([_account("a", fp={"OAI-Device-Id": "from-fp"})])
        self.assertEqual(service.ensure_device_id("a", "new"), "from-fp")
        self.assertEqual(service.ensure_device_id("ghost", "new"), "new")
        self.assertIsNone(service.get_account("ghost"))
        self.assertEqual(service.ensure_device_id("", "x"), "x")


class AuthVerificationTests(_Base):
    def make(self, accounts: list[dict]) -> AccountService:
        service = self.make_service(accounts)
        self.started: list[tuple[str, str]] = []
        service._start_auth_verification = lambda token, event: self.started.append((token, event)) or True
        return service

    def test_schedule_marks_pending_and_blocks_scheduling(self) -> None:
        service = self.make([_account("a"), _account("b")])
        self.assertTrue(service.schedule_auth_verification("a", "search"))  # 与搜索 401 调用点的签名兼容
        self.assertEqual(self.started, [("a", "search")])
        a = service.get_account("a")
        self.assertEqual(a["last_remote_check_result"], "pending")
        self.assertFalse(service._is_image_account_available(a))
        self.assertEqual({service.get_text_access_token() for _ in range(4)}, {"b"})
        self.assertEqual([item["id"] for item in service.list_pending_auth_verification()], [a["management_id"]])

    def test_image_scope_pending_still_serves_text(self) -> None:
        service = self.make([_account("a")])
        service.schedule_auth_verification("a", "image_stream", scope="image")
        self.assertFalse(service._is_image_account_available(service.get_account("a")))
        self.assertEqual(service.get_text_access_token(), "a")
        service.schedule_auth_verification("a", "search")  # 升级为账号级
        self.assertEqual(service.get_text_access_token(), "")

    def test_disabled_or_missing_account_is_not_scheduled(self) -> None:
        service = self.make([_account("a", status="禁用")])
        self.assertFalse(service.schedule_auth_verification("a", "search"))
        self.assertFalse(service.schedule_auth_verification("ghost", "search"))
        self.assertFalse(service.schedule_auth_verification("", "search"))
        self.assertEqual(self.started, [])

    def test_verify_ok_restores_account(self) -> None:
        service = self.make([_account("a")])
        service.schedule_auth_verification("a", "search")

        def fetch(token, event="x", remove_invalid=None):
            service._record_refresh_success(token, event)
            return service.get_account(token)

        service.fetch_remote_info = fetch
        self.assertEqual(service.verify_pending_auth("a", "search"), "ok")
        a = service.get_account("a")
        self.assertEqual(a["last_remote_check_result"], "ok")
        self.assertNotIn("pending_auth_at", a)
        self.assertTrue(service._is_image_account_available(a))

    def test_verify_rejected_goes_through_invalid_flow(self) -> None:
        from services.openai_backend_api import InvalidAccessTokenError

        service = self.make([_account("a")])
        service.schedule_auth_verification("a", "search")

        def fetch(token, event="x", remove_invalid=None):
            service.handle_invalid_token(token, event, error="rejected", remove=remove_invalid)
            raise InvalidAccessTokenError("rejected")

        service.fetch_remote_info = fetch
        self.assertEqual(service.verify_pending_auth("a", "search"), "invalid")
        a = service.get_account("a")
        self.assertEqual(a["status"], "异常")
        self.assertEqual(a["last_remote_check_result"], "invalid")
        self.assertNotIn("pending_auth_event", a)

    def test_verify_transient_error_puts_account_back(self) -> None:
        service = self.make([_account("a")])
        service.schedule_auth_verification("a", "search")

        def fetch(token, event="x", remove_invalid=None):
            raise ConnectionError("network down")

        service.fetch_remote_info = fetch
        self.assertEqual(service.verify_pending_auth("a", "search"), "error")
        a = service.get_account("a")
        self.assertEqual(a["last_remote_check_result"], "error")
        self.assertEqual(a["status"], "正常")
        self.assertEqual(service.get_text_access_token(), "a")

    def test_verify_skips_non_pending(self) -> None:
        service = self.make([_account("a")])
        self.assertEqual(service.verify_pending_auth("a"), "skipped")
        self.assertEqual(service.verify_pending_auth("ghost"), "skipped")

    def test_stale_pending_does_not_block_forever(self) -> None:
        stale = _iso(timedelta(hours=-1))
        service = self.make([_account("a", last_remote_check_result="pending", pending_auth_at=stale,
                                      pending_auth_scope="account")])
        self.assertTrue(service._is_image_account_available(service.get_account("a")))
        self.assertTrue(service.list_pending_auth_verification()[0]["stale"])
        self.assertEqual(service.get_text_access_token(), "a")

    def test_resume_requeues_leftovers(self) -> None:
        service = self.make([
            _account("a", last_remote_check_result="pending", pending_auth_event="search"),
            _account("b"),
        ])
        self.assertEqual(service.resume_pending_auth_verifications(), 1)
        self.assertEqual(self.started, [("a", "search")])

    def test_expired_access_token_without_refresh_token_is_not_scheduled(self) -> None:
        dead = _jwt(-3600)
        service = self.make([
            _account(dead),
            _account(_jwt(-3600) + "x", refresh_token="rt"),
            _account(_jwt(7200)),
        ])
        usable = [t for t, a in service._accounts.items() if service._is_image_account_available(a)]
        self.assertEqual(len(usable), 2)
        self.assertNotIn(dead, usable)
        view = service.scheduling_view(service.get_account(dead))
        self.assertEqual(view["credential_status"], "unavailable")
        recoverable = [a for a in service._accounts.values() if a.get("refresh_token")][0]
        self.assertEqual(service.scheduling_view(recoverable)["credential_status"], "recoverable")


class OperationProgressTests(_Base):
    def test_events_status_percent_and_incremental_read(self) -> None:
        service = self.make_service([_account("a"), _account("b")])
        service.init_refresh_progress("p1", 2)
        self.assertEqual(service.get_operation_progress("p1")["status"], "running")
        service.update_refresh_progress("p1", "a")
        service.update_refresh_progress("p1", "b", error="boom")
        first = service.get_operation_progress("p1")
        self.assertEqual(first["percent"], 100)
        self.assertEqual([e["status"] for e in first["events"]], ["success", "failed"])
        self.assertNotIn("a@", json.dumps(first["events"][0]["message"]))
        service.finish_refresh_progress("p1", {"refreshed": 1, "errors": [], "items": []})
        done = service.get_operation_progress("p1", after_sequence=1)
        self.assertEqual(done["status"], "success")
        self.assertEqual([e["sequence"] for e in done["events"]], [2])
        self.assertEqual(done["last_sequence"], 2)
        service.init_refresh_progress("p2", 1)
        service.finish_refresh_progress("p2", error="cancelled")
        self.assertEqual(service.get_operation_progress("p2")["status"], "failed")
        self.assertIsNone(service.get_operation_progress("missing"))
        # 旧接口读取的字段保持不变
        legacy = service.get_refresh_progress("p1")
        for key in ("total", "processed", "done", "error", "status_counts", "total_quota", "result"):
            self.assertIn(key, legacy)

    def test_event_buffer_is_capped(self) -> None:
        service = self.make_service([_account("a")])
        service.init_refresh_progress("p", 1000)
        for _ in range(520):
            service.update_refresh_progress("p", "a")
        events = service.get_operation_progress("p")["events"]
        self.assertEqual(len(events), 500)
        self.assertEqual(events[-1]["sequence"], 520)


class ApiPayloadTests(_Base):
    def test_account_for_api_exposes_scheduling_without_credentials(self) -> None:
        import api.accounts as accounts_api

        service = self.make_service([_account("secret-tok-123", refresh_token="rt",
                                              new_account_joined_at=_iso(timedelta(hours=-1)))])
        with patch.object(accounts_api, "account_service", service):
            payload = accounts_api._account_for_api(service.get_account("secret-tok-123"))
        self.assertEqual(payload["scheduling"]["new_account_stage"], "cooldown")
        self.assertEqual(payload["scheduling"]["text_concurrency"], 2)
        self.assertNotIn("refresh_token", payload)
        self.assertNotIn("secret-tok-123", json.dumps(payload["scheduling"]))


if __name__ == "__main__":
    unittest.main()
