"""概览统计口径：文本回复不计成败、限流单列、换号统计与旧库补列。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text

from services.dashboard_metrics_service import DashboardMetricsService
from services.storage.dashboard_metrics_repository import DashboardMetricsRepository
from services.storage.database_storage import DatabaseStorageBackend
from utils.timezone import beijing_now


def _now_str() -> str:
    return (beijing_now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


def _call(**extra):
    detail = {
        "endpoint": "/v1/images/generations",
        "model": "gpt-image-2",
        "status": "success",
        "started_at": _now_str(),
        "duration_ms": 1000,
        "request_meta": {"n": 1},
        "urls": ["/images/a.png"],
    }
    detail.update(extra)
    return {"time": _now_str(), "detail": detail}


class DashboardOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.metrics = DashboardMetricsService(self.dir / "dashboard_metrics.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_text_reply_is_counted_in_total_but_not_in_success_or_failed(self) -> None:
        self.metrics.record_call_log(_call())
        self.metrics.record_call_log(_call(status="failed", error="t", error_code="upstream_text_reply", urls=[]))
        self.metrics.record_call_log(_call(status="failed", error="boom", urls=[]))
        summary = self.metrics.summary("24h")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["text_review"], 1)
        self.assertEqual(summary["success_rate"], 50.0)
        self.assertEqual(sum(summary["trend"]["text_review_requests"]), 1)
        self.assertEqual(summary["avg_success_duration_ms"], 1000.0)

    def test_rate_limit_uses_failure_code_catalog(self) -> None:
        self.metrics.record_call_log(
            _call(status="failed", error="q", error_code="image_quota_exhausted", urls=[])
        )
        self.metrics.record_call_log(_call(status="failed", error="q", status_code=429, urls=[]))
        summary = self.metrics.summary("24h")
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(sum(summary["trend"]["rate_limited_requests"]), 2)
        self.assertEqual(sum(summary["trend"]["failed_requests"]), 0)

    def test_partial_success_counts_as_success_and_switch_stats(self) -> None:
        attempts = [
            {"slot": 1, "attempt": 1, "status": "failed", "switched_account": False},
            {"slot": 1, "attempt": 2, "status": "success", "switched_account": True},
        ]
        self.metrics.record_call_log(_call(request_meta={"n": 2}, image_attempts=attempts))
        self.metrics.record_call_log(
            _call(status="failed", error="x", urls=[], image_attempts=[
                {"slot": 1, "attempt": 1, "status": "failed", "switched_account": True},
            ])
        )
        summary = self.metrics.summary("24h")
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["switching"], {"requests": 2, "count": 2, "recovered": 1, "recovery_rate": 50.0})
        self.assertEqual(sum(summary["trend"]["switch_count"]), 2)
        rates = [value for value in summary["trend"]["success_rate"] if value is not None]
        self.assertEqual(rates, [50.0])

    def test_empty_window_has_no_rates(self) -> None:
        summary = self.metrics.summary("24h")
        self.assertIsNone(summary["success_rate"])
        self.assertIsNone(summary["switching"]["recovery_rate"])


class DashboardOutcomeRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.backend = DatabaseStorageBackend(f"sqlite:///{(self.dir / 'app.db').as_posix()}")

    def tearDown(self) -> None:
        self.backend.engine.dispose()
        self._tmp.cleanup()

    def test_old_table_gets_outcome_columns_and_round_trips(self) -> None:
        with self.backend.engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE dashboard_metric_hours (bucket VARCHAR(13) PRIMARY KEY, day VARCHAR(10) NOT NULL, "
                "total INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, "
                "rate_limited INTEGER NOT NULL DEFAULT 0, by_endpoint TEXT NOT NULL DEFAULT '{}', "
                "by_model TEXT NOT NULL DEFAULT '{}', by_status TEXT NOT NULL DEFAULT '{}', "
                "by_error_code TEXT NOT NULL DEFAULT '{}', model_total_times TEXT NOT NULL DEFAULT '{}', "
                "model_time_counts TEXT NOT NULL DEFAULT '{}')"
            ))
        repo = DashboardMetricsRepository(self.backend)
        metrics = DashboardMetricsService(self.dir / "m.json", repository_resolver=lambda: repo)
        metrics.record_call_log(_call(status="failed", error="t", error_code="upstream_text_reply", urls=[]))
        metrics.record_call_log(_call(image_attempts=[
            {"slot": 1, "attempt": 1, "status": "success", "switched_account": True},
        ]))
        metrics.flush()
        again = DashboardMetricsService(self.dir / "m.json", repository_resolver=lambda: repo)
        summary = again.summary("24h")
        self.assertEqual(summary["text_review"], 1)
        self.assertEqual(summary["switching"]["recovered"], 1)


if __name__ == "__main__":
    unittest.main()
