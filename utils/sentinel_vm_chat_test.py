import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import sentinel_vm
from utils.locale_env import LocaleEnv
from utils.pow import CHATGPT_PAGE_WINDOW_KEYS, WINDOW_OWN_KEYS, PowFingerprint


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
SDK_URL = "https://chatgpt.com/sentinel/20260810913b/sdk.js"

# A stand-in for the official SDK: same `var x=new Y;` instance shape, and its
# tokens simply report what the page environment exposes to Sentinel.
FAKE_SDK = r"""
var SentinelSDK=function(t){
class O{
  constructor(){ this.sid = crypto.randomUUID(); }
  config(){
    const proto = Object.getPrototypeOf(navigator);
    const navKeys = Object.keys(proto);
    return {
      date: "" + new Date,
      offset: new Date().getTimezoneOffset(),
      sid: this.sid,
      heap: performance.memory.jsHeapSizeLimit,
      now: performance.now(),
      origin: performance.timeOrigin,
      scripts: Array.from(document.scripts).map((s) => s.src),
      build: document.documentElement.getAttribute("data-build"),
      docKeys: Object.keys(document),
      winKeys: Object.keys(window),
      nav: navKeys.map((k) => { try { return k + "−" + navigator[k].toString(); } catch { return k; } }),
      lang: navigator.language,
      langs: navigator.languages,
      cores: navigator.hardwareConcurrency,
      search: location.search,
      presence: ["ai", "cache", "solana", "dump", "InstallTrigger"].map((k) => k in window),
    };
  }
  async getRequirementsToken(){ return "gAAAAAC" + btoa(unescape(encodeURIComponent(JSON.stringify(this.config())))); }
  async getEnforcementToken(c){ return "gAAAAAB" + btoa(JSON.stringify({ seed: c.proofofwork && c.proofofwork.seed, sid: this.sid })) + "~S"; }
}
var E=new O;
t.token=function(){};
return t}({});
"""


def _decode(token: str) -> dict:
    body = token[7:].split("~", 1)[0]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.b64decode(body).decode("utf-8"))


