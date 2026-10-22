from __future__ import annotations

import contextlib
import json
import threading
import unittest
from typing import Any
from unittest.mock import patch

import services.login_import_service as login_import
from services.login_import_service import LoginImportService, parse_login_lines, verify_and_import, verify_account_payload


class FakeAccounts:
    """号池替身：记录写入、删除，按预设返回远程检查后的状态。"""

    def __init__(self, statuses: dict[str, str] | None = None, refresh: dict[str, Any] | None = None) -> None:
        self.statuses = statuses or {}
        self.refresh = refresh or {}
        self.items: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.remote_checks: list[str] = []

    def _request_access_token_refresh(self, refresh_token: str, item: dict[str, Any]) -> dict[str, Any]:
        outcome = self.refresh.get(refresh_token)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or {}

    def add_account_items(self, items: list[dict[str, Any]], return_items: bool = True) -> dict[str, Any]:
        for item in items:
            self.items[item["access_token"]] = dict(item)
        return {"added": len(items)}

    def fetch_remote_info(self, access_token: str, event: str = "", remove_invalid: bool = True) -> None:
        self.remote_checks.append(access_token)
        status = self.statuses.get(access_token, "正常")
        if status == "raise":
            raise RuntimeError("upstream 503")
        self.items[access_token]["status"] = status

    def get_account(self, access_token: str) -> dict[str, Any] | None:
        return self.items.get(access_token)

    def delete_accounts(self, tokens: list[str], return_items: bool = True) -> dict[str, Any]:
        for token in tokens:
            self.deleted.append(token)
            self.items.pop(token, None)
        return {"removed": len(tokens)}

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.items.values()]


@contextlib.contextmanager
def _no_log_scope(_sink):
    yield


def _service(login_fn, *, accounts=None, pick_proxy_fn=None) -> LoginImportService:
    return LoginImportService(
        login_fn=login_fn,
        pick_proxy_fn=pick_proxy_fn or (lambda failed: ""),
        prepare_fn=lambda: None,
        log_scope_fn=_no_log_scope,
        accounts=accounts or FakeAccounts(),
        gap_seconds=0,
    )


