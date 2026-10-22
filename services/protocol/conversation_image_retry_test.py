import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.config import config
from services.protocol import conversation as conv
from utils.helper import UpstreamHTTPError

ZH_ERROR = "由于我这边发生了错误，我未能生成图片。"


class FakeAccounts:
    def __init__(self, tokens) -> None:
        self.tokens = list(tokens)
        self.results: list[tuple[str, bool]] = []
        self.released: list[str] = []
        self.penalized: list[tuple[str, str]] = []

    def get_available_access_token(self, *, excluded_tokens=None, **_):
        for token in self.tokens:
            if token not in (excluded_tokens or set()):
                return token
        raise RuntimeError("no account")

    def get_account(self, token):
        return {"email": f"{token}@x"}

    def mark_image_result(self, token, success, **_):
        self.results.append((token, success))

    def release_image_slot(self, token):
        self.released.append(token)

    def penalize_image_account(self, token, *, reason):
        self.penalized.append((token, reason))


class FakeBackend:
    def __init__(self, access_token, **_):
        self.access_token = access_token
        self.proxy_profile = SimpleNamespace(image_concurrency_limit=0)
        self.progress_callback = None
        self.cancel_checker = None

    def close(self):
        pass


def _result(request, index, total):
    return conv.ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}])


def _stream(behaviors):
    """behaviors: token -> exception to raise, or text for a message output; missing = success."""

    def stream(backend, request, index, total):
        behavior = behaviors.get(backend.access_token)
        if behavior is None:
            yield _result(request, index, total)
            return
        if isinstance(behavior, BaseException):
            raise behavior
        yield conv.ImageOutput(kind="message", model=request.model, index=index, total=total, text=behavior)

    return stream


class ImageAccountRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_retry = config.data.get("image_account_retry_count")
        config.data["image_account_retry_count"] = 2
        self.accounts = FakeAccounts(["a", "b", "c"])
        self.patches = [
            patch.object(conv, "account_service", self.accounts),
            patch.object(conv, "OpenAIBackendAPI", FakeBackend),
            patch.object(conv, "proxy_settings", SimpleNamespace(
                acquire_image_egress=lambda *a, **k: 0,
                release_image_egress=lambda *a, **k: None,
                get_fallback_proxy_reference=lambda: None,
            )),
            patch.object(conv, "_cleanup_image_conversations_after_success", lambda *a, **k: None),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        if self.saved_retry is None:
            config.data.pop("image_account_retry_count", None)
        else:
            config.data["image_account_retry_count"] = self.saved_retry

    def _run(self, behaviors, request=None):
        with patch.object(conv, "stream_image_outputs", _stream(behaviors)):
            return conv._generate_single_image(request or conv.ConversationRequest(model="gpt-image-2"), 1, 1)

    def test_text_reply_error_switches_account_without_counting_failure(self) -> None:
        exc = conv.ImageTextReplyError(ZH_ERROR)
        outputs = self._run({"a": exc})
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(self.accounts.results, [("b", True)])
        self.assertEqual(self.accounts.penalized, [("a", "upstream_error_reply")])
        attempt = outputs[-1].image_attempts[0]
        self.assertEqual(attempt["failure_code"], "upstream_error_reply")
        self.assertTrue(attempt["switched_account"])

    def test_error_reply_message_output_is_not_returned_as_text(self) -> None:
        outputs = self._run({"a": ZH_ERROR})
        self.assertEqual([o.kind for o in outputs], ["result"])
        self.assertEqual(self.accounts.results, [("b", True)])

    def test_ordinary_text_reply_does_not_switch(self) -> None:
        with self.assertRaises(conv.ImageGenerationError) as info:
            self._run({"a": conv.ImageTextReplyError("这是一只橘猫。")})
        self.assertEqual(info.exception.code, "upstream_text_reply")
        self.assertEqual(info.exception.status_code, 400)
        # 文本结果不是账号的错：不 mark_image_result(False)，只归还槽位且不降权。
        self.assertEqual(self.accounts.results, [])
        self.assertEqual(self.accounts.released, ["a"])
        self.assertEqual(self.accounts.penalized, [])

    def test_ordinary_message_output_still_returned(self) -> None:
        outputs = self._run({"a": "这是一只橘猫。"})
        self.assertEqual(outputs[-1].kind, "message")
        self.assertEqual(self.accounts.results, [])
        self.assertEqual(self.accounts.released, ["a"])

    def test_error_reply_exhausting_retries_keeps_upstream_text(self) -> None:
        with self.assertRaises(conv.ImageGenerationError) as info:
            self._run({t: conv.ImageTextReplyError(ZH_ERROR) for t in "abc"})
        error = info.exception
        self.assertEqual(error.code, "upstream_error_reply")
        self.assertEqual(error.status_code, 502)
        self.assertEqual(str(error), ZH_ERROR)
        self.assertEqual([p[0] for p in self.accounts.penalized], ["a", "b", "c"])
        self.assertEqual(len(error.image_attempts), 3)
        self.assertEqual([a["switched_account"] for a in error.image_attempts], [True, True, False])

    def test_retry_count_zero_disables_switching(self) -> None:
        config.data["image_account_retry_count"] = 0
        with self.assertRaises(conv.ImageGenerationError):
            self._run({"a": conv.ImageTextReplyError(ZH_ERROR)})
        self.assertEqual(self.accounts.penalized, [("a", "upstream_error_reply")])

    def test_rate_limit_switches_and_counts_failure(self) -> None:
        outputs = self._run({"a": UpstreamHTTPError("/backend-api/f/conversation", 429, {"detail": "slow down"})})
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(self.accounts.results, [("a", False), ("b", True)])

    def test_poll_timeout_does_not_switch(self) -> None:
        with self.assertRaises(conv.ImageGenerationError) as info:
            self._run({"a": conv.ImagePollTimeoutError("poll timeout after 300s")})
        self.assertEqual(info.exception.code, "image_poll_timeout")
        self.assertEqual(self.accounts.results, [("a", False)])

    def test_merge_reports_highest_priority_failure(self) -> None:
        behaviors = {
            "a": UpstreamHTTPError("/backend-api/f/conversation", 429, {"detail": "slow down"}),
            "b": conv.ImageTextReplyError(ZH_ERROR),
            "c": conv.ImageTextReplyError(ZH_ERROR),
        }
        with self.assertRaises(conv.ImageGenerationError) as info:
            self._run(behaviors)
        # 429 (priority 6) outranks the later upstream error replies (priority 3).
        self.assertEqual(info.exception.failure.code, "upstream_rate_limited")
        self.assertEqual(len(info.exception.image_attempts), 3)

    def test_no_other_account_reports_original_failure(self) -> None:
        self.accounts.tokens = ["a"]
        with self.assertRaises(conv.ImageGenerationError) as info:
            self._run({"a": conv.ImageTextReplyError(ZH_ERROR)})
        self.assertEqual(info.exception.code, "upstream_error_reply")

    def test_message_as_error_keeps_historical_projection(self) -> None:
        request = conv.ConversationRequest(model="gpt-image-2", message_as_error=True)
        with self.assertRaises(conv.ImageGenerationError) as info:
            self._run({"a": "该请求可能违反我们的内容政策"}, request)
        self.assertEqual((info.exception.status_code, info.exception.code), (400, "content_policy_violation"))
        payload = info.exception.to_openai_error()["error"]
        self.assertEqual((payload["type"], payload["code"]), ("invalid_request_error", "content_policy_violation"))


class PublicMessageCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = config.data.get("image_error_friendly_enabled")
        config.data["image_error_friendly_enabled"] = True

    def tearDown(self) -> None:
        if self.saved is None:
            config.data.pop("image_error_friendly_enabled", None)
        else:
            config.data["image_error_friendly_enabled"] = self.saved

    def test_friendly_messages_match_previous_projection(self) -> None:
        messages = config.get_image_error_messages()
        self.assertEqual(conv.public_image_error_message("image_account_selection:quota_exhausted"), messages["quota"])
        self.assertEqual(conv.public_image_error_message("no account in the pool"), messages["local_busy"])
        self.assertEqual(conv.public_image_error_message("curl: (28) Operation timed out"), messages["connection_timeout"])
        self.assertEqual(conv.public_image_error_message(""), messages["fallback"])
        self.assertEqual(conv.public_image_error_message("status=500 backend-api/x"), messages["fallback"])
        self.assertEqual(conv.public_image_error_message("plain words"), "plain words")
        self.assertTrue(conv.public_image_error_message("x", code="upstream_text_reply").startswith(messages["text_reply"][:6]))


if __name__ == "__main__":
    unittest.main()
