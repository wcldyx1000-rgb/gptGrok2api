"""Image pipeline robustness: request deadline, queue limit, structured failures, downloads, recovery.

Ported from yukkcat/chatgpt2api (AGPL-3.0-only) behaviours; all upstream calls are mocked.
"""

from __future__ import annotations

import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from curl_cffi.requests import exceptions as curl_exceptions

from services import image_failure as failures
from services import openai_backend_api
from services.account_service import AccountService, ImageAccountSelectionError
from services.account_service_test import _MemoryStorage, _image_account
from services.config import config
from services.openai_backend_api import ImageDownloadError, OpenAIBackendAPI
from services.protocol import conversation as conv
from services.proxy_service import ImageEgressDeadlineError, ProxyReferenceUnavailableError, ProxyRuntimeProfile
from utils.helper import UpstreamHTTPError

FILE_ID = "file_00000000" + "a" * 24


# ------------------------------------------------------------------ account queue / deadline
class AccountQueueDeadlineTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(config.data, {
            "image_account_cooldown_secs": 0,
            "image_account_concurrency": 1,
            "image_queue_limit": 1,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        log = patch("services.account_service.log_service")
        log.start()
        self.addCleanup(log.stop)
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            self.service = AccountService(_MemoryStorage([_image_account("t1")]))

    def test_queue_full_rejects_immediately(self) -> None:
        self.service._acquire_next_candidate_token()  # 占满唯一账号的唯一槽位
        self.service._image_waiters = 1  # 已有一个请求在排队，达到上限
        started = time.monotonic()
        with self.assertRaises(ImageAccountSelectionError) as ctx:
            self.service._acquire_next_candidate_token()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(ctx.exception.kind, "queue_full")
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (503, "account_queue_full"))

    def test_waiting_stops_at_deadline(self) -> None:
        self.service._acquire_next_candidate_token()
        started = time.monotonic()
        with self.assertRaises(ImageAccountSelectionError) as ctx:
            self.service._acquire_next_candidate_token(deadline_monotonic=time.monotonic() + 0.15)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(ctx.exception.kind, "deadline_exceeded")
        self.assertEqual(ctx.exception.code, "account_busy_timeout")
        self.assertEqual(self.service._image_waiters, 0)

    def test_expired_deadline_fails_before_selection(self) -> None:
        with self.assertRaises(ImageAccountSelectionError) as ctx:
            self.service.get_available_access_token(deadline_monotonic=time.monotonic() - 1)
        self.assertEqual(ctx.exception.kind, "deadline_exceeded")


# ------------------------------------------------------------------ structured failure classification
class StructuredFailureTest(unittest.TestCase):
    def test_quota_text_is_recognised(self) -> None:
        self.assertTrue(failures.text_says_image_quota_exhausted("您已达到图像生成上限，请稍后再试"))
        self.assertTrue(failures.text_says_image_quota_exhausted("You've hit the image generation limit for now."))
        self.assertFalse(failures.text_says_image_quota_exhausted("这是一只橘猫。"))
        self.assertEqual(
            failures.classify_image_message_text("已达到图像生成上限").code,
            "image_quota_exhausted",
        )
        self.assertTrue(failures.should_refresh_image_account("You hit the image generation limit"))

    def test_text_results_are_not_account_failures(self) -> None:
        for code in ("content_policy_violation", "upstream_text_reply", "invalid_image_input", "image_download_failed"):
            self.assertFalse(failures.image_failure(code).account_failure, code)
        self.assertTrue(failures.image_failure("image_quota_exhausted").account_failure)
        self.assertTrue(failures.image_failure("auth_invalid").account_failure)

    def test_message_facts_classification(self) -> None:
        event = {
            "message": {
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["我不能帮你生成这张图"]},
                "status": "finished_successfully",
                "end_turn": True,
            }
        }
        facts = failures.extract_message_facts(event)
        self.assertEqual((facts["role"], facts["content_type"], facts["end_turn"]), ("assistant", "text", True))
        failure = failures.classify_upstream_message(event)
        self.assertEqual(failure.code, "upstream_text_reply")
        tool_error = failures.classify_upstream_message({
            "message": {
                "author": {"role": "tool"},
                "content": {"content_type": "system_error", "text": "boom"},
            }
        })
        self.assertEqual(tool_error.code, "image_tool_error")
        self.assertIsNone(failures.classify_message_facts(role="assistant", has_image_output=True))
        self.assertEqual(failures.classify_message_facts(role="assistant", blocked=True).code, "content_policy_violation")
        self.assertEqual(failures.classify_message_facts(codes={"insufficient_quota"}).code, "image_quota_exhausted")

    def test_merge_prefers_higher_priority(self) -> None:
        text = failures.image_failure("upstream_text_reply", raw_detail="x")
        quota = failures.image_failure("image_quota_exhausted")
        self.assertEqual(failures.merge_message_failure(text, quota).code, "image_quota_exhausted")
        self.assertEqual(failures.merge_message_failure(quota, text).code, "image_quota_exhausted")

    def test_conversation_failure_uses_current_turn_only(self) -> None:
        conversation = {
            "current_node": "n3",
            "mapping": {
                "n1": {"parent": None, "message": {"author": {"role": "user"}, "content": {"parts": ["hi"]}}},
                "n2": {"parent": "n1", "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["已达到图像生成上限"]},
                    "status": "finished_successfully",
                    "end_turn": True,
                }},
                "n3": {"parent": "n2", "message": {"author": {"role": "tool"}, "metadata": {"is_error": True}}},
            },
        }
        failure = failures.classify_conversation_failure(conversation)
        self.assertEqual(failure.code, "image_quota_exhausted")
        self.assertEqual(failures.terminal_assistant_text(conversation), "已达到图像生成上限")

    def test_signed_asset_http_error_is_not_account_failure(self) -> None:
        exc = UpstreamHTTPError("image_download", 403, "denied", credential_scope="signed_asset")
        failure = failures.classify_upstream_http_error(exc)
        self.assertEqual(failure.code, "image_download_failed")
        self.assertFalse(failure.account_failure)
        account_exc = UpstreamHTTPError("/backend-api/x", 401, "nope")
        self.assertEqual(failures.classify_upstream_http_error(account_exc).code, "auth_invalid")

    def test_state_tracks_failure_and_terminal_tool_arguments(self) -> None:
        state = conv.ConversationState()
        event = {
            "message": {
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["已达到图像生成上限"]},
                "status": "finished_successfully",
                "end_turn": True,
            },
            "conversation_id": "c1",
        }
        conv.update_conversation_state(state, json.dumps(event), event)
        conv.refresh_state_failure(state, "已达到图像生成上限")
        self.assertEqual(state.failure.code, "image_quota_exhausted")

        args_state = conv.ConversationState()
        args = json.dumps({"prompt": "a cat", "size": "1024x1024"})
        event = {
            "message": {
                "author": {"role": "assistant"},
                "content": {"content_type": "code", "text": args},
                "status": "finished_successfully",
                "end_turn": True,
            },
        }
        conv.update_conversation_state(args_state, json.dumps(event), event)
        conv.refresh_state_failure(args_state, args)
        self.assertIsNotNone(args_state.terminal_tool_arguments)
        self.assertEqual(conv._tool_arguments_failure_code(args, args), "image_tool_error")
        self.assertEqual(conv._tool_arguments_failure_code(args + " 已达到图像生成上限", args), "image_quota_exhausted")