class ParseLoginLinesTest(unittest.TestCase):
    def test_accepts_common_separators_and_optional_totp(self) -> None:
        rows, bad = parse_login_lines(
            "\n".join([
                "A@Example.test|pw-a|JBSWY3DPEHPK3PXP",
                "b@example.test----pw-b----SECRET2",
                "c@example.test\tpw-c",
                "d@example.test,pw-d,",
                "e@example.test pw-e",
            ])
        )
        self.assertEqual(bad, [])
        self.assertEqual([row["email"] for row in rows], [
            "a@example.test", "b@example.test", "c@example.test", "d@example.test", "e@example.test",
        ])
        self.assertEqual(rows[0], {"email": "a@example.test", "password": "pw-a", "totp": "JBSWY3DPEHPK3PXP"})
        self.assertEqual(rows[1]["totp"], "SECRET2")
        self.assertEqual(rows[2]["totp"], "")
        self.assertEqual(rows[3]["totp"], "")

    def test_skips_comments_blank_and_duplicate_emails(self) -> None:
        rows, bad = parse_login_lines("# header\n\na@example.test|pw1\nA@example.test|pw2\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["password"], "pw1")
        self.assertEqual(bad, [])

    def test_bad_lines_are_truncated_so_passwords_are_not_echoed_in_full(self) -> None:
        rows, bad = parse_login_lines("not-an-email|password\nonly@example.test\nx|a-very-long-secret-password-value-here")
        self.assertEqual(rows, [])
        self.assertEqual(len(bad), 3)
        self.assertNotIn("a-very-long-secret-password-value-here", "".join(bad))
        self.assertTrue(all(len(item) <= 25 for item in bad))


class VerifyOnImportTest(unittest.TestCase):
    def test_usable_account_stays_in_pool_with_refreshed_tokens(self) -> None:
        accounts = FakeAccounts(refresh={"rt-1": {"access_token": "at-new", "refresh_token": "rt-2"}})
        result = verify_account_payload(
            {"email": "a@example.test", "access_token": "at-old", "refresh_token": "rt-1"},
            accounts=accounts,
        )
        self.assertTrue(result["usable"])
        self.assertEqual(result["access_token"], "at-new")
        self.assertEqual(accounts.items["at-new"]["refresh_token"], "rt-2")
        self.assertNotIn("at-old", accounts.items)
        self.assertEqual(accounts.deleted, [])

    def test_refresh_failure_rejects_without_touching_pool(self) -> None:
        accounts = FakeAccounts(refresh={"rt-bad": RuntimeError("invalid_grant")})
        result = verify_account_payload(
            {"email": "a@example.test", "access_token": "at", "refresh_token": "rt-bad"},
            accounts=accounts,
        )
        self.assertFalse(result["usable"])
        self.assertTrue(result["reason"].startswith("refresh_failed"))
        self.assertEqual(accounts.items, {})

    def test_unusable_status_is_removed_from_pool(self) -> None:
        accounts = FakeAccounts(statuses={"at-dead": "异常"})
        result = verify_account_payload({"email": "a@example.test", "access_token": "at-dead"}, accounts=accounts)
        self.assertFalse(result["usable"])
        self.assertEqual(result["reason"], "status_异常")
        self.assertEqual(accounts.deleted, ["at-dead"])
        self.assertNotIn("at-dead", accounts.items)

    def test_remote_check_error_is_rejected_and_removed(self) -> None:
        accounts = FakeAccounts(statuses={"at": "raise"})
        result = verify_account_payload({"email": "a@example.test", "access_token": "at"}, accounts=accounts)
        self.assertFalse(result["usable"])
        self.assertTrue(result["reason"].startswith("remote_check_failed"))
        self.assertEqual(accounts.deleted, ["at"])

    def test_missing_access_token_is_rejected(self) -> None:
        accounts = FakeAccounts()
        result = verify_account_payload({"email": "a@example.test"}, accounts=accounts)
        self.assertEqual(result["reason"], "missing_access_token")
        self.assertEqual(accounts.items, {})

    def test_verify_and_import_summarises_added_and_rejected(self) -> None:
        accounts = FakeAccounts(statuses={"at-ok": "正常", "at-limited": "限流", "at-dead": "禁用"})
        summary = verify_and_import(
            [
                {"email": "ok@example.test", "access_token": "at-ok"},
                {"email": "lim@example.test", "access_token": "at-limited"},
                {"email": "dead@example.test", "access_token": "at-dead"},
                "not-a-dict",
            ],
            accounts=accounts,
        )
        self.assertEqual(summary["verified"], 3)
        self.assertEqual(summary["added"], 2)
        self.assertEqual(summary["tokens"], ["at-ok", "at-limited"])
        self.assertEqual(summary["rejected"], [{"email": "dead@example.test", "reason": "status_禁用", "status": "禁用"}])
        self.assertEqual(set(accounts.items), {"at-ok", "at-limited"})


class LoginImportJobTest(unittest.TestCase):
    def test_runs_rows_and_writes_pool_without_leaking_credentials(self) -> None:
        accounts = FakeAccounts()
        calls: list[tuple[str, str, str]] = []

        def login(row, proxy, index):
            calls.append((row["email"], row["password"], row["totp"]))
            if row["email"].startswith("bad"):
                raise RuntimeError("wrong password")
            return {"email": row["email"], "access_token": f"at-{index}", "refresh_token": f"rt-{index}"}

        service = _service(login, accounts=accounts)
        job = service.start(
            "ok@example.test|secret-pass-1|TOTPSECRET\nbad@example.test|secret-pass-2",
            verify=False,
            background=False,
        )
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["terminal"])
        self.assertEqual((job["total"], job["done"], job["ok"], job["pool_ok"], job["fail"]), (2, 2, 1, 1, 1))
        self.assertEqual(calls[0], ("ok@example.test", "secret-pass-1", "TOTPSECRET"))
        self.assertIn("at-1", accounts.items)
        self.assertEqual([item["ok"] for item in job["results"]], [True, False])
        dumped = json.dumps(job, ensure_ascii=False)
        for secret in ("secret-pass-1", "secret-pass-2", "TOTPSECRET"):
            self.assertNotIn(secret, dumped)

    def test_verify_mode_counts_pool_usable_separately(self) -> None:
        accounts = FakeAccounts(statuses={"at-2": "异常"})

        def login(row, proxy, index):
            return {"email": row["email"], "access_token": f"at-{index}"}

        job = _service(login, accounts=accounts).start("a@example.test|p\nb@example.test|p", verify=True, background=False)
        self.assertEqual((job["ok"], job["pool_ok"], job["fail"]), (2, 1, 0))
        self.assertEqual([item["usable"] for item in job["results"]], [True, False])
        self.assertEqual(job["results"][1]["error"], "status_异常")
        self.assertEqual(accounts.deleted, ["at-2"])

    def test_rejects_empty_input_and_concurrent_jobs(self) -> None:
        release = threading.Event()

        def login(row, proxy, index):
            release.wait(5)
            return {"email": row["email"], "access_token": "at"}

        service = _service(login)
        with self.assertRaises(ValueError):
            service.start("no valid lines here", background=False)
        first = service.start("a@example.test|p", verify=False)
        try:
            with self.assertRaises(ValueError):
                service.start("b@example.test|p", verify=False)
        finally:
            release.set()
        self._wait_terminal(service, first["job_id"])

    def test_stop_halts_before_remaining_rows(self) -> None:
        started = threading.Event()
        proceed = threading.Event()
        logged_in: list[str] = []

        def login(row, proxy, index):
            logged_in.append(row["email"])
            started.set()
            proceed.wait(5)
            return {"email": row["email"], "access_token": f"at-{index}"}

        service = _service(login)
        job = service.start("a@example.test|p\nb@example.test|p\nc@example.test|p", verify=False)
        self.assertTrue(started.wait(5))
        service.stop(job["job_id"])
        proceed.set()
        final = self._wait_terminal(service, job["job_id"])
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(logged_in, ["a@example.test"])
        self.assertEqual(final["done"], 1)

    def test_proxy_failure_retries_once_on_another_node(self) -> None:
        picks: list[set[str]] = []
        proxies = iter(["http://node-1", "http://node-2"])
        used: list[str] = []

        def pick(failed):
            picks.append(set(failed))
            return next(proxies)

        def login(row, proxy, index):
            used.append(proxy)
            if proxy == "http://node-1":
                raise RuntimeError("curl: (56) proxy CONNECT aborted")
            return {"email": row["email"], "access_token": "at"}

        service = _service(login, pick_proxy_fn=pick)
        with patch.object(login_import, "_is_proxy_error", return_value=True):
            job = service.start("a@example.test|p", verify=False, background=False)
        self.assertEqual(used, ["http://node-1", "http://node-2"])
        self.assertEqual(picks, [set(), {"http://node-1"}])
        self.assertEqual(job["ok"], 1)

    def test_non_proxy_error_and_second_proxy_failure_do_not_retry_further(self) -> None:
        used: list[str] = []

        def login(row, proxy, index):
            used.append(proxy)
            raise RuntimeError("boom")

        service = _service(login, pick_proxy_fn=lambda failed: f"http://node-{len(failed) + 1}")
        with patch.object(login_import, "_is_proxy_error", return_value=False):
            job = service.start("a@example.test|p", verify=False, background=False)
        self.assertEqual(used, ["http://node-1"])
        self.assertEqual(job["fail"], 1)

        used.clear()
        with patch.object(login_import, "_is_proxy_error", return_value=True):
            job = service.start("b@example.test|p", verify=False, background=False)
        self.assertEqual(used, ["http://node-1", "http://node-2"])
        self.assertEqual(job["fail"], 1)

    def test_direct_connection_proxy_error_is_not_retried(self) -> None:
        used: list[str] = []

        def login(row, proxy, index):
            used.append(proxy)
            raise RuntimeError("curl: (7) failed to connect")

        service = _service(login, pick_proxy_fn=lambda failed: "")
        with patch.object(login_import, "_is_proxy_error", return_value=True):
            job = service.start("a@example.test|p", verify=False, background=False)
        self.assertEqual(used, [""])
        self.assertEqual(job["fail"], 1)

    @staticmethod
    def _wait_terminal(service: LoginImportService, job_id: str) -> dict[str, Any]:
        for _ in range(500):
            job = service.get(job_id)
            if job and job.get("terminal"):
                return job
            threading.Event().wait(0.01)
        raise AssertionError("login import job did not finish")


