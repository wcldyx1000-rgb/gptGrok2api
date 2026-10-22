"""概览单次快照、成功耗时口径与旧库补列。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from services.dashboard_metrics_service import DashboardMetricsService
from services.storage.dashboard_metrics_repository import DashboardMetricHourModel, DashboardMetricsRepository
from services.storage.database_storage import DatabaseStorageBackend
from utils.timezone import beijing_now


def _now_str(**delta) -> str:
    return (beijing_now() - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def _call(status="success", model="gpt-5", duration_ms=1000, **extra):
    detail = {
        "endpoint": "/v1/chat/completions",
        "model": model,
        "status": status,
        "started_at": _now_str(),
        "duration_ms": duration_ms,
    }
    detail.update(extra)
    return detail


class DashboardSuccessMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.metrics = DashboardMetricsService(self.dir / "dashboard_metrics.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _record(self, **kwargs) -> None:
        self.metrics.record_call_log({"time": _now_str(), "detail": _call(**kwargs)})

    def test_success_duration_excludes_failed_calls(self) -> None:
        self._record(duration_ms=1000)
        self._record(duration_ms=3000)
        self._record(status="failed", error="boom", duration_ms=60000)
        summary = self.metrics.summary("24h")
        self.assertEqual(summary["avg_success_duration_ms"], 2000.0)
        trend = summary["trend"]
        self.assertEqual(len([v for v in trend["success_duration_ms"] if v is not None]), 1)
        self.assertEqual(trend["model_avg_success_duration_ms"]["gpt-5"][-1], 2000.0)
        # 模型请求分布只统计成功请求；占比/排行仍沿用全部请求口径。
        self.assertEqual(sum(trend["model_success_requests"]["gpt-5"]), 2)
        self.assertEqual(sum(trend["model_requests"]["gpt-5"]), 3)

    def test_no_success_gives_null_not_zero(self) -> None:
        self._record(status="failed", error="x", duration_ms=500)
        summary = self.metrics.summary("24h")
        self.assertIsNone(summary["avg_success_duration_ms"])
        self.assertTrue(all(v is None for v in summary["trend"]["success_duration_ms"]))

    def test_snapshot_many_returns_all_ranges_from_one_load(self) -> None:
        self._record()
        with patch.object(self.metrics, "_load_days", wraps=self.metrics._load_days) as load:
            snapshot = self.metrics.snapshot_many()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(set(snapshot), {"24h", "7d", "30d"})
        self.assertEqual([len(snapshot[r]["trend"]["labels"]) for r in ("24h", "7d", "30d")], [24, 7, 30])
        self.assertTrue(all(snapshot[r]["total"] == 1 for r in snapshot))

    def test_activity_series_available_for_heatmap(self) -> None:
        self._record()
        trend = self.metrics.summary("24h")["trend"]
        self.assertEqual(len(trend["total_requests"]), len(trend["labels"]))
        self.assertEqual(sum(trend["total_requests"]), 1)


class DashboardMetricsRepositoryMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.backend = DatabaseStorageBackend(f"sqlite:///{(self.dir / 'app.db').as_posix()}")

    def tearDown(self) -> None:
        self.backend.engine.dispose()
        self._tmp.cleanup()

    def test_old_table_gets_success_columns(self) -> None:
        with self.backend.engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE dashboard_metric_hours (bucket VARCHAR(13) PRIMARY KEY, day VARCHAR(10) NOT NULL, "
                "total INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, "
                "rate_limited INTEGER NOT NULL DEFAULT 0, by_endpoint TEXT NOT NULL DEFAULT '{}', "
                "by_model TEXT NOT NULL DEFAULT '{}', by_status TEXT NOT NULL DEFAULT '{}', "
                "by_error_code TEXT NOT NULL DEFAULT '{}', model_total_times TEXT NOT NULL DEFAULT '{}', "
                "model_time_counts TEXT NOT NULL DEFAULT '{}')"
            ))
            conn.execute(text("INSERT INTO dashboard_metric_hours (bucket, day, total) VALUES ('2026-01-01 00', '2026-01-01', 4)"))
        repo = DashboardMetricsRepository(self.backend)
        loaded = repo.load("2026-01-01")
        hour = loaded["days"]["2026-01-01"]["hours"]["00"]
        self.assertEqual(hour["total"], 4)
        self.assertEqual(hour["success_duration_count"], 0)
        # 再次初始化是幂等的。
        DashboardMetricsRepository(self.backend)

    def test_success_fields_round_trip_in_database(self) -> None:
        repo = DashboardMetricsRepository(self.backend)
        metrics = DashboardMetricsService(self.dir / "m.json", repository_resolver=lambda: repo)
        metrics.record_call_log({"time": _now_str(), "detail": _call(duration_ms=800)})
        metrics.flush()
        again = DashboardMetricsService(self.dir / "m.json", repository_resolver=lambda: repo)
        summary = again.summary("24h")
        self.assertEqual(summary["avg_success_duration_ms"], 800.0)
        with self.backend.session() as session:
            self.assertEqual(session.query(DashboardMetricHourModel).count(), 1)


class DashboardSnapshotRouteTest(unittest.TestCase):
    def test_snapshot_route_returns_accounts_and_ranges(self) -> None:
        from api import system

        app = FastAPI()
        app.include_router(system.create_router("9.9.9"))
        client = TestClient(app)
        ranges = {r: {"total": 1, "trend": {"labels": []}} for r in ("24h", "7d", "30d")}
        with patch.object(system, "require_admin", return_value=None), \
                patch.object(system, "get_provider_account_stats", new=AsyncMock(return_value={"active": 2, "total": 3})), \
                patch.object(system.dashboard_metrics_service, "snapshot_many", return_value=ranges) as snap:
            response = client.get("/api/dashboard/snapshot")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"], "9.9.9")
        self.assertTrue(body["healthy"])
        self.assertEqual(body["accounts"]["total"], 3)
        self.assertEqual(set(body["ranges"]), {"24h", "7d", "30d"})
        self.assertEqual(snap.call_count, 1)


if __name__ == "__main__":
    unittest.main()