def _node_runs_multiline_scripts() -> bool:
    try:
        node = sentinel_vm._node_binary()
        result = subprocess.run(
            [node, "-e", "const a = 'o';\nprocess.stdout.write(a + 'k')"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.stdout == "ok"
    except Exception:
        return False


class ChatPageEnvTests(unittest.TestCase):
    def test_env_shares_fingerprint_and_locale(self) -> None:
        fingerprint = PowFingerprint.create("device-1")
        locale = LocaleEnv(timezone="Asia/Tokyo", language="ja-JP")
        env = sentinel_vm.chat_page_env(
            fingerprint,
            device_id="device-1",
            user_agent=UA,
            locale=locale,
            script_sources=["https://cdn.oaistatic.com/a.js", SDK_URL],
            data_build="prod-1",
        )
        payload = env.payload(sdk_url=SDK_URL)

        self.assertEqual(payload["sid"], fingerprint.sid)
        self.assertEqual(payload["time_origin"], fingerprint.time_origin_ms)
        self.assertEqual(payload["screen_width"] + payload["screen_height"], fingerprint.screen_sum)
        self.assertEqual(payload["hardware_concurrency"], fingerprint.cores)
        self.assertEqual(payload["js_heap_size_limit"], fingerprint.heap_size_limit)
        self.assertEqual(payload["language"], "ja-JP")
        self.assertEqual(payload["languages"], ["ja-JP", "ja", "en-US", "en"])
        self.assertEqual(payload["timezone_offset_min"], -540)
        self.assertEqual(payload["timezone_long_name"], "Japan Standard Time")
        self.assertEqual(payload["page_url"], "https://chatgpt.com/")
        # The SDK itself is document.currentScript, not a duplicate page script.
        self.assertEqual(payload["page_scripts"], ["https://cdn.oaistatic.com/a.js"])
        self.assertEqual(set(payload["document_keys"]), set(fingerprint.document_keys))
        self.assertEqual(payload["window_keys"], [*WINDOW_OWN_KEYS, *CHATGPT_PAGE_WINDOW_KEYS])
        self.assertIn(["language", "ja-JP"], payload["navigator_probes"])
        self.assertIn(["hardwareConcurrency", str(fingerprint.cores)], payload["navigator_probes"])

    def test_env_without_locale_matches_python_pow_defaults(self) -> None:
        env = sentinel_vm.chat_page_env(
            PowFingerprint.create("device-1"),
            device_id="device-1",
            user_agent=UA,
            locale=None,
            script_sources=[],
            data_build="",
        )
        self.assertEqual(env.language, "en-US")
        self.assertEqual(env.languages, ("en-US", "es-US", "en", "es"))
        self.assertEqual(env.timezone_offset_min, 300)
        self.assertEqual(env.timezone_long_name, "Eastern Standard Time")


class ChatVmTokenTests(unittest.TestCase):
    def _env(self) -> sentinel_vm.ChatPageEnv:
        return sentinel_vm.chat_page_env(
            PowFingerprint.create("device-1"),
            device_id="device-1",
            user_agent=UA,
            locale=None,
            script_sources=["https://cdn.oaistatic.com/a.js"],
            data_build="prod-1",
        )

    def test_requirements_token_rejects_malformed_output(self) -> None:
        with (
            patch.object(sentinel_vm, "_chat_sdk_file", return_value=(Path("sdk.js"), SDK_URL)),
            patch.object(sentinel_vm, "_run_action", return_value={"request_p": "not-a-token"}),
        ):
            with self.assertRaises(RuntimeError):
                sentinel_vm.chat_requirements_token_via_vm(object(), self._env())

    def test_proof_token_drops_short_turnstile_value(self) -> None:
        with (
            patch.object(sentinel_vm, "_chat_sdk_file", return_value=(Path("sdk.js"), SDK_URL)),
            patch.object(sentinel_vm, "_run_action", return_value={"final_p": "gAAAAABabc~S", "t": "x"}) as run,
        ):
            proof, turnstile = sentinel_vm.chat_proof_token_via_vm(
                object(), self._env(), request_p="gAAAAACp", challenge={"proofofwork": {"seed": "1"}},
            )
        self.assertEqual(proof, "gAAAAABabc~S")
        self.assertEqual(turnstile, "")
        payload = run.call_args.kwargs["payload"]
        self.assertEqual(payload["action"], "solve")
        self.assertEqual(payload["request_p"], "gAAAAACp")
        self.assertEqual(payload["challenge"], {"proofofwork": {"seed": "1"}})

    def test_discover_sdk_caches_and_falls_back(self) -> None:
        class Response:
            status_code = 200
            text = "x https://chatgpt.com/sentinel/abc123/sdk.js y"

        class Session:
            calls = 0

            def get(self, *args, **kwargs):
                Session.calls += 1
                return Response()

        with patch.object(sentinel_vm, "_sdk_discovery", None):
            self.assertEqual(sentinel_vm.discover_sdk(Session()), ("abc123", "https://chatgpt.com/sentinel/abc123/sdk.js"))
            sentinel_vm.discover_sdk(Session())
            self.assertEqual(Session.calls, 1)

        class Broken:
            def get(self, *args, **kwargs):
                raise OSError("offline")

        with patch.object(sentinel_vm, "_sdk_discovery", None):
            self.assertEqual(sentinel_vm.discover_sdk(Broken()), (sentinel_vm.SENTINEL_VERSION, sentinel_vm.SENTINEL_SDK_URL))


@unittest.skipUnless(_node_runs_multiline_scripts(), "Node.js is not available")
class ChatVmRuntimeTests(unittest.TestCase):
    """Runs the real Node runner against a fake SDK to check the emulated page."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.sdk_file = Path(cls._tmp.name) / "sdk.js"
        cls.sdk_file.write_text(FAKE_SDK, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _run(self, env: sentinel_vm.ChatPageEnv, action: str, **extra) -> dict:
        return sentinel_vm._run_action(
            sdk_file=self.sdk_file,
            payload={**env.payload(sdk_url=SDK_URL), "action": action, **extra},
            timeout_seconds=20,
        )

    def test_page_environment_looks_like_chatgpt_tab(self) -> None:
        fingerprint = PowFingerprint.create("device-1")
        env = sentinel_vm.chat_page_env(
            fingerprint,
            device_id="device-1",
            user_agent=UA,
            locale=LocaleEnv(timezone="Asia/Tokyo", language="ja-JP"),
            script_sources=["https://cdn.oaistatic.com/c/abc/_next/a.js"],
            data_build="prod-1",
        )
        config = _decode(self._run(env, "requirements")["request_p"])

        self.assertEqual(config["sid"], fingerprint.sid)
        self.assertEqual(config["heap"], fingerprint.heap_size_limit)
        self.assertAlmostEqual(config["origin"], fingerprint.time_origin_ms, places=3)
        self.assertGreater(config["now"], 0)
        self.assertRegex(config["date"], r"^\w{3} \w{3} \d{2} \d{4} \d{2}:\d{2}:\d{2} GMT\+0900 \(Japan Standard Time\)$")
        self.assertEqual(config["offset"], -540)
        self.assertEqual(config["scripts"], ["https://cdn.oaistatic.com/c/abc/_next/a.js", SDK_URL])
        self.assertEqual(config["build"], "prod-1")
        self.assertEqual(set(config["docKeys"]), set(fingerprint.document_keys))
        # No Node globals or runner internals leak into Object.keys(window).
        self.assertEqual(set(config["winKeys"]), {*WINDOW_OWN_KEYS, *CHATGPT_PAGE_WINDOW_KEYS})
        self.assertIn("vendor−Google Inc.", config["nav"])
        self.assertIn("webdriver−false", config["nav"])
        self.assertIn("doNotTrack", config["nav"])
        self.assertIn(f"hardwareConcurrency−{fingerprint.cores}", config["nav"])
        self.assertEqual(config["lang"], "ja-JP")
        self.assertEqual(config["langs"], ["ja-JP", "ja", "en-US", "en"])
        self.assertEqual(config["cores"], fingerprint.cores)
        self.assertEqual(config["search"], "")
        self.assertEqual(config["presence"], [False] * 5)

    def test_requirements_and_proof_share_the_page_sid(self) -> None:
        fingerprint = PowFingerprint.create("device-1")
        env = sentinel_vm.chat_page_env(
            fingerprint, device_id="device-1", user_agent=UA, locale=None, script_sources=[], data_build="",
        )
        solved = self._run(env, "solve", request_p="gAAAAACx", challenge={"proofofwork": {"seed": "s1"}})
        proof = _decode(solved["final_p"])
        self.assertEqual(proof, {"seed": "s1", "sid": fingerprint.sid})
        self.assertIsNone(solved["t"])

    def test_frame_mode_keeps_registration_defaults(self) -> None:
        config = _decode(self._run_frame())
        self.assertEqual(config["lang"], "pt-BR")
        self.assertEqual(config["heap"], 4294705152)
        # timeOrigin is a recent page load, not a fixed 2024 timestamp.
        self.assertGreater(config["origin"], 1_760_000_000_000)
        self.assertTrue(config["search"].startswith("?sv="))

    def _run_frame(self) -> str:
        result = sentinel_vm._run_action(
            sdk_file=self.sdk_file,
            payload={
                "action": "requirements",
                "device_id": "device-1",
                "user_agent": UA,
                "sdk_url": SDK_URL,
                "frame_url": "https://chatgpt.com/backend-api/sentinel/frame.html?sv=20260810913b",
            },
            timeout_seconds=20,
        )
        return result["request_p"]


if __name__ == "__main__":
    unittest.main()
