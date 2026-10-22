"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

凭据与错误处理稳健性：RT 终态判定、按凭据代单飞刷新、凭据换代不覆盖、保存失败回滚、
手动更新凭据清失败态、文本结果不计账号失败、乐观放回限流号、出图休息抖动、脱敏、read_error_body。
全部 mock，不访问外网，不写真实 data/。
"""
from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services.account_credentials import oauth_session_kwargs
from services.account_service import (
    AccountService,
    OAuthRefreshError,
    RefreshCredentialsChangedError,
    TerminalRefreshTokenError,
)
from services.config import config
from utils.diagnostics import (
    collect_import_diagnostic_values,
    sanitize_import_errors,
    scrub_diagnostic_value,
)
from utils.helper import UpstreamHTTPError, ensure_ok, read_error_body


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _iso(seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _account(token: str, **extra) -> dict:
    item = {
        "access_token": token,
        "refresh_token": f"rt-{token}",
        "status": "正常",
        "quota": 10,
        "image_quota_unknown": False,
        "email": f"{token}@example.test",
    }
    item.update(extra)
    return item


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(config.data, {
            "image_account_cooldown_secs": 0,
            "image_preflight_fresh_secs": 0,
            "image_account_concurrency": 3,
            "account_auto_pause_strikes": 3,
            "account_auto_pause_hours": 6,
            "account_failure_cooldown_secs": 300,
            "auto_remove_invalid_accounts": False,
            "auto_remove_rate_limited_accounts": False,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("services.account_service.log_service",):
            log = patch(target)
            log.start()
            self.addCleanup(log.stop)
        wake = patch("services.account_relogin_service.account_relogin_service.wake")
        self.wake = wake.start()
        self.addCleanup(wake.stop)
        needs = patch.object(AccountService, "_token_needs_refresh", return_value=True)
        needs.start()
        self.addCleanup(needs.stop)

    def make_service(self, accounts: list[dict], *, stub_fetch: bool = True) -> AccountService:
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            service = AccountService(_MemoryStorage(accounts))
        if stub_fetch:
            # 选号预检不得走真实网络。
            service.fetch_remote_info = lambda token, event="x", remove_invalid=None: service.get_account(token)
        return service

    @staticmethod
    def management_id(service: AccountService, token: str) -> str:
        return str(service.get_account(token)["management_id"])


class TerminalRefreshClassificationTest(unittest.TestCase):
    def test_structured_rules(self) -> None:
        terminal = AccountService._is_terminal_refresh_error
        # 偶发 401（没有错误码）不是终态。
        self.assertFalse(terminal(401, "", ""))
        self.assertFalse(terminal(401, "", "unauthorized"))
        self.assertTrue(terminal(401, "invalid_grant", ""))
        self.assertTrue(terminal(400, "refresh_token_reused", ""))
        self.assertTrue(terminal(400, "", "Your session has ended"))
        # 408 / 429 / 5xx 永不判终态，即使带终态错误码。
        for status in (408, 429, 500, 502, 503):
            self.assertFalse(terminal(status, "invalid_grant", "session has ended"), status)

    def test_text_fallback_for_legacy_errors(self) -> None:
        text = AccountService._is_terminal_refresh_text
        self.assertFalse(text("oauth_refresh_http_401"))
        self.assertFalse(text("oauth_refresh_http_429: invalid_grant"))
        self.assertFalse(text("oauth_refresh_http_502"))
        self.assertTrue(text("oauth_refresh_http_401: invalid_grant"))
        self.assertTrue(text("oauth_refresh_http_400: refresh_token_expired"))
        self.assertTrue(text("invalid_grant"))

    def test_exception_types(self) -> None:
        is_terminal = AccountService._refresh_exception_is_terminal
        self.assertTrue(is_terminal(TerminalRefreshTokenError(401, "invalid_grant")))
        self.assertFalse(is_terminal(OAuthRefreshError(401)))
        self.assertEqual(str(OAuthRefreshError(401, "bad", "x" * 500)).count("x"), 300)


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = "" if payload is None else "x"

    def json(self) -> dict:
        return self._payload


class RefreshRequestTest(_Base):
    def _run_request(self, response: _FakeResponse):
        captured: dict = {}

        class FakeSession:
            def __init__(self, **kwargs) -> None:
                captured["session"] = kwargs

            def post(self, url, **kwargs):
                captured["post"] = kwargs
                return response

            def close(self) -> None:
                captured["closed"] = True

        service = self.make_service([_account("tok-a")])
        with patch("curl_cffi.requests.Session", FakeSession):
            try:
                result = service._request_access_token_refresh("rt-tok-a", service.get_account("tok-a"))
            except Exception as exc:  # noqa: BLE001
                return captured, exc
        return captured, result

    def test_client_hints_match_user_agent_and_tls_target_is_kept(self) -> None:
        captured, result = self._run_request(_FakeResponse(200, {"access_token": "new"}))
        self.assertEqual(result["access_token"], "new")
        headers = captured["session"]["headers"]
        self.assertIn("Chrome/145", headers["User-Agent"])
        # sec-ch-ua 由 UA 推导，而不是 curl_cffi 为 chrome110 自动补的 Chrome 110 品牌。
        self.assertIn('"Chromium";v="145"', headers["sec-ch-ua"])
        self.assertNotIn("110", headers["sec-ch-ua"])
        # TLS 目标保持原值（chrome110），不随 UA 升级：较新的 TLS 目标更容易被 Cloudflare 挑战。
        self.assertEqual(captured["session"]["impersonate"], "chrome110")
        self.assertTrue(captured.get("closed"))

    def test_oauth_session_kwargs_falls_back_for_non_chrome_user_agent(self) -> None:
        kwargs = oauth_session_kwargs("curl/8.0", "chrome110")
        self.assertEqual(kwargs["impersonate"], "chrome110")

    def test_error_responses_are_structured(self) -> None:
        _, error = self._run_request(_FakeResponse(401, {"error": "invalid_grant", "error_description": "revoked"}))
        self.assertIsInstance(error, TerminalRefreshTokenError)
        _, error = self._run_request(_FakeResponse(401, {}))
        self.assertIsInstance(error, OAuthRefreshError)
        self.assertNotIsInstance(error, TerminalRefreshTokenError)
        self.assertEqual(error.status_code, 401)
        _, error = self._run_request(_FakeResponse(429, {"error": {"code": "invalid_grant"}}))
        self.assertNotIsInstance(error, TerminalRefreshTokenError)
        _, error = self._run_request(_FakeResponse(503, {"error": "overloaded"}))
        self.assertNotIsInstance(error, TerminalRefreshTokenError)


class RefreshFlowTest(_Base):
    def test_transient_401_does_not_flag_relogin(self) -> None:
        service = self.make_service([_account("tok-a")])
        with patch.object(AccountService, "_request_access_token_refresh", side_effect=OAuthRefreshError(401)):
            self.assertEqual(service.refresh_access_token("tok-a", force=True), "tok-a")
        account = service.get_account("tok-a")
        self.assertEqual(service.list_relogin_needed_accounts(), [])
        self.assertNotIn("refresh_token_invalid_at", account)
        self.assertIn("oauth_refresh_http_401", account["last_token_refresh_error"])
        self.wake.assert_not_called()

    def test_terminal_error_marks_rt_invalid_and_wakes_relogin(self) -> None:
        service = self.make_service([_account("tok-a")])
        error = TerminalRefreshTokenError(400, "invalid_grant", "revoked")
        with patch.object(AccountService, "_request_access_token_refresh", side_effect=error) as request:
            service.refresh_access_token("tok-a", force=True)
            account = service.get_account("tok-a")
            self.assertTrue(account["refresh_token_invalid_at"])
            self.assertEqual([item["access_token"] for item in service.list_relogin_needed_accounts()], ["tok-a"])
            self.wake.assert_called()
            # RT 已确认失效：非强制刷新不再打 OAuth。
            service.refresh_access_token("tok-a")
            self.assertEqual(request.call_count, 1)

    def test_refresh_error_text_is_scrubbed(self) -> None:
        service = self.make_service([_account("tok-a", refresh_token="rt-secret-value")])
        error = OAuthRefreshError(500, "boom", "echo rt-secret-value and tok-a")
        with patch.object(AccountService, "_request_access_token_refresh", side_effect=error):
            service.refresh_access_token("tok-a", force=True)
        stored = service.get_account("tok-a")["last_token_refresh_error"]
        self.assertNotIn("rt-secret-value", stored)
        self.assertIn("[credential]", stored)

    def test_different_accounts_refresh_in_parallel(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        a_started = threading.Event()
        release_a = threading.Event()

        def fake_request(_self, refresh_token, account=None):
            if refresh_token == "rt-tok-a":
                a_started.set()
                self.assertTrue(release_a.wait(5))
            return {"access_token": f"new-{account['access_token']}", "refresh_token": refresh_token + "2", "id_token": ""}

        results: dict[str, str] = {}
        with patch.object(AccountService, "_request_access_token_refresh", fake_request):
            thread = threading.Thread(target=lambda: results.update(a=service.refresh_access_token("tok-a", force=True)))
            thread.start()
            self.assertTrue(a_started.wait(5))
            started = time.monotonic()
            # A 的刷新还卡着时，B 不应被全局锁挡住。
            self.assertEqual(service.refresh_access_token("tok-b", force=True), "new-tok-b")
            self.assertLess(time.monotonic() - started, 2.0)
            release_a.set()
            thread.join(5)
        self.assertEqual(results["a"], "new-tok-a")

    def test_same_credential_generation_is_single_flight(self) -> None:
        service = self.make_service([_account("tok-a")])
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fake_request(_self, refresh_token, account=None):
            calls.append(refresh_token)
            started.set()
            self.assertTrue(release.wait(5))
            return {"access_token": "tok-a2", "refresh_token": "rt-a2", "id_token": ""}

        results: list[str] = []
        with patch.object(AccountService, "_request_access_token_refresh", fake_request):
            threads = [
                threading.Thread(target=lambda: results.append(service.refresh_access_token("tok-a", force=True)))
                for _ in range(3)
            ]
            threads[0].start()
            self.assertTrue(started.wait(5))
            for thread in threads[1:]:
                thread.start()
            time.sleep(0.2)
            release.set()
            for thread in threads:
                thread.join(5)
        self.assertEqual(calls, ["rt-tok-a"])
        self.assertEqual(results, ["tok-a2"] * 3)
        self.assertEqual(service._oauth_refresh_flights, {})

    def test_manual_credentials_are_not_overwritten_by_in_flight_refresh(self) -> None:
        service = self.make_service([_account("tok-a")])
        account_id = self.management_id(service, "tok-a")

        def fake_request(_self, refresh_token, account=None):
            service.update_credentials(account_id, refresh_token="rt-manual")
            return {"access_token": "tok-from-old-rt", "refresh_token": "rt-old-rotated", "id_token": ""}

        with patch.object(AccountService, "_request_access_token_refresh", fake_request):
            with self.assertRaises(RefreshCredentialsChangedError):
                service.force_refresh_access_token("tok-a", raise_on_error=True)
        account = service.get_account("tok-a")
        self.assertIsNotNone(account)
        self.assertEqual(account["refresh_token"], "rt-manual")
        self.assertIsNone(service.get_account("tok-from-old-rt"))

    def test_expected_credentials_mismatch_raises(self) -> None:
        service = self.make_service([_account("tok-a")])
        with self.assertRaises(RefreshCredentialsChangedError):
            service.ensure_access_token("tok-a", expected_credentials=("tok-a", "other-rt", ""))

    def test_failure_of_stale_generation_is_not_recorded(self) -> None:
        service = self.make_service([_account("tok-a")])
        account_id = self.management_id(service, "tok-a")

        def fake_request(_self, refresh_token, account=None):
            service.update_credentials(account_id, refresh_token="rt-manual")
            raise TerminalRefreshTokenError(400, "invalid_grant")

        with patch.object(AccountService, "_request_access_token_refresh", fake_request):
            with self.assertRaises(RefreshCredentialsChangedError):
                service.force_refresh_access_token("tok-a", raise_on_error=True)
        account = service.get_account("tok-a")
        self.assertNotIn("refresh_token_invalid_at", account)
        self.assertIsNone(account["last_token_refresh_error"])
        self.assertEqual(service.list_relogin_needed_accounts(), [])

    def test_apply_refreshed_tokens_rolls_back_on_save_failure(self) -> None:
        service = self.make_service([_account("tok-a")])
        before = service.get_account("tok-a")
        with patch.object(service, "_save_accounts", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                service._apply_refreshed_tokens("tok-a", {"access_token": "tok-b", "refresh_token": "rt-b"}, "test")
        self.assertEqual(service.get_account("tok-a"), before)
        self.assertIsNone(service.get_account("tok-b"))
        self.assertNotIn("tok-a", service._token_aliases)

    def test_apply_refreshed_tokens_does_not_steal_another_accounts_token(self) -> None:
        service = self.make_service([_account("tok-a"), _account("tok-b")])
        with self.assertRaises(RefreshCredentialsChangedError):
            service._apply_refreshed_tokens("tok-a", {"access_token": "tok-b"}, "test")
        self.assertEqual(service.get_account("tok-b")["refresh_token"], "rt-tok-b")


class UpdateCredentialsTest(_Base):
    def test_clears_failure_state_like_relogin(self) -> None:
        service = self.make_service([_account(
            "tok-a",
            status="异常",
            relogin_needed_at=_iso(-60),
            relogin_reason="invalid_grant",
            relogin_attempts=2,
            last_relogin_error="otp timeout",
            refresh_token_invalid_at=_iso(-60),
            last_token_refresh_error="oauth_refresh_http_400: invalid_grant",
            last_token_refresh_error_at=_iso(-60),
            last_remote_check_result="pending",
            pending_auth_at=_iso(-5),
            pending_auth_event="image",
            pending_auth_scope="image",
            pending_auth_verification_id="vid",
            auto_paused_until=_iso(3600),
            auto_paused_at=_iso(-5),
            failure_strikes=[_iso(-5)],
        )])
        service._image_failure_streaks["tok-a"] = 2
        service._upstream_failure_streaks["tok-a"] = 2
        service._upstream_cooldown_until["tok-a"] = time.monotonic() + 300
        account_id = self.management_id(service, "tok-a")

        updated = service.update_credentials(account_id, access_token="tok-new", refresh_token="rt-new")

        self.assertEqual(updated["access_token"], "tok-new")
        account = service.get_account("tok-new")
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["refresh_token"], "rt-new")
        for key in (
            "relogin_needed_at", "relogin_reason", "last_relogin_error", "refresh_token_invalid_at",
            "pending_auth_at", "pending_auth_event", "pending_auth_scope", "pending_auth_verification_id",
            "auto_paused_until", "auto_paused_at", "failure_strikes",
        ):
            self.assertNotIn(key, account, key)
        self.assertEqual(account["relogin_attempts"], 0)
        self.assertIsNone(account["last_token_refresh_error"])
        self.assertIsNone(account["last_remote_check_result"])
        self.assertNotIn("tok-a", service._image_failure_streaks)
        self.assertNotIn("tok-new", service._image_failure_streaks)
        self.assertNotIn("tok-new", service._upstream_failure_streaks)
        self.assertNotIn("tok-new", service._upstream_cooldown_until)
        self.assertEqual(service.list_relogin_needed_accounts(), [])
        self.assertEqual(service.resolve_access_token("tok-a"), "tok-new")

    def test_refresh_token_only_change_also_clears(self) -> None:
        service = self.make_service([_account("tok-a", relogin_needed_at=_iso(-60), refresh_token_invalid_at=_iso(-60))])
        service.update_credentials(self.management_id(service, "tok-a"), refresh_token="rt-new")
        account = service.get_account("tok-a")
        self.assertNotIn("relogin_needed_at", account)
        self.assertNotIn("refresh_token_invalid_at", account)

    def test_no_change_keeps_failure_state(self) -> None:
        service = self.make_service([_account("tok-a", relogin_needed_at=_iso(-60))])
        service.update_credentials(self.management_id(service, "tok-a"), refresh_token="rt-tok-a")
        self.assertIn("relogin_needed_at", service.get_account("tok-a"))

    def test_save_failure_rolls_back(self) -> None:
        service = self.make_service([_account("tok-a")])
        before = service.get_account("tok-a")
        with patch.object(service, "_save_accounts", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                service.update_credentials(self.management_id(service, "tok-a"), access_token="tok-new")
        self.assertEqual(service.get_account("tok-a"), before)
        self.assertIsNone(service.get_account("tok-new"))
        self.assertNotIn("tok-a", service._token_aliases)


class MarkImageResultTest(_Base):
    def test_text_result_is_not_an_account_failure(self) -> None:
        service = self.make_service([_account("tok-a")])
        for _ in range(8):
            token = service.get_available_access_token()
            service.mark_image_result(token, False, failure_code="content_policy_violation", status_code=400)
        account = service.get_account("tok-a")
        self.assertEqual(account["fail"], 0)
        self.assertNotIn("auto_paused_until", account)
        self.assertNotIn("tok-a", service._image_failure_streaks)
        self.assertFalse(service._is_cooling_down_locked("tok-a"))
        self.assertNotIn("last_image_failure_at", account)
        self.assertEqual(service._image_inflight, {})
        self.assertTrue(account["last_used_at"])

    def test_text_result_helper_and_explicit_flag(self) -> None:
        service = self.make_service([_account("tok-a")])
        token = service.get_available_access_token()
        service.mark_image_text_result(token, failure_code="upstream_text_reply")
        token = service.get_available_access_token()
        service.mark_image_result(token, False, failure_code="whatever", account_fault=False)
        self.assertEqual(service.get_account("tok-a")["fail"], 0)
        self.assertEqual(service._image_inflight, {})

    def test_real_failure_still_counts_and_auto_pauses(self) -> None:
        service = self.make_service([_account("tok-a")])
        for _ in range(9):
            token = service.get_available_access_token()
            service.mark_image_result(token, False, failure_code="image_tool_error", status_code=502)
        account = service.get_account("tok-a")
        self.assertEqual(account["fail"], 9)
        self.assertTrue(account.get("auto_paused_until"))

    def test_account_fault_true_overrides_text_code(self) -> None:
        service = self.make_service([_account("tok-a")])
        token = service.get_available_access_token()
        service.mark_image_result(token, False, failure_code="content_policy_violation", account_fault=True)
        self.assertEqual(service.get_account("tok-a")["fail"], 1)

    def test_save_failure_still_releases_slot_and_rolls_back(self) -> None:
        service = self.make_service([_account("tok-a")])
        token = service.get_available_access_token()
        self.assertEqual(service._image_inflight, {"tok-a": 1})
        with patch.object(service, "_save_accounts", side_effect=OSError("cas conflict")):
            self.assertIsNone(service.mark_image_result(token, False, failure_code="image_tool_error"))
        self.assertEqual(service._image_inflight, {})
        self.assertEqual(service.get_account("tok-a")["fail"], 0)

    def test_release_slot_survives_usage_tracker_error(self) -> None:
        service = self.make_service([_account("tok-a")])
        token = service.get_available_access_token()
        with patch.object(service._image_usage, "record_release", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                service.release_image_slot(token)
        self.assertEqual(service._image_inflight, {})


class FetchRemoteInfoGenerationTest(_Base):
    def test_old_credential_401_does_not_blacklist_rotated_account(self) -> None:
        service = self.make_service([_account("tok-old")], stub_fetch=False)
        account_id = self.management_id(service, "tok-old")
        from services.openai_backend_api import InvalidAccessTokenError

        seen: list[str] = []

        class FakeBackend:
            def __init__(self, token: str) -> None:
                self.token = token

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

            def get_user_info(self):
                seen.append(self.token)
                if self.token == "tok-old":
                    # 请求在途时账号凭据被别处轮换。
                    service.update_credentials(account_id, access_token="tok-new")
                    raise InvalidAccessTokenError("401")
                return {"quota": 7, "status": "正常", "type": "plus"}

        with patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend):
            result = service.fetch_remote_info("tok-old", "test")
        self.assertEqual(seen, ["tok-old", "tok-new"])
        self.assertEqual(result["access_token"], "tok-new")
        account = service.get_account("tok-new")
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["last_remote_check_result"], "ok")

    def test_genuine_rejection_with_dead_refresh_token_marks_invalid(self) -> None:
        service = self.make_service([_account("tok-a")], stub_fetch=False)
        from services.openai_backend_api import InvalidAccessTokenError

        class FakeBackend:
            def __init__(self, token: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

            def get_user_info(self):
                raise InvalidAccessTokenError("401")

        with patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend), patch.object(
            AccountService, "_request_access_token_refresh",
            side_effect=TerminalRefreshTokenError(400, "invalid_grant"),
        ):
            with self.assertRaises(InvalidAccessTokenError):
                service.fetch_remote_info("tok-a", "test")
        self.assertEqual(service.get_account("tok-a")["status"], "异常")

    def test_transient_refresh_failure_does_not_blacklist(self) -> None:
        service = self.make_service([_account("tok-a")], stub_fetch=False)
        from services.openai_backend_api import InvalidAccessTokenError

        class FakeBackend:
            def __init__(self, token: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

            def get_user_info(self):
                raise InvalidAccessTokenError("401")

        with patch("services.openai_backend_api.OpenAIBackendAPI", FakeBackend), patch.object(
            AccountService, "_request_access_token_refresh", side_effect=OAuthRefreshError(502),
        ):
            with self.assertRaises(OAuthRefreshError):
                service.fetch_remote_info("tok-a", "test")
        account = service.get_account("tok-a")
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["last_remote_check_result"], "error")

    def test_stale_generation_guard_on_invalid_marking(self) -> None:
        service = self.make_service([_account("tok-a")])
        stale = ("tok-a", "old-rt", "")
        self.assertFalse(service.handle_invalid_token("tok-a", "test", error="401", expected_credentials=stale))
        self.assertEqual(service.get_account("tok-a")["status"], "正常")
        current = service._credential_generation("tok-a", service.get_account("tok-a"))
        service.handle_invalid_token("tok-a", "test", error="401", expected_credentials=current)
        self.assertEqual(service.get_account("tok-a")["status"], "异常")


class AuthVerificationSchedulingTest(_Base):
    def test_schedule_sets_scope_and_verification_id(self) -> None:
        service = self.make_service([_account("tok-a")])
        with patch.object(service, "_start_auth_verification", return_value=True) as start:
            self.assertTrue(service.schedule_auth_verification("tok-a", "image", scope="image"))
        account = service.get_account("tok-a")
        self.assertEqual(account["pending_auth_scope"], "image")
        self.assertTrue(account["pending_auth_verification_id"])
        self.assertEqual(account["last_remote_check_result"], "pending")
        start.assert_called_once()

    def test_each_schedule_gets_new_verification_id(self) -> None:
        service = self.make_service([_account("tok-a")])
        with patch.object(service, "_start_auth_verification", return_value=True):
            service.schedule_auth_verification("tok-a", "e1", scope="image")
            first = service.get_account("tok-a")["pending_auth_verification_id"]
            service.schedule_auth_verification("tok-a", "e2", scope="account")
            second = service.get_account("tok-a")
        self.assertNotEqual(first, second["pending_auth_verification_id"])
        self.assertEqual(second["pending_auth_scope"], "account")

    def test_scheduling_failure_degrades_to_check_error(self) -> None:
        service = self.make_service([_account("tok-a")])
        with patch.object(service, "_start_auth_verification", return_value=False):
            self.assertFalse(service.schedule_auth_verification("tok-a", "image", scope="image"))
        account = service.get_account("tok-a")
        self.assertEqual(account["last_remote_check_result"], "error")
        self.assertNotIn("pending_auth_verification_id", account)
        self.assertTrue(service._is_image_account_available(account))

    def test_scheduling_exception_degrades(self) -> None:
        service = self.make_service([_account("tok-a")])
        with patch.object(service, "_start_auth_verification", side_effect=RuntimeError("no threads")):
            self.assertFalse(service.schedule_auth_verification("tok-a", "image", scope="image"))
        self.assertEqual(service.get_account("tok-a")["last_remote_check_result"], "error")

    def test_already_running_verification_is_not_a_failure(self) -> None:
        service = self.make_service([_account("tok-a")])
        service._auth_verify_active.add("tok-a")
        self.assertTrue(service.schedule_auth_verification("tok-a", "image", scope="image"))
        self.assertEqual(service.get_account("tok-a")["last_remote_check_result"], "pending")

    def test_outdated_verification_error_does_not_clobber_new_round(self) -> None:
        service = self.make_service([_account("tok-a")])
        with patch.object(service, "_start_auth_verification", return_value=True):
            service.schedule_auth_verification("tok-a", "e1", scope="image")
            old_id = service.get_account("tok-a")["pending_auth_verification_id"]
            service.schedule_auth_verification("tok-a", "e2", scope="image")
        service._record_remote_check_error("tok-a", "e1", "late", expected_verification_id=old_id)
        self.assertEqual(service.get_account("tok-a")["last_remote_check_result"], "pending")


class LimitedReleaseTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.fetch = MagicMock()

    def _service(self, *accounts: dict) -> AccountService:
        service = self.make_service(list(accounts))
        service.fetch_remote_info = self.fetch
        return service

    def test_due_account_released_without_requests(self) -> None:
        service = self._service(
            _account("due", status="限流", restore_at=_iso(-60), quota=0),
            _account("later", status="限流", restore_at=_iso(3600), quota=0),
            _account("pending", status="限流", restore_at=_iso(-60), last_remote_check_result="pending"),
        )
        released = service.release_recovered_limited_accounts()
        self.assertEqual(released, ["due@example.test"])
        due = service.get_account("due")
        self.assertEqual(due["status"], "正常")
        self.assertTrue(due["image_quota_unknown"])
        self.assertTrue(due["optimistic_release_at"])
        self.assertEqual(service.get_account("later")["status"], "限流")
        self.assertEqual(service.get_account("pending")["status"], "限流")
        self.fetch.assert_not_called()

    def test_limits_progress_reset_after_is_honoured(self) -> None:
        service = self._service(_account(
            "tok-a", status="限流", quota=0,
            limits_progress=[{"feature_name": "image_gen", "remaining": 0, "reset_after": _iso(-5)}],
        ))
        self.assertEqual(len(service.release_recovered_limited_accounts()), 1)

    def test_released_account_must_be_preflighted_again(self) -> None:
        with patch.dict(config.data, {"image_preflight_fresh_secs": 600}):
            service = self._service(_account(
                "tok-a", status="限流", quota=0, restore_at=_iso(-5),
                last_remote_check_result="ok", last_remote_checked_at=_iso(-10),
            ))
            service.release_recovered_limited_accounts()
            self.assertFalse(service._image_preflight_is_fresh("tok-a"))

    def test_retry_gap_after_optimistic_release(self) -> None:
        item = _account("tok-a", status="限流", restore_at=_iso(-60), optimistic_release_at=_iso(-60))
        self.assertFalse(AccountService._limited_release_due(item, datetime.now(timezone.utc)))
        item["optimistic_release_at"] = _iso(-AccountService.LIMITED_RELEASE_RETRY_SECS - 5)
        self.assertTrue(AccountService._limited_release_due(item, datetime.now(timezone.utc)))

    def test_unknown_reset_waits_three_hours(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = _account("a", status="限流", last_remote_check_attempt_at=_iso(-3600))
        old = _account("b", status="限流", last_remote_check_attempt_at=_iso(-4 * 3600))
        self.assertFalse(AccountService._limited_release_due(fresh, now))
        self.assertTrue(AccountService._limited_release_due(old, now))

    def test_concurrent_release_runs_once(self) -> None:
        service = self._service(_account("tok-a", status="限流", restore_at=_iso(-60)))
        self.assertTrue(service._limited_release_lock.acquire(blocking=False))
        try:
            self.assertEqual(service.release_recovered_limited_accounts(), [])
        finally:
            service._limited_release_lock.release()
        self.assertEqual(service.get_account("tok-a")["status"], "限流")


class ImageRestJitterTest(_Base):
    def test_default_has_no_rest(self) -> None:
        service = self.make_service([_account("tok-a")])
        before = time.monotonic()
        service.release_image_slot(service.get_available_access_token())
        self.assertLessEqual(service._image_released_at["tok-a"], time.monotonic())
        self.assertGreaterEqual(service._image_released_at["tok-a"], before)

    def test_rest_is_jittered_within_thirty_percent(self) -> None:
        with patch.dict(config.data, {"image_account_cooldown_secs": 100}):
            offsets = {AccountService._image_rest_jitter() for _ in range(200)}
            self.assertGreater(len(offsets), 20)
            self.assertTrue(all(-30.0001 <= value <= 30.0001 for value in offsets))
            with patch("services.account_service.random.uniform", return_value=0.7):
                self.assertAlmostEqual(AccountService._image_rest_jitter(), -30.0)
            with patch("services.account_service.random.uniform", return_value=1.3):
                self.assertAlmostEqual(AccountService._image_rest_jitter(), 30.0)

    def test_zero_cooldown_has_zero_jitter(self) -> None:
        self.assertEqual(AccountService._image_rest_jitter(), 0.0)


class ErrorBodyTest(unittest.TestCase):
    class _StreamResponse:
        status_code = 429
        headers = {"Retry-After": "7"}
        text = ""

        def iter_content(self, *_args, **_kwargs):
            yield b'{"detail": {"code": "image_quota'
            yield b'_exhausted"}}'

    def test_reads_error_body_from_stream(self) -> None:
        self.assertEqual(read_error_body(self._StreamResponse()), {"detail": {"code": "image_quota_exhausted"}})

    def test_ensure_ok_uses_stream_body_and_scope(self) -> None:
        with self.assertRaises(UpstreamHTTPError) as caught:
            ensure_ok(self._StreamResponse(), "/backend-api/conversation")
        error = caught.exception
        self.assertEqual(error.body, {"detail": {"code": "image_quota_exhausted"}})
        self.assertEqual(error.retry_after, 7)
        self.assertEqual(error.credential_scope, "account")
        with self.assertRaises(UpstreamHTTPError) as caught:
            ensure_ok(self._StreamResponse(), "image_download", credential_scope="signed_asset")
        self.assertEqual(caught.exception.credential_scope, "signed_asset")
        self.assertEqual(UpstreamHTTPError("x", 401, "b", credential_scope="bogus").credential_scope, "account")

    def test_plain_text_and_json_text(self) -> None:
        class Plain:
            text = "<html>blocked</html>"

        class Json:
            text = '{"error": "x"}'

        self.assertEqual(read_error_body(Plain()), "<html>blocked</html>")
        self.assertEqual(read_error_body(Json()), {"error": "x"})

    def test_success_response_does_not_raise(self) -> None:
        response = MagicMock()
        response.status_code = 204
        ensure_ok(response, "ok")


class DiagnosticScrubTest(unittest.TestCase):
    def test_scrub_value_is_recursive(self) -> None:
        payload = {
            "access_token": "at-123456",
            "nested": [{"message": "failed with at-123456 via http://u:pw@proxy.test:8080", "proxy": "http://u:pw@proxy.test:8080"}],
            "secret_key": "sk-abc",
            "note": "sk-abc leaked",
        }
        cleaned = scrub_diagnostic_value(payload)
        self.assertEqual(cleaned["access_token"], "[credential]")
        self.assertEqual(cleaned["secret_key"], "[credential]")
        text = str(cleaned)
        for secret in ("at-123456", "sk-abc", "u:pw"):
            self.assertNotIn(secret, text)

    def test_import_error_scrub(self) -> None:
        sensitive, proxies = collect_import_diagnostic_values(
            {"secret_key": "sk-abc"},
            {"credentials": '{"refresh_token": "rt-xyz"}', "proxy": "http://u:pw@proxy.test:8080"},
        )
        self.assertIn("sk-abc", sensitive)
        self.assertIn("rt-xyz", sensitive)
        errors = sanitize_import_errors(
            [{"name": "a.json", "error": "HTTP 500 sk-abc rt-xyz via http://u:pw@proxy.test:8080"}, "junk"],
            sensitive_values=sensitive,
            proxy_values=proxies,
        )
        self.assertEqual(len(errors), 1)
        for secret in ("sk-abc", "rt-xyz", "u:pw"):
            self.assertNotIn(secret, errors[0]["error"])


class _FakeImportConfig:
    """只实现导入服务 _append_error / _update_job 用到的几个方法，不碰真实 data/。"""

    def __init__(self, entry: dict) -> None:
        self.entry = entry
        self.job = {"errors": [], "failed": 0, "completed": 0}

    def get_pool(self, _id):
        return dict(self.entry)

    get_server = get_pool

    def get_import_job(self, _id):
        return dict(self.job)

    def set_import_job(self, _id, job):
        self.job = dict(job)
        return {"import_job": self.job}


class ImportErrorScrubTest(unittest.TestCase):
    def test_cpa_errors_hide_secret_key_and_proxy_password(self) -> None:
        from services import cpa_service

        config_stub = _FakeImportConfig({"id": "p1", "secret_key": "cpa-secret-key"})
        service = cpa_service.CPAImportService(config_stub)
        service._append_error("p1", "file-cpa-secret-key.json", "401 for cpa-secret-key via http://u:pw@proxy.test:8080")
        error = config_stub.job["errors"][0]
        text = str(error)
        for secret in ("cpa-secret-key", "u:pw"):
            self.assertNotIn(secret, text)
        self.assertEqual(config_stub.job["failed"], 1)

    def test_cpa_pool_load_scrubs_persisted_errors(self) -> None:
        from services import cpa_service

        pool = cpa_service._normalize_pool({
            "id": "p1", "secret_key": "cpa-secret-key", "base_url": "http://x",
            "import_job": {"status": "failed", "errors": [{"name": "a", "error": "bad cpa-secret-key"}]},
        })
        self.assertNotIn("cpa-secret-key", str(pool["import_job"]["errors"]))

    def test_sub2api_errors_hide_password_and_api_key(self) -> None:
        from services import sub2api_service

        config_stub = _FakeImportConfig({"id": "s1", "password": "pw-123456", "api_key": "key-abcdef"})
        service = sub2api_service.Sub2APIImportService(config_stub)
        service._append_error("s1", "acc-1", "login failed pw-123456 / key-abcdef")
        text = str(config_stub.job["errors"][0])
        self.assertNotIn("pw-123456", text)
        self.assertNotIn("key-abcdef", text)

    def test_sub2api_server_load_scrubs_persisted_errors(self) -> None:
        from services import sub2api_service

        server = sub2api_service._normalize_server({
            "id": "s1", "base_url": "http://x", "api_key": "key-abcdef",
            "import_job": {"status": "failed", "errors": [{"name": "a", "error": "bad key-abcdef"}]},
        })
        self.assertNotIn("key-abcdef", str(server["import_job"]["errors"]))


class CodexOAuthSessionTest(unittest.TestCase):
    def test_token_session_hints_follow_user_agent(self) -> None:
        import re
        from services import codex_oauth_service

        captured: dict = {}

        class FakeSession:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        with patch.object(codex_oauth_service.requests, "Session", FakeSession):
            codex_oauth_service._token_session()
        headers = captured["headers"]
        major = re.search(r"Chrome/(\d+)", headers["User-Agent"]).group(1)
        self.assertIn(f'"Chromium";v="{major}"', headers["sec-ch-ua"])
        self.assertTrue(captured["impersonate"].startswith("chrome"))


if __name__ == "__main__":
    unittest.main()
