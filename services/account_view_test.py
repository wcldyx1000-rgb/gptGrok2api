"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

账号展示字段：AT/RT 状态、救回方式、额度来源、出口、错误脱敏，以及响应里没有任何 secret。
"""
from __future__ import annotations

import base64
import json
import time
import unittest
from unittest.mock import patch

import api.accounts as accounts_api
from services import account_view
from services.account_service import AccountService


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _jwt(exp_offset: int, iat_offset: int = -3600) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    now = int(time.time())
    return f"{b64({'alg': 'none'})}.{b64({'exp': now + exp_offset, 'iat': now + iat_offset})}.sig"


_CTX = {"slots": {}, "down": set(), "capacity": 0, "groups": {}, "mail": {}}


def _fields(account: dict, **kwargs) -> dict:
    return account_view.account_display_fields(account, context=_CTX, **kwargs)


class CredentialStatusTests(unittest.TestCase):
    def test_valid_access_token_with_valid_refresh_token(self) -> None:
        token = _jwt(10 * 86400)
        fields = _fields({"access_token": token, "refresh_token": "rt-secret", "email": "a@example.test"})
        self.assertEqual(fields["access_token_status"], "valid")
        self.assertEqual(fields["access_token_label"], "AT 有效")
        self.assertEqual(fields["refresh_token_status"], "valid")
        self.assertEqual(fields["refresh_token_label"], "RT 有效")
        self.assertTrue(fields["can_refresh_access_token"])
        self.assertEqual(fields["rescue_code"], "refresh")
        self.assertGreater(fields["access_token_expires_at"], int(time.time()))
        self.assertLess(fields["access_token_issued_at"], int(time.time()))

    def test_expiring_access_token(self) -> None:
        fields = _fields({"access_token": _jwt(3600), "refresh_token": "rt"})
        self.assertEqual(fields["access_token_status"], "expiring")
        self.assertEqual(fields["access_token_label"], "AT 临期")
        self.assertEqual(fields["access_token_tone"], "warning")
        self.assertEqual(fields["credential_availability"], "usable")

    def test_invalid_refresh_token_makes_expired_account_unavailable(self) -> None:
        fields = _fields({
            "access_token": _jwt(-60),
            "refresh_token": "rt",
            "refresh_token_invalid_at": "2026-01-02T03:04:05+00:00",
        })
        self.assertEqual(fields["access_token_status"], "invalid")
        self.assertEqual(fields["refresh_token_status"], "invalid")
        self.assertEqual(fields["refresh_token_label"], "RT 失效")
        self.assertEqual(fields["refresh_token_tone"], "error")
        self.assertFalse(fields["can_refresh_access_token"])
        self.assertEqual(fields["credential_availability"], "unavailable")
        self.assertEqual(fields["refresh_token_invalid_at"], 1767323045)

    def test_missing_refresh_token_with_outlook_credentials_rescue_label(self) -> None:
        fields = _fields(
            {
                "access_token": _jwt(86400 * 3),
                "email": "user@outlook.com",
                "outlook_client_id": "client-1",
                "outlook_refresh_token": "ms-rt-secret",
                "relogin_attempts": 1,
                "last_relogin_error": "login failed",
            },
        )
        self.assertEqual(fields["refresh_token_status"], "missing")
        self.assertEqual(fields["rescue_code"], "outlook")
        self.assertEqual(fields["rescue_label"], "Outlook 重登")
        self.assertTrue(fields["has_outlook_credentials"])
        self.assertEqual(fields["relogin_failures"], 1)
        self.assertEqual(fields["relogin_last_error"], "login failed")
        self.assertIn("AT 到期时间", fields["rescue_detail"])

    def test_rescue_exhausted_manual_and_none(self) -> None:
        base = {"access_token": _jwt(86400 * 3), "email": "user@outlook.com"}
        exhausted = _fields({**base, "outlook_client_id": "c", "outlook_refresh_token": "r", "relogin_attempts": 3})
        self.assertEqual(exhausted["rescue_code"], "exhausted")
        self.assertEqual(exhausted["rescue_tone"], "error")
        manual = _fields({**base, "password": "pw-secret", "2fa": "ABCDEF"})
        self.assertEqual(manual["rescue_code"], "manual")
        self.assertTrue(manual["has_password"])
        self.assertTrue(manual["has_totp"])
        self.assertFalse(manual["has_outlook_credentials"])
        none = _fields(base)
        self.assertEqual(none["rescue_code"], "none")
        self.assertFalse(none["has_password"])


class SanitizeTests(unittest.TestCase):
    def test_error_texts_are_redacted(self) -> None:
        access = _jwt(100)
        fields = _fields({
            "access_token": access,
            "refresh_token": "rt-very-secret",
            "proxy": "http://user:pass@10.0.0.1:8080",
            "last_token_refresh_error": (
                f"refresh failed rt-very-secret via http://user:pass@10.0.0.1:8080 Bearer {access} password=hunter2"
            ),
            "last_relogin_error": "outlook token ms-secret-xyz failed",
            "outlook_refresh_token": "ms-secret-xyz",
        })
        blob = json.dumps(fields)
        for secret in ("rt-very-secret", "user:pass", "hunter2", "ms-secret-xyz", access):
            self.assertNotIn(secret, blob)
        self.assertIn("[credential]", fields["last_token_refresh_error"])


class QuotaAndEgressTests(unittest.TestCase):
    def test_quota_estimated_after_local_debit(self) -> None:
        fields = _fields({
            "access_token": "t",
            "quota": 4,
            "last_remote_checked_at": "2026-01-01 10:00:00",
            "last_quota_debit_at": "2026-01-01 11:00:00",
        })
        self.assertEqual(fields["quota_source"], "estimated")
        self.assertEqual(fields["quota_state"], "available")
        self.assertEqual(fields["quota_label"], "4")

    def test_idle_reset_window_detected(self) -> None:
        synced = "2026-01-01 10:00:00"
        synced_ts = account_view.timestamp_seconds(synced)
        fields = _fields({
            "access_token": "t",
            "quota": 5,
            "last_remote_checked_at": synced,
            "limits_progress": [{"feature_name": "image_gen", "reset_after": synced_ts + 86400}],
        })
        self.assertTrue(fields["quota_reset_idle"])
        self.assertEqual(fields["quota_source"], "remote")

    def test_egress_slot_direct_and_pending_switch(self) -> None:
        ctx = {**_CTX, "slots": {"http://mihomo:7002": 2}, "down": {2}}
        slot = account_view.egress_view({"proxy": "http://mihomo:7002"}, ctx)
        self.assertEqual((slot["mode"], slot["slot"], slot["usable"]), ("slot", 2, False))
        direct = account_view.egress_view({"proxy": "direct", "egress_switch_target": "slot:2",
                                           "egress_switch_at": "2026-01-02T00:00:00+00:00"}, ctx)
        self.assertEqual(direct["mode"], "direct")
        self.assertEqual(direct["switch_to"], "slot:2")
        self.assertEqual(direct["switch_to_label"], "槽位 2")
        self.assertEqual(direct["switch_at"], 1767312000)
        custom = account_view.egress_view({"proxy": "http://u:p@1.2.3.4:80"}, ctx)
        self.assertEqual(custom["label"], "自定义代理")
        self.assertNotIn("1.2.3.4", json.dumps(custom))

    def test_group_proxy_fallback(self) -> None:
        ctx = {**_CTX, "groups": {"g1": {"name": "组一", "proxy": "direct", "enabled": True}}}
        view = account_view.egress_view({"group_id": "g1"}, ctx)
        self.assertEqual((view["mode"], view["source"]), ("direct", "group"))


class ApiPayloadTests(unittest.TestCase):
    def test_response_contains_no_secret_material(self) -> None:
        access = _jwt(86400)
        account = {
            "access_token": access,
            "management_id": "acct_test",
            "refresh_token": "rt-secret",
            "id_token": "id-secret",
            "password": "pw-secret",
            "2fa": "TOTP-SECRET",
            "outlook_refresh_token": "ms-rt-secret",
            "outlook_password": "ms-pw-secret",
            "email": "user@outlook.com",
            "status": "正常",
            "quota": 3,
            "group_id": "g1",
        }
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            service = AccountService(_MemoryStorage([account]))
        with patch.object(accounts_api, "account_service", service), \
                patch.object(accounts_api, "_account_display_context", return_value=_CTX):
            payload = accounts_api._account_for_api(service.get_account(access))
        blob = json.dumps({k: v for k, v in payload.items() if k != "access_token"})
        for secret in ("rt-secret", "id-secret", "pw-secret", "TOTP-SECRET", "ms-rt-secret", "ms-pw-secret"):
            self.assertNotIn(secret, blob)
        for key in ("refresh_token", "id_token", "password", "2fa", "outlook_refresh_token", "outlook_password"):
            self.assertNotIn(key, payload)
        self.assertTrue(payload["has_refresh_token"])
        self.assertTrue(payload["has_password"])
        self.assertTrue(payload["has_totp"])
        self.assertEqual(payload["refresh_token_label"], "RT 有效")
        self.assertTrue(payload["id"].startswith("acct_"))


if __name__ == "__main__":
    unittest.main()