# ------------------------------------------------------------------ backend: model slug / search / codex / download
def _backend() -> OpenAIBackendAPI:
    account = {"access_token": "token", "fp": {"oai-device-id": "device-1"}}
    with patch.object(openai_backend_api.account_service, "get_account", return_value=account):
        return OpenAIBackendAPI(access_token="token", proxy_profile=ProxyRuntimeProfile(egress_key="direct"))


class FakeResponse:
    def __init__(self, status_code=200, content=b"img", text="", json_data=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self._json = json_data

    def json(self):
        return self._json

    def iter_content(self, *_a, **_k):
        return iter([self.text.encode()] if self.text else [])


class BackendRobustnessTest(unittest.TestCase):
    def test_image_model_slug_never_uses_gpt_5_3(self) -> None:
        backend = _backend()
        self.assertEqual(backend._image_model_slug("gpt-image-2"), "auto")
        self.assertEqual(backend._image_model_slug("codex-gpt-image-2"), "codex-gpt-image-2")

    def test_download_uses_signed_headers_for_foreign_origin(self) -> None:
        backend = _backend()
        calls = []

        def get(url, headers=None, timeout=None):
            calls.append((url, headers))
            return FakeResponse()

        backend.session = SimpleNamespace(get=get, headers=dict(backend.session.headers), close=lambda: None)
        backend.download_image_bytes(["https://files.oaiusercontent.com/x.png", "https://chatgpt.com/y.png"])
        foreign_headers = calls[0][1]
        self.assertEqual(foreign_headers.get("Authorization"), "")
        self.assertEqual(foreign_headers.get("OAI-Device-Id"), "")
        same_headers = calls[1][1]
        lowered = {str(k).lower(): v for k, v in same_headers.items()}
        self.assertTrue(str(lowered.get("authorization", "")).startswith("Bearer "))

    def test_download_retries_empty_response_once_then_raises(self) -> None:
        backend = _backend()
        responses = [FakeResponse(content=b""), FakeResponse(content=b"")]
        backend.session = SimpleNamespace(get=lambda *a, **k: responses.pop(0), headers={}, close=lambda: None)
        with self.assertRaises(ImageDownloadError) as ctx:
            backend.download_image_bytes(["https://cdn.example/x.png"])
        self.assertEqual(ctx.exception.failure.code, "image_download_failed")
        self.assertEqual(responses, [])

    def test_download_retries_exception_once_then_succeeds(self) -> None:
        backend = _backend()
        outcomes = [RuntimeError("reset"), FakeResponse(content=b"ok")]

        def get(*_a, **_k):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        backend.session = SimpleNamespace(get=get, headers={}, close=lambda: None)
        self.assertEqual(backend.download_image_bytes(["https://cdn.example/x.png"]), [b"ok"])

    def test_resolve_urls_retries_file_not_ready_once(self) -> None:
        backend = _backend()
        attempts = {"n": 0}

        def get_url(file_id):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise UpstreamHTTPError("/backend-api/files/x/download", 404, json.dumps({"error_code": "file_not_ready"}))
            return "https://cdn.example/a.png"

        backend._get_file_download_url = get_url
        self.assertEqual(backend._resolve_image_urls("", [FILE_ID], []), ["https://cdn.example/a.png"])
        self.assertEqual(attempts["n"], 2)

    def test_resolve_urls_retries_empty_url_once(self) -> None:
        backend = _backend()
        urls = iter(["", "https://cdn.example/b.png"])
        backend._get_file_download_url = lambda file_id: next(urls)
        self.assertEqual(backend._resolve_image_urls("", [FILE_ID], []), ["https://cdn.example/b.png"])

    def test_resolve_urls_all_empty_raises_download_error(self) -> None:
        backend = _backend()
        backend._get_file_download_url = lambda file_id: ""
        with self.assertRaises(ImageDownloadError):
            backend._resolve_image_urls("c1", [FILE_ID], [])

    def test_resolve_urls_auth_failure_after_success_schedules_recovery(self) -> None:
        backend = _backend()
        other = "file_00000000" + "b" * 24

        def get_url(file_id):
            if file_id == FILE_ID:
                return "https://cdn.example/a.png"
            raise UpstreamHTTPError("/backend-api/files/x/download", 401, "{}")

        backend._get_file_download_url = get_url
        with patch.object(backend, "_schedule_auth_recovery") as schedule:
            urls = backend._resolve_image_urls("", [FILE_ID, other], [])
        self.assertEqual(urls, ["https://cdn.example/a.png"])
        schedule.assert_called_once()

    def test_search_result_skips_thoughts_and_prefers_terminal(self) -> None:
        backend = _backend()
        conversation = {"mapping": {
            "a": {"message": {
                "id": "answer", "author": {"role": "assistant"}, "create_time": 1.0, "end_turn": True,
                "content": {"content_type": "text", "parts": ["最终答案"]},
            }},
            "b": {"message": {
                "id": "thought", "author": {"role": "assistant"}, "create_time": 5.0,
                "content": {"content_type": "reasoning_recap", "parts": ["思考摘要"]},
            }},
            "c": {"message": {
                "id": "tool", "author": {"role": "assistant"}, "create_time": 6.0, "recipient": "browser",
                "content": {"content_type": "text", "parts": ["search(...)"]},
            }},
        }}
        result = backend._extract_search_result("conv", conversation)
        self.assertEqual(result["answer"], "最终答案")
        self.assertEqual(result["assistant_message_id"], "answer")

    def test_codex_events_stop_at_terminal_event(self) -> None:
        lines = [
            b'data: {"type": "response.created"}\n', b"\n",
            b'data: {"type": "response.completed", "response": {}}\n', b"\n",
        ]

        class Raw:
            headers = {"content-type": "text/event-stream"}
            status = 200

            def __init__(self):
                self.reads = 0

            def readline(self):
                if self.reads < len(lines):
                    self.reads += 1
                    return lines[self.reads - 1]
                raise AssertionError("kept reading after response.completed")

            def close(self):
                pass

        raw = Raw()
        events = list(OpenAIBackendAPI._iter_codex_response_events(raw))
        self.assertEqual([e["type"] for e in events], ["response.created", "response.completed"])


# ------------------------------------------------------------------ conversation pipeline
class FakeAccounts:
    def __init__(self, tokens) -> None:
        self.tokens = list(tokens)
        self.results: list[tuple[str, bool]] = []
        self.released: list[str] = []
        self.penalized: list[tuple[str, str]] = []
        self.acquire_kwargs: list[dict] = []
        self.mark_error: Exception | None = None

    def get_available_access_token(self, *, excluded_tokens=None, **kwargs):
        self.acquire_kwargs.append(kwargs)
        for token in self.tokens:
            if token not in (excluded_tokens or set()):
                return token
        raise RuntimeError("no account")

    def get_account(self, token):
        return {"email": f"{token}@x"}

    def mark_image_result(self, token, success, **_):
        if self.mark_error is not None:
            raise self.mark_error
        self.results.append((token, success))

    def release_image_slot(self, token):
        self.released.append(token)

    def penalize_image_account(self, token, *, reason):
        self.penalized.append((token, reason))


class FakeBackend:
    def __init__(self, access_token, **kwargs):
        self.access_token = access_token
        self.init_kwargs = kwargs
        self.proxy_profile = SimpleNamespace(image_concurrency_limit=0)
        self.progress_callback = None
        self.cancel_checker = None

    def close(self):
        pass


def _result(request, index, total):
    return conv.ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])


