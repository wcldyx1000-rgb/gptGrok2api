import threading
import unittest
from unittest.mock import patch

from services import retention_cleanup_service as module
from services.retention_cleanup_service import RetentionCleanupCoordinator


def _make(calls, **overrides):
    def log_runner(hours, dry_run):
        calls.append(("logs", hours, dry_run))
        return {"removed": 2, "removed_size_bytes": 10, "dry_run": dry_run}

    def image_runner(hours, dry_run):
        calls.append(("images", hours, dry_run))
        return {"removed": 3, "removed_size_bytes": 30, "dry_run": dry_run}

    kwargs = dict(
        log_runner=log_runner,
        image_runner=image_runner,
        image_space_runner=lambda: None,
        image_target_runner=lambda target, dry_run: {"removed": 1, "target": target, "dry_run": dry_run},
        interval_seconds=1,
    )
    kwargs.update(overrides)
    return RetentionCleanupCoordinator(**kwargs)


class RetentionCleanupCoordinatorTest(unittest.TestCase):
    def test_manual_run_uses_hours_and_totals(self):
        calls = []
        coordinator = _make(calls)
        result = coordinator.run_retention(log_retention_hours=48, image_retention_hours=6)
        self.assertEqual(calls, [("logs", 48, False), ("images", 6, False)])
        self.assertEqual(result["logs"]["retention_hours"], 48)
        self.assertEqual(result["images"]["retention_hours"], 6)
        self.assertEqual(result["total_removed"], 5)
        self.assertEqual(result["total_size_bytes"], 40)
        status = coordinator.runtime_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["last_removed"], {"logs": 2, "images": 3})

    def test_preview_does_not_touch_status(self):
        calls = []
        coordinator = _make(calls)
        result = coordinator.run_retention(log_retention_hours=1, image_retention_hours=1, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertTrue(all(call[2] for call in calls))
        self.assertIsNone(coordinator.runtime_status()["last_started_at"])

    def test_failure_is_recorded_and_reraised(self):
        def boom(hours, dry_run):
            raise RuntimeError("disk gone")

        coordinator = _make([], image_runner=boom)
        with self.assertRaises(RuntimeError):
            coordinator.run_retention(log_retention_hours=1, image_retention_hours=1)
        status = coordinator.runtime_status()
        self.assertEqual(status["last_error"]["images"], "disk gone")
        self.assertFalse(status["running"])

    def test_default_hours_come_from_config(self):
        calls = []
        coordinator = _make(calls)
        with patch.object(module, "config") as fake_config:
            fake_config.log_retention_hours = 24
            fake_config.image_retention_hours = 72
            coordinator.run_retention()
        self.assertEqual(calls, [("logs", 24, False), ("images", 72, False)])

    def test_automatic_run_collects_errors_and_image_space(self):
        calls = []
        space = {"free_mb": 1, "min_free_mb": 500, "cleanup": {"removed": 4}}

        def failing_logs(hours, dry_run):
            raise OSError("locked")

        coordinator = _make(calls, log_runner=failing_logs, image_space_runner=lambda: space)
        with patch.object(module, "config") as fake_config:
            fake_config.log_retention_hours = 24
            fake_config.image_retention_hours = 24
            result = coordinator.run_automatic(enforce_image_free_space=True)
        self.assertEqual(result["errors"], {"logs": "locked"})
        self.assertEqual(result["image_space"], space)
        self.assertEqual(coordinator.runtime_status()["last_removed"]["images"], 3 + 4)

    def test_automatic_run_skipped_when_stopping(self):
        calls = []
        coordinator = _make(calls)
        stop = threading.Event()
        stop.set()
        result = coordinator.run_automatic(enforce_image_free_space=True, stop_event=stop)
        self.assertEqual(calls, [])
        self.assertIsNone(result["logs"])

    def test_cleanup_to_target_goes_through_coordinator(self):
        coordinator = _make([])
        self.assertEqual(coordinator.cleanup_images_to_target(800)["target"], 800)
        self.assertEqual(coordinator.runtime_status()["last_removed"]["images"], 1)

    def test_scheduler_runs_startup_and_wakes_on_change(self):
        calls = []
        coordinator = _make(calls, interval_seconds=3600)
        stop = threading.Event()
        with patch.object(module, "config") as fake_config:
            fake_config.log_retention_hours = 1
            fake_config.image_retention_hours = 1
            thread = coordinator.start_scheduler(stop)
            for _ in range(100):
                if len(calls) >= 2:
                    break
                stop.wait(0.05)
            self.assertEqual({call[0] for call in calls}, {"logs", "images"})
            coordinator.notify_retention_change()
            for _ in range(100):
                if len(calls) >= 4:
                    break
                stop.wait(0.05)
            stop.set()
            thread.join(timeout=3)
        self.assertGreaterEqual(len(calls), 4)
        self.assertFalse(thread.is_alive())

    def test_free_space_guard_disabled_by_zero(self):
        with patch.object(module, "config") as fake_config:
            fake_config.image_min_free_mb = 0
            self.assertIsNone(module._enforce_image_free_space())


if __name__ == "__main__":
    unittest.main()
