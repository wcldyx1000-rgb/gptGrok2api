import time
import unittest
from datetime import datetime, timedelta, timezone
from threading import Lock
from unittest.mock import patch

from services.account_service import AccountService
from services.config import config


class AccountServiceSessionImportTests(unittest.TestCase):
    def test_prepare_account_payload_keeps_camel_case_session_tokens(self) -> None:
        payload = AccountService._prepare_account_payload(
            {
                "type": "codex",
                "accessToken": "access-token",
                "refreshToken": "refresh-token",
                "idToken": "id-token",
                "sessionToken": "session-token",
                "expiresAt": "2026-07-12T00:00:00Z",
            }
        )

        self.assertEqual(
            payload,
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "session_token": "session-token",
                "expired": "2026-07-12T00:00:00Z",
                "export_type": "codex",
                "source_type": "codex",
            },
        )

    def test_prepare_account_payload_prefers_existing_snake_case_tokens(self) -> None:
        payload = AccountService._prepare_account_payload(
            {
                "access_token": "access-token",
                "refresh_token": "stored-refresh-token",
                "refreshToken": "browser-refresh-token",
            }
        )

        self.assertEqual(payload["refresh_token"], "stored-refresh-token")
        self.assertNotIn("refreshToken", payload)

    def test_chatgpt_web_session_is_not_scheduled_for_platform_oauth_refresh(self) -> None:
        service = object.__new__(AccountService)
        due_at = service._refresh_token_keepalive_due_at(
            {"source_type": "chatgpt_web", "refresh_token": "browser-session-refresh"},
            datetime.now(timezone.utc),
        )

        self.assertIsNone(due_at)

    def test_ready_pix_checkout_rejects_later_failure_state(self) -> None:
        final_url = "https://payments.stripe.com/qr/instructions/pix-ready"
        service = object.__new__(AccountService)
        service._lock = Lock()
        service._token_aliases = {}
        service._image_inflight = {}
        service._accounts = {
            "live-token": {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": final_url,
                "checkout_attempt_count": 3,
                "checkout_last_error": None,
                "checkout_billing_address": {
                    "country": "BR",
                    "postal_code": "01310100",
                },
            }
        }

        with patch.object(service, "_save_accounts"):
            updated = service.update_account(
                "live-token",
                {
                    "checkout_link_status": "pending",
                    "checkout_attempt_count": 4,
                    "checkout_last_error": "approve blocked",
                    "checkout_last_error_at": "2026-07-14T15:00:00+00:00",
                    "checkout_billing_address": None,
                },
                quiet=True,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["checkout_link_status"], "ready")
        self.assertEqual(updated["checkout_final_url"], final_url)
        self.assertEqual(updated["checkout_attempt_count"], 3)
        self.assertIsNone(updated["checkout_last_error"])
        self.assertEqual(
            updated["checkout_billing_address"],
            {"country": "BR", "postal_code": "01310100"},
        )

    def test_ready_pix_checkout_accepts_a_complete_new_success(self) -> None:
        old_url = "https://payments.stripe.com/qr/instructions/pix-old"
        new_url = "https://payments.stripe.com/qr/instructions/pix-new"
        service = object.__new__(AccountService)
        service._lock = Lock()
        service._token_aliases = {}
        service._image_inflight = {}
        service._accounts = {
            "live-token": {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": old_url,
            }
        }

        with patch.object(service, "_save_accounts"):
            updated = service.update_account(
                "live-token",
                {
                    "checkout_link_status": "ready",
                    "checkout_final_url": new_url,
                    "checkout_last_error": None,
                },
                quiet=True,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["checkout_final_url"], new_url)

    def test_ready_upi_checkout_rejects_later_failure_state(self) -> None:
        final_url = "https://payments.stripe.com/upi/instructions/upi-ready?client_secret=secret"
        service = object.__new__(AccountService)
        service._lock = Lock()
        service._token_aliases = {}
        service._image_inflight = {}
        service._accounts = {
            "live-token": {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": final_url,
                "checkout_attempt_count": 5,
                "checkout_billing_address": {"country": "IN", "postal_code": "700016"},
            }
        }

        with patch.object(service, "_save_accounts"):
            updated = service.update_account(
                "live-token",
                {
                    "checkout_link_status": "failed",
                    "checkout_attempt_count": 6,
                    "checkout_last_error": "approve blocked",
                    "checkout_billing_address": None,
                },
                quiet=True,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["checkout_link_status"], "ready")
        self.assertEqual(updated["checkout_final_url"], final_url)
        self.assertEqual(updated["checkout_attempt_count"], 5)
        self.assertEqual(
            updated["checkout_billing_address"],
            {"country": "IN", "postal_code": "700016"},
        )


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _image_account(token: str, *, checked_ago: float | None = 60) -> dict:
    item = {"access_token": token, "status": "正常", "quota": 10, "email": f"{token}@example.test"}
    if checked_ago is not None:
        checked_at = datetime.now(timezone.utc) - timedelta(seconds=checked_ago)
        item["last_remote_checked_at"] = checked_at.isoformat()
        item["last_remote_check_result"] = "ok"
    return item


class AccountServiceImageSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._config_data = patch.dict(
            config.data,
            {"image_account_cooldown_secs": 0, "image_preflight_fresh_secs": 600, "image_account_concurrency": 3},
        )
        self._config_data.start()
        self.addCleanup(self._config_data.stop)
        self._log = patch("services.account_service.log_service")
        self._log.start()
        self.addCleanup(self._log.stop)

    def make_service(self, accounts: list[dict]) -> AccountService:
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            service = AccountService(_MemoryStorage(accounts))
        service.refresh_access_token = lambda token, **_kwargs: token
        service.remote_checks = []

        def fetch_remote_info(token: str, event: str = "fetch_remote_info", remove_invalid=None):
            service.remote_checks.append(token)
            service._record_refresh_success(token, event)
            return service.update_account(token, {"status": "正常", "quota": 10}, quiet=True)

        service.fetch_remote_info = fetch_remote_info
        return service

    def test_recently_checked_account_skips_preflight(self) -> None:
        service = self.make_service([_image_account("tok-a", checked_ago=60)])
        token = service.get_available_access_token()
        self.assertEqual(token, "tok-a")
        self.assertEqual(service.remote_checks, [])

    def test_stale_or_unchecked_account_runs_preflight(self) -> None:
        service = self.make_service([
            _image_account("tok-a", checked_ago=601),
        ])
        service.get_available_access_token()
        self.assertEqual(service.remote_checks, ["tok-a"])

        service = self.make_service([_image_account("tok-b", checked_ago=None)])
        service.get_available_access_token()
        self.assertEqual(service.remote_checks, ["tok-b"])

    def test_zero_fresh_window_always_runs_preflight(self) -> None:
        config.data["image_preflight_fresh_secs"] = 0
        service = self.make_service([_image_account("tok-a", checked_ago=1)])
        service.get_available_access_token()
        self.assertEqual(service.remote_checks, ["tok-a"])

    def test_image_failure_invalidates_freshness_until_next_check(self) -> None:
        service = self.make_service([_image_account("tok-a", checked_ago=60)])
        token = service.get_available_access_token()
        service.mark_image_result(token, False)
        service.get_available_access_token()
        self.assertEqual(service.remote_checks, ["tok-a"])
        # The preflight just refreshed the check, so the next lease is fresh again.
        service.mark_image_result(token, True)
        service.get_available_access_token()
        self.assertEqual(service.remote_checks, ["tok-a"])

    def test_remote_check_error_and_drained_quota_invalidate_freshness(self) -> None:
        service = self.make_service([_image_account("tok-a", checked_ago=60)])
        service._record_remote_check_error("tok-a", "test", "boom")
        self.assertFalse(service._image_preflight_is_fresh("tok-a"))

        service = self.make_service([_image_account("tok-b", checked_ago=60)])
        service.update_account("tok-b", {"quota": 1}, quiet=True)
        token = service.get_available_access_token()
        service.mark_image_result(token, True)
        self.assertFalse(service._image_preflight_is_fresh("tok-b"))

    def test_cooldown_rests_account_after_request_ends(self) -> None:
        config.data["image_account_cooldown_secs"] = 1
        service = self.make_service([_image_account("tok-a"), _image_account("tok-b")])
        with patch("services.account_service.random.uniform", return_value=1.0):
            first = service.get_available_access_token()
            service.mark_image_result(first, True)
            second = service.get_available_access_token()
            service.mark_image_result(second, True)
        self.assertEqual({first, second}, {"tok-a", "tok-b"})

        started = time.monotonic()
        third = service.get_available_access_token()
        waited = time.monotonic() - started
        self.assertEqual(third, first)
        self.assertGreaterEqual(waited, 0.8)
        self.assertLess(waited, 2.0)

    def test_cooldown_does_not_delay_retry_on_another_account(self) -> None:
        config.data["image_account_cooldown_secs"] = 30
        service = self.make_service([_image_account("tok-a"), _image_account("tok-b")])
        first = service.get_available_access_token()
        service.mark_image_result(first, False)
        started = time.monotonic()
        second = service.get_available_access_token()
        self.assertNotEqual(second, first)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_zero_cooldown_reuses_account_immediately(self) -> None:
        service = self.make_service([_image_account("tok-a")])
        for _ in range(3):
            started = time.monotonic()
            token = service.get_available_access_token()
            service.mark_image_result(token, True)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_cooldown_follows_token_rotation_and_is_cleared_on_delete(self) -> None:
        config.data["image_account_cooldown_secs"] = 30
        service = self.make_service([_image_account("tok-a", checked_ago=None)])
        service.release_image_slot(service.get_available_access_token())
        with patch.object(service, "_save_accounts"):
            service._apply_refreshed_tokens("tok-a", {"access_token": "tok-a2"}, "test")
        self.assertIn("tok-a2", service._image_released_at)
        self.assertNotIn("tok-a", service._image_released_at)
        service.delete_accounts(["tok-a2"], return_items=False)
        self.assertEqual(service._image_released_at, {})

    def test_config_clamps_cooldown_and_fresh_window(self) -> None:
        for raw, expected in ((None, 0), (0, 0), (-5, 0), (45, 45), (99999, 600), ("bad", 0)):
            if raw is None:
                config.data.pop("image_account_cooldown_secs", None)
            else:
                config.data["image_account_cooldown_secs"] = raw
            self.assertEqual(config.image_account_cooldown_secs, expected)
        for raw, expected in ((None, 600), (-1, 0), (99999, 3600), ("bad", 600)):
            if raw is None:
                config.data.pop("image_preflight_fresh_secs", None)
            else:
                config.data["image_preflight_fresh_secs"] = raw
            self.assertEqual(config.image_preflight_fresh_secs, expected)


if __name__ == "__main__":
    unittest.main()
