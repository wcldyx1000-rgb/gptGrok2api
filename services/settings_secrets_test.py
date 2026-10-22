from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.config import (
    ConfigStore,
    config as global_config,
    mask_proxy_credentials,
    restore_masked_proxy_credentials,
)

MARK = "LEAKMARK"


def _proxy(index: int) -> str:
    return f"socks5h://user{index}:{MARK}pw{index}@10.0.0.{index}:1080"


class _TempConfigMixin:
    def setUp(self) -> None:  # noqa: N802
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps({"auth-key": "test-auth-key"}), encoding="utf-8")
        self.store = ConfigStore(path)

    def tearDown(self) -> None:  # noqa: N802
        self._tmp.cleanup()


class SettingsSecretMaskTest(_TempConfigMixin, unittest.TestCase):
    def _seed(self) -> None:
        self.store.update({
            "backup": {
                "enabled": True,
                "account_id": "acc",
                "access_key_id": "AKIDPUBLIC",
                "secret_access_key": f"{MARK}_r2_secret",
                "bucket": "bucket",
                "encrypt": True,
                "passphrase": f"{MARK}_passphrase",
            },
            "image_storage": {
                "enabled": True,
                "mode": "webdav",
                "webdav_url": "https://dav.example.com",
                "webdav_username": "dav-user",
                "webdav_password": f"{MARK}_webdav",
            },
            "ai_review": {"enabled": True, "base_url": "https://x", "api_key": f"{MARK}_ai", "model": "m"},
        })

    def test_get_masks_secrets_and_reports_has_flags(self) -> None:
        self._seed()
        public = self.store.get()
        self.assertEqual(public["backup"]["secret_access_key"], "")
        self.assertEqual(public["backup"]["passphrase"], "")
        self.assertTrue(public["backup"]["has_secret_access_key"])
        self.assertTrue(public["backup"]["has_passphrase"])
        self.assertEqual(public["backup"]["access_key_id"], "AKIDPUBLIC")
        self.assertEqual(public["image_storage"]["webdav_password"], "")
        self.assertTrue(public["image_storage"]["has_webdav_password"])
        self.assertEqual(public["image_storage"]["webdav_username"], "dav-user")
        self.assertEqual(public["ai_review"]["api_key"], "")
        self.assertTrue(public["ai_review"]["has_api_key"])
        self.assertNotIn(MARK, json.dumps(public))

    def test_has_flags_false_when_not_configured(self) -> None:
        public = self.store.get()
        self.assertFalse(public["backup"]["has_secret_access_key"])
        self.assertFalse(public["backup"]["has_passphrase"])
        self.assertFalse(public["image_storage"]["has_webdav_password"])

    def test_internal_getters_still_return_plaintext(self) -> None:
        self._seed()
        self.assertEqual(self.store.get_backup_settings()["secret_access_key"], f"{MARK}_r2_secret")
        self.assertEqual(self.store.get_backup_settings()["passphrase"], f"{MARK}_passphrase")
        self.assertEqual(self.store.get_image_storage_settings()["webdav_password"], f"{MARK}_webdav")
        self.assertEqual(self.store.get_internal()["backup"]["secret_access_key"], f"{MARK}_r2_secret")
        self.assertEqual(self.store.ai_review["api_key"], f"{MARK}_ai")
        # 对外的专用 public getter 也不含明文
        self.assertEqual(self.store.get_public_backup_settings()["secret_access_key"], "")
        self.assertTrue(self.store.get_public_backup_settings()["has_secret_access_key"])
        self.assertEqual(self.store.get_public_image_storage_settings()["webdav_password"], "")

    def test_save_with_blank_secret_keeps_stored_value(self) -> None:
        self._seed()
        public = self.store.get()
        # 前端把脱敏后的配置原样回传（密钥为空串 + has_* 标志）
        result = self.store.update({"backup": dict(public["backup"], bucket="new-bucket"),
                                    "image_storage": dict(public["image_storage"]),
                                    "ai_review": dict(public["ai_review"], model="m2")})
        self.assertEqual(result["backup"]["bucket"], "new-bucket")
        self.assertEqual(self.store.get_backup_settings()["secret_access_key"], f"{MARK}_r2_secret")
        self.assertEqual(self.store.get_backup_settings()["passphrase"], f"{MARK}_passphrase")
        self.assertEqual(self.store.get_image_storage_settings()["webdav_password"], f"{MARK}_webdav")
        self.assertEqual(self.store.ai_review["api_key"], f"{MARK}_ai")
        self.assertEqual(self.store.ai_review["model"], "m2")
        self.assertNotIn("has_api_key", self.store.ai_review)

    def test_save_without_section_keeps_stored_secrets(self) -> None:
        self._seed()
        self.store.update({"base_url": "https://example.com"})
        self.assertEqual(self.store.get_backup_settings()["secret_access_key"], f"{MARK}_r2_secret")
        self.assertEqual(self.store.get_image_storage_settings()["webdav_password"], f"{MARK}_webdav")

    def test_save_with_new_value_replaces(self) -> None:
        self._seed()
        self.store.update({"backup": {"secret_access_key": "new-secret", "passphrase": "new-pass"},
                           "image_storage": {"enabled": True, "webdav_url": "https://dav.example.com",
                                             "webdav_password": "new-dav"}})
        self.assertEqual(self.store.get_backup_settings()["secret_access_key"], "new-secret")
        self.assertEqual(self.store.get_backup_settings()["passphrase"], "new-pass")
        self.assertEqual(self.store.get_image_storage_settings()["webdav_password"], "new-dav")

    def test_explicit_clear_removes_only_flagged_secret(self) -> None:
        self._seed()
        self.store.update({"backup": {"secret_access_key": "", "passphrase": "", "clear_passphrase": True}})
        self.assertEqual(self.store.get_backup_settings()["passphrase"], "")
        self.assertEqual(self.store.get_backup_settings()["secret_access_key"], f"{MARK}_r2_secret")
        self.assertFalse(self.store.get()["backup"]["has_passphrase"])
        self.assertTrue(self.store.get()["backup"]["has_secret_access_key"])
        self.store.update({"ai_review": {"api_key": "", "clear_api_key": True}})
        self.assertEqual(self.store.ai_review["api_key"], "")

    def test_clear_flags_and_has_flags_are_not_persisted(self) -> None:
        self._seed()
        self.store.update({"backup": {"has_passphrase": True, "clear_secret_access_key": True}})
        stored = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn("clear_secret_access_key", stored["backup"])
        self.assertNotIn("has_passphrase", stored["backup"])

    def test_webdav_enabled_validation_uses_stored_password(self) -> None:
        self._seed()
        # 密码留空沿用已存值，因此启用 WebDAV 的校验不会误报
        self.store.update({"image_storage": {"enabled": True, "webdav_url": "https://dav.example.com",
                                             "webdav_password": ""}})
        clearing = {"enabled": True, "webdav_url": "https://dav.example.com",
                    "webdav_password": "", "clear_webdav_password": True}
        with self.assertRaises(ValueError):
            self.store.update({"image_storage": clearing})

    def test_top_level_legacy_secrets_never_returned(self) -> None:
        self.store.data["webdav_password"] = f"{MARK}_top"
        self.store.data["basic"] = {"api_key": f"{MARK}_basic", "base_url": "https://x"}
        public = self.store.get()
        self.assertNotIn("webdav_password", public)
        self.assertNotIn("api_key", public["basic"])
        self.assertNotIn(MARK, json.dumps(public))

    def test_backup_service_update_settings_returns_masked(self) -> None:
        from services import backup_service as backup_module

        self._seed()
        with patch.object(backup_module, "config", self.store):
            service = backup_module.BackupService()
            public = service.get_settings()
            self.assertEqual(public["secret_access_key"], "")
            self.assertTrue(public["has_secret_access_key"])
            updated = service.update_settings({"bucket": "b2", "secret_access_key": "********", "passphrase": ""})
            self.assertEqual(updated["secret_access_key"], "")
            self.assertEqual(self.store.get_backup_settings()["secret_access_key"], f"{MARK}_r2_secret")
            self.assertEqual(self.store.get_backup_settings()["bucket"], "b2")