def _stream(behaviors):
    def stream(backend, request, index, total):
        behavior = behaviors.get(backend.access_token)
        if behavior is None:
            yield _result(request, index, total)
            return
        if isinstance(behavior, BaseException):
            raise behavior
        yield conv.ImageOutput(kind="message", model=request.model, index=index, total=total, text=behavior)

    return stream


class PipelineTestBase(unittest.TestCase):
    egress = None

    def setUp(self) -> None:
        saved = config.data.get("image_account_retry_count")
        config.data["image_account_retry_count"] = 2
        self.addCleanup(lambda: config.data.pop("image_account_retry_count", None) if saved is None
                        else config.data.__setitem__("image_account_retry_count", saved))
        self.accounts = FakeAccounts(["a", "b", "c"])
        proxy = SimpleNamespace(
            acquire_image_egress=self.egress or (lambda *a, **k: 0),
            release_image_egress=lambda *a, **k: None,
            get_fallback_proxy_reference=lambda: None,
        )
        for item in (
            patch.object(conv, "account_service", self.accounts),
            patch.object(conv, "OpenAIBackendAPI", FakeBackend),
            patch.object(conv, "proxy_settings", proxy),
            patch.object(conv, "_cleanup_image_conversations_after_success", lambda *a, **k: None),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_single(self, behaviors, request=None):
        with patch.object(conv, "stream_image_outputs", _stream(behaviors)):
            return conv._generate_single_image(request or conv.ConversationRequest(model="gpt-image-2"), 1, 1)


class TextResultAccountingTest(PipelineTestBase):
    def test_content_policy_does_not_count_against_account(self) -> None:
        with self.assertRaises(conv.ImageGenerationError) as ctx:
            self.run_single({"a": conv.ImageContentPolicyError("该请求可能违反我们的内容政策")})
        self.assertEqual(ctx.exception.code, "content_policy_violation")
        self.assertEqual(self.accounts.results, [])
        self.assertEqual(self.accounts.released, ["a"])
        self.assertEqual(self.accounts.penalized, [])

    def test_synthetic_incomplete_message_still_counts_as_failure(self) -> None:
        text = "Image generation completed upstream but the result could not be retrieved. Please try again in a moment."
        outputs = self.run_single({"a": text})
        self.assertEqual(outputs[-1].kind, "message")
        self.assertEqual(self.accounts.results, [("a", False)])

    def test_quota_text_reply_switches_account(self) -> None:
        outputs = self.run_single({"a": "You've hit the image generation limit for now."})
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(self.accounts.results, [("a", False), ("b", True)])

    def test_mark_result_exception_still_releases_slot(self) -> None:
        self.accounts.mark_error = RuntimeError("storage conflict")
        self.run_single({})
        self.assertEqual(self.accounts.released, ["a"])


class DeadlineTest(PipelineTestBase):
    def test_deadline_is_passed_to_account_selection(self) -> None:
        request = conv.ConversationRequest(model="gpt-image-2", deadline_monotonic=time.monotonic() + 1000)
        self.run_single({}, request)
        self.assertIn("deadline_monotonic", self.accounts.acquire_kwargs[0])
        self.assertLess(self.accounts.acquire_kwargs[0]["deadline_monotonic"], request.deadline_monotonic)

    def test_no_deadline_kwarg_without_request_deadline(self) -> None:
        self.run_single({})
        self.assertNotIn("deadline_monotonic", self.accounts.acquire_kwargs[0])

    def test_selection_deadline_maps_to_busy_timeout(self) -> None:
        def raise_deadline(**_kwargs):
            raise ImageAccountSelectionError("deadline_exceeded", "waited too long")

        self.accounts.get_available_access_token = raise_deadline
        with self.assertRaises(conv.ImageGenerationError) as ctx:
            self.run_single({})
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (503, "account_busy_timeout"))

    def test_egress_deadline_is_busy_timeout_not_account_failure(self) -> None:
        def deadline_egress(*_a, **_k):
            raise ImageEgressDeadlineError("deadline while waiting for egress")

        with patch.object(conv, "proxy_settings", SimpleNamespace(
            acquire_image_egress=deadline_egress,
            release_image_egress=lambda *a, **k: None,
            get_fallback_proxy_reference=lambda: None,
        )):
            request = conv.ConversationRequest(model="gpt-image-2", deadline_monotonic=time.monotonic() + 1000)
            with self.assertRaises(conv.ImageGenerationError) as ctx:
                self.run_single({}, request)
        self.assertEqual(ctx.exception.code, "account_busy_timeout")
        self.assertEqual(self.accounts.results, [])
        self.assertEqual(self.accounts.released, ["a"])

    def test_unavailable_proxy_reference_does_not_fall_back_or_count_failure(self) -> None:
        def boom(*_a, **_k):
            raise ProxyReferenceUnavailableError("proxy group is unavailable: g1")

        with patch.object(conv, "OpenAIBackendAPI", boom):
            with self.assertRaises(conv.ImageGenerationError) as ctx:
                self.run_single({})
        self.assertEqual(ctx.exception.code, "proxy_unavailable")
        self.assertEqual(self.accounts.results, [])
        self.assertEqual(self.accounts.released, ["a"])

    def test_recovery_poll_budget_respects_deadline(self) -> None:
        with patch.dict(config.data, {"image_poll_timeout_secs": 300}):
            self.assertEqual(conv._recovery_poll_budget_secs(conv.ConversationRequest()), 300.0)
            near = conv.ConversationRequest(deadline_monotonic=time.monotonic() + 50)
            self.assertLess(conv._recovery_poll_budget_secs(near), 41.0)
            expired = conv.ConversationRequest(deadline_monotonic=time.monotonic() - 5)
            self.assertEqual(conv._recovery_poll_budget_secs(expired), 0.0)


