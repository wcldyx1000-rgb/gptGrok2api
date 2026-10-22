from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import image_tasks
from services.image_task_service import ImageTaskQueueFullError


class ImageTaskQueueFullApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(image_tasks.create_router())
        self.client = TestClient(app)
        self.service = MagicMock()
        for target in (
            patch.object(image_tasks, "image_task_service", self.service),
            patch.object(image_tasks, "require_identity", return_value={"id": "u", "role": "admin"}),
            patch.object(image_tasks, "check_request"),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_generation_queue_full_is_503_with_code_and_retry_after(self) -> None:
        self.service.submit_generation.side_effect = ImageTaskQueueFullError()
        response = self.client.post(
            "/api/image-tasks/generations", json={"client_task_id": "t1", "prompt": "cat"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "image_task_queue_full")
        self.assertEqual(response.headers["retry-after"], "5")

    def test_edit_rejects_before_reading_uploads_and_releases_reservation(self) -> None:
        self.service.reserve_submission.side_effect = ImageTaskQueueFullError()
        with patch.object(image_tasks, "read_image_sources") as read_sources:
            response = self.client.post(
                "/api/image-tasks/edits",
                data={"client_task_id": "t1", "prompt": "p", "model": "gpt-image-2"},
                files={"image": ("a.png", b"\x89PNG", "image/png")},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "image_task_queue_full")
        read_sources.assert_not_called()

    def test_edit_failure_after_reservation_rolls_it_back(self) -> None:
        reservation = MagicMock()
        self.service.reserve_submission.return_value = reservation
        self.service.submit_edit.side_effect = ValueError("bad")
        response = self.client.post(
            "/api/image-tasks/edits",
            data={"client_task_id": "t1", "prompt": "p", "model": "gpt-image-2"},
            files={"image": ("a.png", b"\x89PNG", "image/png")},
        )
        self.assertEqual(response.status_code, 400)
        reservation.rollback.assert_called()

    def test_list_passes_limit(self) -> None:
        self.service.list_tasks.return_value = {"items": [], "missing_ids": [], "has_more": False}
        with patch.object(image_tasks, "get_image_account_stats", new=MagicMock(side_effect=lambda: _async({}))):
            response = self.client.get("/api/image-tasks?limit=7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.list_tasks.call_args.kwargs["limit"], 7)
        self.assertIn("has_more", response.json())


async def _async(value):
    return value


if __name__ == "__main__":
    unittest.main()
