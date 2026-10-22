import unittest

from services.image_failure import (
    classify_image_error_text,
    classify_image_exception,
    classify_image_message_text,
    classify_upstream_http_error,
    friendly_message_key,
    image_failure,
    image_failure_priority,
    is_upstream_error_reply_text,
    merge_failures,
    upstream_error_reply_failure,
)
from utils.helper import UpstreamHTTPError

ZH_ERROR = "由于我这边发生了错误，我未能生成图片。"
EN_ERROR = "I wasn't able to generate the image because an error occurred on my end."


def _text_reply(text: str):
    return image_failure("upstream_text_reply", raw_detail=text).with_public_detail(text)


class UpstreamErrorReplyTest(unittest.TestCase):
    def test_error_replies_become_switchable_failures(self) -> None:
        for text in (
            ZH_ERROR,
            "抱歉，生成图片时出现了一些问题，请稍后再试。",
            EN_ERROR,
            "Something went wrong while generating the image.",
        ):
            with self.subTest(text=text):
                failure = upstream_error_reply_failure(_text_reply(text))
                self.assertEqual(failure.code, "upstream_error_reply")
                self.assertTrue(failure.switch_account)
                self.assertFalse(failure.account_failure)
                self.assertEqual(failure.status_code, 502)
                self.assertEqual(failure.public_detail, text)

    def test_ordinary_text_replies_stay_text(self) -> None:
        for text in (
            "这是一只坐在窗台上的橘猫。",
            "我不能生成涉及真实人物的图片。",
            "Here is a description of the scene you asked for.",
            "发生了错误，未能生成图片。" + "详细说明" * 100,
        ):
            with self.subTest(text=text[:20]):
                original = _text_reply(text)
                self.assertIs(upstream_error_reply_failure(original), original)
                self.assertFalse(is_upstream_error_reply_text(text))

    def test_other_codes_are_untouched(self) -> None:
        original = image_failure("image_tool_error", raw_detail=ZH_ERROR)
        self.assertIs(upstream_error_reply_failure(original, ZH_ERROR), original)

    def test_raw_text_argument_used_when_failure_has_no_detail(self) -> None:
        failure = upstream_error_reply_failure(image_failure("upstream_text_reply"), ZH_ERROR)
        self.assertEqual(failure.code, "upstream_error_reply")


class MessageClassificationTest(unittest.TestCase):
    def test_terminal_messages_keep_historical_400_projection(self) -> None:
        policy = classify_image_message_text("该请求可能违反我们的内容政策")
        self.assertEqual((policy.status_code, policy.error_type, policy.code),
                         (400, "invalid_request_error", "content_policy_violation"))
        tool_json = classify_image_message_text('{"size":"1024x1024","n":1}')
        self.assertEqual((tool_json.status_code, tool_json.code), (400, "upstream_text_reply"))
        plain = classify_image_message_text("这是一只猫。")
        self.assertEqual((plain.status_code, plain.error_type, plain.code),
                         (400, "invalid_request_error", "no_image_generated"))

    def test_error_reply_message_is_switchable_server_error(self) -> None:
        failure = classify_image_message_text(ZH_ERROR)
        self.assertEqual((failure.status_code, failure.code), (502, "upstream_error_reply"))
        self.assertTrue(failure.switch_account)


class TextClassificationTest(unittest.TestCase):
    def test_legacy_texts_map_to_codes_and_friendly_keys(self) -> None:
        cases = {
            "image_account_selection:quota_exhausted": ("image_quota_exhausted", "quota"),
            "image_account_selection:unavailable": ("no_available_account", "no_account"),
            "insufficient_quota for images": ("image_quota_exhausted", "quota"),
            "no account in the pool could generate images": ("account_busy", "local_busy"),
            "unsupported image model,supported models: x": ("unsupported_model", "unsupported_model"),
            "ImagePollTimeoutError: waited 300s": ("image_poll_timeout", "poll_timeout"),
            "curl: (92) HTTP/2 stream 1 was not closed cleanly": ("image_stream_interrupted", "stream_interrupted"),
            "curl: (28) Operation timed out": ("upstream_connection_timeout", "connection_timeout"),
            "curl: (35) TLS connect error": ("upstream_connection_failed", "connection_failed"),
            "upstream completed without generating images": ("upstream_text_reply", "text_reply"),
            "token_invalidated": ("auth_invalid", ""),
            "status=429 too many requests": ("upstream_rate_limited", ""),
        }
        for text, (code, key) in cases.items():
            with self.subTest(text=text):
                failure = classify_image_error_text(text)
                self.assertIsNotNone(failure)
                self.assertEqual(failure.code, code)
                self.assertEqual(friendly_message_key(failure), key)

    def test_unknown_text_has_no_classification(self) -> None:
        self.assertIsNone(classify_image_error_text("something odd"))
        self.assertIsNone(classify_image_error_text(""))

    def test_explicit_text_reply_code_wins(self) -> None:
        failure = classify_image_error_text("anything", code="no_image_generated")
        self.assertEqual(friendly_message_key(failure), "text_reply")