class SerialToleranceTest(unittest.TestCase):
    def _run(self, outcomes):
        calls = []

        def fake_single(request, index, total):
            calls.append(index)
            outcome = outcomes[index - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return [conv.ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])]

        request = conv.ConversationRequest(model="gpt-image-2", n=len(outcomes))
        with patch.dict(config.data, {"image_parallel_generation": False}), \
                patch.object(conv, "_generate_single_image", fake_single):
            return calls, list(conv.stream_image_outputs_with_pool(request))

    def test_serial_failure_does_not_abort_following_slots(self) -> None:
        calls, outputs = self._run([conv.ImageGenerationError("boom"), None, None])
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(len([o for o in outputs if o.kind == "result"]), 2)

    def test_serial_all_failed_raises(self) -> None:
        with self.assertRaises(conv.ImageGenerationError):
            self._run([conv.ImageGenerationError("boom"), conv.ImageGenerationError("boom2")])

    def test_pool_sets_request_deadline(self) -> None:
        seen = []

        def fake_single(request, index, total):
            seen.append(request.deadline_monotonic)
            return [conv.ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])]

        request = conv.ConversationRequest(model="gpt-image-2", n=1)
        with patch.object(conv, "_generate_single_image", fake_single):
            list(conv.stream_image_outputs_with_pool(request))
        self.assertGreater(seen[0], time.monotonic())


