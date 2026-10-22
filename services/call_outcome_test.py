import unittest

from services.call_outcome import (
    apply_outcome_fields,
    build_outcome_fields,
    call_image_counts,
    call_outcome,
    call_switch_count,
    dashboard_outcome,
)


def image_call(**overrides):
    detail = {
        "endpoint": "/v1/images/generations",
        "model": "gpt-image-2",
        "status": "success",
        "request_meta": {"n": 2},
        "urls": ["/images/a.png", "/images/b.png"],
    }
    detail.update(overrides)
    return {"type": "call", "detail": detail}


class CallOutcomeTests(unittest.TestCase):
    def test_full_success(self):
        item = image_call()
        self.assertEqual(call_image_counts(item), (2, 2, 0, "success"))
        self.assertEqual(call_outcome(item), "success")
        self.assertEqual(dashboard_outcome(item), "success")

    def test_partial_success_when_fewer_images_than_requested(self):
        item = image_call(urls=["/images/a.png"])
        self.assertEqual(call_image_counts(item), (2, 1, 1, "partial_success"))
        self.assertEqual(call_outcome(item), "partial_success")
        self.assertEqual(dashboard_outcome(item), "success")

    def test_failed_with_one_succeeded_attempt_slot_is_partial(self):
        item = image_call(
            status="failed",
            error="boom",
            urls=[],
            image_attempts=[
                {"slot": 1, "attempt": 1, "status": "success"},
                {"slot": 2, "attempt": 1, "status": "failed"},
            ],
        )
        self.assertEqual(call_outcome(item), "partial_success")

    def test_text_reply_is_excluded_from_dashboard(self):
        item = image_call(status="failed", error="text", error_code="upstream_text_reply", urls=[], request_meta={"n": 1})
        self.assertEqual(call_outcome(item), "text_review")
        self.assertEqual(dashboard_outcome(item), "excluded")

    def test_rate_limit_by_code_or_status_code(self):
        by_code = image_call(status="failed", error="x", error_code="image_quota_exhausted", urls=[], request_meta={"n": 1})
        by_status = image_call(status="failed", error="x", status_code=429, urls=[], request_meta={"n": 1})
        self.assertEqual(call_outcome(by_code), "rate_limited")
        self.assertEqual(call_outcome(by_status), "rate_limited")
        self.assertEqual(dashboard_outcome(by_code), "final_failed")

    def test_plain_failure_and_non_image_call(self):
        failed = {"detail": {"endpoint": "/v1/chat/completions", "model": "gpt-5", "status": "failed", "error": "e"}}
        ok = {"detail": {"endpoint": "/v1/chat/completions", "model": "gpt-5", "status": "success"}}
        self.assertEqual(call_outcome(failed), "failed")
        self.assertEqual(call_outcome(ok), "success")
        self.assertEqual(call_image_counts(ok), (0, 0, 0, ""))

    def test_unknown_status_keeps_legacy_success_in_dashboard(self):
        item = {"detail": {"endpoint": "/v1/models", "status": ""}}
        self.assertEqual(call_outcome(item), "unknown")
        self.assertEqual(dashboard_outcome(item), "success")

    def test_switch_count_uses_attempts_and_explicit_fields(self):
        attempts = [
            {"slot": 1, "attempt": 1, "status": "failed", "switched_account": False},
            {"slot": 1, "attempt": 2, "status": "success", "switched_account": True},
        ]
        self.assertEqual(call_switch_count(image_call(image_attempts=attempts)), 1)
        self.assertEqual(call_switch_count(image_call(switch_count=3)), 3)

    def test_apply_outcome_fields_does_not_override_explicit_values(self):
        detail = image_call(outcome="failed")["detail"]
        apply_outcome_fields(detail)
        self.assertEqual(detail["outcome"], "failed")
        self.assertEqual(detail["image_requested_count"], 2)
        self.assertEqual(detail["image_succeeded_count"], 2)
        self.assertNotIn("switch_count", detail)

    def test_build_outcome_fields_skips_image_counts_for_text_calls(self):
        fields = build_outcome_fields({"endpoint": "/v1/chat/completions", "model": "gpt-5", "status": "success"})
        self.assertEqual(fields, {"outcome": "success", "attempt_count": 0, "switch_count": 0})


if __name__ == "__main__":
    unittest.main()
