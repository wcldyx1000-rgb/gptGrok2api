from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from curl_cffi import requests

from services.register import grok_protocol, grok_register, mail_provider, openai_register
import services.register_service as register_service_module
from services.register_service import RegisterService
from services.xai_session_cookies import apply_cookie_jar, snapshot_session_cookie_jar


def _profile(proxy_url: str) -> SimpleNamespace:
    return SimpleNamespace(proxy_url=proxy_url)


class RegisterErrorClassificationTest(unittest.TestCase):
    def test_proxy_transport_errors(self) -> None:
        self.assertTrue(openai_register._is_proxy_transport_error("curl: (7) Failed to connect to proxy"))
        self.assertTrue(openai_register._is_proxy_transport_error(RuntimeError("CONNECT tunnel failed, response 502")))
        self.assertFalse(openai_register._is_proxy_transport_error("validate_otp_http_400"))

    def test_geo_block_and_mailbox_exhaustion(self) -> None:
        self.assertTrue(openai_register._is_geo_blocked_error("unsupported_country_region_territory"))
        self.assertTrue(
            openai_register._is_mailbox_exhausted_error(
                mail_provider.ICloudPrivacyMailError("no_available_mailbox", "iCloud Privacy Mail: 没有可用隐私邮箱")
            )
        )
        self.assertTrue(
            openai_register._is_mailbox_exhausted_error(
                RuntimeError("[pool] OutlookToken 邮箱池暂无可用邮箱（共 3 个）")
            )
        )
        self.assertFalse(openai_register._is_mailbox_exhausted_error(RuntimeError("platform_authorize_http_400")))

    def test_transient_classification(self) -> None:
        for message in (
            "passwordless_send_otp_http_429: rate_limit_exceeded",
            "curl: (28) Operation timed out",
            "被 Cloudflare 拦截",
            "invalid_state: session is no longer valid",
        ):
            with self.subTest(message=message):
                self.assertTrue(openai_register._is_transient_register_error(message))
        for message in (
            "unsupported_email: The email you provided is not supported",
            "unsupported_country_region_territory",
            "所有启用邮箱来源均未收到 ChatGPT 验证码",
        ):
            with self.subTest(message=message):
                self.assertFalse(openai_register._is_transient_register_error(message))

    def test_icloud_rate_limit_detection(self) -> None:
        limited = mail_provider.ICloudPrivacyMailError("icloud_api_error", "busy", retryable=True, status_code=429)
        self.assertTrue(openai_register._is_icloud_rate_limited_error(limited))
        self.assertFalse(openai_register._is_icloud_rate_limited_error(RuntimeError("429")))


class RegisterWithRetriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._clear_node_health()
        self.addCleanup(self._clear_node_health)
        self.sleeps: list[float] = []
        sleep_patch = patch.object(openai_register, "_interruptible_sleep", side_effect=self.sleeps.append)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        # 重试换节点时的预热会走真实 proxy_settings，这里统一关掉；专门的用例在 openai_register_reliability_test。
        warm_patch = patch.object(openai_register, "_warm_up_retry_egress", return_value=False)
        warm_patch.start()
        self.addCleanup(warm_patch.stop)
        self.config_patch = patch.dict(openai_register.config, {"proxy": ""})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    @staticmethod
    def _clear_node_health() -> None:
        store = openai_register.proxy_settings
        with store._lock:
            store._node_failures.clear()
            store._node_disabled_until.clear()
            store._node_last_error.clear()

    def test_transport_error_marks_node_dead_and_retries_on_another_node(self) -> None:
        openai_register.config["proxy"] = "group:pool"
        profiles = iter([_profile("http://node-a:1"), _profile("http://node-a:1"), _profile("http://node-b:1")])
        success = (MagicMock(), {"email": "ok@example.test"})
        with patch.object(
            openai_register.proxy_settings, "get_profile", side_effect=lambda **_: next(profiles)
        ), patch.object(
            openai_register,
            "_register_with_fresh_email",
            side_effect=[RuntimeError("curl: (7) Failed to connect to proxy"), success],
        ) as register:
            result = openai_register._register_with_retries(1)

        self.assertIs(result, success)
        self.assertEqual([item.args[1] for item in register.call_args_list], ["http://node-a:1", "http://node-b:1"])
        self.assertTrue(openai_register.proxy_settings.is_proxy_disabled("http://node-a:1"))
        self.assertEqual(len(self.sleeps), 1)
        self.assertLess(self.sleeps[0], 2.0)

    def test_success_clears_node_failure_record(self) -> None:
        openai_register.config["proxy"] = "group:pool"
        store = openai_register.proxy_settings
        success = (MagicMock(), {"email": "ok@example.test"})
        with patch.object(store, "_node_health_settings", return_value=(3, 600.0)), patch.object(
            store, "get_profile", return_value=_profile("http://node-a:1")
        ), patch.object(openai_register, "_register_with_fresh_email", return_value=success):
            store.report_proxy_outcome("http://node-a:1", ok=False)
            self.assertIs(openai_register._register_with_retries(1), success)
        self.assertNotIn("http://node-a:1", store._node_failures)

    def test_dead_node_is_skipped_by_later_tasks_until_cooldown(self) -> None:
        openai_register._mark_register_proxy_dead("http://node-a:1")
        profiles = iter([_profile("http://node-a:1"), _profile("http://node-b:1")])
        with patch.object(openai_register.proxy_settings, "get_profile", side_effect=lambda **_: next(profiles)):
            self.assertEqual(openai_register._register_egress_for_attempt("group:pool", set()), "http://node-b:1")

        store = openai_register.proxy_settings
        with store._lock:
            store._node_disabled_until["http://node-a:1"] = time.time() - 1
        with patch.object(
            openai_register.proxy_settings, "get_profile", return_value=_profile("http://node-a:1")
        ):
            self.assertEqual(openai_register._register_egress_for_attempt("group:pool", set()), "http://node-a:1")
        self.assertFalse(store.is_proxy_disabled("http://node-a:1"))

    def test_literal_proxy_and_direct_are_used_as_is(self) -> None:
        with patch.object(openai_register.proxy_settings, "get_profile") as get_profile:
            self.assertEqual(openai_register._register_egress_for_attempt("http://fixed:1", set()), "http://fixed:1")
            self.assertEqual(openai_register._register_egress_for_attempt("", set()), "")
        get_profile.assert_not_called()

    def test_rate_limit_triggers_global_cooldown_and_long_delay(self) -> None:
        success = (MagicMock(), {"email": "ok@example.test"})
        with patch.object(
            openai_register,
            "_register_with_fresh_email",
            side_effect=[RuntimeError("platform_authorize_http_429: rate_limit_exceeded"), success],
        ), patch.object(openai_register, "_mark_otp_rate_limited") as mark_limited:
            self.assertIs(openai_register._register_with_retries(1), success)

        mark_limited.assert_called_once_with()
        self.assertGreaterEqual(self.sleeps[0], openai_register.REGISTER_RATE_LIMIT_BASE_DELAY_SECONDS)

    def test_mailbox_exhaustion_aborts_the_job(self) -> None:
        error = mail_provider.ICloudPrivacyMailError("no_available_mailbox", "iCloud Privacy Mail: 没有可用隐私邮箱")
        with patch.object(openai_register, "_register_with_fresh_email", side_effect=error) as register:
            with self.assertRaises(openai_register.RegisterJobAborted) as raised:
                openai_register._register_with_retries(1)
        self.assertIn("邮箱已用尽", raised.exception.reason)
        register.assert_called_once()

    def test_geo_block_on_direct_aborts_but_retries_with_proxy(self) -> None:
        geo = RuntimeError("unsupported_country_region_territory")
        with patch.object(openai_register, "_register_with_fresh_email", side_effect=geo):
            with self.assertRaises(openai_register.RegisterJobAborted):
                openai_register._register_with_retries(1)

        openai_register.config["proxy"] = "http://fixed:1"
        success = (MagicMock(), {"email": "ok@example.test"})
        with patch.object(openai_register, "_register_with_fresh_email", side_effect=[geo, success]) as register:
            self.assertIs(openai_register._register_with_retries(1), success)
        self.assertEqual(register.call_count, 2)

    def test_non_transient_error_is_not_retried(self) -> None:
        with patch.object(
            openai_register,
            "_register_with_fresh_email",
            side_effect=RuntimeError("unsupported_email"),
        ) as register:
            with self.assertRaises(RuntimeError):
                openai_register._register_with_retries(1)
        register.assert_called_once()
        self.assertEqual(self.sleeps, [])

    def test_attempts_are_capped(self) -> None:
        with patch.object(
            openai_register,
            "_register_with_fresh_email",
            side_effect=RuntimeError("curl: (28) Operation timed out"),
        ) as register:
            with self.assertRaises(RuntimeError):
                openai_register._register_with_retries(1)
        self.assertEqual(register.call_count, openai_register.REGISTER_TASK_MAX_ATTEMPTS)

    def test_icloud_rate_limit_waits_without_spending_an_attempt(self) -> None:
        limited = mail_provider.ICloudPrivacyMailError("icloud_api_error", "busy", retryable=True, status_code=429)
        timeout = RuntimeError("curl: (28) Operation timed out")
        success = (MagicMock(), {"email": "ok@example.test"})
        with patch.object(
            openai_register,
            "_register_with_fresh_email",
            side_effect=[limited, timeout, timeout, success],
        ) as register:
            self.assertIs(openai_register._register_with_retries(1), success)
        self.assertEqual(register.call_count, 4)
        self.assertEqual(self.sleeps[0], openai_register.REGISTER_ICLOUD_RATE_LIMIT_WAIT_SECONDS)

    def test_failure_after_stop_request_reports_stop(self) -> None:
        with patch.object(
            openai_register,
            "_register_with_fresh_email",
            side_effect=RuntimeError("curl: (28) Operation timed out"),
        ), patch.object(openai_register, "should_stop_callback", side_effect=[False, True]):
            with self.assertRaises(openai_register.RegisterStopped):
                openai_register._register_with_retries(1)


