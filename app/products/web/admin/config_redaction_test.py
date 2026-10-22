from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform.auth.middleware import verify_admin_key
from app.products.web import admin as admin_mod

MARKERS = {
    "app_key": "LEAK-APP-KEY-111",
    "api_key": "LEAK-API-KEY-222",
    "webui_key": "LEAK-WEBUI-333",
    "cf_cookies": "LEAK-CF-COOKIES-444",
    "cf_clearance": "LEAK-CF-CLEAR-555",
    "proxy_pw": "LEAK-PROXY-PW-666",
}


def _raw() -> dict:
    return {
        "app": {
            "app_key": MARKERS["app_key"],
            "api_key": MARKERS["api_key"],
            "webui_key": MARKERS["webui_key"],
            "app_url": "https://example.test",
        },
        "proxy": {
            "cf_cookies": MARKERS["cf_cookies"],
            "egress": {
                "proxy_url": f"http://user:{MARKERS['proxy_pw']}@proxy.test:8080",
                "resource_proxy_url": "http://plain.test:8080",
                "proxy_pool": [f"http://u:{MARKERS['proxy_pw']}@p1.test:1"],
            },
            "clearance": {
                "cf_cookies": MARKERS["cf_cookies"],
                "cf_clearance": MARKERS["cf_clearance"],
                "user_agent": "UA",
            },
        },
    }


class ConfigRedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(admin_mod.router)
        app.dependency_overrides[verify_admin_key] = lambda: None
        self.client = TestClient(app)

    def test_get_config_leaks_no_marker(self) -> None:
        with patch.object(admin_mod.config, "raw", return_value=_raw()):
            response = self.client.get("/admin/api/config")
        self.assertEqual(response.status_code, 200)
        text = response.text
        for name, marker in MARKERS.items():
            self.assertNotIn(marker, text, name)
        data = response.json()
        self.assertEqual(data["app"]["app_key"], "")
        self.assertTrue(data["app"]["has_app_key"])
        self.assertTrue(data["proxy"]["clearance"]["has_cf_cookies"])
        self.assertEqual(data["proxy"]["egress"]["proxy_pool"], [])
        # Non-secret values stay visible.
        self.assertEqual(data["app"]["app_url"], "https://example.test")
        self.assertEqual(data["proxy"]["egress"]["resource_proxy_url"], "http://plain.test:8080")

    def test_blank_secret_keeps_stored_value(self) -> None:
        patch_body = {
            "app": {"app_key": "", "has_app_key": True, "app_url": "https://new.test"},
            "proxy": {"egress": {"proxy_url": "", "proxy_pool": []}},
        }
        merged = admin_mod.merge_config_patch_secrets(patch_body, _raw())
        self.assertNotIn("app_key", merged["app"])
        self.assertNotIn("has_app_key", merged["app"])
        self.assertEqual(merged["app"]["app_url"], "https://new.test")
        self.assertNotIn("proxy_url", merged["proxy"]["egress"])
        self.assertNotIn("proxy_pool", merged["proxy"]["egress"])

    def test_new_value_and_clear_flag(self) -> None:
        merged = admin_mod.merge_config_patch_secrets(
            {"app": {"app_key": "new-key", "clear_api_key": True}}, _raw()
        )
        self.assertEqual(merged["app"]["app_key"], "new-key")
        self.assertEqual(merged["app"]["api_key"], "")
        self.assertNotIn("clear_api_key", merged["app"])

    def test_plain_url_can_be_cleared_with_blank(self) -> None:
        merged = admin_mod.merge_config_patch_secrets(
            {"proxy": {"egress": {"resource_proxy_url": ""}}}, _raw()
        )
        self.assertEqual(merged["proxy"]["egress"]["resource_proxy_url"], "")

    def test_post_config_does_not_persist_blank_secret(self) -> None:
        update = AsyncMock()
        with patch.object(admin_mod.config, "raw", return_value=_raw()), \
             patch.object(admin_mod.config, "update", update), \
             patch.object(admin_mod.config, "load", AsyncMock()), \
             patch.object(admin_mod, "reload_file_logging"), \
             patch("app.control.account.runtime.reconcile_refresh_runtime", return_value="x"):
            response = self.client.post(
                "/admin/api/config",
                content=json.dumps({"app": {"app_key": "", "has_app_key": True}}),
                headers={"content-type": "application/json"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        persisted = update.await_args.args[0]
        self.assertNotIn("app_key", persisted["app"])
        self.assertNotIn("has_app_key", persisted["app"])


if __name__ == "__main__":
    unittest.main()
