import unittest

from services.realtime_monitor_service import RealtimeMonitorService


def finish(service, call_id, **detail):
    service.start(call_id, endpoint=detail.get("endpoint", "/v1/images/generations"), model="gpt-image-2")
    base = {
        "call_id": call_id,
        "endpoint": "/v1/images/generations",
        "model": "gpt-image-2",
        "status": "success",
        "duration_ms": 1000,
    }
    base.update(detail)
    service.finish(base)


class RealtimeMonitorSummaryTests(unittest.TestCase):
    def test_summary_splits_outcomes_and_switch_stats(self):
        service = RealtimeMonitorService()
        finish(service, "ok1")
        finish(service, "partial", request_meta={"n": 2}, urls=["/images/a.png"], outcome="partial_success")
        finish(service, "limited", status="failed", error="quota", error_code="image_quota_exhausted")
        finish(service, "text", status="failed", error="reply", error_code="upstream_text_reply")
        finish(service, "bad", status="failed", error="boom")
        finish(service, "recovered", switch_count=2, outcome="success")
        finish(service, "unrecovered", status="failed", error="x", switch_count=1)

        summary = service.snapshot()["summary"]

        self.assertEqual(summary["completed"], 7)
        self.assertEqual(summary["success"], 2)
        self.assertEqual(summary["partial_success"], 1)
        self.assertEqual(summary["rate_limited"], 1)
        self.assertEqual(summary["text_review"], 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["measured"], 6)
        self.assertEqual(summary["success_rate"], 50.0)
        self.assertEqual(summary["account_switch_requests"], 2)
        self.assertEqual(summary["account_switches"], 3)
        self.assertEqual(summary["account_switch_success"], 1)
        self.assertEqual(summary["account_switch_recovery_rate"], 50.0)
        self.assertEqual(summary["switch_unrecovered"], 1)
        self.assertEqual(summary["switch_average"], 1.5)

    def test_records_carry_outcome_fields_and_stream_errors_are_counted(self):
        service = RealtimeMonitorService()
        service.start("c1", endpoint="/v1/images/generations", model="gpt-image-2")
        service.stage("c1", "image_stream_failed", stream_error_ms=1200)
        service.finish({
            "call_id": "c1",
            "endpoint": "/v1/images/generations",
            "model": "gpt-image-2",
            "status": "failed",
            "error": "stream broke",
            "error_code": "image_stream_interrupted",
            "status_code": 502,
            "attempt_count": 3,
        })
        snapshot = service.snapshot()
        record = snapshot["recent"][0]
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["error_code"], "image_stream_interrupted")
        self.assertEqual(record["status_code"], 502)
        self.assertEqual(record["attempt_count"], 3)
        self.assertEqual(snapshot["summary"]["stream_error_requests"], 1)

    def test_empty_summary_is_zero(self):
        summary = RealtimeMonitorService().snapshot()["summary"]
        self.assertEqual(summary["success_rate"], 0)
        self.assertEqual(summary["account_switch_recovery_rate"], 0)


if __name__ == "__main__":
    unittest.main()
