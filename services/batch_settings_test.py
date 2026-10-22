from __future__ import annotations

import io
import os
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import services.backup_service as backup_module
from api.system import SETTINGS_UPDATE_KEYS
from services.account_processing import (
    account_processing_slot,
    account_processing_worker_count,
)
from services.bounded_task_runner import BoundedTaskRunner
from services.config import DEFAULT_BACKUP_INCLUDE, config
from services.image_task_service import ImageTaskQueueFullError, ImageTaskService
from services.realtime_monitor_service import DEFAULT_THREAD_TOKENS, thread_tokens


_SETTING_KEYS = (
    "image_poll_initial_wait_secs",
    "image_poll_interval_secs",
    "image_settle_secs",
    "image_queue_limit",
    "console_request_timeout_secs",
    "account_processing_concurrency",
    "image_stream_timeout_secs",
    "image_poll_timeout_secs",
)


class _ConfigIsolation(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(config.data, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        reload_patcher = patch.object(config, "reload_if_changed", lambda: None)
        reload_patcher.start()
        self.addCleanup(reload_patcher.stop)
        for key in _SETTING_KEYS:
            config.data.pop(key, None)


class ImagePollDefaultsTest(_ConfigIsolation):
    def test_fast_poll_defaults(self) -> None:
        self.assertEqual(config.image_poll_initial_wait_secs, 1.0)
        self.assertEqual(config.image_poll_interval_secs, 2.0)
        self.assertEqual(config.image_settle_secs, 1.5)

    def test_poll_values_are_still_clamped(self) -> None:
        config.data.update({"image_poll_interval_secs": 0.1, "image_settle_secs": 0, "image_poll_initial_wait_secs": -3})
        self.assertEqual(config.image_poll_interval_secs, 0.5)
        self.assertEqual(config.image_settle_secs, 0.5)
        self.assertEqual(config.image_poll_initial_wait_secs, 0.0)

    def test_image_request_timeout_is_stream_plus_poll_plus_margin(self) -> None:
        config.data.update({"image_stream_timeout_secs": 100, "image_poll_timeout_secs": 40})
        self.assertEqual(config.image_request_timeout_secs, 170)


class NewSettingsClampTest(_ConfigIsolation):
    def test_defaults(self) -> None:
        self.assertEqual(config.image_queue_limit, 0)
        self.assertEqual(config.console_request_timeout_secs, 600)
        self.assertEqual(config.account_processing_concurrency, 30)

    def test_clamping_and_invalid_values(self) -> None:
        config.data.update({
            "image_queue_limit": -5,
            "console_request_timeout_secs": 5,
            "account_processing_concurrency": 999,
        })
        self.assertEqual(config.image_queue_limit, 0)
        self.assertEqual(config.console_request_timeout_secs, 30)
        self.assertEqual(config.account_processing_concurrency, 100)
        config.data.update({
            "image_queue_limit": 99999,
            "console_request_timeout_secs": 99999,
            "account_processing_concurrency": 0,
        })
        self.assertEqual(config.image_queue_limit, 10000)
        self.assertEqual(config.console_request_timeout_secs, 3600)
        self.assertEqual(config.account_processing_concurrency, 1)
        config.data.update({
            "image_queue_limit": "abc",
            "console_request_timeout_secs": None,
            "account_processing_concurrency": "x",
        })
        self.assertEqual(config.image_queue_limit, 0)
        self.assertEqual(config.console_request_timeout_secs, 600)
        self.assertEqual(config.account_processing_concurrency, 30)

    def test_get_exposes_new_settings_and_they_are_saveable(self) -> None:
        with patch.object(config, "get_storage_backend", create=True):
            data = config.get()
        for key in ("image_queue_limit", "console_request_timeout_secs", "account_processing_concurrency"):
            self.assertIn(key, data)
            self.assertIn(key, SETTINGS_UPDATE_KEYS)


class ImageQueueLimitTest(_ConfigIsolation):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.started = threading.Semaphore(0)
        log_patch = patch("services.image_task_service.log_service.add")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.identity = {"id": "u", "name": "k", "role": "admin"}

    def _handler(self, payload: dict) -> dict:
        self.started.release()
        self.release.wait(5)
        return {"created": 1, "data": [{"url": "https://example.test/x.png"}]}

    def _submit(self, service: ImageTaskService, task_id: str) -> dict:
        return service.submit_generation(
            self.identity,
            client_task_id=task_id,
            prompt="p",
            model="gpt-image-2",
            n=1,
            base_url="https://example.test",
        )

    def test_configured_limit_rejects_when_in_flight_reaches_limit(self) -> None:
        config.data["image_queue_limit"] = 2
        runner = BoundedTaskRunner(name="test-queue-limit", max_workers=1, queue_size=10)
        service = ImageTaskService(
            Path(self.temp_dir.name) / "tasks.json",
            generation_handler=self._handler,
            task_runner=runner,
        )
        self._submit(service, "a")
        self.assertTrue(self.started.acquire(timeout=2))
        self._submit(service, "b")
        with self.assertRaises(ImageTaskQueueFullError):
            self._submit(service, "c")
        self.assertEqual(service.list_tasks(self.identity, ["c"])["items"], [])
        self.release.set()
        runner.shutdown(wait=True)

    def test_zero_means_automatic_and_only_runner_capacity_applies(self) -> None:
        config.data["image_queue_limit"] = 0
        runner = BoundedTaskRunner(name="test-queue-auto", max_workers=1, queue_size=3)
        service = ImageTaskService(
            Path(self.temp_dir.name) / "tasks.json",
            generation_handler=self._handler,
            task_runner=runner,
        )
        for index in range(4):
            self._submit(service, f"t{index}")
        with self.assertRaises(ImageTaskQueueFullError):
            self._submit(service, "overflow")
        self.release.set()
        runner.shutdown(wait=True)


class AccountProcessingTest(_ConfigIsolation):
    def test_worker_count_respects_setting(self) -> None:
        self.assertEqual(account_processing_worker_count(100), 30)
        self.assertEqual(account_processing_worker_count(3), 3)
        self.assertEqual(account_processing_worker_count(0), 0)
        config.data["account_processing_concurrency"] = 4
        self.assertEqual(account_processing_worker_count(100), 4)

    def test_slot_limits_concurrency_and_is_reentrant(self) -> None:
        config.data["account_processing_concurrency"] = 1
        with account_processing_slot():
            with account_processing_slot():
                pass
            entered = threading.Event()

            def other() -> None:
                with account_processing_slot():
                    entered.set()

            thread = threading.Thread(target=other, daemon=True)
            thread.start()
            self.assertFalse(entered.wait(0.2))
        self.assertTrue(entered.wait(2))
        thread.join(2)


class BackupEditableFilesTest(unittest.TestCase):
    def _build(self, include: dict) -> set[str]:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            (data_dir / "files" / "ppt").mkdir(parents=True)
            (data_dir / "files" / "ppt" / "deck.pptx").write_bytes(b"pptx")
            with patch.object(backup_module, "DATA_DIR", data_dir):
                payload = backup_module.BackupService()._build_backup_archive({"include": include}, trigger="test")
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                return {member.name for member in archive.getmembers() if member.isfile()}

    def test_editable_files_included_when_enabled(self) -> None:
        self.assertIn("data/files/ppt/deck.pptx", self._build({"editable_files": True}))

    def test_editable_files_excluded_when_disabled(self) -> None:
        self.assertNotIn("data/files/ppt/deck.pptx", self._build({"editable_files": False}))

    def test_default_include_has_editable_files(self) -> None:
        self.assertTrue(DEFAULT_BACKUP_INCLUDE["editable_files"])


class ThreadTokensTest(unittest.TestCase):
    def _env(self, **values: str):
        cleaned = {key: "" for key in ("GPTGROK2API_THREAD_TOKENS", "CHATGPT2API_THREAD_TOKENS")}
        cleaned.update(values)
        return patch.dict(os.environ, cleaned)

    def test_default_is_120(self) -> None:
        with self._env():
            self.assertEqual(thread_tokens(), DEFAULT_THREAD_TOKENS)
            self.assertEqual(DEFAULT_THREAD_TOKENS, 120)

    def test_new_name_wins_and_legacy_name_is_compatible(self) -> None:
        with self._env(CHATGPT2API_THREAD_TOKENS="77"):
            self.assertEqual(thread_tokens(), 77)
        with self._env(CHATGPT2API_THREAD_TOKENS="77", GPTGROK2API_THREAD_TOKENS="33"):
            self.assertEqual(thread_tokens(), 33)

    def test_invalid_value_falls_back_to_default(self) -> None:
        with self._env(GPTGROK2API_THREAD_TOKENS="abc"):
            self.assertEqual(thread_tokens(), 120)


if __name__ == "__main__":
    unittest.main()