# ------------------------------------------------------------------ SSE interruption recovery
class StreamInterruptionRecoveryTest(unittest.TestCase):
    def _backend(self, conversation, urls):
        backend = MagicMock()
        backend._query_backend_tasks.return_value = []
        backend._get_conversation.return_value = conversation
        backend._conversation_poll_snapshot.return_value = ({"messages": []}, "")
        backend._extract_image_tool_records.return_value = []
        backend._find_content_policy_error_in_conversation.return_value = ""
        backend.resolve_conversation_image_urls.return_value = urls
        backend.download_image_bytes.return_value = [b"\x89PNG"]
        return backend

    def _events(self, error):
        def events(*_a, **_k):
            yield {"type": "conversation.event", "conversation_id": "c1", "file_ids": [], "sediment_ids": [],
                   "raw": {"type": "x"}, "text": "", "blocked": False, "tool_invoked": True,
                   "turn_use_case": "image gen"}
            raise error

        return events

    def test_interrupted_stream_keeps_polling_conversation(self) -> None:
        backend = self._backend({"mapping": {}}, ["https://cdn.example/a.png"])
        request = conv.ConversationRequest(model="gpt-image-2", prompt="cat", response_format="b64_json")
        with patch.object(conv, "conversation_events", self._events(curl_exceptions.ConnectionError("reset"))), \
                patch.object(conv, "save_image_bytes", return_value="http://x/a.png"), \
                patch.object(conv, "image_size_from_bytes", return_value=None), \
                patch.object(conv, "upscale_image_if_needed", side_effect=lambda data, size: data):
            outputs = list(conv.stream_image_outputs(backend, request))
        self.assertEqual(outputs[-1].kind, "result")
        kwargs = backend.resolve_conversation_image_urls.call_args.kwargs
        self.assertGreater(kwargs["poll_timeout_secs"], 0)

    def test_interruption_without_conversation_reraises(self) -> None:
        def events(*_a, **_k):
            raise curl_exceptions.ConnectionError("reset")
            yield  # pragma: no cover

        backend = self._backend({}, [])
        request = conv.ConversationRequest(model="gpt-image-2", prompt="cat")
        with patch.object(conv, "conversation_events", events):
            with self.assertRaises(curl_exceptions.ConnectionError):
                list(conv.stream_image_outputs(backend, request))

    def test_interrupted_stream_reports_interrupted_when_nothing_found(self) -> None:
        backend = self._backend({"mapping": {}}, [])
        request = conv.ConversationRequest(model="gpt-image-2", prompt="cat")
        with patch.object(conv, "conversation_events", self._events(curl_exceptions.ConnectionError("reset"))), \
                patch.dict(config.data, {"image_poll_timeout_secs": 5}):
            with self.assertRaises(conv.ImageGenerationError) as ctx:
                list(conv.stream_image_outputs(backend, request))
        self.assertEqual(ctx.exception.code, "image_stream_interrupted")

    def test_quota_text_in_conversation_is_reported_as_quota(self) -> None:
        conversation = {
            "current_node": "n2",
            "mapping": {
                "n1": {"parent": None, "message": {"author": {"role": "user"}, "content": {"parts": ["hi"]}}},
                "n2": {"parent": "n1", "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["已达到图像生成上限"]},
                    "status": "finished_successfully", "end_turn": True,
                }},
            },
        }
        backend = self._backend(conversation, [])
        request = conv.ConversationRequest(model="gpt-image-2", prompt="cat")
        with patch.object(conv, "conversation_events", self._events(curl_exceptions.ConnectionError("reset"))):
            with self.assertRaises(conv.ImageGenerationError) as ctx:
                list(conv.stream_image_outputs(backend, request))
        self.assertEqual(ctx.exception.code, "image_quota_exhausted")
        self.assertEqual(ctx.exception.status_code, 429)


