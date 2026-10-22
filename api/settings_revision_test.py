from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system
from services.config import ConfigStore, SettingsRevisionConflictError


class SettingsRevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = patch.dict("os.environ", {"CHATGPT2API_AUTH_KEY": "test-admin-key"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.store = ConfigStore(Path(self._tmp.name) / "config.json")
        patcher = patch.object(system, "config", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        auth = patch.object(system, "require_admin", return_value={"role": "admin"})
        auth.start()
        self.addCleanup(auth.stop)
        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        self.client = TestClient(app)

    def test_get_returns_revision_and_hides_internal_field(self) -> None:
        body = self.client.get("/api/settings").json()
        self.assertEqual(body["revision"], 0)
        self.assertNotIn("_revision", body["config"])
        self.assertEqual(body["read_only"], {})

    def test_save_increments_revision(self) -> None:
        first = self.client.post("/api/settings", json={"global_system_prompt": "a"}).json()
        second = self.client.post("/api/settings", json={"global_system_prompt": "b"}).json()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertNotIn("_revision", second["config"])
        self.assertEqual(self.client.get("/api/settings/revision").json(), {"revision": 2})

    def test_stale_revision_returns_409_with_current_revision(self) -> None:
        self.client.post("/api/settings", json={"global_system_prompt": "a"})
        response = self.client.post("/api/settings", json={"global_system_prompt": "x", "revision": 0})
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "settings_revision_conflict")
        self.assertEqual(detail["revision"], 1)
        self.assertEqual(self.store.get()["global_system_prompt"], "a")

    def test_matching_revision_saves(self) -> None:
        response = self.client.post("/api/settings", json={"global_system_prompt": "x", "revision": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 1)

    def test_legacy_client_without_revision_is_accepted(self) -> None:
        self.client.post("/api/settings", json={"global_system_prompt": "a"})
        response = self.client.post("/api/settings", json={"global_system_prompt": "b"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["global_system_prompt"], "b")

    def test_unknown_keys_are_reported_not_silently_dropped(self) -> None:
        response = self.client.post(
            "/api/settings", json={"global_system_prompt": "a", "typo_key": 1, "_revision": 99}
        ).json()
        self.assertEqual(response["ignored_keys"], ["_revision", "typo_key"])
        self.assertEqual(response["revision"], 1)

    def test_env_overridden_base_url_is_marked_read_only(self) -> None:
        with patch.dict("os.environ", {"CHATGPT2API_BASE_URL": "https://env.example"}):
            body = self.client.get("/api/settings").json()
            saved = self.client.post("/api/settings", json={"base_url": "https://file.example"}).json()
        self.assertEqual(body["read_only"]["base_url"]["source"], "environment")
        self.assertEqual(saved["changed_read_only_keys"], ["base_url"])

    def test_store_raises_conflict_error(self) -> None:
        self.store.update({"global_system_prompt": "a"})
        with self.assertRaises(SettingsRevisionConflictError) as ctx:
            self.store.update({"global_system_prompt": "b"}, expected_revision=0)
        self.assertEqual(ctx.exception.current_revision, 1)


if __name__ == "__main__":
    unittest.main()


class ProxyDefaultsRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict("os.environ", {"CHATGPT2API_AUTH_KEY": "test-admin-key"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.store = ConfigStore(Path(self._tmp.name) / "config.json")
        for target in (
            patch.object(system, "config", self.store),
            patch.object(system, "require_admin", return_value={"role": "admin"}),
        ):
            target.start()
            self.addCleanup(target.stop)
        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        self.client = TestClient(app)

    def test_view_defaults_to_no_direct_locale(self) -> None:
        body = self.client.get("/api/proxy/view").json()
        self.assertIsNone(body["direct_locale"])
        self.assertEqual(body["revision"], 0)

    def test_save_and_clear_direct_locale(self) -> None:
        saved = self.client.post(
            "/api/proxy/defaults",
            json={"direct_locale": {"timezone": "Asia/Tokyo", "language": "ja-jp"}},
        ).json()
        self.assertEqual(saved["direct_locale"]["timezone"], "Asia/Tokyo")
        self.assertEqual(saved["direct_locale"]["language"], "ja-JP")
        self.assertEqual(saved["direct_locale"]["timezone_offset_min"], -540)
        self.assertEqual(self.store.get_direct_locale(), {"timezone": "Asia/Tokyo", "language": "ja-JP"})

        cleared = self.client.post(
            "/api/proxy/defaults", json={"direct_locale": {"timezone": "", "language": ""}}
        ).json()
        self.assertIsNone(cleared["direct_locale"])
        self.assertEqual(self.store.get_direct_locale(), {})

    def test_omitted_direct_locale_is_untouched(self) -> None:
        self.client.post(
            "/api/proxy/defaults", json={"direct_locale": {"timezone": "Asia/Tokyo", "language": "ja-JP"}}
        )
        view = self.client.post("/api/proxy/defaults", json={"fallback_proxy": ""}).json()
        self.assertEqual(view["direct_locale"]["timezone"], "Asia/Tokyo")

    def test_half_filled_or_invalid_locale_is_rejected(self) -> None:
        for payload in (
            {"timezone": "Asia/Tokyo", "language": ""},
            {"timezone": "Mars/Base", "language": "en-US"},
        ):
            response = self.client.post("/api/proxy/defaults", json={"direct_locale": payload})
            self.assertEqual(response.status_code, 400, payload)
        self.assertEqual(self.store.get_direct_locale(), {})


class RevisionTrackingScopeTest(unittest.TestCase):
    def test_background_keys_do_not_advance_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"CHATGPT2API_AUTH_KEY": "k"}):
            store = ConfigStore(Path(tmp) / "config.json")
            store.update({"proxy_groups": [{"id": "g"}]})
            self.assertEqual(store.revision, 0)
            store.update({"global_system_prompt": "x"})
            self.assertEqual(store.revision, 1)
            # 内容没变的重复保存不推进 revision。
            store.update({"global_system_prompt": "x"})
            self.assertEqual(store.revision, 1)
