"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

覆盖搜索总时限/收尾余量、搜索鉴权失败调用点，以及 sentinel locale 接线。
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from services import openai_backend_api as backend_module
from services.checkout_protocol import pix
from services.register import openai_register
from utils.locale_env import LocaleEnv


def _backend() -> backend_module.OpenAIBackendAPI:
    backend = backend_module.OpenAIBackendAPI.__new__(backend_module.OpenAIBackendAPI)
    backend.access_token = "tok"
    return backend


def _http_error(status: int) -> Exception:
    return backend_module.UpstreamHTTPError("search", status, "body")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += max(secs, 1.0)


class SearchTimeoutTest(unittest.TestCase):
    def test_constants_aligned_with_reference(self) -> None:
        self.assertEqual(backend_module.SEARCH_TIMEOUT_SECS, 600.0)
        self.assertEqual(backend_module.SEARCH_FINAL_GRACE_SECS, 30.0)

    def test_overtime_returns_latest_answer_within_grace(self) -> None:
        backend = _backend()
        clock = _Clock()
        results = [
            {"answer": "a1", "status": "in_progress"},
            {"answer": "a2", "status": "in_progress"},
            {"answer": "final", "status": "in_progress"},
        ]
        with patch.object(backend, "_get_search_conversation", return_value={}), \
                patch.object(backend, "_extract_search_result", side_effect=results), \
                patch.object(backend_module.time, "time", clock.time), \
                patch.object(backend_module.time, "sleep", clock.sleep):
            out = backend._wait_search_result("c1", 1.0, 1.0)
        self.assertEqual(out["answer"], "a2")  # 到点后第一次取到的响应即作为最终响应

    def test_no_result_times_out_only_after_grace(self) -> None:
        backend = _backend()
        clock = _Clock()
        start = clock.now
        with patch.object(backend, "_get_search_conversation", return_value={}), \
                patch.object(backend, "_extract_search_result", return_value=None), \
                patch.object(backend_module.time, "time", clock.time), \
                patch.object(backend_module.time, "sleep", clock.sleep):
            with self.assertRaises(RuntimeError):
                backend._wait_search_result("c1", 5.0, 1.0)
        self.assertGreaterEqual(clock.now - start, 5.0 + backend_module.SEARCH_FINAL_GRACE_SECS)

    def test_search_passes_remaining_budget(self) -> None:
        backend = _backend()
        with patch.object(backend, "_prepare_search_conversation", return_value="c"), \
                patch.object(backend, "_bootstrap"), \
                patch.object(backend, "_run_search_conversation", return_value="cid"), \
                patch.object(backend, "_wait_search_result", return_value={"answer": "x"}) as wait:
            backend.search("q", timeout_secs=100.0)
        self.assertLessEqual(wait.call_args.args[1], 100.0)
        self.assertGreater(wait.call_args.args[1], 90.0)


class SearchAuthFailureTest(unittest.TestCase):
    def test_401_schedules_verification_when_available(self) -> None:
        backend = _backend()
        fake_service = MagicMock()
        with patch.object(backend, "_prepare_search_conversation", side_effect=_http_error(401)), \
                patch("services.account_service.account_service", fake_service):
            with self.assertRaises(backend_module.UpstreamHTTPError):
                backend.search("q")
        fake_service.schedule_auth_verification.assert_called_once_with("tok", "search")
        fake_service.handle_invalid_token.assert_not_called()

    def test_401_falls_back_to_invalid_handler(self) -> None:
        backend = _backend()
        fake_service = MagicMock(spec=["handle_invalid_token"])
        with patch.object(backend, "_prepare_search_conversation", side_effect=_http_error(401)), \
                patch("services.account_service.account_service", fake_service):
            with self.assertRaises(backend_module.UpstreamHTTPError):
                backend.search("q")
        fake_service.handle_invalid_token.assert_called_once()
        self.assertEqual(fake_service.handle_invalid_token.call_args.args[:2], ("tok", "search"))

    def test_non_401_does_not_touch_account(self) -> None:
        backend = _backend()
        fake_service = MagicMock()
        with patch.object(backend, "_prepare_search_conversation", side_effect=_http_error(500)), \
                patch("services.account_service.account_service", fake_service):
            with self.assertRaises(backend_module.UpstreamHTTPError):
                backend.search("q")
        self.assertEqual(fake_service.method_calls, [])


class SentinelLocaleWiringTest(unittest.TestCase):
    def test_register_wrapper_forwards_locale(self) -> None:
        locale = LocaleEnv(timezone="Asia/Taipei", language="zh-TW")
        with patch.object(openai_register, "_build_sentinel_token_tuple", return_value=("sv", "sc")) as tuple_fn, \
                patch.object(openai_register, "_set_oai_sc_cookie"):
            openai_register.build_sentinel_token(MagicMock(), "dev", "flow", None, locale)
        self.assertIs(tuple_fn.call_args.kwargs["locale"], locale)

    def test_register_wrapper_default_locale_none(self) -> None:
        with patch.object(openai_register, "_build_sentinel_token_tuple", return_value=("sv", "sc")) as tuple_fn, \
                patch.object(openai_register, "_set_oai_sc_cookie"):
            openai_register.build_sentinel_token(MagicMock(), "dev", "flow")
        self.assertIsNone(tuple_fn.call_args.kwargs["locale"])

    def test_locale_for_proxy_uses_slot_locale(self) -> None:
        locale = LocaleEnv(timezone="Asia/Tokyo", language="ja-JP")
        with patch("services.proxy_service._egress_slot_locale", return_value=locale):
            self.assertIs(openai_register._locale_for_proxy("http://p:1"), locale)
        with patch("services.proxy_service._egress_slot_locale", side_effect=RuntimeError("x")):
            self.assertIsNone(openai_register._locale_for_proxy("http://p:1"))

    def test_pix_locale_only_when_explicitly_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIX_BROWSER_LOCALE", None)
            os.environ.pop("PIX_BROWSER_TIMEZONE", None)
            self.assertIsNone(pix.payment_sentinel_locale())
        with patch.dict(os.environ, {"PIX_BROWSER_LOCALE": "pt-BR", "PIX_BROWSER_TIMEZONE": "America/Sao_Paulo"}):
            locale = pix.payment_sentinel_locale()
        self.assertEqual(locale.language, "pt-BR")
        self.assertEqual(locale.timezone, "America/Sao_Paulo")


if __name__ == "__main__":
    unittest.main()
