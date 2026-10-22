"""日志归类字段：写入、状态筛选与统计。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from services import log_service as log_module
from services.log_service import LOG_TYPE_CALL, LoggedCall, LogService
from services.storage.call_record_repository import CallRecordRepository
from services.storage.database_storage import DatabaseStorageBackend


class LogOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = LogService(Path(self._tmp.name) / "logs.jsonl")

    def tearDown(self):
        self.logs.close()
        self._tmp.cleanup()

    def _add(self, **detail):
        base = {"endpoint": "/v1/images/generations", "model": "gpt-image-2", "status": "success"}
        base.update(detail)
        self.logs.add(LOG_TYPE_CALL, "文生图", base)

    def test_status_filters_and_stats_cover_partial_and_text(self):
        self._add(outcome="success")
        self._add(outcome="partial_success", request_meta={"n": 2})
        self._add(status="failed", error="t", error_code="upstream_text_reply", outcome="text_review")
        self._add(status="failed", error="boom", outcome="failed")
        self.logs.flush()

        partial = self.logs.list_page(status="partial", limit=10)
        text = self.logs.list_page(status="text", limit=10)
        everything = self.logs.list_page(limit=10)

        self.assertEqual([item["detail"]["outcome"] for item in partial["items"]], ["partial_success"])
        self.assertEqual([item["detail"]["outcome"] for item in text["items"]], ["text_review"])
        self.assertEqual(everything["stats"]["partial_success"], 1)
        self.assertEqual(everything["stats"]["text_review"], 1)

    def test_logged_call_writes_outcome_fields(self):
        call = LoggedCall(
            {"id": "k", "name": "key", "role": "user"},
            "/v1/images/generations",
            "gpt-image-2",
            "文生图",
            trace_metadata={"n": 2},
        )
        captured = {}
        with patch.object(log_module.log_service, "add", side_effect=lambda t, s, d: captured.update(d)):
            call.log("调用完成", {"data": [{"url": "http://x/images/a.png"}]})
        self.assertEqual(captured["outcome"], "partial_success")
        self.assertEqual(captured["image_requested_count"], 2)
        self.assertEqual(captured["image_succeeded_count"], 1)
        self.assertEqual(captured["image_failed_count"], 1)


class LogOutcomeDatabaseTests(unittest.TestCase):
    def test_old_table_gets_outcome_column_and_filters_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = DatabaseStorageBackend(f"sqlite:///{(Path(tmp) / 'app.db').as_posix()}")
            with backend.engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE log_records (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id VARCHAR(64) NOT NULL UNIQUE, "
                    "event_time VARCHAR(32) NOT NULL, event_day VARCHAR(10) NOT NULL, type VARCHAR(32) NOT NULL DEFAULT '', "
                    "status VARCHAR(64) NOT NULL DEFAULT '', status_lower VARCHAR(64) NOT NULL DEFAULT '', "
                    "endpoint VARCHAR(255) NOT NULL DEFAULT '', model VARCHAR(255) NOT NULL DEFAULT '', "
                    "account_email VARCHAR(320) NOT NULL DEFAULT '', conversation_id VARCHAR(255) NOT NULL DEFAULT '', "
                    "is_failed INTEGER NOT NULL DEFAULT 0, is_limited INTEGER NOT NULL DEFAULT 0, "
                    "is_image INTEGER NOT NULL DEFAULT 0, search_text TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL)"
                ))
            repo = CallRecordRepository(backend)
            try:
                logs = LogService(Path(tmp) / "logs.jsonl", store_resolver=lambda: repo)
                base = {"endpoint": "/v1/images/generations", "model": "gpt-image-2", "status": "success"}
                logs.add(LOG_TYPE_CALL, "a", {**base, "outcome": "partial_success"})
                logs.add(LOG_TYPE_CALL, "b", {**base, "status": "failed", "error": "t", "outcome": "text_review"})
                logs.add(LOG_TYPE_CALL, "c", {**base, "outcome": "success"})
                logs.flush()
                partial = logs.list_page(status="partial", limit=10)
                text_page = logs.list_page(status="text", limit=10)
                everything = logs.list_page(limit=10)
                self.assertEqual([item["summary"] for item in partial["items"]], ["a"])
                self.assertEqual([item["summary"] for item in text_page["items"]], ["b"])
                self.assertEqual(everything["stats"]["partial_success"], 1)
                self.assertEqual(everything["stats"]["text_review"], 1)
                # 再次初始化是幂等的。
                CallRecordRepository(backend)
            finally:
                repo.close()
                backend.engine.dispose()


if __name__ == "__main__":
    unittest.main()
