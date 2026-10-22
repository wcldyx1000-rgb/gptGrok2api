from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from services.register import openai_register


def _error_url(payload: dict, *, b64: bool = False) -> str:
    raw = json.dumps(payload)
    if b64:
        raw = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        return f"https://auth.openai.com/error?payload={raw}"
    return f"https://auth.openai.com/error?payload={quote(raw)}"


class AuthErrorPageTest(unittest.TestCase):
    def test_parses_error_code_from_plain_and_base64_payload(self) -> None:
        for b64 in (False, True):
            with self.subTest(b64=b64):
                url = _error_url({"errorCode": "rate_limit_exceeded"}, b64=b64)
                self.assertEqual(openai_register._auth_error_code_from_url(url), "rate_limit_exceeded")

    def test_non_error_pages_are_ignored(self) -> None:
        for url in (
            "https://auth.openai.com/email-verification",
            "https://auth.openai.com/create-account/password",
            "https://auth.openai.com/log-in",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(openai_register._auth_error_code_from_url(url), "")

    def test_unparseable_payload_still_flags_an_error_page(self) -> None:
        self.assertEqual(openai_register._auth_error_code_from_url("https://auth.openai.com/error?payload=%%%"), "auth_error")
        self.assertEqual(openai_register._auth_error_code_from_url("https://auth.openai.com/error"), "auth_error")
        self.assertEqual(
            openai_register._auth_error_code_from_url("https://auth.openai.com/error?payload=rate_limit_exceeded"),
            "rate_limit_exceeded",
        )


class AuthorizeLandsOnErrorPageTest(unittest.TestCase):
    def _registrar(self):
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.session = MagicMock()
        registrar.session.cookies = MagicMock()
        registrar.device_id = "device"
        registrar.proxy = "http://node-a:1"
        registrar.fingerprint = {}
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar._navigate_headers = MagicMock(return_value={})
        return registrar

    def _authorize(self, final_url: str):
        registrar = self._registrar()
        response = SimpleNamespace(status_code=200, url=final_url, headers={}, text="", json=lambda: {})
        with (
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
            patch.object(openai_register, "_headers_with_clearance", return_value={}),
            patch.object(openai_register, "step"),
            patch.object(openai_register, "_mark_otp_rate_limited") as mark_limited,
        ):
            try:
                return registrar, registrar._platform_authorize("a@example.test", 1), mark_limited
            except Exception as error:  # noqa: BLE001
                return registrar, error, mark_limited

    def test_rate_limit_page_is_treated_as_http_429(self) -> None:
        _registrar, error, mark_limited = self._authorize(_error_url({"errorCode": "rate_limit_exceeded"}))

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("platform_authorize_http_429", str(error))
        self.assertTrue(openai_register._is_rate_limit_error(error))
        self.assertTrue(openai_register._is_transient_register_error(error))
        mark_limited.assert_called_once_with()

    def test_other_error_pages_are_reported_and_retryable_but_not_rate_limited(self) -> None:
        _registrar, error, mark_limited = self._authorize(_error_url({"errorCode": "something_else"}))

        self.assertIn("platform_authorize_error:something_else", str(error))
        self.assertFalse(openai_register._is_rate_limit_error(error))
        self.assertTrue(openai_register._is_transient_register_error(error))
        mark_limited.assert_not_called()

    def test_normal_signup_page_is_unaffected(self) -> None:
        _registrar, landed, mark_limited = self._authorize("https://auth.openai.com/create-account/password")

        self.assertEqual(landed, "signup")
        mark_limited.assert_not_called()

    def test_retry_loop_backs_off_and_avoids_the_limited_node(self) -> None:
        limited = RuntimeError(
            'platform_authorize_http_429, detail={"error": {"code": "rate_limit_exceeded", '
            '"message": "authorize landed on rate limit error page"}}'
        )
        success = (MagicMock(), {"email": "ok@example.test"})
        acquired: list[set[str]] = []

        def acquire(index, base, failed):
            acquired.append(set(failed))
            return "http://node-a:1" if len(acquired) == 1 else "http://node-b:1"

        sleeps: list[float] = []
        with (
            patch.object(openai_register, "_acquire_register_egress", side_effect=acquire),
            patch.object(openai_register, "_register_with_fresh_email", side_effect=[limited, success]),
            patch.object(openai_register, "_interruptible_sleep", side_effect=sleeps.append),
            patch.object(openai_register, "_warm_up_retry_egress", return_value=False),
            patch.object(openai_register, "_mark_otp_rate_limited"),
            patch.object(openai_register, "_mark_register_proxy_dead") as mark_dead,
            patch.object(openai_register, "step"),
            patch.dict(openai_register.config, {"proxy": "group:pool"}),
        ):
            self.assertIs(openai_register._register_with_retries(1), success)

        self.assertEqual(acquired, [set(), {"http://node-a:1"}])
        self.assertGreaterEqual(sleeps[0], openai_register.REGISTER_RATE_LIMIT_BASE_DELAY_SECONDS)
        mark_dead.assert_not_called()  # 限流不等于节点坏了，不计入全局节点失败


class InvalidStateMarkersTest(unittest.TestCase):
    def test_extra_invalid_state_phrases(self) -> None:
        for text in (
            "Your sign-in session is no longer valid",
            "login_continue_http_409",
            "passwordless_send_otp_http_409",
            "invalid_state",
        ):
            with self.subTest(text=text):
                self.assertTrue(openai_register._is_invalid_state_error(text))
        self.assertFalse(openai_register._is_invalid_state_error("validate_otp_http_400"))


class TokenExchangeFallbackTest(unittest.TestCase):
    def _registrar(self):
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.session = MagicMock()
        registrar.platform_auth_code = "code-1"
        registrar.code_verifier = "verifier"
        registrar.proxy = "http://node-a:1"
        registrar.fingerprint = {"user_agent": "ua"}
        return registrar

    def test_api_endpoint_is_primary_and_legacy_is_not_called_on_success(self) -> None:
        registrar = self._registrar()
        tokens = {"access_token": "at", "refresh_token": "rt"}
        with (
            patch.object(openai_register, "request_platform_oauth_token", return_value=tokens) as primary,
            patch.object(openai_register, "request_platform_oauth_token_legacy") as legacy,
            patch.object(openai_register, "step"),
        ):
            self.assertIs(registrar._exchange_registered_tokens(1), tokens)
        primary.assert_called_once()
        legacy.assert_not_called()

    def test_falls_back_to_legacy_with_a_fresh_session_when_api_fails(self) -> None:
        registrar = self._registrar()
        legacy_tokens = {"access_token": "at2", "refresh_token": "rt2", "id_token": "it"}

        def primary(session, code, verifier, errors, fingerprint):
            errors.append("api token 接口拒绝: status=502")
            return None

        with (
            patch.object(openai_register, "request_platform_oauth_token", side_effect=primary),
            patch.object(openai_register, "request_platform_oauth_token_legacy", return_value=legacy_tokens) as legacy,
            patch.object(openai_register, "step"),
        ):
            self.assertIs(registrar._exchange_registered_tokens(1), legacy_tokens)

        args, kwargs = legacy.call_args
        self.assertEqual(args[1:3], ("code-1", "verifier"))
        self.assertEqual(args[3], "http://node-a:1")
        self.assertTrue(kwargs["fresh_session"])

    def test_consumed_code_does_not_trigger_a_pointless_legacy_call(self) -> None:
        registrar = self._registrar()

        def primary(session, code, verifier, errors, fingerprint):
            errors.append('api token 接口拒绝: status=400, {"error": "invalid_grant"}')
            return None

        with (
            patch.object(openai_register, "request_platform_oauth_token", side_effect=primary),
            patch.object(openai_register, "request_platform_oauth_token_legacy") as legacy,
            patch.object(openai_register, "step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "token换取失败"):
                registrar._exchange_registered_tokens(1)
        legacy.assert_not_called()

    def test_both_failing_reports_the_collected_reasons(self) -> None:
        registrar = self._registrar()

        def primary(session, code, verifier, errors, fingerprint):
            errors.append("api token 请求异常: timeout")
            return None

        def legacy(session, code, verifier, proxy, errors, **kwargs):
            errors.append("legacy token 接口拒绝: status=500")
            return None

        with (
            patch.object(openai_register, "request_platform_oauth_token", side_effect=primary),
            patch.object(openai_register, "request_platform_oauth_token_legacy", side_effect=legacy),
            patch.object(openai_register, "step"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                registrar._exchange_registered_tokens(1)
        self.assertIn("api token 请求异常", str(ctx.exception))
        self.assertIn("legacy token 接口拒绝", str(ctx.exception))

    def test_missing_code_still_fails_fast(self) -> None:
        registrar = self._registrar()
        registrar.platform_auth_code = ""
        with patch.object(openai_register, "request_platform_oauth_token") as primary, patch.object(openai_register, "step"):
            with self.assertRaises(RuntimeError):
                registrar._exchange_registered_tokens(1)
        primary.assert_not_called()


class RetryWarmUpTest(unittest.TestCase):
    def setUp(self) -> None:
        openai_register._retry_warmup_last.clear()
        self.addCleanup(openai_register._retry_warmup_last.clear)

    def test_new_node_gets_one_warm_up_then_is_rate_limited(self) -> None:
        with patch.object(openai_register.proxy_settings, "warm_up", return_value=1) as warm_up:
            self.assertTrue(openai_register._warm_up_retry_egress("http://node-b:1", "http://node-a:1"))
            self.assertFalse(openai_register._warm_up_retry_egress("http://node-b:1", "http://node-a:1"))
            # 另一个新节点不受影响
            self.assertTrue(openai_register._warm_up_retry_egress("http://node-c:1", "http://node-b:1"))
        self.assertEqual([call.args[0] for call in warm_up.call_args_list], [["http://node-b:1"], ["http://node-c:1"]])
        self.assertEqual(warm_up.call_args.args[1], openai_register.auth_base)

    def test_same_node_or_direct_is_not_warmed(self) -> None:
        with patch.object(openai_register.proxy_settings, "warm_up", return_value=1) as warm_up:
            self.assertFalse(openai_register._warm_up_retry_egress("http://node-a:1", "http://node-a:1"))
            self.assertFalse(openai_register._warm_up_retry_egress("", "http://node-a:1"))
        warm_up.assert_not_called()

    def test_window_expiry_allows_another_warm_up(self) -> None:
        with patch.object(openai_register.proxy_settings, "warm_up", return_value=1) as warm_up:
            self.assertTrue(openai_register._warm_up_retry_egress("http://node-b:1", "x"))
            openai_register._retry_warmup_last[openai_register.normalize_proxy_url("http://node-b:1")] -= (
                openai_register.REGISTER_RETRY_WARMUP_MIN_INTERVAL_SECONDS + 1
            )
            self.assertTrue(openai_register._warm_up_retry_egress("http://node-b:1", "x"))
        self.assertEqual(warm_up.call_count, 2)

    def test_warm_up_failure_never_breaks_the_retry(self) -> None:
        with patch.object(openai_register.proxy_settings, "warm_up", side_effect=RuntimeError("boom")):
            self.assertFalse(openai_register._warm_up_retry_egress("http://node-b:1", "http://node-a:1"))

    def test_retry_on_a_new_node_triggers_warm_up_but_first_attempt_does_not(self) -> None:
        success = (MagicMock(), {"email": "ok@example.test"})
        nodes = iter(["http://node-a:1", "http://node-b:1"])
        calls: list[tuple[str, str]] = []

        def warm(proxy, previous=""):
            calls.append((proxy, previous))
            return True

        with (
            patch.object(openai_register, "_acquire_register_egress", side_effect=lambda *a: next(nodes)),
            patch.object(
                openai_register,
                "_register_with_fresh_email",
                side_effect=[RuntimeError("curl: (7) Failed to connect to proxy"), success],
            ),
            patch.object(openai_register, "_warm_up_retry_egress", side_effect=warm),
            patch.object(openai_register, "_interruptible_sleep"),
            patch.object(openai_register, "_mark_register_proxy_dead"),
            patch.object(openai_register, "step"),
            patch.dict(openai_register.config, {"proxy": "group:pool"}),
        ):
            self.assertIs(openai_register._register_with_retries(1), success)

        self.assertEqual(calls, [("http://node-b:1", "http://node-a:1")])


if __name__ == "__main__":
    unittest.main()