# ------------------------------------------------------------------ storage retry
class SaveImageBytesTest(unittest.TestCase):
    def test_retries_once_then_succeeds(self) -> None:
        service = MagicMock()
        service.save.side_effect = [OSError("disk"), SimpleNamespace(url="http://x/a.png")]
        with patch.object(conv, "image_storage_service", service):
            self.assertEqual(conv.save_image_bytes(b"x"), "http://x/a.png")
        self.assertEqual(service.save.call_count, 2)

    def test_raises_download_error_after_two_failures(self) -> None:
        service = MagicMock()
        service.save.side_effect = OSError("disk")
        with patch.object(conv, "image_storage_service", service):
            with self.assertRaises(ImageDownloadError) as ctx:
                conv.save_image_bytes(b"x")
        self.assertEqual(ctx.exception.failure.code, "image_download_failed")
        self.assertEqual(service.save.call_count, 2)

    def test_no_retry_after_deadline(self) -> None:
        service = MagicMock()
        service.save.side_effect = OSError("disk")
        with patch.object(conv, "image_storage_service", service):
            with self.assertRaises(ImageDownloadError):
                conv.save_image_bytes(b"x", deadline_monotonic=time.monotonic() - 1)
        self.assertEqual(service.save.call_count, 1)


# ------------------------------------------------------------------ error body privacy
class ErrorBodyTest(unittest.TestCase):
    def test_openai_error_never_contains_account_email(self) -> None:
        error = conv.ImageGenerationError("boom", account_email="user@example.com")
        self.assertNotIn("account_email", error.to_openai_error()["error"])
        self.assertNotIn("user@example.com", json.dumps(error.to_openai_error()))


