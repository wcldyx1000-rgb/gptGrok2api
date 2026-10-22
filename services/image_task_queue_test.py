from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.bounded_task_runner import BoundedTaskRunner
from services.image_task_service import ImageTaskQueueFullError, ImageTaskService


class ImageTaskBoundedQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "image_tasks.json"
        self.identity = {"id": "test-user", "name": "test-key", "role": "admin"}
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.log_patch = patch("services.image_task_service.log_service.add")
        self.log_patch.start()

    def tearDown(self) -> None:
        self.release.set()
        self.log_patch.stop()
        self.temp_dir.cleanup()

    def _blocking_handler(self, payload: dict) -> dict:
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

    def _wait(self, service: ImageTaskService, task_id: str) -> dict:
        deadline = time.time() + 3
        while time.time() < deadline:
            items = service.list_tasks(self.identity, [task_id])["items"]
            if items and items[0]["status"] in {"success", "error"}:
                return items[0]
            time.sleep(0.01)
        self.fail(f"task {task_id} did not finish")

    def test_tasks_run_with_bounded_workers_and_reject_when_full(self) -> None:
        runner = BoundedTaskRunner(name="test-image-task", max_workers=1, queue_size=1)
        service = ImageTaskService(self.path, generation_handler=self._blocking_handler, task_runner=runner)

        self._submit(service, "t1")
        self.assertTrue(self.started.acquire(timeout=2))
        self._submit(service, "t2")  # 排队
        with self.assertRaises(ImageTaskQueueFullError):
            self._submit(service, "t3")
        # 被拒绝的任务不应留下记录。
        self.assertEqual(service.list_tasks(self.identity, ["t3"])["items"], [])
        # 重复提交同一个 id 直接返回已有任务，不占新名额。
        self.assertEqual(self._submit(service, "t2")["id"], "t2")

        self.release.set()
        self.assertEqual(self._wait(service, "t1")["status"], "success")
        self.assertEqual(self._wait(service, "t2")["status"], "success")
        runner.shutdown(wait=True)

    def test_queued_task_cancelled_on_shutdown_is_marked_error(self) -> None:
        runner = BoundedTaskRunner(name="test-image-task", max_workers=1, queue_size=2)
        service = ImageTaskService(self.path, generation_handler=self._blocking_handler, task_runner=runner)
        self._submit(service, "t1")
        self.assertTrue(self.started.acquire(timeout=2))
        self._submit(service, "t2")

        service.shutdown()
        item = service.list_tasks(self.identity, ["t2"])["items"][0]
        self.assertEqual(item["status"], "error")
        self.release.set()
        runner.shutdown(wait=True)

    def test_reserve_submission_occupies_slot_and_rollback_releases_it(self) -> None:
        runner = BoundedTaskRunner(name="test-image-task", max_workers=1, queue_size=1)
        service = ImageTaskService(self.path, generation_handler=self._blocking_handler, task_runner=runner)

        first = service.reserve_submission()
        second = service.reserve_submission()
        with self.assertRaises(ImageTaskQueueFullError) as ctx:
            service.reserve_submission()
        self.assertEqual(ctx.exception.code, "image_task_queue_full")
        first.rollback()
        service.reserve_submission().rollback()
        second.rollback()
        self.assertEqual(runner.status()["accepted"], 0)

    def test_submit_with_reservation_uses_that_slot(self) -> None:
        runner = BoundedTaskRunner(name="test-image-task", max_workers=1, queue_size=1)
        service = ImageTaskService(self.path, generation_handler=self._blocking_handler, task_runner=runner)

        reservation = service.reserve_submission()
        row = service.submit_edit(
            self.identity,
            client_task_id="e1",
            prompt="p",
            model="gpt-image-2",
            images=[(b"x", "a.png", "image/png")],
            reservation=reservation,
        )
        self.assertEqual(row["id"], "e1")
        self.assertEqual(runner.status()["accepted"], 1)
        # 同 id 重复提交：已有任务直接返回，并释放传入的预占名额。
        again = service.reserve_submission()
        service.submit_edit(self.identity, client_task_id="e1", prompt="p", model="gpt-image-2", reservation=again)
        self.assertEqual(runner.status()["accepted"], 1)
        self.release.set()
        runner.shutdown(wait=True)

    def test_image_queue_limit_rejects_at_reservation(self) -> None:
        runner = BoundedTaskRunner(name="test-image-task", max_workers=2, queue_size=4)
        service = ImageTaskService(self.path, generation_handler=self._blocking_handler, task_runner=runner)
        with patch("services.image_task_service.config") as cfg:
            cfg.image_queue_limit = 1
            held = service.reserve_submission()
            with self.assertRaises(ImageTaskQueueFullError):
                service.reserve_submission()
            held.rollback()
        self.assertEqual(runner.status()["accepted"], 0)

    def test_list_tasks_without_ids_is_limited_and_reports_has_more(self) -> None:
        runner = BoundedTaskRunner(name="test-image-task", max_workers=1, queue_size=8)
        service = ImageTaskService(self.path, generation_handler=lambda payload: {"data": [{"url": "u"}]}, task_runner=runner)
        for index in range(3):
            self._submit(service, f"t{index}")
        for index in range(3):
            self._wait(service, f"t{index}")

        page = service.list_tasks(self.identity, [], limit=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        full = service.list_tasks(self.identity, [], limit=10)
        self.assertEqual(len(full["items"]), 3)
        self.assertFalse(full["has_more"])
        by_id = service.list_tasks(self.identity, ["t0", "t1", "t2"], limit=1)
        self.assertEqual(len(by_id["items"]), 3)
        runner.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
