import base64
import json
import unittest
from unittest.mock import patch

from services import openai_backend_api
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_service import ProxyRuntimeProfile
from utils import sentinel_vm
from utils.pow import PowFingerprint

PREPARE = {
    "prepare_token": "prep-1",
    "proofofwork": {"required": True, "seed": "0.5", "difficulty": "0fffff"},
    "turnstile": {"required": False},
}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self) -> dict:
        return self._payload


def _backend(account: dict | None = None) -> OpenAIBackendAPI:
    account = {"access_token": "token", "fp": {"oai-device-id": "device-1"}} if account is None else account
    with patch.object(openai_backend_api.account_service, "get_account", return_value=account):
        return OpenAIBackendAPI(access_token="token", proxy_profile=ProxyRuntimeProfile(egress_key="direct"))


def _decode(token: str) -> list:
    return json.loads(base64.b64decode(token[7:]))


class SentinelRequirementsTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(config.data, {"chat_sentinel_vm_enabled": False})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = _backend()
        self.addCleanup(self.backend.close)
        self.posts: list[tuple[str, dict]] = []
        self.finalize_responses: list[FakeResponse] = []

    def _post(self, url: str, **kwargs):
        self.posts.append((url, kwargs.get("json") or {}))
        if url.endswith("/prepare"):
            return FakeResponse(200, PREPARE)
        if self.finalize_responses:
            return self.finalize_responses.pop(0)
        return FakeResponse(200, {"token": "chat-token"})

    def test_python_path_reuses_one_page_fingerprint(self) -> None:
        with patch.object(self.backend.session, "post", side_effect=self._post):
            first = self.backend._get_chat_requirements()
            second = self.backend._get_chat_requirements()

        self.assertEqual(first.token, "chat-token")
        p_tokens = [payload["p"] for url, payload in self.posts if url.endswith("/prepare")]
        first_config, second_config = _decode(p_tokens[0]), _decode(p_tokens[1])
        fingerprint = self.backend.pow_fingerprint
        for config_row in (first_config, second_config):
            self.assertEqual(config_row[14], fingerprint.sid)
            self.assertEqual(config_row[16], fingerprint.cores)
            self.assertEqual(config_row[0], fingerprint.screen_sum)
        proof_config = _decode(first.proof_token.split("~")[0])
        self.assertEqual(proof_config[14], fingerprint.sid)

    def test_device_environment_is_stable_across_backends_of_one_account(self) -> None:
        other = _backend()
        self.addCleanup(other.close)
        self.assertEqual(
            (self.backend.pow_fingerprint.screen_sum, self.backend.pow_fingerprint.cores),
            (other.pow_fingerprint.screen_sum, other.pow_fingerprint.cores),
        )
        self.assertNotEqual(self.backend.pow_fingerprint.sid, other.pow_fingerprint.sid)

    def test_account_without_stored_device_id_still_gets_a_stable_device(self) -> None:
        account = {"access_token": "token", "email": "a@example.com"}
        first, second = _backend(account), _backend(account)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertNotEqual(first.device_id, second.device_id)
        self.assertEqual(first._pow_device_seed(), "a@example.com")
        self.assertEqual(first.pow_fingerprint.screen_sum, second.pow_fingerprint.screen_sum)
        self.assertEqual(first.pow_fingerprint.cores, second.pow_fingerprint.cores)

    def test_vm_disabled_by_default_never_spawns_node(self) -> None:
        with (
            patch.dict(config.data, {}, clear=False),
            patch.object(sentinel_vm, "chat_requirements_token_via_vm") as vm_requirements,
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            config.data.pop("chat_sentinel_vm_enabled", None)
            self.backend._get_chat_requirements()
        vm_requirements.assert_not_called()

    def _start(self, patcher) -> None:
        patcher.start()
        self.addCleanup(patcher.stop)

    def _enable_vm(self):
        self._start(patch.dict(config.data, {"chat_sentinel_vm_enabled": True}))
        self._start(patch.object(sentinel_vm, "node_available", return_value=True))

    def test_vm_path_uses_sdk_tokens(self) -> None:
        self._enable_vm()
        with (
            patch.object(sentinel_vm, "chat_requirements_token_via_vm", return_value="gAAAAACvm") as vm_req,
            patch.object(sentinel_vm, "chat_proof_token_via_vm", return_value=("gAAAAABvm~S", "")) as vm_proof,
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            requirements = self.backend._get_chat_requirements()

        self.assertEqual(self.posts[0][1], {"p": "gAAAAACvm"})
        self.assertEqual(self.posts[1][1]["proof_token"], "gAAAAABvm~S")
        self.assertEqual(requirements.proof_token, "gAAAAABvm~S")
        env = vm_req.call_args.args[1]
        self.assertEqual(env.sid, self.backend.pow_fingerprint.sid)
        self.assertEqual(vm_proof.call_args.kwargs["challenge"], PREPARE)
        self.assertEqual(vm_proof.call_args.kwargs["request_p"], "gAAAAACvm")

    def test_vm_requirements_failure_falls_back_to_python_pow(self) -> None:
        self._enable_vm()
        with (
            patch.object(sentinel_vm, "chat_requirements_token_via_vm", side_effect=RuntimeError("node crashed")),
            patch.object(sentinel_vm, "chat_proof_token_via_vm") as vm_proof,
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            requirements = self.backend._get_chat_requirements()

        vm_proof.assert_not_called()
        self.assertTrue(self.posts[0][1]["p"].startswith("gAAAAAC"))
        self.assertFalse(requirements.proof_token.endswith("~S"))
        self.assertEqual(requirements.token, "chat-token")

    def test_vm_proof_failure_restarts_with_python_pow(self) -> None:
        self._enable_vm()
        with (
            patch.object(sentinel_vm, "chat_requirements_token_via_vm", return_value="gAAAAACvm"),
            patch.object(sentinel_vm, "chat_proof_token_via_vm", side_effect=RuntimeError("timeout")),
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            requirements = self.backend._get_chat_requirements()

        prepares = [payload for url, payload in self.posts if url.endswith("/prepare")]
        self.assertEqual(len(prepares), 2)
        self.assertNotEqual(prepares[1]["p"], "gAAAAACvm")
        self.assertEqual(requirements.token, "chat-token")

    def test_rejected_vm_tokens_retry_once_with_python_pow(self) -> None:
        self._enable_vm()
        self.finalize_responses = [FakeResponse(403, {"detail": "invalid proof"})]
        with (
            patch.object(sentinel_vm, "chat_requirements_token_via_vm", return_value="gAAAAACvm"),
            patch.object(sentinel_vm, "chat_proof_token_via_vm", return_value=("gAAAAABvm~S", "")),
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            requirements = self.backend._get_chat_requirements()

        finalizes = [payload for url, payload in self.posts if url.endswith("/finalize")]
        self.assertEqual(len(finalizes), 2)
        self.assertFalse(finalizes[1]["proof_token"].endswith("~S"))
        self.assertEqual(requirements.token, "chat-token")

    def test_auth_errors_are_not_retried(self) -> None:
        self._enable_vm()
        self.finalize_responses = [FakeResponse(401, {"detail": "token expired"})]
        with (
            patch.object(sentinel_vm, "chat_requirements_token_via_vm", return_value="gAAAAACvm"),
            patch.object(sentinel_vm, "chat_proof_token_via_vm", return_value=("gAAAAABvm~S", "")),
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            with self.assertRaises(Exception):
                self.backend._get_chat_requirements()
        self.assertEqual(len([url for url, _ in self.posts if url.endswith("/finalize")]), 1)

    def test_vm_skipped_when_node_is_missing(self) -> None:
        self._start(patch.dict(config.data, {"chat_sentinel_vm_enabled": True}))
        with (
            patch.object(sentinel_vm, "node_available", return_value=False),
            patch.object(sentinel_vm, "chat_requirements_token_via_vm") as vm_req,
            patch.object(self.backend.session, "post", side_effect=self._post),
        ):
            self.backend._get_chat_requirements()
        vm_req.assert_not_called()

    def test_warm_state_brings_its_page_fingerprint(self) -> None:
        warm_fingerprint = PowFingerprint.create("device-1")
        self.backend._apply_page_identity("session-x", [], ["https://a/x.js"], "prod", pow_fingerprint=warm_fingerprint)
        self.assertIs(self.backend.pow_fingerprint, warm_fingerprint)
        self.backend._apply_page_identity("session-y", [], [], "")
        self.assertIs(self.backend.pow_fingerprint, warm_fingerprint)


class BootstrapScriptPoolTests(unittest.TestCase):
    def test_bootstrap_adds_loader_and_injected_sdk(self) -> None:
        backend = _backend()
        self.addCleanup(backend.close)
        html = '<html><script src="/assets/app.js"></script><script src="https://cdn.oaistatic.com/c/b1/_x.js"></script></html>'
        sdk = "https://chatgpt.com/sentinel/v9/sdk.js"
        with (
            patch.object(backend.session, "get", return_value=FakeResponse(200, {})) as get,
            patch.object(sentinel_vm, "discover_sdk", return_value=("v9", sdk)),
        ):
            get.return_value.text = html
            backend._bootstrap()
        self.assertEqual(
            backend.pow_script_sources,
            [
                "https://chatgpt.com/assets/app.js",
                "https://cdn.oaistatic.com/c/b1/_x.js",
                "https://chatgpt.com/backend-api/sentinel/sdk.js",
                sdk,
            ],
        )
        self.assertEqual(backend.pow_data_build, "c/b1/_")


if __name__ == "__main__":
    unittest.main()
