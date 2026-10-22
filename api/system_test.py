import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system


class SystemVersionRoutesTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        self.client = TestClient(app)

    def test_update_meta_uses_github_update_service_and_forwards_force(self):
        payload = {
            "current_version": "1.0.3",
            "latest_version": "1.0.3",
            "status": "ok",
            "changelog": "# Changelog",
        }
        update_info = AsyncMock(return_value=payload)

        with patch.object(system, "get_latest_release_info", new=update_info):
            response = self.client.get("/meta/update?force=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        update_info.assert_awaited_once_with(force=True)


class SystemUpdateRoutesTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        self.client = TestClient(app)

    def test_update_requires_admin(self):
        with patch.object(system, "require_admin", side_effect=system.HTTPException(status_code=401)):
            self.assertEqual(self.client.post("/api/system/update", json={"confirm": True}).status_code, 401)
            self.assertEqual(self.client.get("/api/system/update-status").status_code, 401)

    def test_update_needs_explicit_confirmation(self):
        with patch.object(system, "require_admin"), patch.object(system.update_service, "start") as start:
            response = self.client.post("/api/system/update", json={})
            self.assertEqual(response.status_code, 400)
            response = self.client.post("/api/system/update", json={"confirm": False})
            self.assertEqual(response.status_code, 400)
        start.assert_not_called()

    def test_update_failure_to_start_is_conflict(self):
        error = system.UpdateInstallError("当前部署不支持持久化在线更新")
        with patch.object(system, "require_admin"), patch.object(system.update_service, "start", side_effect=error):
            response = self.client.post("/api/system/update", json={"confirm": True})
        self.assertEqual(response.status_code, 409)

    def test_update_status_forwards_force(self):
        view = system.UpdateStatusView(
            current_tag="v1.0.3",
            update_available=False,
            release_url="https://github.com/AuuCoder/gptGrok2api/releases",
            status_label="已是最新",
            status_message="ok",
            tone="muted",
        )
        with patch.object(system, "require_admin"),                 patch.object(system.update_status_service, "view", return_value=view) as mocked:
            response = self.client.get("/api/system/update-status?force=true")
        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with("1.0.3", force=True)

    def test_retention_cleanup_accepts_legacy_days_and_hours(self):
        with patch.object(system, "require_admin"),                 patch.object(system.retention_cleanup_coordinator, "run_retention", return_value={"ok": 1}) as run:
            self.client.post("/api/settings/retention-cleanup/preview", json={"log_retention_days": 2, "image_retention_hours": 5})
        run.assert_called_once_with(log_retention_hours=48, image_retention_hours=5, dry_run=True)


class HealthRouteTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        self.client = TestClient(app)

    def test_unauthenticated_health_only_exposes_status_and_version(self):
        stats = AsyncMock(side_effect=AssertionError("must not collect stats"))
        with patch.object(system, "require_admin", side_effect=system.HTTPException(status_code=401)),              patch.object(system, "get_provider_account_stats", new=stats):
            for url in ("/health", "/health?format=json"):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok", "version": "1.0.3"})

    def test_admin_health_returns_full_diagnostics(self):
        stats = {"active": 1, "unlimited_quota_count": 0}
        storage = type("S", (), {
            "health_check": staticmethod(lambda: {"ok": True}),
            "get_backend_info": staticmethod(lambda: {"type": "json"}),
        })()
        with patch.object(system, "require_admin"),              patch.object(system, "get_provider_account_stats", new=AsyncMock(return_value=stats)),              patch.object(system.config, "get_storage_backend", return_value=storage),              patch.object(system.proxy_settings, "get_runtime_status", return_value={"mode": "direct"}):
            response = self.client.get("/health?format=json", headers={"Authorization": "Bearer k"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["accounts"], stats)
        self.assertIn("storage", body)
        self.assertIn("proxy_runtime", body)


if __name__ == "__main__":
    unittest.main()


class ModelCatalogAccessTest(unittest.TestCase):
    def test_user_key_can_read_model_catalog(self):
        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        client = TestClient(app)
        with patch.object(system, "require_identity", return_value={"role": "user"}), \
                patch.object(system, "get_model_catalog", return_value={"object": "model_catalog"}):
            response = client.get("/api/model-catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "model_catalog")


class LegacyDashboardRouteTest(unittest.TestCase):
    def test_uses_metrics_projection_without_scanning_call_logs(self):
        from unittest.mock import MagicMock

        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        client = TestClient(app)
        storage = MagicMock()
        storage.get_backend_info.return_value = {"type": "json"}
        storage.health_check.return_value = {"ok": True}
        summary = {"total": 3, "success": 2, "failed": 1, "recent_failures": [], "trend": {}}
        failure = {"id": "x", "time": "2026-01-01 00:00:00", "summary": "s", "detail": {"status": "failed", "error": "boom"}}
        with patch.object(system, "require_admin"), \
                patch.object(system, "get_provider_account_stats", new=AsyncMock(return_value={"active": 1})), \
                patch.object(system.config, "get_storage_backend", return_value=storage), \
                patch.object(system, "storage_stats", return_value={}), \
                patch.object(system.dashboard_metrics_service, "summary", return_value=summary), \
                patch.object(system.dashboard_metrics_service, "backfill_if_empty") as backfill, \
                patch.object(system.log_service, "list") as scan, \
                patch.object(system.log_service, "list_page", return_value={"items": [failure]}) as page:
            response = client.get("/api/dashboard?log_limit=20000")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["logs"]["total"], 3)
        self.assertEqual(len(body["logs"]["recent_failures"]), 1)
        scan.assert_not_called()
        backfill.assert_not_called()
        self.assertEqual(page.call_args.kwargs["limit"], 10)


class LogsLimitTest(unittest.TestCase):
    def test_logs_limit_is_capped_at_500(self):
        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        client = TestClient(app)
        with patch.object(system, "require_admin"):
            self.assertEqual(client.get("/api/logs?limit=501").status_code, 422)
