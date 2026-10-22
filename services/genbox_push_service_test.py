# Ported from yukkcat/chatgpt2api (AGPL-3.0-only) test intent; all network access is mocked.
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from PIL import Image

from services import genbox_push_service as svc
from services.config import config
from services.image_storage_service import ImageStorageService

SETTINGS = {
    "enabled": True,
    "base_url": "https://genbox.example",
    "source_id": "src-1",
    "push_key": "secret-key",
    "timeout_secs": 20,
    "auto_push_after_studio": True,
}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def _resp(status: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = json.dumps(body).encode()
    return r


def _settings(**override):
    return lambda self_: {**SETTINGS, **override}


class GenBoxPushTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = _png()
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.storage = MagicMock()
        self.storage.get_bytes.return_value = self.payload
        self.storage.record_genbox_push.side_effect = lambda rel, status, sha256, updated_at: {
            "status": status, "sha256": sha256, "updated_at": updated_at,
        }
        p1 = patch.object(svc, "image_storage_service", self.storage)
        p2 = patch.object(type(config), "get_genbox_push_settings", _settings())
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def _session(self, probe, push):
        session = MagicMock()
        session.get.return_value = probe
        session.post.return_value = push
        return session

    def test_push_success_sends_key_header_and_returns_state(self) -> None:
        probe = _resp(200, {"ok": True, "contract_version": "v1", "source_id": "src-1", "max_image_bytes": 10**7})
        push = _resp(200, {"ok": True, "contract_version": "v1", "source_id": "src-1", "sha256": self.sha, "status": "imported"})
        session = self._session(probe, push)
        with patch.object(svc.requests, "Session", return_value=session):
            result = svc.push_gallery_image("2026/01/01/a.png", metadata={"prompt": "cat", "model": "m"})
        self.assertEqual(result["status"], "imported")
        self.assertTrue(result["source_retained"])
        self.assertEqual(session.get.call_args.kwargs["headers"]["X-GenBox-Key"], "secret-key")
        self.assertEqual(session.post.call_args.kwargs["data"]["prompt"], "cat")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("secret-key", json.dumps(result))

    def test_not_configured_when_disabled_or_missing_key(self) -> None:
        for override in ({"enabled": False}, {"push_key": ""}):
            with patch.object(type(config), "get_genbox_push_settings", _settings(**override)):
                with self.assertRaises(svc.GenBoxPushError) as ctx:
                    svc.push_gallery_image("a.png")
            self.assertEqual(ctx.exception.code, "genbox_not_configured")
            self.assertEqual(ctx.exception.status_code, 409)

    def test_probe_rejected_and_bad_receipt_cases(self) -> None:
        good_probe = _resp(200, {"ok": True, "contract_version": "v1", "source_id": "src-1", "max_image_bytes": 10**7})
        bad_source = _resp(200, {"ok": True, "contract_version": "v1", "source_id": "other", "max_image_bytes": 10**7})
        bad_receipt = _resp(200, {"ok": True, "contract_version": "v1", "source_id": "src-1", "sha256": "0" * 64, "status": "imported"})
        cases = [
            (_resp(500, {}), None, "genbox_unavailable"),
            (bad_source, None, "genbox_probe_rejected"),
            (good_probe, _resp(403, {}), "genbox_rejected"),
            (good_probe, bad_receipt, "genbox_invalid_receipt"),
        ]
        for probe, push, code in cases:
            with patch.object(svc.requests, "Session", return_value=self._session(probe, push)):
                with self.assertRaises(svc.GenBoxPushError) as ctx:
                    svc.push_gallery_image("a.png")
            self.assertEqual(ctx.exception.code, code)

    def test_network_exception_maps_to_request_failed_without_leaking_key(self) -> None:
        session = MagicMock()
        session.get.side_effect = OSError("boom secret-key")
        with patch.object(svc.requests, "Session", return_value=session):
            with self.assertRaises(svc.GenBoxPushError) as ctx:
                svc.push_gallery_image("a.png")
        self.assertEqual(ctx.exception.code, "genbox_request_failed")
        self.assertNotIn("secret-key", ctx.exception.message)

    def test_invalid_image_is_404(self) -> None:
        self.storage.get_bytes.return_value = b"not an image"
        with self.assertRaises(svc.GenBoxPushError) as ctx:
            svc.push_gallery_image("a.png")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_auto_push_skipped_when_disabled_and_dispatches_when_enabled(self) -> None:
        self.storage.get_genbox_push_state.return_value = None
        base = "https://host.example"
        with patch.object(type(config), "get_genbox_push_settings", _settings(auto_push_after_studio=False)), \
                patch.object(svc, "_submit_auto_push") as submit:
            svc.auto_push_gallery_urls([f"{base}/images/2026/a.png"], base_url=base)
        submit.assert_not_called()
        urls = [f"{base}/images/2026/a.png", f"{base}/images/2026/a.png", "https://evil.example/images/x.png"]
        with patch.object(svc, "_submit_auto_push", return_value=True) as submit, \
                patch.object(type(config), "get_image_storage_settings", lambda self_: {}):
            svc.auto_push_gallery_urls(urls, base_url=base)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.args[0], "2026/a.png")

    def test_auto_push_skips_already_pushed(self) -> None:
        self.storage.get_genbox_push_state.return_value = {"status": "imported", "sha256": self.sha, "updated_at": "t"}
        base = "https://host.example"
        with patch.object(svc, "_submit_auto_push") as submit, \
                patch.object(type(config), "get_image_storage_settings", lambda self_: {}):
            svc.auto_push_gallery_urls([f"{base}/images/a.png"], base_url=base)
        submit.assert_not_called()


