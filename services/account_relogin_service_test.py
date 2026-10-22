import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services import account_relogin_service as relogin
from services.account_relogin_service import AccountReloginService, ReloginDeferred
from services.config import config


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _dead_account(**overrides) -> dict:
    account = {
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "email": "user@outlook.com",
        "status": "正常",
        "source_type": "web",
        "last_token_refresh_error": "oauth_refresh_http_401: invalid_grant",
        "outlook_client_id": "client-1",
        "outlook_refresh_token": "ms-rt-1",
    }
    account.update(overrides)
    return account


class FakeAccounts:
    """Mirrors the account_service relogin interface (task F)."""

    def __init__(self, accounts: list[dict]) -> None:
        self.items = {item["access_token"]: dict(item) for item in accounts}
        self.applied: list[tuple[str, dict, str]] = []
        self.updates: list[tuple[str, dict]] = []
        self.failures: list[tuple[str, str]] = []
        self.marked: list[tuple[str, str]] = []

    def list_accounts(self) -> list[dict]:
        return [dict(item) for item in self.items.values()]

    def list_relogin_needed_accounts(self) -> list[dict]:
        return [dict(item) for item in self.items.values() if item.get("relogin_needed_at") and item.get("status") != "禁用"]

    def mark_account_relogin_needed(self, access_token: str, reason: str) -> dict | None:
        self.marked.append((access_token, reason))
        item = self.items[access_token]
        item.setdefault("relogin_needed_at", _ago(0))
        item["relogin_reason"] = reason
        return dict(item)

    def get_account(self, access_token: str) -> dict | None:
        item = self.items.get(access_token)
        return dict(item) if item else None

    def apply_relogin_tokens(self, access_token: str, token_data: dict, *, event: str = "relogin") -> str:
        self.applied.append((access_token, dict(token_data), event))
        item = self.items.pop(access_token)
        item.update(token_data)
        for key in ("relogin_needed_at", "relogin_reason", "last_relogin_error"):
            item.pop(key, None)
        item["relogin_attempts"] = 0
        item["last_relogin_at"] = _ago(0)
        if item.get("status") == "异常":
            item["status"] = "正常"
        self.items[token_data["access_token"]] = item
        return token_data["access_token"]

    def record_relogin_failure(self, access_token: str, error: str) -> dict | None:
        self.failures.append((access_token, error))
        item = self.items[access_token]
        item["relogin_attempts"] = int(item.get("relogin_attempts") or 0) + 1
        item["last_relogin_at"] = _ago(0)
        item["last_relogin_error"] = error[:500]
        return dict(item)

    def update_account(self, access_token: str, updates: dict, quiet: bool = False) -> dict | None:
        self.updates.append((access_token, dict(updates)))
        self.items[access_token].update(updates)
        return dict(self.items[access_token])


class ConfigOverride:
    def __init__(self, **values) -> None:
        self.values = values
        self.previous: dict = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.previous[key] = config.data.get(key, ConfigOverride)
            config.data[key] = value
        return self

    def __exit__(self, *exc) -> None:
        for key, value in self.previous.items():
            if value is ConfigOverride:
                config.data.pop(key, None)
            else:
                config.data[key] = value


class RefreshTokenDeadTest(unittest.TestCase):
    def test_oauth_rejection_marks_refresh_token_dead(self) -> None:
        self.assertTrue(relogin.refresh_token_dead(_dead_account()))
        self.assertTrue(relogin.refresh_token_dead(_dead_account(last_token_refresh_error="invalid_grant")))

    def test_transient_refresh_errors_are_not_dead(self) -> None:
        for error in ("curl: (28) timeout", "oauth_refresh_http_429", "oauth_refresh_http_502", ""):
            self.assertFalse(relogin.refresh_token_dead(_dead_account(last_token_refresh_error=error)), error)

    def test_missing_refresh_token_needs_abnormal_status(self) -> None:
        self.assertFalse(relogin.refresh_token_dead(_dead_account(refresh_token="", last_token_refresh_error="")))
        self.assertTrue(relogin.refresh_token_dead(_dead_account(refresh_token="", status="异常")))

    def test_account_service_flag_marks_dead(self) -> None:
        flagged = _dead_account(last_token_refresh_error="", relogin_needed_at=_ago(60))
        self.assertTrue(relogin.refresh_token_dead(flagged))
        self.assertFalse(relogin.refresh_token_dead({**flagged, "status": "禁用"}))

    def test_disabled_and_chatgpt_web_accounts(self) -> None:
        self.assertFalse(relogin.refresh_token_dead(_dead_account(status="禁用")))
        web = _dead_account(source_type="chatgpt_web", last_token_refresh_error="")
        self.assertFalse(relogin.refresh_token_dead(web))
        self.assertTrue(relogin.refresh_token_dead({**web, "status": "异常"}))