class ReactivateTest(unittest.TestCase):
    def test_uses_relogin_implementation_and_reports_each_email(self) -> None:
        accounts = FakeAccounts()
        accounts.items = {
            "at-a": {"email": "a@example.test", "access_token": "at-a"},
            "at-b": {"email": "b@example.test", "access_token": "at-b"},
        }
        applied: list[tuple[str, dict[str, str]]] = []

        def resolve(account):
            return None if account["email"] == "b@example.test" else {"email": account["email"]}

        def apply(service_accounts, token, account, tokens):
            applied.append((token, tokens))
            return tokens["access_token"]

        service = _service(lambda *args: {}, accounts=accounts)
        with patch("services.account_relogin_service.resolve_outlook_mailbox", side_effect=resolve), patch(
            "services.account_relogin_service._default_login", return_value={"access_token": "at-a2"},
        ) as login_mock, patch("services.account_relogin_service._apply_tokens", side_effect=apply):
            result = service.reactivate(["A@example.test", "a@example.test", "b@example.test", "missing@example.test"])

        self.assertEqual((result["ok"], result["fail"]), (1, 2))
        self.assertEqual([item["email"] for item in result["results"]], [
            "a@example.test", "b@example.test", "missing@example.test",
        ])
        self.assertEqual(login_mock.call_count, 1)
        self.assertEqual(applied, [("at-a", {"access_token": "at-a2"})])
        self.assertIn("Outlook", result["results"][1]["error"])
        self.assertIn("没有这个邮箱", result["results"][2]["error"])

    def test_login_without_access_token_is_a_failure(self) -> None:
        accounts = FakeAccounts()
        accounts.items = {"at-a": {"email": "a@example.test", "access_token": "at-a"}}
        service = _service(lambda *args: {}, accounts=accounts)
        with patch("services.account_relogin_service.resolve_outlook_mailbox", return_value={"email": "a"}), patch(
            "services.account_relogin_service._default_login", return_value={},
        ), patch("services.account_relogin_service._apply_tokens") as apply_mock:
            result = service.reactivate(["a@example.test"])
        self.assertEqual(result["fail"], 1)
        apply_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
