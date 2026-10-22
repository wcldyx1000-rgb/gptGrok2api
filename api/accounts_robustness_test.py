"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

账号 API 稳健性：状态枚举、错误不泄露 token 片段、后台任务强引用、缺失账号写进度、
远程导入进行中 409、账号测试写日志、oauth/finish 不打印回调地址。
"""
from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_api
from api.accounts_management_test import _service


class _Case(unittest.TestCase):
    ACCOUNTS = [
        {"access_token": "tok-secret-aaaa", "refresh_token": "rt-a", "email": "a@example.test"},
        {"access_token": "tok-secret-bbbb", "refresh_token": "rt-b", "email": "b@example.test"},
    ]

    def setUp(self) -> None:
        self.service = _service([dict(item) for item in self.ACCOUNTS])
        self.log = MagicMock()
        for target in (
            patch.object(accounts_api, "account_service", self.service),
            patch.object(accounts_api, "require_admin"),
            patch("services.account_service.log_service"),
            patch.object(accounts_api, "log_service", self.log),
        ):
            target.start()
            self.addCleanup(target.stop)
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)

    def id_of(self, token: str) -> str:
        return self.service.get_account(token)["management_id"]


class StatusEnumTest(_Case):
    def test_update_rejects_free_form_status(self) -> None:
        response = self.client.post("/api/accounts/update", json={"id": self.id_of("tok-secret-aaaa"), "status": "banana"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.get_account("tok-secret-aaaa")["status"], "正常")

    def test_update_accepts_the_four_statuses(self) -> None:
        for status in ("限流", "异常", "禁用", "正常"):
            response = self.client.post(
                "/api/accounts/update", json={"id": self.id_of("tok-secret-aaaa"), "status": status},
            )
            self.assertEqual(response.status_code, 200, status)
            self.assertEqual(self.service.get_account("tok-secret-aaaa")["status"], status)

    def test_batch_update_rejects_free_form_status(self) -> None:
        response = self.client.post("/api/accounts/batch-update", json={
            "account_ids": [self.id_of("tok-secret-aaaa")], "status": "whatever",
        })
        self.assertEqual(response.status_code, 422)

    def test_proxy_stays_updatable_for_the_edit_form(self) -> None:
        response = self.client.post("/api/accounts/update", json={
            "id": self.id_of("tok-secret-aaaa"), "proxy": "http://127.0.0.1:7890",
        })
        self.assertEqual(response.status_code, 200)


class ErrorTextTest(_Case):
    def test_batch_errors_do_not_contain_token_fragments(self) -> None:
        with patch.object(self.service, "update_account", return_value=None):
            update = self.client.post("/api/accounts/batch-update", json={
                "access_tokens": ["tok-secret-aaaa"], "status": "禁用",
            })
            group = self.client.post("/api/accounts/group", json={
                "access_tokens": ["tok-secret-bbbb"], "group_id": "__ungrouped__",
            })
        for response in (update, group):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("tok-sec", str(response.json()["errors"]))
            error = response.json()["errors"][0]
            self.assertEqual(error["code"], "account_not_found")

    def test_error_responses_never_carry_account_email(self) -> None:
        response = self.client.post("/api/accounts/update", json={"id": "acct_missing", "status": "禁用"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("example.test", response.text)


class BackgroundOperationTest(_Case):
    def test_tasks_are_strongly_referenced_until_done(self) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()

            async def work() -> None:
                await gate.wait()

            task = accounts_api._schedule_account_operation(work())
            self.assertIn(task, accounts_api._ACCOUNT_OPERATION_TASKS)
            gate.set()
            await task
            await asyncio.sleep(0)
            self.assertNotIn(task, accounts_api._ACCOUNT_OPERATION_TASKS)

        asyncio.run(scenario())

    def test_missing_accounts_are_written_to_progress(self) -> None:
        with patch.object(accounts_api, "_force_refresh_access_tokens", return_value={}):
            response = self.client.post("/api/accounts/refresh-access-token", json={
                "account_ids": [self.id_of("tok-secret-aaaa"), "acct_gone"],
            })
        self.assertEqual(response.status_code, 200)
        progress = self.service.get_refresh_progress(response.json()["progress_id"])
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["processed"], 1)
        failed = [event for event in progress["events"] if event["status"] == "failed"]
        self.assertEqual([event["account_id"] for event in failed], ["acct_gone"])
        self.assertEqual(failed[0]["message"], "账号不存在")

    def test_sync_creates_progress_before_the_worker_runs(self) -> None:
        gate = MagicMock()
        with patch.object(self.service, "refresh_accounts", gate):
            response = self.client.post("/api/accounts/sync", json={
                "account_ids": [self.id_of("tok-secret-aaaa"), "acct_gone"],
            })
        progress = self.service.get_refresh_progress(response.json()["progress_id"])
        self.assertIsNotNone(progress)
        self.assertEqual(progress["total"], 2)
        self.assertEqual(response.json()["errors"][0]["id"], "acct_gone")

    def test_ensure_progress_keeps_existing_events(self) -> None:
        self.service.ensure_refresh_progress("p", 1)
        self.service.report_missing_accounts("p", ["acct_x", "acct_x", ""])
        self.service.ensure_refresh_progress("p", 1)
        progress = self.service.get_refresh_progress("p")
        self.assertEqual((progress["total"], progress["processed"], len(progress["events"])), (2, 1, 1))
        self.service.finish_refresh_progress("p", {})
        self.service.ensure_refresh_progress("p", 5)
        self.assertEqual(self.service.get_refresh_progress("p")["total"], 5)


class RemoteImportGuardTest(_Case):
    RUNNING = {"id": "p1", "name": "pool", "import_job": {"status": "running"}}
    IDLE = {"id": "p1", "name": "pool", "import_job": {"status": "completed"}}

    def test_cpa_pool_update_and_delete_conflict_while_importing(self) -> None:
        with patch.object(accounts_api.cpa_config, "get_pool", return_value=self.RUNNING), \
                patch.object(accounts_api.cpa_config, "update_pool") as update, \
                patch.object(accounts_api.cpa_config, "delete_pool") as delete:
            self.assertEqual(self.client.post("/api/cpa/pools/p1", json={"name": "x"}).status_code, 409)
            self.assertEqual(self.client.delete("/api/cpa/pools/p1").status_code, 409)
        update.assert_not_called()
        delete.assert_not_called()

    def test_cpa_pool_update_and_delete_work_when_idle_or_missing(self) -> None:
        with patch.object(accounts_api.cpa_config, "get_pool", return_value=self.IDLE), \
                patch.object(accounts_api.cpa_config, "update_pool", return_value=dict(self.IDLE, base_url="u", secret_key="s")), \
                patch.object(accounts_api.cpa_config, "delete_pool", return_value=True), \
                patch.object(accounts_api.cpa_config, "list_pools", return_value=[]):
            self.assertEqual(self.client.post("/api/cpa/pools/p1", json={"name": "x"}).status_code, 200)
            self.assertEqual(self.client.delete("/api/cpa/pools/p1").status_code, 200)
        with patch.object(accounts_api.cpa_config, "get_pool", return_value=None), \
                patch.object(accounts_api.cpa_config, "update_pool", return_value=None):
            self.assertEqual(self.client.post("/api/cpa/pools/p1", json={"name": "x"}).status_code, 404)

    def test_sub2api_server_update_and_delete_conflict_while_importing(self) -> None:
        with patch.object(accounts_api.sub2api_config, "get_server", return_value=self.RUNNING), \
                patch.object(accounts_api.sub2api_config, "update_server") as update, \
                patch.object(accounts_api.sub2api_config, "delete_server") as delete:
            self.assertEqual(self.client.post("/api/sub2api/servers/p1", json={"name": "x"}).status_code, 409)
            self.assertEqual(self.client.delete("/api/sub2api/servers/p1").status_code, 409)
        update.assert_not_called()
        delete.assert_not_called()


class AccountTestLogTest(_Case):
    def test_probe_test_writes_account_log_without_secrets(self) -> None:
        result = {"ok": True, "duration_ms": 3, "message": "剩余额度 10", "item": {}}
        with patch.object(accounts_api, "_test_account", return_value=result):
            response = self.client.post(f"/api/accounts/{self.id_of('tok-secret-aaaa')}/test", json={})
        self.assertEqual(response.status_code, 200)
        calls = [call for call in self.log.add.call_args_list if call.args[1] == "账号测试"]
        self.assertEqual(len(calls), 1)
        detail = calls[0].args[2]
        self.assertEqual(detail["id"], self.id_of("tok-secret-aaaa"))
        self.assertTrue(detail["ok"])
        self.assertNotIn("tok-secret", str(detail))
        self.assertNotIn("prompt", detail)

    def test_missing_account_test_is_logged_as_failure(self) -> None:
        response = self.client.post("/api/accounts/acct_missing/test", json={})
        self.assertEqual(response.status_code, 404)
        detail = [call for call in self.log.add.call_args_list if call.args[1] == "账号测试"][0].args[2]
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["status_code"], 404)

    def test_capability_test_is_not_logged_twice(self) -> None:
        result = {"status": "success", "duration_ms": 1}
        with patch.object(accounts_api, "_run_account_capability_test", return_value=result):
            self.client.post(
                f"/api/accounts/{self.id_of('tok-secret-aaaa')}/test",
                json={"mode": "chat", "model": "gpt-5", "prompt": "super secret prompt"},
            )
        self.assertEqual([call for call in self.log.add.call_args_list if call.args[1] == "账号测试"], [])

    def test_probe_error_text_is_scrubbed(self) -> None:
        with patch.object(self.service, "fetch_remote_info", side_effect=RuntimeError("boom tok-secret-aaaa Bearer abc.def")):
            result = accounts_api._test_account("tok-secret-aaaa")
        self.assertNotIn("tok-secret-aaaa", result["error"])
        self.assertNotIn("abc.def", result["error"])


class OAuthFinishLogTest(_Case):
    def test_finish_does_not_print_callback_or_session(self) -> None:
        from services.oauth_login_service import OAuthLoginError

        callback = "http://localhost:1455/auth/callback?code=SECRETCODE123&state=s"
        out = io.StringIO()
        with patch.object(accounts_api.oauth_login_service, "finish", side_effect=OAuthLoginError("state 不匹配")), \
                redirect_stdout(out):
            response = self.client.post("/api/accounts/oauth/finish", json={
                "session_id": "SESSIONID-1234567890", "callback": callback,
            })
        self.assertEqual(response.status_code, 400)
        printed = out.getvalue()
        self.assertNotIn("SECRETCODE123", printed)
        self.assertNotIn("SESSIONID", printed)
        self.assertNotIn("state=", printed)
        self.assertIn("callback_present=True", printed)
        self.assertIn("OAuthLoginError", printed)


if __name__ == "__main__":
    unittest.main()
