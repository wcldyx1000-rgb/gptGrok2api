from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from utils import sentinel


class SentinelTokenTest(unittest.TestCase):
    def test_turnstile_token_is_not_reused_as_session_observer_token(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.text = "response"
        response.headers = {"set-cookie": "oai-sc=0server-cookie; Domain=.chatgpt.com; Path=/"}
        response.json.return_value = {
            "token": "challenge-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "turnstile-dx"},
            "so": {"required": True, "snapshot_dx": "opaque-input"},
        }
        session = MagicMock()
        session.post.return_value = response

        with (
            patch.object(sentinel, "_get_vm_sentinel_token", return_value=None),
            patch.object(sentinel, "solve_turnstile_token", return_value="turnstile-token"),
        ):
            sentinel_value, so_token, oai_sc = sentinel.build_sentinel_with_so_token(
                session,
                "device-id",
                "authorize_continue",
            )

        self.assertEqual(so_token, "")
        self.assertEqual(oai_sc, "0server-cookie")
        self.assertEqual(json.loads(sentinel_value)["t"], "turnstile-token")

    def test_missing_session_observer_token_does_not_reuse_turnstile_token(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.text = "response"
        response.headers = {"set-cookie": "oai-sc=0server-cookie; Domain=.chatgpt.com; Path=/"}
        response.json.return_value = {
            "token": "challenge-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "turnstile-dx"},
        }
        session = MagicMock()
        session.post.return_value = response

        with (
            patch.object(sentinel, "_get_vm_sentinel_token", return_value=None),
            patch.object(sentinel, "solve_turnstile_token", return_value="turnstile-token"),
        ):
            sentinel_value, so_token, _oai_sc = sentinel.build_sentinel_with_so_token(
                session,
                "device-id",
                "authorize_continue",
            )

        self.assertEqual(so_token, "")
        self.assertEqual(json.loads(sentinel_value)["t"], "turnstile-token")

    def test_vm_primary_token_is_used_before_synthetic_fallback(self) -> None:
        session = MagicMock()
        vm_token = json.dumps(
            {"p": "sdk-proof", "t": "sdk-turnstile", "c": "challenge-token", "id": "device-id", "flow": "authorize_continue"}
        )

        with patch.object(
            sentinel,
            "_get_vm_sentinel_token",
            return_value=(vm_token, "0vm-cookie"),
        ):
            sentinel_value, so_token, oai_sc = sentinel.build_sentinel_with_so_token(
                session,
                "device-id",
                "authorize_continue",
            )

        self.assertEqual(sentinel_value, vm_token)
        self.assertEqual(so_token, "")
        self.assertEqual(oai_sc, "0vm-cookie")


class SentinelGeneratorFieldsTest(unittest.TestCase):
    @staticmethod
    def _decode(token: str) -> list:
        import base64

        return json.loads(base64.b64decode(token[len("gAAAAAC"):]).decode("utf-8"))

    def test_device_fields_are_stable_per_device_id(self) -> None:
        first = sentinel.SentinelTokenGenerator("device-a", "UA")
        second = sentinel.SentinelTokenGenerator("device-a", "UA")
        a, b = self._decode(first.generate_requirements_token()), self._decode(second.generate_requirements_token())
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[2], b[2])
        self.assertEqual(a[16], b[16])
        self.assertEqual(a[0], first.screen_sum)
        self.assertNotEqual(a[14], b[14])

    def test_locale_and_navigator_probe_fields(self) -> None:
        from utils.locale_env import LocaleEnv

        locale = LocaleEnv(timezone="Asia/Taipei", language="zh-TW")
        generator = sentinel.SentinelTokenGenerator("device-b", "UA", locale, "https://x/sentinel/v/sdk.js")
        data = self._decode(generator.generate_requirements_token())
        self.assertEqual(data[3], 1)
        self.assertEqual(data[5], "https://x/sentinel/v/sdk.js")
        self.assertEqual(data[7], "zh-TW")
        self.assertEqual(data[8], "zh-TW,zh,en-US,en")
        self.assertIn("GMT+0800", data[1])
        # navigator.doNotTrack 为 null 时 SDK 的 toString() 抛错，只返回属性名（见 utils/pow.py），
        # 所以该探测项允许不带“−”（随机抽到时约占 2%）。
        self.assertTrue("−" in data[10] or data[10] == "doNotTrack")
        self.assertIsInstance(data[9], float)

    def test_pow_slot_9_is_elapsed_ms(self) -> None:
        import base64

        generator = sentinel.SentinelTokenGenerator("device-c", "UA")
        token = generator.generate_token("seed", "f")
        self.assertTrue(token.startswith("gAAAAAB") and token.endswith("~S"))
        data = json.loads(base64.b64decode(token[len("gAAAAAB"):-2]).decode("utf-8"))
        self.assertIsInstance(data[9], int)

    def test_frame_sdk_src_is_parsed_and_cached(self) -> None:
        html = '<html><script src="/sentinel/20991231abcd/sdk.js"></script></html>'
        response = MagicMock(status_code=200, text=html)
        session = MagicMock()
        session.get.return_value = response
        sentinel._frame_sdk_cache = None
        try:
            src = sentinel.sentinel_frame_sdk_src(session, "UA")
            again = sentinel.sentinel_frame_sdk_src(session, "UA")
        finally:
            sentinel._frame_sdk_cache = None
        self.assertEqual(src, "https://chatgpt.com/sentinel/20991231abcd/sdk.js")
        self.assertEqual(again, src)
        self.assertEqual(session.get.call_count, 1)

    def test_frame_sdk_src_failure_falls_back_to_default(self) -> None:
        session = MagicMock()
        session.get.side_effect = RuntimeError("boom")
        sentinel._frame_sdk_cache = None
        try:
            self.assertEqual(sentinel.sentinel_frame_sdk_src(session, "UA"), "")
        finally:
            sentinel._frame_sdk_cache = None
        generator = sentinel.SentinelTokenGenerator("d", "UA", None, "")
        self.assertEqual(generator.sdk_src, sentinel.DEFAULT_SENTINEL_SDK_SRC)


if __name__ == "__main__":
    unittest.main()
