"""Outlook 邮箱池逐邮箱管理视图 / 启动释放瞬时 failed 的测试。

Ported from chatgpt-register (AGPL-3.0-only).
状态库一律放在临时目录，绝不触碰真实 data/。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import services.register_service as register_service_module
from services.register import mail_provider
from services.register_service import OutlookPoolConflict, OutlookPoolNotFound, RegisterService


def _pool_text(count: int = 3) -> str:
    return "\n".join(
        f"user{i}@outlook.com----pw-secret-{i}----client-id-{i}----refresh-token-secret-{i}" for i in range(count)
    )


class _TempStateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(
            mail_provider, "OUTLOOK_TOKEN_USED_FILE", Path(self._tmp.name) / "outlook_token_used.json"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        mail_provider._outlook_transient_state.clear()
        self.addCleanup(mail_provider._outlook_transient_state.clear)

    def _state(self, address: str) -> str:
        return str(mail_provider._load_outlook_token_state().get(address.lower(), {}).get("state") or "")


class ReleaseTransientFailedTest(_TempStateCase):
    def test_releases_only_transient_failed(self) -> None:
        set_state = mail_provider._set_outlook_token_state
        set_state("a@outlook.com", "failed", "curl: (28) Operation timed out")
        set_state("b@outlook.com", "failed", "连续 4 次瞬时失败：Cloudflare challenge")
        set_state("c@outlook.com", "failed", "create_account_http_503")
        set_state("d@outlook.com", "failed", "账号被锁 account has been locked")
        set_state("e@outlook.com", "failed", "some unknown deterministic failure")
        set_state("f@outlook.com", "failed", "")
        set_state("g@outlook.com", "token_invalid", "invalid_grant timeout")
        set_state("h@outlook.com", "login_required", "login flow broke")
        set_state("i@outlook.com", "used", "timeout")

        released = mail_provider.release_outlook_transient_failed()

        self.assertEqual(released, 3)
        for address in ("a", "b", "c"):
            self.assertEqual(self._state(f"{address}@outlook.com"), "")
        self.assertEqual(self._state("d@outlook.com"), "failed")
        self.assertEqual(self._state("e@outlook.com"), "failed")
        self.assertEqual(self._state("f@outlook.com"), "failed")
        self.assertEqual(self._state("g@outlook.com"), "token_invalid")
        self.assertEqual(self._state("h@outlook.com"), "login_required")
        self.assertEqual(self._state("i@outlook.com"), "used")

    def test_does_not_create_state_db_when_missing(self) -> None:
        self.assertEqual(mail_provider.release_outlook_transient_failed(), 0)
        self.assertFalse(mail_provider._outlook_token_state_db_file().exists())

    def test_service_start_hook_logs_count(self) -> None:
        mail_provider._set_outlook_token_state("a@outlook.com", "failed", "connection reset by peer")
        service = RegisterService(Path(self._tmp.name) / "register.json")
        self.assertEqual(service.release_transient_outlook_failures(), 1)
        self.assertTrue(any("自动释放 1 个" in str(item.get("text") or item) for item in service._logs))


class ResetMailboxStateTest(_TempStateCase):
    def test_reset_rules(self) -> None:
        set_state = mail_provider._set_outlook_token_state
        set_state("f@outlook.com", "failed", "x")
        set_state("u@outlook.com", "used")
        set_state("t@outlook.com", "token_invalid", "bad")
        self.assertEqual(mail_provider.reset_outlook_mailbox_state("f@outlook.com"), "reset")
        self.assertEqual(mail_provider.reset_outlook_mailbox_state("u@outlook.com"), "reset")
        self.assertEqual(mail_provider.reset_outlook_mailbox_state("t@outlook.com"), "reset")
        self.assertEqual(mail_provider.reset_outlook_mailbox_state("nobody@outlook.com"), "missing")
        self.assertEqual(self._state("f@outlook.com"), "")

    def test_active_lease_cannot_be_reset_but_stale_can(self) -> None:
        pool = [{"email": "a@outlook.com", "password": "p", "client_id": "c", "refresh_token": "r"}]
        credential, lease, _ = mail_provider._claim_outlook_credential_ex(pool)
        self.assertIsNotNone(credential)
        self.assertEqual(mail_provider.reset_outlook_mailbox_state("a@outlook.com"), "leased")
        self.assertEqual(self._state("a@outlook.com"), "in_use")
        self.assertEqual(
            mail_provider._load_outlook_token_state()["a@outlook.com"]["lease_token"], lease
        )
        with patch.object(mail_provider, "OUTLOOK_IN_USE_STALE_SECONDS", 0):
            self.assertEqual(mail_provider.reset_outlook_mailbox_state("a@outlook.com"), "reset")

    def test_use_count_increments_on_used(self) -> None:
        mail_provider._set_outlook_token_state("a@outlook.com", "used")
        mail_provider._set_outlook_token_state("a@outlook.com", "used")
        self.assertEqual(mail_provider._load_outlook_token_state()["a@outlook.com"]["use_count"], 2)


class PoolEntriesProjectionTest(_TempStateCase):
    def test_projection_never_exposes_secrets(self) -> None:
        creds = mail_provider.parse_outlook_credentials(_pool_text(2))
        mail_provider._set_outlook_token_state(
            "user0@outlook.com", "failed", "refresh failed for user0@outlook.com " + "A" * 60
        )
        rows = mail_provider.outlook_pool_entries(creds, {})
        blob = repr(rows)
        for secret in ("pw-secret", "client-id-", "refresh-token-secret", "lease_token"):
            self.assertNotIn(secret, blob)
        self.assertNotIn("user0@outlook.com", {row["email"] for row in rows})
        first = rows[0]
        self.assertTrue(first["has_password"] and first["has_client_id"] and first["has_refresh_token"])
        self.assertEqual(first["state"], "failed")
        self.assertNotIn("user0@outlook.com", first["reason"])
        self.assertNotIn("A" * 40, first["reason"])
        self.assertTrue(first["resettable"])

    def test_alias_rows_family_inherit_and_lease(self) -> None:
        creds = mail_provider.parse_outlook_credentials(_pool_text(1))
        provider = {"alias_enabled": True, "alias_per_email": 2, "alias_prefix": "t"}
        mail_provider._set_outlook_token_state("user0@outlook.com", "used")
        rows = mail_provider.outlook_pool_entries(creds, provider)
        self.assertEqual(len(rows), 3)
        base, alias1 = rows[0], rows[1]
        self.assertFalse(base["is_alias"])
        self.assertTrue(base["deletable"])
        self.assertTrue(alias1["is_alias"])
        self.assertFalse(alias1["deletable"])
        self.assertEqual(alias1["state"], "family_used")
        self.assertTrue(alias1["inherited"])
        self.assertFalse(alias1["resettable"])
        self.assertTrue(alias1["alias_of"])

        mail_provider.reset_outlook_mailbox_state("user0@outlook.com")
        pool = mail_provider.expand_outlook_aliases(creds, provider)
        credential, _lease, _ = mail_provider._claim_outlook_credential_ex(pool)
        rows = mail_provider.outlook_pool_entries(creds, provider)
        leased = [row for row in rows if row["state"] == "leased"]
        self.assertEqual(len(leased), 1)
        self.assertTrue(leased[0]["leased_at"])
        self.assertFalse(leased[0]["resettable"])


class ServicePoolManagementTest(_TempStateCase):
    def _service(self, count: int = 5, **provider_extra) -> RegisterService:
        service = RegisterService(Path(self._tmp.name) / "register.json")
        service._config["mail"]["providers"] = [
            {"id": "outlook-main", "type": "outlook_token", "enable": True, "mailboxes": _pool_text(count), **provider_extra}
        ]
        return service

    @staticmethod
    def _id(email: str) -> str:
        return register_service_module._outlook_mailbox_id("outlook-main", email)

    def test_pagination_search_and_state_filter(self) -> None:
        service = self._service(5)
        mail_provider._set_outlook_token_state("user1@outlook.com", "failed", "boom")
        mail_provider._set_outlook_token_state("user3@outlook.com", "used")
        page = service.outlook_pool_entries("outlook-main", page=2, page_size=2)
        self.assertEqual((page["total"], page["pages"], page["page"], len(page["items"])), (5, 3, 2, 2))
        self.assertEqual(page["state_counts"], {"unused": 3, "failed": 1, "used": 1})
        failed = service.outlook_pool_entries("outlook-main", state="failed")
        self.assertEqual([item["state"] for item in failed["items"]], ["failed"])
        found = service.outlook_pool_entries("outlook-main", query="USER3@")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["id"], self._id("user3@outlook.com"))
        self.assertNotIn("user3@outlook.com", repr(found))
        self.assertEqual(found["stats"]["used"], 1)
        with self.assertRaises(OutlookPoolNotFound):
            service.outlook_pool_entries("nope")

    def test_reset_via_service_rejects_leased(self) -> None:
        service = self._service(2)
        mail_provider._set_outlook_token_state("user0@outlook.com", "failed", "x")
        result = service.reset_outlook_mailbox("outlook-main", self._id("user0@outlook.com"))
        self.assertEqual(result["state_counts"].get("failed", 0), 0)
        pool = mail_provider.parse_outlook_credentials(_pool_text(2))
        mail_provider._claim_outlook_credential_ex(pool)
        with self.assertRaises(OutlookPoolConflict):
            service.reset_outlook_mailbox("outlook-main", self._id("user0@outlook.com"))
        with self.assertRaises(OutlookPoolNotFound):
            service.reset_outlook_mailbox("outlook-main", "deadbeef")

    def test_delete_removes_credential_keeps_used_record_and_blocks_on_lease(self) -> None:
        service = self._service(3)
        mail_provider._set_outlook_token_state("user0@outlook.com", "used")
        mail_provider._set_outlook_token_state("user1@outlook.com", "failed", "x")
        with patch.object(service, "_sync_backend_config"):
            service.delete_outlook_mailbox("outlook-main", self._id("user1@outlook.com"))
            service.delete_outlook_mailbox("outlook-main", self._id("user0@outlook.com"))
        remaining = [c["email"] for c in mail_provider.parse_outlook_credentials(
            service._config["mail"]["providers"][0]["mailboxes"])]
        self.assertEqual(remaining, ["user2@outlook.com"])
        self.assertEqual(self._state("user1@outlook.com"), "")
        self.assertEqual(self._state("user0@outlook.com"), "used")

        pool = mail_provider.parse_outlook_credentials(service._config["mail"]["providers"][0]["mailboxes"])
        mail_provider._claim_outlook_credential_ex(pool)
        with patch.object(service, "_sync_backend_config"), self.assertRaises(OutlookPoolConflict):
            service.delete_outlook_mailbox("outlook-main", self._id("user2@outlook.com"))

    def test_alias_row_cannot_be_deleted(self) -> None:
        service = self._service(1, alias_enabled=True, alias_per_email=1, alias_prefix="t")
        alias_id = self._id("user0+t1@outlook.com")
        with patch.object(service, "_sync_backend_config"), self.assertRaises(ValueError):
            service.delete_outlook_mailbox("outlook-main", alias_id)

    def test_public_snapshot_still_hides_secrets(self) -> None:
        service = self._service(1)
        public = repr(service.get_public()["mail"])
        self.assertNotIn("pw-secret", public)
        self.assertNotIn("refresh-token-secret", public)


if __name__ == "__main__":
    unittest.main()