# ------------------------------------------------------------------ text cross-account retry
class TextCrossAccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.slots_held: list[str] = []
        self.released: list[str] = []
        self.failures: list[str] = []
        accounts = MagicMock()
        order = iter(["t2", "t3"])
        accounts.acquire_text_access_token.side_effect = lambda *a, **k: (
            "t1" if k.get("preferred_token") else next(order, "")
        )
        accounts.release_text_slot.side_effect = self.released.append
        accounts.record_upstream_failure.side_effect = self.failures.append
        accounts.get_account.return_value = {"email": "x@y"}
        self.accounts = accounts
        for item in (
            patch.object(conv, "account_service", accounts),
            patch.object(conv, "OpenAIBackendAPI", FakeBackend),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _events(self, behaviors):
        def events(backend, **_k):
            behavior = behaviors.get(backend.access_token)
            if isinstance(behavior, BaseException):
                raise behavior
            yield {"type": "conversation.delta", "delta": behavior or "ok"}

        return events

    def test_switches_account_on_transient_failure_before_output(self) -> None:
        request = conv.ConversationRequest(model="auto", prompt="hi")
        backend = SimpleNamespace(access_token="t1")
        with patch.object(conv, "conversation_events", self._events({"t1": ConnectionError("reset by peer")})):
            text = conv.collect_text(backend, request)
        self.assertEqual(text, "ok")
        self.assertEqual(self.failures, ["t1"])
        self.assertEqual(self.released, ["t1", "t2"])

    def test_gives_up_after_max_attempts(self) -> None:
        request = conv.ConversationRequest(model="auto", prompt="hi")
        backend = SimpleNamespace(access_token="t1")
        behaviors = {t: ConnectionError("reset by peer") for t in ("t1", "t2", "t3")}
        with patch.object(conv, "conversation_events", self._events(behaviors)):
            with self.assertRaises(ConnectionError):
                conv.collect_text(backend, request)
        self.assertEqual(self.failures, ["t1", "t2", "t3"])
        self.assertEqual(sorted(self.released), ["t1", "t2", "t3"])

    def test_no_switch_after_output_was_emitted(self) -> None:
        def events(backend, **_k):
            yield {"type": "conversation.delta", "delta": "partial"}
            raise ConnectionError("reset by peer")

        request = conv.ConversationRequest(model="auto", prompt="hi")
        backend = SimpleNamespace(access_token="t1")
        with patch.object(conv, "conversation_events", events):
            gen = conv.stream_text_deltas(backend, request)
            self.assertEqual(next(gen), "partial")
            with self.assertRaises(ConnectionError):
                next(gen)
        self.assertEqual(self.released, ["t1"])

    def test_schedule_failure_does_not_mask_error(self) -> None:
        self.accounts.schedule_auth_verification.side_effect = RuntimeError("queue down")
        self.accounts.refresh_access_token.return_value = "t1"
        request = conv.ConversationRequest(model="auto", prompt="hi")
        backend = SimpleNamespace(access_token="t1")
        err = UpstreamHTTPError("/backend-api/conversation", 401, "token_invalidated")
        with patch.object(conv, "conversation_events", self._events({"t1": err})):
            text = conv.collect_text(backend, request)
        self.assertEqual(text, "ok")


if __name__ == "__main__":
    unittest.main()