class GenBoxStateStorageTest(unittest.TestCase):
    def test_record_and_get_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ImageStorageService(Path(tmp) / "idx.json")
            storage._save_index({"a.png": {"rel": "a.png", "local": True}})
            state = storage.record_genbox_push("a.png", status="imported", sha256="a" * 64, updated_at="t")
            self.assertEqual(state["status"], "imported")
            self.assertEqual(storage.get_genbox_push_state("a.png")["sha256"], "a" * 64)
            self.assertIsNone(storage.get_genbox_push_state("missing.png"))
            with self.assertRaises(HTTPException):
                storage.record_genbox_push("missing.png", status="imported", sha256="a" * 64, updated_at="t")


class GenBoxConfigTest(unittest.TestCase):
    def test_public_settings_mask_push_key(self) -> None:
        with patch.dict(config.data, {"genbox_push": SETTINGS}):
            public = config.get_public_genbox_push_settings()
            self.assertEqual(public["push_key"], "")
            self.assertTrue(public["has_push_key"])
            self.assertNotIn("secret-key", json.dumps(config.get()["genbox_push"]))

    def test_default_off(self) -> None:
        with patch.dict(config.data, {}, clear=False):
            config.data.pop("genbox_push", None)
            s = config.get_genbox_push_settings()
        self.assertFalse(s["enabled"])
        self.assertFalse(s["auto_push_after_studio"])

    def test_update_keeps_existing_key_when_blank_and_clears_on_flag(self) -> None:
        with patch.dict(config.data, {"genbox_push": dict(SETTINGS)}), \
                patch.object(type(config), "_save", lambda self_: None):
            config.update({"genbox_push": {**SETTINGS, "push_key": "", "timeout_secs": 30}})
            self.assertEqual(config.get_genbox_push_settings()["push_key"], "secret-key")
            self.assertEqual(config.get_genbox_push_settings()["timeout_secs"], 30)
            config.update({"genbox_push": {**SETTINGS, "enabled": False, "push_key": "", "clear_push_key": True}})
            self.assertEqual(config.get_genbox_push_settings()["push_key"], "")
            with self.assertRaises(ValueError):
                config.update({"genbox_push": {**SETTINGS, "push_key": "", "enabled": True}})
            with self.assertRaises(ValueError):
                config.update({"genbox_push": {**SETTINGS, "source_id": "bad id!"}})
            with self.assertRaises(ValueError):
                config.update({"image_upscale_engine": "bogus"})



class GenBoxApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api import system

        self.system = system
        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        self.client = TestClient(app)

    def test_requires_admin(self) -> None:
        with patch.object(self.system, "require_admin", side_effect=HTTPException(status_code=401)):
            self.assertEqual(self.client.post("/api/images/genbox-push", json={"path": "a.png"}).status_code, 401)

    def test_maps_push_error_and_success(self) -> None:
        err = svc.GenBoxPushError(409, "genbox_not_configured", "not configured")
        with patch.object(self.system, "require_admin"), patch.object(self.system, "push_gallery_image", side_effect=err):
            response = self.client.post("/api/images/genbox-push", json={"path": "a.png"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "genbox_not_configured")
        ok = {"path": "a.png", "status": "imported"}
        with patch.object(self.system, "require_admin"), patch.object(self.system, "push_gallery_image", return_value=ok):
            self.assertEqual(self.client.post("/api/images/genbox-push", json={"path": "a.png"}).json(), ok)

    def test_rejects_extra_fields_and_empty_path(self) -> None:
        with patch.object(self.system, "require_admin"):
            self.assertEqual(self.client.post("/api/images/genbox-push", json={"path": ""}).status_code, 422)
            self.assertEqual(self.client.post("/api/images/genbox-push", json={"path": "a.png", "x": 1}).status_code, 422)

    def test_settings_endpoint_masks_push_key(self) -> None:
        with patch.object(self.system, "require_admin"), patch.dict(config.data, {"genbox_push": SETTINGS}):
            body = self.client.get("/api/settings").text
        self.assertNotIn("secret-key", body)


if __name__ == "__main__":
    unittest.main()