class ProxyCredentialMaskTest(_TempConfigMixin, unittest.TestCase):
    def test_mask_helper(self) -> None:
        self.assertEqual(mask_proxy_credentials("socks5h://u:p%40ss@h:1080"), "socks5h://u:***@h:1080")
        self.assertEqual(mask_proxy_credentials("http://h:8080"), "http://h:8080")
        self.assertEqual(
            mask_proxy_credentials("http://a:b@h1:1\nhttp://c:d@h2:2"),
            "http://a:***@h1:1\nhttp://c:***@h2:2",
        )
        self.assertEqual(mask_proxy_credentials("http://a:***@h1:1"), "http://a:***@h1:1")

    def test_get_masks_proxy_urls_everywhere(self) -> None:
        self.store.update({
            "proxy": _proxy(1),
            "fallback_proxy": _proxy(2),
            "proxy_runtime": {"proxy_url": _proxy(3), "resource_proxy_url": _proxy(4)},
            "proxy_profiles": [{"id": "p", "proxy": _proxy(5)}],
            "proxy_groups": [{"id": "g", "nodes": [{"id": "n", "url": _proxy(6)}]}],
            "account_groups": [{"id": "a", "proxy": _proxy(7)}],
        })
        public = self.store.get()
        self.assertNotIn(MARK, json.dumps(public))
        self.assertEqual(public["proxy"], "socks5h://user1:***@10.0.0.1:1080")
        self.assertEqual(public["basic"]["proxy"], "socks5h://user1:***@10.0.0.1:1080")
        # 内部使用仍是明文
        self.assertEqual(self.store.get_proxy_settings(), _proxy(1))
        self.assertEqual(self.store.get_internal()["proxy_groups"][0]["nodes"][0]["url"], _proxy(6))
        self.assertNotIn(MARK, json.dumps(self.store.get_public_proxy_runtime_settings()))

    def test_save_restores_masked_proxy_credentials(self) -> None:
        self.store.update({"proxy": _proxy(1), "proxy_groups": [{"id": "g", "nodes": [{"id": "n", "url": _proxy(6)}]}]})
        public = self.store.get()
        self.store.update({"proxy": public["proxy"], "proxy_groups": public["proxy_groups"], "base_url": "https://b"})
        self.assertEqual(self.store.get_proxy_settings(), _proxy(1))
        self.assertEqual(self.store.get_internal()["proxy_groups"][0]["nodes"][0]["url"], _proxy(6))

    def test_user_entered_new_credentials_are_kept(self) -> None:
        self.store.update({"proxy": _proxy(1)})
        self.store.update({"proxy": "http://newuser:newpass@1.2.3.4:80"})
        self.assertEqual(self.store.get_proxy_settings(), "http://newuser:newpass@1.2.3.4:80")

    def test_restore_helper_leaves_unknown_mask(self) -> None:
        masked = "http://x:***@unknown:1"
        self.assertEqual(restore_masked_proxy_credentials(masked, {"proxy": _proxy(1)}), masked)
        self.assertEqual(
            restore_masked_proxy_credentials(mask_proxy_credentials(_proxy(1)), {"proxy": _proxy(1)}),
            _proxy(1),
        )


