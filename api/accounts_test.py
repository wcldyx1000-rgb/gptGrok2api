from __future__ import annotations

import io
import json
import unittest
import zipfile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_api
from api.accounts import _account_for_api, _status_matches_filter


class AccountApiSerializationTest(unittest.TestCase):
    def test_hides_browser_and_oauth_session_material(self) -> None:
        item = _account_for_api({
            "access_token": "displayed-admin-token",
            "cookie_header": "__Secure-next-auth.session-token=secret",
            "session_token": "session-secret",
            "refresh_token": "refresh-secret",
            "id_token": "id-secret",
            "status": "正常",
        })

        self.assertEqual(item["access_token"], "displayed-admin-token")
        self.assertNotIn("cookie_header", item)
        self.assertNotIn("session_token", item)
        self.assertNotIn("refresh_token", item)
        self.assertNotIn("id_token", item)

    def test_valid_checkout_filter_requires_ready_final_url_and_future_expiry(self) -> None:
        base = {
            "checkout_link_status": "ready",
            "checkout_channel": "pix",
            "checkout_final_url": "https://payments.stripe.com/qr/instructions/pix-test",
            "checkout_qr_expires_at": 1_800_000_100,
        }
        self.assertTrue(_status_matches_filter(base, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_qr_expires_at": 1}, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_link_status": "pending"}, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_final_url": "https://example.test"}, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_qr_expires_at": ""}, "valid_checkout"))


class AccountClearAutoPauseApiTest(unittest.TestCase):
    def test_clears_auto_pause_and_hides_secrets(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        client = TestClient(app)
        cleared = {"access_token": "tok-a", "status": "正常", "outlook_refresh_token": "mail-rt", "outlook_password": "pw"}

        def clear(token: str):
            return dict(cleared) if token == "tok-a" else None

        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service, "clear_auto_pause", side_effect=clear,
        ) as clear_mock:
            response = client.post("/api/accounts/clear-auto-pause", json={"access_tokens": ["tok-a", "missing", "tok-a"]})
            empty = client.post("/api/accounts/clear-auto-pause", json={"access_tokens": []})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cleared"], 1)
        self.assertEqual(clear_mock.call_count, 2)
        self.assertNotIn("outlook_refresh_token", body["items"][0])
        self.assertNotIn("outlook_password", body["items"][0])
        self.assertEqual(empty.status_code, 400)


class AccountExportApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)
        self.item = {
            "type": "codex",
            "email": "person@example.test",
            "account_id": "chatgpt-account",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "expired": "2030-01-01T00:00:00+00:00",
            "last_refresh": "2029-01-01T00:00:00+00:00",
        }

    @staticmethod
    def _agent_identity(account_id: str, email: str) -> dict:
        return {
            "auth_mode": "agent_identity",
            "agent_identity": {
                "agent_runtime_id": f"runtime-{account_id}",
                "agent_private_key": f"private-{account_id}",
                "account_id": account_id,
                "chatgpt_user_id": f"user-{account_id}",
                "email": email,
                "plan_type": "free",
                "chatgpt_account_is_fedramp": False,
            },
        }

    def test_sub2api_export_uses_standard_account_envelope(self) -> None:
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "build_export_items",
            return_value=[self.item],
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": ["access-token"], "format": "sub2api"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)
        account = payload["accounts"][0]
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "chatgpt-account")
        self.assertEqual(account["credentials"]["refresh_token"], "refresh-token")

    def test_cpa_export_is_zip_of_codex_auth_files(self) -> None:
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "build_export_items",
            return_value=[self.item],
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": ["access-token"], "format": "cpa"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["person-example.test.json"])
            exported = json.loads(archive.read(archive.namelist()[0]))
        self.assertEqual(exported["type"], "codex")
        self.assertEqual(exported["access_token"], "access-token")

    def test_single_agent_identity_export_is_auth_json(self) -> None:
        auth_json = self._agent_identity("one", "one@example.test")
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "list_accounts",
            return_value=[self.item],
        ), patch.object(
            accounts_api,
            "ensure_openai_agent_identity",
            return_value=auth_json,
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": ["access-token"], "format": "agent_identity"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="auth.json"')
        self.assertEqual(response.json(), auth_json)

    def test_multiple_agent_identity_export_is_zip(self) -> None:
        accounts = [
            {**self.item, "access_token": "token-one", "email": "one@example.test"},
            {**self.item, "access_token": "token-two", "email": "two@example.test"},
        ]
        identities = [
            self._agent_identity("one", "one@example.test"),
            self._agent_identity("two", "two@example.test"),
        ]
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "list_accounts",
            return_value=accounts,
        ), patch.object(
            accounts_api,
            "ensure_openai_agent_identity",
            side_effect=identities,
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": [], "format": "agent_identity"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), [
                "one-example.test/auth.json",
                "two-example.test/auth.json",
            ])
            exported = [json.loads(archive.read(name)) for name in archive.namelist()]
        self.assertEqual(exported, identities)

    def test_agent_identity_list_does_not_include_private_keys(self) -> None:
        summaries = [{
            "account_id": "one",
            "email": "one@example.test",
            "agent_runtime_id": "runtime-one",
            "updated_at": "2030-01-01T00:00:00+00:00",
        }]
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.openai_agent_identity_store,
            "summary",
            return_value=summaries,
        ):
            response = self.client.get("/api/accounts/agent-identities")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total": 1, "items": summaries})
        self.assertNotIn("agent_private_key", response.text)


class AccountHealthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)
        self.accounts = [
            {"access_token": "tok-ok", "status": "正常", "quota": 50, "refresh_token": "rt"},
            {"access_token": "tok-risk", "status": "正常", "quota": 50, "refresh_token": "rt"},
            {"access_token": "tok-off", "status": "禁用", "quota": 50},
        ]
        usage = {"accounts": {"tok-risk": {"forbidden_24h": 1}}, "pool": {}}
        patches = [
            patch.object(accounts_api, "require_admin"),
            patch.object(accounts_api.account_service, "list_accounts", return_value=self.accounts),
            patch.object(accounts_api.account_service, "image_usage_snapshot", return_value=usage),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_account_list_carries_health_and_filters(self) -> None:
        response = self.client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        levels = {item["access_token"]: item["health_level"] for item in response.json()["items"]}
        self.assertEqual(levels, {"tok-ok": "healthy", "tok-risk": "danger", "tok-off": "idle"})
        self.assertNotIn("refresh_token", response.json()["items"][0])

        risky = self.client.get("/api/accounts", params={"health": "risk"}).json()
        self.assertEqual([item["access_token"] for item in risky["items"]], ["tok-risk"])
        self.assertEqual(risky["total"], 1)

    def test_pool_health_endpoint(self) -> None:
        response = self.client.get("/api/accounts/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["pool"]["level"], "danger")
        self.assertEqual(body["counts"], {"healthy": 1, "danger": 1, "idle": 1})

    def test_list_survives_health_evaluation_error(self) -> None:
        with patch.object(accounts_api, "account_health_report", side_effect=RuntimeError("boom")):
            response = self.client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("health_level", response.json()["items"][0])


class AccountTestApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)
        admin = patch.object(accounts_api, "require_admin")
        admin.start()
        self.addCleanup(admin.stop)

    def test_probe_reports_quota_without_secrets(self) -> None:
        account = {"access_token": "tok-a", "status": "正常", "quota": 7, "refresh_token": "rt"}
        with patch.object(accounts_api.account_service, "get_account", return_value=account), patch.object(
            accounts_api.account_service, "fetch_remote_info", return_value=account,
        ) as fetch:
            response = self.client.post("/api/accounts/test", json={"access_token": "tok-a"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "剩余额度 7")
        self.assertNotIn("refresh_token", body["item"])
        fetch.assert_called_once_with("tok-a", "account_batch_test", remove_invalid=False)

    def test_probe_reports_failure(self) -> None:
        with patch.object(accounts_api.account_service, "get_account", return_value={"access_token": "tok-a"}), patch.object(
            accounts_api.account_service, "fetch_remote_info", side_effect=RuntimeError("HTTP 401 token expired"),
        ):
            body = self.client.post("/api/accounts/test", json={"access_token": "tok-a"}).json()
        self.assertFalse(body["ok"])
        self.assertIn("401", body["error"])

    def test_probe_limited_account_is_not_ok(self) -> None:
        account = {"access_token": "tok-a", "status": "限流", "quota": 0}
        with patch.object(accounts_api.account_service, "get_account", return_value=account), patch.object(
            accounts_api.account_service, "fetch_remote_info", return_value=account,
        ):
            body = self.client.post("/api/accounts/test", json={"access_token": "tok-a"}).json()
        self.assertFalse(body["ok"])

    def test_probe_unknown_account(self) -> None:
        with patch.object(accounts_api.account_service, "get_account", return_value=None):
            response = self.client.post("/api/accounts/test", json={"access_token": "missing"})
        self.assertEqual(response.status_code, 404)


class AccountVerifyOnImportApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)

    def test_verify_on_import_returns_rejected_and_hides_secrets(self) -> None:
        stored = {
            "at-ok": {
                "access_token": "at-ok",
                "email": "ok@example.test",
                "status": "正常",
                "refresh_token": "rt-secret",
                "outlook_password": "mail-pw",
            },
        }
        verified = {
            "verified": 2,
            "added": 1,
            "tokens": ["at-ok"],
            "rejected": [{"email": "dead@example.test", "reason": "status_异常", "status": "异常"}],
        }
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api, "verify_and_import", return_value=verified,
        ) as verify_mock, patch.object(
            accounts_api.account_service, "get_account", side_effect=stored.get,
        ), patch.object(accounts_api.account_service, "add_accounts") as add_mock:
            response = self.client.post(
                "/api/accounts",
                json={
                    "tokens": ["at-plain"],
                    "accounts": [{"access_token": "at-ok", "email": "ok@example.test"}],
                    "verify_on_import": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["added"], body["verified"]), (1, 2))
        self.assertEqual(body["rejected"][0]["email"], "dead@example.test")
        add_mock.assert_not_called()
        payloads = verify_mock.call_args.args[0]
        self.assertEqual([item["access_token"] for item in payloads], ["at-ok", "at-plain"])
        self.assertEqual(payloads[1]["source_type"], "web")
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("rt-secret", dumped)
        self.assertNotIn("mail-pw", dumped)

    def test_default_import_does_not_verify(self) -> None:
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api, "verify_and_import",
        ) as verify_mock, patch.object(
            accounts_api.account_service, "add_accounts", return_value={"added": 1, "skipped": 0, "items": []},
        ):
            response = self.client.post("/api/accounts", json={"tokens": ["at"], "refresh": False})
        self.assertEqual(response.status_code, 200)
        verify_mock.assert_not_called()


class LoginImportApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)

    def test_start_get_stop_routes(self) -> None:
        job = {"job_id": "job-1", "status": "running", "results": [], "logs": []}
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.login_import_service, "start", return_value=job,
        ) as start_mock, patch.object(
            accounts_api.login_import_service, "get", return_value=job,
        ) as get_mock, patch.object(
            accounts_api.login_import_service, "stop", return_value={**job, "status": "stopped"},
        ) as stop_mock:
            started = self.client.post(
                "/api/accounts/login-import",
                json={"text": "a@example.test|pw|TOTP", "verify": False},
            )
            fetched = self.client.get("/api/accounts/login-import", params={"job_id": "job-1"})
            stopped = self.client.post("/api/accounts/login-import/stop", json={"job_id": "job-1"})

        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["job"]["job_id"], "job-1")
        start_mock.assert_called_once_with("a@example.test|pw|TOTP", verify=False)
        get_mock.assert_called_once_with("job-1")
        self.assertEqual(fetched.json()["job"]["status"], "running")
        stop_mock.assert_called_once_with("job-1")
        self.assertEqual(stopped.json()["job"]["status"], "stopped")

    def test_start_rejects_unparseable_text(self) -> None:
        with patch.object(accounts_api, "require_admin"):
            response = self.client.post("/api/accounts/login-import", json={"text": "nothing useful"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("邮箱|密码|2FA", json.dumps(response.json(), ensure_ascii=False))

    def test_job_payload_never_contains_password_or_totp(self) -> None:
        from services.login_import_service import LoginImportService

        def login(row, proxy, index):
            raise RuntimeError("login rejected")

        service = LoginImportService(
            login_fn=login,
            pick_proxy_fn=lambda failed: "",
            prepare_fn=lambda: None,
            log_scope_fn=lambda sink: __import__("contextlib").nullcontext(),
            gap_seconds=0,
        )
        service.start("a@example.test|pw-very-secret|TOTPSEED123", verify=False, background=False)
        with patch.object(accounts_api, "require_admin"), patch.object(accounts_api, "login_import_service", service):
            body = self.client.get("/api/accounts/login-import").json()
        self.assertEqual(body["job"]["fail"], 1)
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("pw-very-secret", dumped)
        self.assertNotIn("TOTPSEED123", dumped)

    def test_reactivate_route(self) -> None:
        result = {"ok": 1, "fail": 0, "results": [{"email": "a@example.test", "ok": True, "error": ""}]}
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.login_import_service, "reactivate", return_value=result,
        ) as reactivate_mock:
            response = self.client.post("/api/accounts/reactivate", json={"emails": [" a@example.test ", ""]})
            empty = self.client.post("/api/accounts/reactivate", json={"emails": ["  "]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], 1)
        reactivate_mock.assert_called_once_with(["a@example.test"])
        self.assertEqual(empty.status_code, 400)


if __name__ == "__main__":
    unittest.main()