class OutlookMailboxTest(unittest.TestCase):
    def test_account_fields_build_mailbox(self) -> None:
        mailbox = relogin.resolve_outlook_mailbox(_dead_account(), mail={})
        self.assertEqual(mailbox["provider"], "outlook_token")
        self.assertEqual(mailbox["address"], "user@outlook.com")
        self.assertEqual(mailbox["client_id"], "client-1")
        self.assertEqual(mailbox["refresh_token"], "ms-rt-1")

    def test_pool_lookup_matches_plus_alias(self) -> None:
        mail = {
            "providers": [
                {"type": "outlook_token", "id": "p1", "mailboxes": "user@outlook.com----pw----client-9----ms-rt-9"},
            ]
        }
        account = _dead_account(email="user+c2api1@outlook.com", outlook_client_id="", outlook_refresh_token="")
        mailbox = relogin.resolve_outlook_mailbox(account, mail=mail)
        self.assertIsNotNone(mailbox)
        self.assertEqual(mailbox["address"], "user+c2api1@outlook.com")
        self.assertEqual(mailbox["client_id"], "client-9")
        self.assertEqual(mailbox["provider_ref"], "outlook_token:p1")

    def test_no_credentials_returns_none(self) -> None:
        account = _dead_account(outlook_client_id="", outlook_refresh_token="")
        self.assertIsNone(relogin.resolve_outlook_mailbox(account, mail={"providers": []}))
        self.assertFalse(relogin.needs_relogin(account, mail={"providers": []}))


class ReloginDueTest(unittest.TestCase):
    def test_backoff_schedule(self) -> None:
        now = time.time()
        self.assertTrue(relogin.relogin_due(_dead_account(), now, mail={}))
        once = _dead_account(relogin_attempts=1, last_relogin_at=_ago(10 * 60))
        self.assertFalse(relogin.relogin_due(once, now, mail={}))
        self.assertTrue(relogin.relogin_due({**once, "last_relogin_at": _ago(31 * 60)}, now, mail={}))
        twice = _dead_account(relogin_attempts=2, last_relogin_at=_ago(60 * 60))
        self.assertFalse(relogin.relogin_due(twice, now, mail={}))
        self.assertTrue(relogin.relogin_due({**twice, "last_relogin_at": _ago(2 * 3600 + 5)}, now, mail={}))

    def test_exhausted_after_max_failures(self) -> None:
        account = _dead_account(relogin_attempts=3, last_relogin_at=_ago(86400))
        self.assertFalse(relogin.relogin_due(account, mail={}))
        with ConfigOverride(account_relogin_enabled=True):
            self.assertFalse(relogin.relogin_holds(account, mail={}))
            self.assertTrue(relogin.relogin_holds(_dead_account(), mail={}))

    def test_holds_only_when_enabled(self) -> None:
        with ConfigOverride(account_relogin_enabled=False):
            self.assertFalse(relogin.relogin_holds(_dead_account(), mail={}))


class ReloginServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(relogin, "_mail_settings", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_rotates_tokens_and_clears_failure_state(self) -> None:
        accounts = FakeAccounts([_dead_account(relogin_attempts=1, last_relogin_at=_ago(3600), status="异常")])
        calls: list[tuple[dict, dict]] = []

        def login(account: dict, mailbox: dict) -> dict:
            calls.append((account, mailbox))
            return {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "id-new"}

        service = AccountReloginService(login_fn=login, accounts=accounts)
        self.assertTrue(service.relogin({"access_token": "at-old"}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["client_id"], "client-1")
        self.assertEqual(accounts.applied[0][0], "at-old")
        self.assertEqual(accounts.applied[0][2], "account_relogin")
        stored = accounts.get_account("at-new")
        self.assertEqual(stored["refresh_token"], "rt-new")
        self.assertEqual(stored["status"], "正常")
        self.assertEqual(stored["relogin_attempts"], 0)
        self.assertNotIn("last_relogin_error", stored)
        self.assertEqual(accounts.failures, [])

    def test_chatgpt_web_account_switches_to_platform_source(self) -> None:
        accounts = FakeAccounts([_dead_account(source_type="chatgpt_web", status="异常", last_token_refresh_error="")])
        service = AccountReloginService(login_fn=lambda a, m: {"access_token": "at-new"}, accounts=accounts)
        self.assertTrue(service.relogin({"access_token": "at-old"}))
        self.assertEqual(accounts.get_account("at-new")["source_type"], "web")

    def test_due_accounts_uses_flags_and_backfills_legacy_dead_accounts(self) -> None:
        accounts = FakeAccounts([
            _dead_account(access_token="at-flagged", email="f@outlook.com", last_token_refresh_error="", relogin_needed_at=_ago(60)),
            _dead_account(access_token="at-legacy", email="l@outlook.com"),
            _dead_account(access_token="at-healthy", email="h@outlook.com", last_token_refresh_error=""),
        ])
        service = AccountReloginService(login_fn=lambda a, m: {}, accounts=accounts)
        due = {item["access_token"] for item in service.due_accounts()}
        self.assertEqual(due, {"at-flagged", "at-legacy"})
        self.assertEqual([token for token, _ in accounts.marked], ["at-legacy"])
        self.assertIn("invalid_grant", accounts.marked[0][1])

    @staticmethod
    def _raise(message: str, error_type: type[Exception] = RuntimeError):
        def login(account: dict, mailbox: dict) -> dict:
            raise error_type(message)

        return login

    def test_failure_records_count_and_error(self) -> None:
        accounts = FakeAccounts([_dead_account(relogin_attempts=1, last_relogin_at=_ago(3600))])
        service = AccountReloginService(login_fn=self._raise("等待 OpenAI 登录验证码超时"), accounts=accounts)
        self.assertFalse(service.relogin({"access_token": "at-old"}))
        stored = accounts.get_account("at-old")
        self.assertEqual(stored["relogin_attempts"], 2)
        self.assertIn("验证码超时", stored["last_relogin_error"])
        self.assertTrue(stored["last_relogin_at"])
        # Backoff now blocks an immediate retry.
        self.assertFalse(service.relogin({"access_token": "at-old"}))

    def test_deactivated_account_is_exhausted_at_once(self) -> None:
        accounts = FakeAccounts([_dead_account()])
        service = AccountReloginService(login_fn=self._raise("account_deactivated"), accounts=accounts)
        self.assertFalse(service.relogin({"access_token": "at-old"}))
        self.assertEqual(accounts.get_account("at-old")["relogin_attempts"], relogin.RELOGIN_MAX_FAILURES)
        self.assertEqual(len(accounts.failures), 1)

    def test_deferred_login_does_not_count_as_failure(self) -> None:
        accounts = FakeAccounts([_dead_account()])
        service = AccountReloginService(login_fn=self._raise("stopped", ReloginDeferred), accounts=accounts)
        self.assertFalse(service.relogin({"access_token": "at-old"}))
        self.assertEqual(accounts.updates, [])
        self.assertEqual(accounts.failures, [])

    def test_account_without_credentials_is_skipped(self) -> None:
        accounts = FakeAccounts([_dead_account(outlook_client_id="", outlook_refresh_token="")])
        login_calls: list = []
        service = AccountReloginService(login_fn=lambda a, m: login_calls.append(a) or {}, accounts=accounts)
        with ConfigOverride(account_relogin_enabled=True):
            self.assertEqual(service.run_due(), 0)
        self.assertEqual(login_calls, [])

    def test_healthy_refresh_token_is_skipped(self) -> None:
        accounts = FakeAccounts([_dead_account(last_token_refresh_error="")])
        login_calls: list = []
        service = AccountReloginService(login_fn=lambda a, m: login_calls.append(a) or {}, accounts=accounts)
        with ConfigOverride(account_relogin_enabled=True):
            self.assertEqual(service.run_due(), 0)
        self.assertEqual(login_calls, [])

    def test_disabled_switch_does_not_run(self) -> None:
        accounts = FakeAccounts([_dead_account()])
        login_calls: list = []
        service = AccountReloginService(login_fn=lambda a, m: login_calls.append(a) or {}, accounts=accounts)
        with ConfigOverride(account_relogin_enabled=False):
            self.assertEqual(service.run_due(), 0)
        self.assertEqual(login_calls, [])

    def test_run_due_spaces_out_logins(self) -> None:
        accounts = FakeAccounts([
            _dead_account(access_token="at-1", email="a@outlook.com"),
            _dead_account(access_token="at-2", email="b@outlook.com"),
        ])
        sleeps: list[float] = []
        counter = iter(range(100))
        service = AccountReloginService(
            login_fn=lambda a, m: {"access_token": f"new-{next(counter)}"},
            sleep_fn=sleeps.append,
            accounts=accounts,
        )
        with ConfigOverride(account_relogin_enabled=True, account_relogin_concurrency=1):
            self.assertEqual(service.run_due(), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], relogin.RELOGIN_GAP_SECS)


class RealAccountServiceTest(unittest.TestCase):
    """Runs the relogin cycle against the real AccountService relogin interface."""

    def setUp(self) -> None:
        for target in ("services.account_service.log_service", "services.account_relogin_service._mail_settings"):
            patcher = patch(target, return_value={}) if target.endswith("_mail_settings") else patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_service(self, accounts: list[dict]):
        from services.account_service import AccountService

        class Storage:
            def __init__(self, items: list[dict]) -> None:
                self.items = items

            def load_accounts(self) -> list[dict]:
                return [dict(item) for item in self.items]

            def save_accounts(self, items: list[dict]) -> None:
                self.items = [dict(item) for item in items]

        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            return AccountService(Storage(accounts))

    def test_failure_then_success_round_trip(self) -> None:
        service = self.make_service([_dead_account(status="异常")])
        service.mark_account_relogin_needed("at-old", "oauth_refresh_http_401")

        failing = AccountReloginService(login_fn=lambda a, m: (_ for _ in ()).throw(RuntimeError("otp timeout")), accounts=service)
        self.assertFalse(failing.relogin({"access_token": "at-old"}))
        stored = service.get_account("at-old")
        self.assertEqual(stored["relogin_attempts"], 1)
        self.assertEqual(stored["last_relogin_error"], "otp timeout")
        self.assertEqual(failing.due_accounts(), [])  # 30-minute backoff

        service.update_account("at-old", {"last_relogin_at": _ago(31 * 60)}, quiet=True)
        ok = AccountReloginService(login_fn=lambda a, m: {"access_token": "at-new", "refresh_token": "rt-new"}, accounts=service)
        self.assertTrue(ok.relogin({"access_token": "at-old"}))
        stored = service.get_account("at-new")
        self.assertEqual(stored["refresh_token"], "rt-new")
        self.assertEqual(stored["status"], "正常")
        self.assertEqual(stored["relogin_attempts"], 0)
        self.assertNotIn("relogin_needed_at", stored)
        self.assertEqual(service.list_relogin_needed_accounts(), [])
        self.assertEqual(service.get_account("at-old")["access_token"], "at-new")


class ReloginLoopTest(unittest.TestCase):
    def test_service_runs_when_enabled_and_stops(self) -> None:
        service = AccountReloginService(login_fn=lambda a, m: {}, accounts=FakeAccounts([]))
        ran = threading.Event()
        with ConfigOverride(account_relogin_enabled=True), \
                patch.object(relogin, "RELOGIN_STARTUP_DELAY_SECS", 0.0), \
                patch.object(service, "run_due", side_effect=lambda stop_event=None: ran.set() or 0):
            thread = service.start()
            self.assertTrue(ran.wait(2))
            started = time.monotonic()
            service.stop(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 3)

    def test_disabled_loop_skips_work_and_honours_stop_event(self) -> None:
        service = AccountReloginService(login_fn=lambda a, m: {}, accounts=FakeAccounts([]))
        stop_event = threading.Event()
        with ConfigOverride(account_relogin_enabled=False), \
                patch.object(relogin, "RELOGIN_STARTUP_DELAY_SECS", 0.0), \
                patch.object(service, "run_due") as run_due:
            thread = service.start(stop_event)
            time.sleep(0.2)
            stop_event.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            run_due.assert_not_called()


class ConfigTest(unittest.TestCase):
    def test_relogin_settings_are_clamped(self) -> None:
        with ConfigOverride(account_relogin_interval_secs=5, account_relogin_concurrency=99):
            self.assertEqual(config.account_relogin_interval_secs, 60)
            self.assertEqual(config.account_relogin_concurrency, 4)
        with ConfigOverride(account_relogin_interval_secs="bad", account_relogin_concurrency=0):
            self.assertEqual(config.account_relogin_interval_secs, 300)
            self.assertEqual(config.account_relogin_concurrency, 1)
        saved = config.data.pop("account_relogin_enabled", None)
        try:
            self.assertFalse(config.account_relogin_enabled)
        finally:
            if saved is not None:
                config.data["account_relogin_enabled"] = saved

    def test_account_listing_hides_outlook_secrets(self) -> None:
        from api.accounts import _account_for_api

        item = _account_for_api(_dead_account(outlook_password="pw"))
        self.assertNotIn("outlook_refresh_token", item)
        self.assertNotIn("outlook_password", item)
        self.assertEqual(item["outlook_client_id"], "client-1")


if __name__ == "__main__":
    unittest.main()
