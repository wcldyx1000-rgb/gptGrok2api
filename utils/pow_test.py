import base64
import json
import unittest

from utils.locale_env import LocaleEnv
from utils.pow import (
    CHATGPT_PAGE_WINDOW_KEYS,
    JS_HEAP_SIZE_LIMIT_8GB,
    JS_HEAP_SIZE_LIMIT_HIGH_MEMORY,
    WINDOW_OWN_KEYS,
    PowFingerprint,
    build_legacy_requirements_token,
    build_proof_token,
    parse_pow_resources,
    parse_sentinel_loader,
    with_injected_scripts,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"


def _decode(token: str) -> list:
    return json.loads(base64.b64decode(token[7:]))


class PowFingerprintTests(unittest.TestCase):
    def test_device_fields_are_stable_per_device_id(self) -> None:
        first = PowFingerprint.create("device-1")
        second = PowFingerprint.create("device-1")
        self.assertEqual(
            (first.screen_sum, first.cores, first.heap_size_limit, first.screen_width, first.screen_height),
            (second.screen_sum, second.cores, second.heap_size_limit, second.screen_width, second.screen_height),
        )
        # sid / timeOrigin / React key belong to one page load.
        self.assertNotEqual(first.sid, second.sid)
        self.assertEqual(first.screen_width + first.screen_height, first.screen_sum)
        self.assertIn(first.heap_size_limit, (JS_HEAP_SIZE_LIMIT_HIGH_MEMORY, JS_HEAP_SIZE_LIMIT_8GB))

    def test_document_keys_share_one_react_suffix(self) -> None:
        container, listening, location = PowFingerprint.create("d").document_keys
        self.assertTrue(container.startswith("__reactContainer$"))
        self.assertEqual(container.split("$", 1)[1], listening.removeprefix("_reactListening"))
        self.assertEqual(location, "location")


class PowConfigTests(unittest.TestCase):
    def test_tokens_of_one_page_share_session_fields(self) -> None:
        fingerprint = PowFingerprint.create("device-1")
        locale = LocaleEnv(timezone="Europe/Berlin", language="de-DE")
        first = _decode(build_legacy_requirements_token(UA, ["https://a/x.js"], "prod-1", locale, fingerprint))
        second = _decode(build_legacy_requirements_token(UA, ["https://a/x.js"], "prod-1", locale, fingerprint))

        for index in (0, 2, 14, 16, 17):
            self.assertEqual(first[index], second[index], index)
        self.assertEqual(first[14], fingerprint.sid)
        self.assertEqual(first[16], fingerprint.cores)
        self.assertEqual(first[7], "de-DE")
        self.assertEqual(first[8], "de-DE,de,en-US,en")
        self.assertRegex(first[1], r"GMT\+0[12]00 \(Central European (Standard|Summer) Time\)$")
        self.assertEqual(first[3], 1)
        self.assertIsInstance(first[9], float)  # elapsed config time, not Math.random()
        self.assertLess(first[9], 1000)
        self.assertIn(first[11], fingerprint.document_keys)
        self.assertIn(first[12], (*WINDOW_OWN_KEYS, *CHATGPT_PAGE_WINDOW_KEYS))
        self.assertEqual(first[18:], [0] * 7)
        self.assertEqual(len(first), 25)

    def test_navigator_probe_agrees_with_language_and_cores(self) -> None:
        fingerprint = PowFingerprint.create("device-2")
        locale = LocaleEnv(timezone="Asia/Tokyo", language="ja-JP")
        for _ in range(300):
            probe = _decode(build_legacy_requirements_token(UA, locale=locale, fingerprint=fingerprint))[10]
            if probe.startswith("language−"):
                self.assertEqual(probe, "language−ja-JP")
            if probe.startswith("hardwareConcurrency−"):
                self.assertEqual(probe, f"hardwareConcurrency−{fingerprint.cores}")
            self.assertNotEqual(probe, "doNotTrack−null")

    def test_proof_token_records_attempts_and_elapsed_ms(self) -> None:
        fingerprint = PowFingerprint.create("device-1")
        token = build_proof_token("0.42", "0fffff", UA, fingerprint=fingerprint)
        self.assertTrue(token.startswith("gAAAAAB"))
        config = _decode(token)
        self.assertIsInstance(config[3], int)
        self.assertIsInstance(config[9], int)
        self.assertGreaterEqual(config[9], 0)
        self.assertEqual(config[14], fingerprint.sid)


class PageResourceTests(unittest.TestCase):
    def test_resolves_relative_scripts_and_first_build_marker(self) -> None:
        html = (
            '<html data-build="prod-html"><script src="/assets/app.js"></script>'
            '<script src="https://cdn.oaistatic.com/c/first/_next/a.js"></script>'
            '<script src="https://cdn.oaistatic.com/c/second/_next/b.js"></script></html>'
        )
        sources, build = parse_pow_resources(html, "https://chatgpt.com/")
        self.assertEqual(sources[0], "https://chatgpt.com/assets/app.js")
        self.assertEqual(build, "c/first/_")

    def test_injected_loader_and_sdk_join_the_pool_once(self) -> None:
        sdk = parse_sentinel_loader("s.src='https://chatgpt.com/sentinel/v1/sdk.js'")
        pool = with_injected_scripts(["https://a/x.js", sdk], "https://chatgpt.com/", sdk)
        self.assertEqual(pool, ["https://a/x.js", sdk, "https://chatgpt.com/backend-api/sentinel/sdk.js"])
        self.assertEqual(parse_sentinel_loader("no script here"), "")


if __name__ == "__main__":
    unittest.main()
