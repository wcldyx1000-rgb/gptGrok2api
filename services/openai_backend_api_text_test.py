import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import model_catalog_service, openai_backend_api
from services.config import config
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import anthropic_v1_messages
from services.proxy_service import ProxyRuntimeProfile


class FakeResponse:
    status_code = 200
    text = "{}"
    headers: dict = {}

    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def iter_lines(self, *args, **kwargs):
        return iter(())

    def close(self) -> None:
        pass


def _backend(token: str = "token") -> OpenAIBackendAPI:
    account = {"access_token": token, "fp": {"oai-device-id": "device-1"}} if token else None
    with patch.object(openai_backend_api.account_service, "get_account", return_value=account):
        return OpenAIBackendAPI(access_token=token, proxy_profile=ProxyRuntimeProfile(egress_key="direct"))


MESSAGES = [{"role": "user", "content": "你好"}]


class TextConversationFlowTest(unittest.TestCase):
    def _run(self, backend: OpenAIBackendAPI, **kwargs) -> list[dict]:
        calls: list[dict] = []

        def fake_post(url, **kw):
            calls.append({"step": url.rsplit("/backend", 1)[-1], "headers": kw.get("headers") or {}, "json": kw.get("json")})
            if url.endswith("/f/conversation/prepare"):
                return FakeResponse({"conduit_token": "conduit-1"})
            return FakeResponse()

        def fake_requirements():
            calls.append({"step": "sentinel"})
            return ChatRequirements(token="req-token", proof_token="proof", turnstile_token="", so_token="")

        with (
            patch.object(backend, "_bootstrap"),
            patch.object(backend, "_get_chat_requirements", side_effect=fake_requirements),
            patch.object(backend.session, "post", side_effect=fake_post),
            patch.object(openai_backend_api, "iter_sse_payloads", return_value=iter(())),
        ):
            list(backend.stream_conversation(messages=MESSAGES, **kwargs))
        return calls

    def test_authenticated_order_is_prepare_sentinel_conversation(self) -> None:
        with _backend() as backend:
            calls = self._run(backend, model="auto")
        self.assertEqual([c["step"] for c in calls], ["-api/f/conversation/prepare", "sentinel", "-api/f/conversation"])
        prepare, _, conversation = calls
        self.assertEqual(prepare["json"]["partial_query"]["content"]["parts"], ["你好"])
        self.assertEqual(prepare["json"]["client_prepare_dispatch"], "debounced")
        self.assertEqual(conversation["headers"]["X-Conduit-Token"], "conduit-1")
        self.assertEqual(conversation["headers"]["OpenAI-Sentinel-Chat-Requirements-Token"], "req-token")
        self.assertEqual(conversation["headers"]["X-Oai-Turn-Trace-Id"], prepare["headers"]["X-Oai-Turn-Trace-Id"])

    def test_payload_matches_web_shape(self) -> None:
        with _backend() as backend:
            payload = self._run(backend, model="gpt-6")[-1]["json"]
        self.assertEqual(payload["model"], "gpt-6")
        self.assertEqual(payload["parent_message_id"], "client-created-root")
        self.assertEqual(payload["supported_encodings"], ["v1"])
        self.assertEqual(payload["turn_attribution"], {"turn_trigger": "composer"})
        self.assertEqual(payload["client_contextual_info"], {"app_name": "chatgpt.com"})
        message = payload["messages"][0]
        self.assertEqual(message["author"]["role"], "user")
        self.assertEqual(message["content"]["parts"], ["你好"])
        self.assertEqual(message["recipient"], "all")
        self.assertNotIn("thinking_effort", payload)
        self.assertNotIn("system_hints", payload)

    def test_prepare_failure_does_not_block_conversation(self) -> None:
        with _backend() as backend:
            with patch.object(backend, "_prepare_text_conversation", return_value=""):
                calls = self._run(backend)
        self.assertNotIn("X-Conduit-Token", calls[-1]["headers"])

    def test_anonymous_flow_unchanged(self) -> None:
        with _backend("") as backend:
            calls = self._run(backend)
        self.assertEqual([c["step"] for c in calls], ["sentinel", "-anon/conversation"])
        payload = calls[-1]["json"]
        self.assertTrue(payload["history_and_training_disabled"])
        self.assertEqual(payload["conversation_mode"], {"kind": "primary_assistant"})

    def test_reasoning_effort_sets_reason_system_hint(self) -> None:
        for effort in ("low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort), _backend() as backend:
                calls = self._run(backend, thinking_effort=effort)
            self.assertEqual(calls[0]["json"]["system_hints"], ["reason"])
            self.assertEqual(calls[-1]["json"]["system_hints"], ["reason"])
            self.assertNotIn("thinking_effort", calls[-1]["json"])
        with _backend() as backend:
            calls = self._run(backend, thinking_effort="none")
        self.assertNotIn("system_hints", calls[-1]["json"])

    def test_temporary_mode_switch(self) -> None:
        with _backend() as backend:
            payload = self._run(backend)[-1]["json"]
        self.assertTrue(payload["history_and_training_disabled"])
        with patch.dict(config.data, {"chat_temporary_mode": False}), _backend() as backend:
            calls = self._run(backend)
        for call in (calls[0], calls[-1]):
            self.assertNotIn("history_and_training_disabled", call["json"])

    def test_temporary_mode_defaults_to_true(self) -> None:
        with patch.dict(config.data, {}, clear=False):
            config.data.pop("chat_temporary_mode", None)
            self.assertTrue(config.chat_temporary_mode)


class ThinkingEffortPassThroughTest(unittest.TestCase):
    def test_messages_endpoint_reads_effort_params(self) -> None:
        with patch.object(anthropic_v1_messages, "text_backend", return_value=SimpleNamespace()):
            for body, expected in (
                ({"reasoning_effort": "high"}, "high"),
                ({"thinking_effort": "medium"}, "medium"),
                ({"reasoning": {"effort": "low"}}, "low"),
                ({}, ""),
            ):
                request = anthropic_v1_messages.message_request({"model": "auto", "messages": [{"role": "user", "content": "hi"}], **body})
                self.assertEqual(request.thinking_effort, expected)

    def test_chat_and_responses_pass_effort_to_request(self) -> None:
        from services.protocol import openai_v1_chat_complete, openai_v1_response

        seen: list[str] = []

        def fake_deltas(backend, request):
            seen.append(request.thinking_effort)
            yield "ok"

        with patch.object(openai_v1_chat_complete, "stream_text_deltas", side_effect=fake_deltas):
            list(openai_v1_chat_complete.stream_text_chat_completion(SimpleNamespace(), MESSAGES, "auto", "high"))
        self.assertEqual(seen, ["high"])
        self.assertTrue(hasattr(openai_v1_response, "thinking_effort_from_body"))


class PickerModelsTest(unittest.TestCase):
    def test_list_picker_models_follows_site_order(self) -> None:
        data = {
            "default_model_slug": "auto",
            "categories": [{"default_model": "gpt-6-t-mini"}, {"default_model": "gpt-6"}, {"default_model": "gpt-6"}, {}],
        }
        with _backend() as backend, patch.object(backend.session, "get", return_value=FakeResponse(data)):
            self.assertEqual(backend.list_picker_models(), ["auto", "gpt-6-t-mini", "gpt-6"])


class UpstreamChatModelsCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = model_catalog_service.upstream_chat_models
        self.cache.set([])
        self.addCleanup(self.cache.set, [])

    def _catalog(self, settings: dict | None = None) -> dict:
        with (
            patch.object(model_catalog_service.config, "get", return_value=settings or {}),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
        ):
            return model_catalog_service.get_model_catalog()

    def test_fallback_when_upstream_not_loaded(self) -> None:
        catalog = self._catalog()
        self.assertEqual(catalog["source"]["chat"], "fallback")
        for model in ("auto", "gpt-6-t-mini", "gpt-6"):
            self.assertIn(model, catalog["chat_models"])
        self.assertIn("gpt-image-2.5", catalog["image_models"])

    def test_get_does_not_touch_network(self) -> None:
        with patch.object(model_catalog_service.account_service, "get_text_access_token") as token:
            self.cache.get()
            self._catalog()
        token.assert_not_called()

    def test_refresh_success_feeds_catalog(self) -> None:
        fake_cls = _fake_backend_cls(["auto", "gpt-6-x"])
        with (
            patch.object(model_catalog_service.account_service, "get_text_access_token", return_value="tok"),
            patch.object(openai_backend_api, "OpenAIBackendAPI", fake_cls),
        ):
            self.assertTrue(self.cache.refresh())
        catalog = self._catalog()
        self.assertEqual(catalog["source"]["chat"], "upstream")
        self.assertIn("gpt-6-x", catalog["chat_models"])
        self.assertNotIn("gpt-6", catalog["chat_models"])

    def test_refresh_failure_keeps_previous_and_fallback(self) -> None:
        with (
            patch.object(model_catalog_service.account_service, "get_text_access_token", side_effect=RuntimeError("boom")),
        ):
            self.assertFalse(self.cache.refresh())
        self.assertEqual(self._catalog()["source"]["chat"], "fallback")
        self.cache.set(["auto", "kept"])
        with patch.object(model_catalog_service.account_service, "get_text_access_token", return_value=""):
            self.assertFalse(self.cache.refresh())
        self.assertIn("kept", self._catalog()["chat_models"])

    def test_explicit_config_wins_and_grok_models_survive(self) -> None:
        self.cache.set(["auto", "gpt-6-x"])
        grok = {"chat_models": ["grok-4"], "image_models": [], "image_edit_models": [], "video_models": []}
        settings = {"model_catalog": {"chat_models": ["my-model"]}}
        with (
            patch.object(model_catalog_service.config, "get", return_value=settings),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
            patch("services.grok_runtime.grok_runtime") as runtime,
        ):
            runtime.available = True
            runtime.run_sync.return_value = grok
            catalog = model_catalog_service.get_model_catalog()
        self.assertEqual(catalog["source"]["chat"], "config")
        self.assertEqual(catalog["chat_models"], ["my-model", "grok-4"])

    def test_scheduler_refreshes_then_stops(self) -> None:
        stop = threading.Event()
        done = threading.Event()

        def fake_refresh() -> bool:
            done.set()
            return True

        with patch.object(self.cache, "refresh", side_effect=fake_refresh):
            thread = self.cache.start(stop)
            self.assertTrue(done.wait(2))
            stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())


def _fake_backend_cls(models: list[str]):
    class FakeBackend:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def list_picker_models(self) -> list[str]:
            return list(models)

    return FakeBackend


if __name__ == "__main__":
    unittest.main()