class RegisterStopTest(unittest.TestCase):
    def test_interruptible_sleep_ends_on_stop(self) -> None:
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        with patch.object(openai_register, "should_stop_callback", stop.is_set):
            with self.assertRaises(openai_register.RegisterStopped):
                openai_register._interruptible_sleep(10)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_worker_reports_stopped_and_aborted_tasks(self) -> None:
        with patch.object(openai_register, "_register_with_retries", side_effect=openai_register.RegisterStopped()):
            stopped = openai_register.worker(1)
        self.assertEqual((stopped["ok"], stopped.get("stopped")), (False, True))

        with patch.object(
            openai_register,
            "_register_with_retries",
            side_effect=openai_register.RegisterJobAborted("邮箱已用尽"),
        ):
            aborted = openai_register.worker(2)
        self.assertFalse(aborted["ok"])
        self.assertEqual(aborted["abort_job"], "邮箱已用尽")

    def test_stopped_registration_releases_mailbox(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        mailbox = {"provider": "outlook_token", "address": "person@example.test", "label": "pool"}
        registrar._create_registration_mailbox = MagicMock(return_value=mailbox)
        registrar._platform_authorize = MagicMock(side_effect=openai_register.RegisterStopped())
        with patch.object(openai_register.mail_provider, "release_mailbox") as release, patch.object(
            openai_register.mail_provider, "mark_mailbox_result"
        ) as mark:
            with self.assertRaises(openai_register.RegisterStopped):
                registrar.register(1)
        release.assert_called_once_with(mailbox)
        mark.assert_not_called()


class ConfirmRefreshTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.last_mailbox = {"provider": "outlook_token", "address": "person@example.test"}
        registrar._reset_auth_cookies = MagicMock()
        registrar._platform_authorize = MagicMock(return_value="login")
        registrar._passwordless_login = MagicMock(
            return_value={"access_token": "relogin-at", "refresh_token": "relogin-rt", "id_token": "relogin-id"}
        )
        self.registrar = registrar
        self.result = {
            "email": "person@example.test",
            "access_token": "reg-at",
            "refresh_token": "reg-rt",
            "id_token": "reg-id",
        }

    def test_successful_probe_takes_rotated_tokens(self) -> None:
        with patch.object(
            openai_register.account_service,
            "_request_access_token_refresh",
            return_value={"access_token": "new-at", "refresh_token": "new-rt", "id_token": ""},
        ) as refresh:
            updated = self.registrar.confirm_refresh_token(1, self.result)
        refresh.assert_called_once_with("reg-rt", self.result)
        self.assertEqual((updated["access_token"], updated["refresh_token"]), ("new-at", "new-rt"))
        self.assertEqual(updated["id_token"], "reg-id")

    def test_revoked_refresh_token_logs_in_again_with_same_mailbox(self) -> None:
        with patch.object(
            openai_register.account_service,
            "_request_access_token_refresh",
            side_effect=RuntimeError("oauth_refresh_http_401: invalid_grant"),
        ):
            updated = self.registrar.confirm_refresh_token(1, self.result)
        self.registrar._platform_authorize.assert_called_once_with(
            "person@example.test", 1, screen_hint="login_or_signup"
        )
        self.registrar._passwordless_login.assert_called_once_with(
            "person@example.test", self.registrar.last_mailbox, 1
        )
        self.assertEqual(updated["refresh_token"], "relogin-rt")

    def test_other_probe_failures_keep_registration_tokens(self) -> None:
        with patch.object(
            openai_register.account_service,
            "_request_access_token_refresh",
            side_effect=RuntimeError("curl: (28) timed out"),
        ):
            updated = self.registrar.confirm_refresh_token(1, self.result)
        self.assertEqual(updated, self.result)
        self.registrar._passwordless_login.assert_not_called()

    def test_revoked_token_without_mailbox_keeps_registration_tokens(self) -> None:
        self.registrar.last_mailbox = None
        with patch.object(
            openai_register.account_service,
            "_request_access_token_refresh",
            side_effect=RuntimeError("oauth_refresh_http_401"),
        ):
            updated = self.registrar.confirm_refresh_token(1, self.result)
        self.assertEqual(updated, self.result)

    def test_failed_relogin_keeps_registration_tokens(self) -> None:
        self.registrar._passwordless_login.side_effect = RuntimeError("等待 OpenAI 登录验证码超时")
        with patch.object(
            openai_register.account_service,
            "_request_access_token_refresh",
            side_effect=RuntimeError("oauth_refresh_http_401"),
        ):
            updated = self.registrar.confirm_refresh_token(1, self.result)
        self.assertEqual(updated, self.result)


class _PrefetchClient:
    def __init__(self, *, detached=None, detached_error: Exception | None = None, delay: float = 0.0) -> None:
        self.config: dict = {"captcha_timeout": 30}
        self.browser_bridge = None
        self.calls: list[str] = []
        self._detached = detached or ("prefetched-token", {"cookies": []})
        self._detached_error = detached_error
        self._delay = delay
        self.adopted: list[dict] = []
        self.created: dict = {}

    def session_cookie_jar(self):
        return [{"name": "seed", "value": "1", "domain": ".x.ai", "path": "/"}]

    def solve_turnstile_detached(self, initial_cookies):
        self.calls.append("solve_turnstile_detached")
        if self._delay:
            time.sleep(self._delay)
        if self._detached_error is not None:
            raise self._detached_error
        return self._detached

    def adopt_turnstile_solution(self, solution, *, cookies_before):
        self.calls.append("adopt_turnstile_solution")
        self.adopted.append({"solution": solution, "cookies_before": cookies_before})

    def bootstrap(self):
        self.calls.append("bootstrap")

    def send_email_validation_code(self, email):
        self.calls.append("send_email_validation_code")

    def verify_email_validation_code(self, email, code):
        self.calls.append("verify_email_validation_code")

    def validate_password(self, email, password):
        self.calls.append("validate_password")

    def solve_turnstile(self):
        self.calls.append("solve_turnstile")
        return "inline-token"

    def create_user_and_session(self, **kwargs):
        self.calls.append("create_user_and_session")
        self.created = kwargs
        return {"sso": "sso-token", "redirect_url": "https://grok.com/"}

    def session_cookies(self):
        return {}


class GrokTurnstilePrefetchTest(unittest.TestCase):
    def _register(self, client: _PrefetchClient, wait_seconds: float = 0.0) -> dict:
        def wait_for_code(*_args, **_kwargs):
            if wait_seconds:
                time.sleep(wait_seconds)
            return "ABC-123"

        mailbox = {"provider": "icloud_api", "address": "relay@example.test", "label": "Grok"}
        with patch.object(grok_register.mail_provider, "create_mailbox", return_value=mailbox), patch.object(
            grok_register.mail_provider, "wait_for_code", side_effect=wait_for_code
        ), patch.object(grok_register.mail_provider, "mark_mailbox_result"), patch.object(
            grok_register, "_authorize_live_oauth", return_value={}
        ):
            return grok_register._register_once(1, client, {}, 1, 1)

    def test_prefetched_token_is_used_and_adopted_after_password_step(self) -> None:
        client = _PrefetchClient()
        self._register(client, wait_seconds=0.05)

        self.assertEqual(client.created["turnstile_token"], "prefetched-token")
        self.assertNotIn("solve_turnstile", client.calls)
        self.assertLess(client.calls.index("validate_password"), client.calls.index("adopt_turnstile_solution"))
        self.assertLess(client.calls.index("adopt_turnstile_solution"), client.calls.index("create_user_and_session"))
        self.assertEqual(client.adopted[0]["cookies_before"][0]["name"], "seed")

    def test_prefetch_starts_while_waiting_for_mail(self) -> None:
        client = _PrefetchClient(delay=0.3)
        started = time.monotonic()
        self._register(client, wait_seconds=0.3)
        # Solving and the mailbox wait overlap instead of adding up.
        self.assertLess(time.monotonic() - started, 0.55)
        self.assertEqual(client.created["turnstile_token"], "prefetched-token")

    def test_expired_prefetch_is_solved_again_inline(self) -> None:
        client = _PrefetchClient()
        with patch.object(grok_register, "TURNSTILE_PREFETCH_TTL_SECONDS", -1.0):
            self._register(client)
        self.assertEqual(client.created["turnstile_token"], "inline-token")
        self.assertNotIn("adopt_turnstile_solution", client.calls)

    def test_failed_prefetch_falls_back_to_inline_solve(self) -> None:
        client = _PrefetchClient(detached_error=RuntimeError("solver busy"))
        self._register(client)
        self.assertEqual(client.created["turnstile_token"], "inline-token")

    def test_prefetch_can_be_disabled_and_is_skipped_for_browser_flow(self) -> None:
        disabled = _PrefetchClient()
        disabled.config["turnstile_prefetch"] = False
        self._register(disabled)
        self.assertNotIn("solve_turnstile_detached", disabled.calls)

        browser = _PrefetchClient()
        browser.browser_bridge = object()
        self._register(browser)
        self.assertNotIn("solve_turnstile_detached", browser.calls)


class GrokAdoptTurnstileSolutionTest(unittest.TestCase):
    def test_cookies_newer_than_the_solver_snapshot_survive_adoption(self) -> None:
        client = object.__new__(grok_protocol.GrokProtocolClient)
        client.session = requests.Session()
        self.addCleanup(client.session.close)
        apply_cookie_jar(client.session, [{"name": "a", "value": "1", "domain": ".x.ai", "path": "/"}])
        before = snapshot_session_cookie_jar(client.session)
        # Requests made while the solver ran changed "a" and added "b".
        apply_cookie_jar(
            client.session,
            [
                {"name": "a", "value": "2", "domain": ".x.ai", "path": "/"},
                {"name": "b", "value": "new", "domain": ".x.ai", "path": "/"},
            ],
        )
        solution = {
            "cookies": [
                {"name": "a", "value": "1", "domain": ".x.ai", "path": "/"},
                {"name": "b", "value": "stale", "domain": ".x.ai", "path": "/"},
                {"name": "cf_clearance", "value": "browser", "domain": ".x.ai", "path": "/"},
            ]
        }
        client._adopt_turnstile_browser_state = lambda value: apply_cookie_jar(client.session, value["cookies"])

        client.adopt_turnstile_solution(solution, cookies_before=before)

        values = {item["name"]: item["value"] for item in snapshot_session_cookie_jar(client.session)}
        self.assertEqual(values, {"a": "2", "b": "new", "cf_clearance": "browser"})


class RegisterRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.service = RegisterService(Path(temp_dir.name) / "register.json")
        metrics = patch.object(
            self.service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}
        )
        metrics.start()
        self.addCleanup(metrics.stop)
        self.service._config["enabled"] = True
        self.service._config["proxy"] = ""

    def test_abort_stops_the_job_and_skips_remaining_tasks(self) -> None:
        backend = SimpleNamespace(
            worker=MagicMock(return_value={"ok": False, "index": 1, "error": "x", "abort_job": "邮箱已用尽"})
        )
        self.service._run("openai", backend, {"threads": 1, "total": 5, "mode": "total"})

        backend.worker.assert_called_once_with(1)
        stats = self.service.get()["stats"]
        self.assertEqual((stats["done"], stats["fail"]), (1, 1))
        self.assertFalse(self.service.get()["enabled"])
        self.assertTrue(any("注册任务已中止：邮箱已用尽" in item["text"] for item in self.service.get()["logs"]))
        self.assertFalse(self.service._register_stop_event.is_set())

    def test_stopped_results_are_not_counted_and_pool_usability_is_tracked(self) -> None:
        results = iter(
            [
                {"ok": True, "index": 1, "result": {}, "pool_usable": True},
                {"ok": True, "index": 2, "result": {}, "pool_usable": False},
                {"ok": False, "index": 3, "error": "stopped", "stopped": True},
            ]
        )
        backend = SimpleNamespace(worker=lambda _index: next(results))
        with patch.object(self.service, "_sleep_while_running", return_value=True):
            self.service._run("openai", backend, {"threads": 1, "total": 3, "mode": "total"})

        stats = self.service.get()["stats"]
        self.assertEqual((stats["done"], stats["success"], stats["fail"]), (2, 2, 0))
        self.assertEqual((stats["pool_ok"], stats["pool_fail"], stats["pool_usable_rate"]), (1, 1, 50.0))

    def test_direct_egress_staggers_task_starts(self) -> None:
        backend = SimpleNamespace(worker=MagicMock(return_value={"ok": True, "index": 1, "result": {}}))
        with patch.object(self.service, "_sleep_while_running", return_value=True) as sleep:
            self.service._run("openai", backend, {"threads": 3, "total": 3, "mode": "total"})
        stagger_calls = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(
            stagger_calls,
            [register_service_module.REGISTER_DIRECT_STAGGER_SECONDS] * 2,
        )

        self.service._config["enabled"] = True
        self.service._config["proxy"] = "http://proxy:1"
        with patch.object(self.service, "_sleep_while_running", return_value=True) as sleep:
            self.service._run("openai", backend, {"threads": 2, "total": 2, "mode": "total"})
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [register_service_module.REGISTER_PROXY_STAGGER_SECONDS],
        )

    def test_stop_interrupts_waits_inside_running_tasks(self) -> None:
        def worker(index: int) -> dict:
            return openai_register.worker(index)

        def slow_register(_index: int):
            openai_register._interruptible_sleep(30)
            raise AssertionError("stop did not interrupt the wait")

        backend = SimpleNamespace(worker=worker)
        with patch.object(openai_register, "_register_with_retries", side_effect=slow_register):
            runner = threading.Thread(
                target=self.service._run,
                args=("openai", backend, {"threads": 1, "total": 1, "mode": "total"}),
            )
            self.service._runner = runner
            runner.start()
            time.sleep(0.3)
            started = time.monotonic()
            self.service.stop()
            runner.join(timeout=5)

        self.assertFalse(runner.is_alive())
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(self.service.get()["stats"]["done"], 0)
        self.assertFalse(self.service._register_stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
