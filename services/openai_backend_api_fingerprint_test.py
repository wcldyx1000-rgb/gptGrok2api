import unittest
from unittest.mock import patch

from services import openai_backend_api
from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_service import ProxyRuntimeProfile
from utils.fingerprint_pool import pick_fingerprint
from utils.locale_env import LocaleEnv

CHROME_136_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


def _backend(account: dict, locale: LocaleEnv | None = None) -> OpenAIBackendAPI:
    with patch.object(openai_backend_api.account_service, "get_account", return_value=account):
        return OpenAIBackendAPI(access_token="token", proxy_profile=ProxyRuntimeProfile(locale=locale))


class BackendFingerprintTest(unittest.TestCase):
    def test_account_without_fp_uses_stable_pool_fingerprint(self) -> None:
        account = {"email": "pool@example.com", "access_token": "token"}
        with _backend(account) as first, _backend(account) as second:
            expected = pick_fingerprint("pool@example.com")
            self.assertEqual(first.user_agent, expected["user-agent"])
            self.assertEqual(first.fp["impersonate"], expected["impersonate"])
            self.assertEqual(second.user_agent, first.user_agent)
            major = first.fp["impersonate"].removeprefix("chrome")
            self.assertIn(f'v="{major}"', first.session.headers["Sec-Ch-Ua"])
            self.assertNotIn("Microsoft Edge", first.session.headers["Sec-Ch-Ua"])

    def test_existing_fp_keeps_ua_and_tls_target(self) -> None:
        account = {
            "email": "old@example.com",
            "fp": {"user-agent": CHROME_136_WINDOWS, "impersonate": "chrome136"},
        }
        with _backend(account) as backend:
            self.assertEqual(backend.user_agent, CHROME_136_WINDOWS)
            self.assertEqual(backend.fp["impersonate"], "chrome136")
            self.assertIn('"Google Chrome";v="136"', backend.session.headers["Sec-Ch-Ua"])
            # 精简 UA 不编造完整版本号。
            self.assertNotIn("Sec-Ch-Ua-Full-Version-List", backend.session.headers)

    def test_existing_ua_without_tls_target_keeps_legacy_target(self) -> None:
        account = {"fp": {"user-agent": CHROME_136_WINDOWS}}
        with _backend(account) as backend:
            self.assertEqual(backend.fp["impersonate"], openai_backend_api.LEGACY_IMPERSONATE)

    def test_stale_stored_client_hints_are_replaced(self) -> None:
        account = {
            "fp": {
                "user-agent": CHROME_136_WINDOWS,
                "impersonate": "chrome136",
                "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not_A Brand";v="99"',
            },
        }
        with _backend(account) as backend:
            self.assertNotIn("Not_A Brand", backend.session.headers["Sec-Ch-Ua"])

    def test_bootstrap_headers_match_session_client_hints(self) -> None:
        with _backend({"email": "boot@example.com"}) as backend:
            headers = backend._bootstrap_headers()
            self.assertEqual(headers["Sec-Ch-Ua"], backend.session.headers["Sec-Ch-Ua"])
            self.assertEqual(headers["Sec-Ch-Ua-Platform"], backend.session.headers["Sec-Ch-Ua-Platform"])


class BackendLocaleTest(unittest.TestCase):
    def test_default_locale_is_unchanged(self) -> None:
        with _backend({"email": "cn@example.com"}) as backend:
            self.assertEqual(backend.session.headers["OAI-Language"], "zh-CN")
            self.assertTrue(backend.session.headers["Accept-Language"].startswith("zh-CN"))
            self.assertEqual(backend._timezone_offset_min(), -480)
            self.assertEqual(backend._timezone_name(), "Asia/Shanghai")
            self.assertEqual(backend._chat_target()[1], "Asia/Shanghai")

    def test_egress_locale_drives_headers_and_timezone(self) -> None:
        locale = LocaleEnv("America/New_York", "en-US")
        with _backend({"email": "us@example.com"}, locale) as backend:
            self.assertEqual(backend.session.headers["OAI-Language"], "en-US")
            self.assertEqual(backend.session.headers["Accept-Language"], "en-US,en;q=0.9")
            self.assertEqual(backend._timezone_name(), "America/New_York")
            self.assertEqual(backend._timezone_offset_min(), locale.offset_min())
            self.assertEqual(backend._chat_target()[1], "America/New_York")
            payload = backend._conversation_payload([], "auto", backend._chat_target()[1])
            self.assertEqual(payload["timezone"], "America/New_York")
            self.assertEqual(payload["timezone_offset_min"], locale.offset_min())

    def test_pow_config_uses_egress_locale(self) -> None:
        locale = LocaleEnv("Europe/Berlin", "de-DE")
        with _backend({"email": "de@example.com"}, locale) as backend:
            with patch.object(openai_backend_api, "build_proof_token", return_value="proof") as proof:
                backend._build_requirements({"token": "t", "proofofwork": {"required": True, "seed": "s", "difficulty": "0f"}})
            self.assertIs(proof.call_args.kwargs["locale"], locale)


if __name__ == "__main__":
    unittest.main()