class SettingsLeakScanTest(unittest.TestCase):
    """灌入带固定标记的敏感值，断言对外序列化结果不含这些标记。"""

    def test_config_get_has_no_markers(self) -> None:
        planted = {
            "webdav_password": f"{MARK}_top_webdav",
            "backup": {"secret_access_key": f"{MARK}_r2", "passphrase": f"{MARK}_pass", "access_key_id": "AKID"},
            "image_storage": {"webdav_password": f"{MARK}_img", "webdav_url": "http://dav", "webdav_username": "u"},
            "ai_review": {"api_key": f"{MARK}_ai", "base_url": "http://x"},
            "genbox_push": {"push_key": f"{MARK}_gb"},
            "basic": {"api_key": f"{MARK}_basic"},
            "proxy": _proxy(1),
            "fallback_proxy": _proxy(2),
            "proxy_runtime": {
                "proxy_url": _proxy(3),
                "resource_proxy_url": _proxy(4),
                "clearance": {
                    "cf_cookies": f"{MARK}_cfc",
                    "cf_clearance": f"{MARK}_cfcl",
                    "flaresolverr_url": f"http://u:{MARK}fs@h:8191",
                },
            },
            "proxy_profiles": [{"id": "p", "proxy": _proxy(5)}],
            "proxy_groups": [{"id": "g", "nodes": [{"id": "n", "url": _proxy(6)}]}],
            "account_groups": [{"id": "a", "proxy": _proxy(7)}],
        }
        with patch.dict(global_config.data, planted):
            serialized = json.dumps(global_config.get(), ensure_ascii=False)
            self.assertNotIn(MARK, serialized)
            # 内部取值不受影响
            self.assertEqual(global_config.get_backup_settings()["secret_access_key"], f"{MARK}_r2")

    def test_settings_endpoint_has_no_markers(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api import system

        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        client = TestClient(app)
        planted = {
            "webdav_password": f"{MARK}_top_webdav",
            "backup": {"secret_access_key": f"{MARK}_r2", "passphrase": f"{MARK}_pass"},
            "image_storage": {"webdav_password": f"{MARK}_img"},
            "ai_review": {"api_key": f"{MARK}_ai"},
            "proxy": _proxy(1),
            "proxy_profiles": [{"id": "p", "proxy": _proxy(5)}],
            "proxy_groups": [{"id": "g", "nodes": [{"id": "n", "url": _proxy(6)}]}],
        }
        with patch.object(system, "require_admin"), patch.dict(global_config.data, planted):
            for path in ("/api/settings", "/api/proxy/profiles", "/api/proxy/groups", "/api/proxy/runtime"):
                self.assertNotIn(MARK, client.get(path).text, path)

    def test_register_public_snapshot_has_no_markers(self) -> None:
        from services.register_service import register_service

        providers = []
        for kind in ("cloudflare_temp_email", "tempmail_lol", "moemail", "duckmail", "gptmail", "icloud_api",
                     "donemail", "yyds_mail", "ddg_mail", "cloudmail_gen", "custom_domain"):
            provider = {"type": kind, "id": f"{kind}-1"}
            for key in ("api_key", "admin_key", "admin_password", "cf_api_key", "cf_api_token", "ddg_token",
                        "cf_inbox_jwt", "imap_password"):
                provider[key] = f"{MARK}_{kind}_{key}"
            providers.append(provider)
        planted = {
            "mail": {"providers": providers},
            "proxy": _proxy(1),
            "checkout": {"checkout_proxy_enabled": True, "checkout_proxy_url": _proxy(2)},
            "grok": {"api_key": f"{MARK}_captcha", "custom_headers": {"Authorization": f"{MARK}_hdr"}},
        }
        with patch.dict(register_service._config, planted):
            serialized = json.dumps(register_service.get_public(), ensure_ascii=False)
            self.assertNotIn(MARK, serialized)
            internal = register_service.get()
            self.assertEqual(internal["proxy"], _proxy(1))
            self.assertEqual(internal["grok"]["api_key"], f"{MARK}_captcha")
            public = register_service.get_public()
            self.assertTrue(public["grok"]["api_key_set"])
            first = public["mail"]["providers"][0]
            self.assertTrue(first["api_key_set"])
            self.assertEqual(first["api_key"], "")


class RegisterSecretMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from services.register_service import RegisterService

        self.service = RegisterService(Path(self._tmp.name) / "register.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _provider(self) -> dict:
        return self.service._config["mail"]["providers"][0]

    def test_blank_provider_secret_keeps_stored_and_clear_flag_removes(self) -> None:
        self.service.update({"mail": {"providers": [
            {"type": "moemail", "id": "m1", "api_base": "https://m", "api_key": f"{MARK}_moe", "domain": ["a.com"]},
        ]}})
        self.assertEqual(self._provider()["api_key"], f"{MARK}_moe")

        result = self.service.update({"mail": {"providers": [
            {"type": "moemail", "id": "m1", "api_base": "https://m2", "api_key": "", "api_key_set": True, "domain": ["a.com"]},
        ]}})
        self.assertEqual(self._provider()["api_key"], f"{MARK}_moe")
        self.assertEqual(self._provider()["api_base"], "https://m2")
        self.assertNotIn("api_key_set", self._provider())
        self.assertNotIn(MARK, json.dumps(result))

        self.service.update({"mail": {"providers": [
            {"type": "moemail", "id": "m1", "api_base": "https://m2", "api_key": "", "clear_api_key": True, "domain": ["a.com"]},
        ]}})
        self.assertEqual(self._provider()["api_key"], "")
        self.assertNotIn("clear_api_key", self._provider())

    def test_grok_api_key_blank_keeps_and_proxy_mask_restored(self) -> None:
        self.service.update({"proxy": _proxy(1), "grok": {"api_key": f"{MARK}_captcha"}})
        public = self.service.get_public()
        self.assertEqual(public["grok"]["api_key"], "")
        self.service.update({"proxy": public["proxy"], "grok": dict(public["grok"], provider="2captcha")})
        self.assertEqual(self.service._config["grok"]["api_key"], f"{MARK}_captcha")
        self.assertEqual(self.service._config["proxy"], _proxy(1))


if __name__ == "__main__":
    unittest.main()