class ExceptionClassificationTest(unittest.TestCase):
    def test_http_status_classification(self) -> None:
        cases = {
            (401, "/backend-api/conversation"): "auth_invalid",
            (429, "/backend-api/conversation"): "upstream_rate_limited",
            (429, "/backend-api/files"): "file_upload_throttled",
            (403, "/backend-api/conversation"): "upstream_unavailable",
            (503, "/backend-api/conversation"): "upstream_unavailable",
            (504, "/backend-api/conversation"): "upstream_connection_timeout",
            (400, "/backend-api/conversation"): "invalid_image_input",
        }
        for (status, context), code in cases.items():
            with self.subTest(status=status, context=context):
                exc = UpstreamHTTPError(context, status, {"detail": "x"})
                self.assertEqual(classify_upstream_http_error(exc).code, code)

    def test_structured_body_codes_refine_status(self) -> None:
        exc = UpstreamHTTPError("/backend-api/conversation", 429, {"error": {"code": "insufficient_quota"}})
        self.assertEqual(classify_image_exception(exc).code, "image_quota_exhausted")
        exc = UpstreamHTTPError("/backend-api/conversation", 400, {"error": {"code": "content_policy_violation"}})
        self.assertEqual(classify_image_exception(exc).code, "content_policy_violation")

    def test_generic_code_falls_back_to_text(self) -> None:
        exc = RuntimeError("status=429 rate_limit_exceeded")
        setattr(exc, "code", "upstream_error")
        self.assertEqual(classify_image_exception(exc).code, "upstream_rate_limited")

    def test_attached_failure_is_returned(self) -> None:
        failure = image_failure("image_tool_error")
        exc = RuntimeError("x")
        setattr(exc, "failure", failure)
        self.assertIs(classify_image_exception(exc), failure)

    def test_transport_exceptions(self) -> None:
        self.assertEqual(classify_image_exception(TimeoutError("slow")).code, "upstream_connection_timeout")
        self.assertEqual(classify_image_exception(ConnectionError("refused")).code, "upstream_connection_failed")
        self.assertEqual(classify_image_exception(ValueError("weird")).code, "upstream_error")


class MergePriorityTest(unittest.TestCase):
    def test_higher_priority_wins_and_keeps_details(self) -> None:
        reply = upstream_error_reply_failure(_text_reply(ZH_ERROR))
        quota = image_failure("image_quota_exhausted")
        merged = merge_failures(reply, quota)
        self.assertEqual(merged.code, "image_quota_exhausted")
        self.assertEqual(merged.public_detail, ZH_ERROR)
        self.assertEqual(merge_failures(quota, reply).code, "image_quota_exhausted")

    def test_same_code_keeps_latest(self) -> None:
        first = image_failure("upstream_rate_limited", raw_detail="first")
        second = image_failure("upstream_rate_limited", raw_detail="second")
        self.assertEqual(merge_failures(first, second).raw_detail, "second")

    def test_none_handling_and_ordering(self) -> None:
        failure = image_failure("image_tool_error")
        self.assertIs(merge_failures(None, failure), failure)
        self.assertIs(merge_failures(failure, None), failure)
        ordered = sorted(
            ["upstream_error", "image_poll_timeout", "upstream_text_reply", "upstream_error_reply",
             "content_policy_violation", "upstream_rate_limited", "image_quota_exhausted", "auth_invalid"],
            key=lambda code: image_failure_priority(image_failure(code)),
        )
        self.assertEqual(ordered[-1], "auth_invalid")
        self.assertEqual(ordered[0], "upstream_error")


if __name__ == "__main__":
    unittest.main()
