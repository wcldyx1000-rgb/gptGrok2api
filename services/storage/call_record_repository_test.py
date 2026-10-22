import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from services.dashboard_metrics_service import DashboardMetricsService
from services.log_service import FILE_IMPORT_STATE_KEY, LOG_TYPE_ACCOUNT, LOG_TYPE_CALL, LogService
from services.storage.call_record_repository import CallRecordModel, CallRecordRepository
from services.storage.dashboard_metrics_repository import DashboardMetricHourModel, DashboardMetricsRepository
from services.storage.database_storage import DatabaseStorageBackend
from utils.timezone import beijing_now


def _now_str(**delta) -> str:
    return (beijing_now() - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def _call(status="success", endpoint="/v1/chat/completions", model="gpt-5", **extra):
    detail = {
        "endpoint": endpoint,
        "model": model,
        "status": status,
        "started_at": _now_str(),
        "duration_ms": 1200,
    }
    detail.update(extra)
    return detail


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.backend = DatabaseStorageBackend(f"sqlite:///{(self.dir / 'app.db').as_posix()}")
        self.repo = CallRecordRepository(self.backend)
        self.logs = LogService(self.dir / "logs.jsonl", store_resolver=lambda: self.repo)

    def tearDown(self) -> None:
        self.repo.close()
        self.backend.engine.dispose()
        self._tmp.cleanup()


class CallRecordStoreTests(_DbCase):
    def test_add_is_async_and_reads_see_pending_writes(self) -> None:
        self.logs.add(LOG_TYPE_CALL, "调用完成", _call())
        # 读取前会先把写队列落库
        page = self.logs.list_page(limit=10)
        self.assertEqual(page["total"], 1)
        self.assertFalse((self.dir / "logs.jsonl").exists())

    def test_list_page_filters_facets_and_stats_match_file_semantics(self) -> None:
        file_logs = LogService(self.dir / "file-logs.jsonl")
        entries = [
            (LOG_TYPE_CALL, _call()),
            (LOG_TYPE_CALL, _call(status="failed", error="rate limit 429", error_code="rate_limited")),
            (LOG_TYPE_CALL, _call(status="failed", error="boom", account_email="a@example.com")),
            (LOG_TYPE_CALL, _call(endpoint="/v1/images/generations", model="gpt-image-1")),
            (LOG_TYPE_ACCOUNT, {"token": "x"}),
        ]
        for type_, detail in entries:
            self.logs.add(type_, "s", dict(detail))
            file_logs.add(type_, "s", dict(detail))

        for query in (
            {"status": "failed"},
            {"status": "limited"},
            {"status": "success"},
            {"search": "BOOM"},
            {"endpoint": "/v1/images/generations"},
            {"account": "a@example.com"},
            {"type": LOG_TYPE_ACCOUNT, "start_date": "2000-01-01"},
        ):
            with self.subTest(query=query):
                db_page = self.logs.list_page(limit=50, **query)
                file_page = file_logs.list_page(limit=50, **query)
                self.assertEqual(db_page["total"], file_page["total"])
                self.assertEqual(db_page["stats"], file_page["stats"])
                self.assertEqual(db_page["facets"], file_page["facets"])
                self.assertEqual(
                    sorted(item["detail"].get("status", "") for item in db_page["items"]),
                    sorted(item["detail"].get("status", "") for item in file_page["items"]),
                )

    def test_unfiltered_page_is_newest_first_with_full_facets(self) -> None:
        self.repo.append_many([
            self.logs._record_write({"id": "old", "time": _now_str(hours=2), "type": LOG_TYPE_CALL, "summary": "", "detail": _call()}),
            self.logs._record_write({"id": "new", "time": _now_str(), "type": LOG_TYPE_CALL, "summary": "", "detail": _call()}),
        ])
        page = self.logs.list_page(limit=1)
        self.assertEqual([item["id"] for item in page["items"]], ["new"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["facets"]["statuses"], {"success": 2})
        self.assertNotIn("stats_scope", page)

    def test_list_and_delete(self) -> None:
        for _ in range(3):
            self.logs.add(LOG_TYPE_CALL, "s", _call())
        items = self.logs.list(type=LOG_TYPE_CALL, limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(self.logs.delete([items[0]["id"], "missing"]), {"removed": 1})
        self.assertEqual(self.repo.count(), 2)

    def test_cleanup_respects_retention_and_dry_run(self) -> None:
        self.repo.append_many([
            self.logs._record_write({"id": "old", "time": _now_str(days=40), "type": LOG_TYPE_CALL, "detail": _call()}),
            self.logs._record_write({"id": "new", "time": _now_str(), "type": LOG_TYPE_CALL, "detail": _call()}),
        ])
        preview = self.logs.preview_cleanup_old(30)
        self.assertEqual((preview["removed"], preview["kept"], preview["dry_run"]), (1, 1, True))
        self.assertGreater(preview["removed_size_bytes"], 0)
        self.assertEqual(self.repo.count(), 2)
        result = self.logs.cleanup_old(30)
        self.assertEqual((result["removed"], result["kept"]), (1, 1))
        self.assertEqual([item["id"] for item in self.logs.list(limit=10)], ["new"])

    def test_file_import_is_idempotent_and_keeps_legacy_ids(self) -> None:
        lines = [
            json.dumps({"id": "a", "time": _now_str(minutes=5), "type": LOG_TYPE_CALL, "detail": _call()}),
            json.dumps({"time": _now_str(minutes=4), "type": LOG_TYPE_ACCOUNT, "summary": "legacy"}),
            "not json",
        ]
        (self.dir / "logs.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        first = self.logs.import_file_into_store()
        self.assertEqual(first, {"imported": 2, "skipped": False})
        self.assertEqual(self.logs.import_file_into_store(), {"imported": 0, "skipped": True})
        # 即使标记丢失，按 id 去重也不会重复导入
        self.repo.set_state(FILE_IMPORT_STATE_KEY, {})
        with self.backend.session() as session:
            from services.storage.call_record_repository import RecordStateModel

            session.query(RecordStateModel).delete()
            session.commit()
        self.assertEqual(self.logs.import_file_into_store()["imported"], 0)
        self.assertEqual(self.repo.count(), 2)

    def test_concurrent_log_and_account_writes_do_not_deadlock(self) -> None:
        errors: list[BaseException] = []

        def write_logs(worker: int) -> None:
            try:
                for index in range(150):
                    self.logs.add(LOG_TYPE_CALL, "s", _call(worker=worker, index=index))
                    if index % 50 == 0:
                        self.logs.list_page(limit=5, status="success")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def write_accounts(worker: int) -> None:
            try:
                for index in range(40):
                    self.backend.save_accounts([{"access_token": f"t{worker}-{index}", "n": index}])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write_logs, args=(n,)) for n in range(6)]
        threads += [threading.Thread(target=write_accounts, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads), "writers deadlocked")
        self.assertEqual(errors, [])
        self.logs.flush()
        self.assertEqual(self.repo.count(), 6 * 150)

    def test_failed_batch_is_retried(self) -> None:
        calls = {"n": 0}
        original = self.repo.append_many

        def flaky(records):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db down")
            return original(records)

        self.repo._writer._sink = flaky
        self.logs.add(LOG_TYPE_CALL, "s", _call())
        self.assertTrue(self.repo.flush(timeout=5))
        self.assertEqual(self.repo.count(), 1)


class DashboardMetricsRepositoryTests(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.metrics_repo = DashboardMetricsRepository(self.backend)
        self.metrics = DashboardMetricsService(self.dir / "dashboard_metrics.json", repository_resolver=lambda: self.metrics_repo)

    def _record(self, count: int, **kwargs) -> None:
        for _ in range(count):
            self.metrics.record_call_log({"time": _now_str(), "detail": _call(**kwargs)})

    def test_incremental_flush_accumulates_in_database(self) -> None:
        self._record(2)
        self.metrics.flush()
        self._record(1, status="failed", error="x", error_code="rate_limited")
        self.metrics.flush()
        summary = self.metrics.summary("24h")
        self.assertEqual((summary["total"], summary["success"], summary["failed"]), (3, 2, 1))
        self.assertEqual(summary["by_model"], {"gpt-5": 3})
        self.assertEqual(sum(summary["trend"]["rate_limited_requests"]), 1)
        self.assertFalse((self.dir / "dashboard_metrics.json").exists())
        with self.backend.session() as session:
            self.assertEqual(session.query(DashboardMetricHourModel).count(), 1)

    def test_summary_includes_unflushed_pending_without_double_counting_days(self) -> None:
        self._record(2)
        self.metrics.flush()
        self._record(1)
        self.assertEqual(self.metrics.summary("7d")["total"], 3)
        self.assertEqual(self.metrics.summary("24h")["total"], 3)

    def test_rebuild_from_records_replaces_projection(self) -> None:
        self._record(5)
        self.metrics.flush()
        items = [{"time": _now_str(), "detail": _call()} for _ in range(2)]
        self.assertEqual(self.metrics.rebuild_from_records(items), {"days": 1})
        self.assertEqual(self.metrics.summary("24h")["total"], 2)

    def test_json_buckets_imported_once(self) -> None:
        legacy = DashboardMetricsService(self.dir / "dashboard_metrics.json")
        for _ in range(4):
            legacy.record_call_log({"time": _now_str(), "detail": _call()})
        legacy.flush()
        metrics = DashboardMetricsService(self.dir / "dashboard_metrics.json", repository_resolver=lambda: self.metrics_repo)
        self.assertEqual(metrics.summary("24h")["total"], 4)
        again = DashboardMetricsService(self.dir / "dashboard_metrics.json", repository_resolver=lambda: self.metrics_repo)
        self.assertEqual(again.summary("24h")["total"], 4)

    def test_prune_drops_buckets_outside_retention(self) -> None:
        self.metrics_repo.merge(
            {"days": {"2000-01-01": {"hours": {"03": {"total": 9, "success": 9}}}}},
            lambda target, source: target.update(source),
        )
        self._record(1)
        self.metrics.flush()
        with self.backend.session() as session:
            days = {row.day for row in session.query(DashboardMetricHourModel).all()}
        self.assertNotIn("2000-01-01", days)

    def test_backfill_only_when_empty(self) -> None:
        items = [{"time": _now_str(), "detail": _call()} for _ in range(3)]
        self.metrics.backfill_if_empty(items)
        self.metrics.backfill_if_empty(items)
        self.assertEqual(self.metrics.summary("24h")["total"], 3)


class JsonBackendFallbackTests(unittest.TestCase):
    def test_file_backends_keep_using_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            logs = LogService(path / "logs.jsonl", store_resolver=lambda: None)
            logs.add(LOG_TYPE_CALL, "s", _call())
            self.assertFalse(logs.uses_database())
            self.assertTrue((path / "logs.jsonl").exists())
            self.assertEqual(logs.list_page(limit=5)["total"], 1)
            self.assertEqual(logs.import_file_into_store(), {"imported": 0, "skipped": True})

            metrics = DashboardMetricsService(path / "dashboard_metrics.json", repository_resolver=lambda: None)
            metrics.record_call_log({"time": _now_str(), "detail": _call()})
            metrics.flush()
            self.assertTrue((path / "dashboard_metrics.json").exists())
            self.assertEqual(metrics.summary("24h")["total"], 1)
            self.assertEqual(metrics.rebuild_from_records(list(logs.iter_records(type=LOG_TYPE_CALL))), {"days": 1})
            self.assertEqual(metrics.summary("24h")["total"], 1)

    def test_store_resolver_failure_falls_back_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def broken():
                raise RuntimeError("no db")

            logs = LogService(Path(tmp) / "logs.jsonl", store_resolver=broken)
            logs.add(LOG_TYPE_CALL, "s", _call())
            self.assertEqual(logs.list(limit=5)[0]["type"], LOG_TYPE_CALL)


if __name__ == "__main__":
    unittest.main()
