import json
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_api
from services.account_service import AccountService
from services.oauth_login_service import OAuthLoginError
from services.remote_import_job_status import RemoteImportConflictError


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _service(accounts: list[dict]) -> AccountService:
    with patch.object(AccountService, "_load_cumulative_total", return_value=0), \
            patch.object(AccountService, "_save_cumulative_total", return_value=None), \
            patch("services.account_service.log_service"):
        return AccountService(_MemoryStorage(accounts))


class _ApiCase(unittest.TestCase):
    ACCOUNTS: list[dict] = []

    def setUp(self) -> None:
        self.service = _service([dict(item) for item in self.ACCOUNTS])
        for target in (
            patch.object(accounts_api, "account_service", self.service),
            patch.object(accounts_api, "require_admin"),
            patch("services.account_service.log_service"),
            patch.object(accounts_api, "log_service"),
        ):
            target.start()
            self.addCleanup(target.stop)
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)

    def id_of(self, token: str) -> str:
        return self.service.get_account(token)["management_id"]


class AccountIdListTest(_ApiCase):
    ACCOUNTS = [
        {"access_token": "tok-a", "refresh_token": "rt-a", "codex_access_token": "cx-a", "codex_refresh_token": "cr-a",
         "email": "a@example.test"},
        {"access_token": "tok-b", "email": "b@example.test", "group_id": "g1"},
    ]

    def test_list_exposes_id_without_extra_secrets(self) -> None:
        body = self.client.get("/api/accounts").json()
        by_token = {item["access_token"]: item for item in body["items"]}
        item = by_token["tok-a"]
        self.assertEqual(item["id"], self.id_of("tok-a"))
        self.assertNotIn("management_id", item)
        self.assertTrue(item["has_refresh_token"])
        self.assertTrue(item["codex_bound"])
        for key in ("refresh_token", "codex_access_token", "codex_refresh_token"):
            self.assertNotIn(key, item)
        self.assertFalse(by_token["tok-b"]["has_refresh_token"])
        self.assertFalse(by_token["tok-b"]["codex_bound"])

    def test_keyword_search_matches_id(self) -> None:
        account_id = self.id_of("tok-b")
        body = self.client.get("/api/accounts", params={"keyword": account_id}).json()
        self.assertEqual([item["access_token"] for item in body["items"]], ["tok-b"])


class AccountUpdateByIdTest(_ApiCase):
    ACCOUNTS = [
        {"access_token": "tok-a", "refresh_token": "rt-a", "quota": 1},
        {"access_token": "tok-b"},
    ]

    def test_update_by_id_changes_credentials_and_keeps_id(self) -> None:
        account_id = self.id_of("tok-a")
        response = self.client.post("/api/accounts/update", json={
            "id": account_id, "access_token": "tok-new", "refresh_token": "rt-new", "quota": 5,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["item"]["id"], account_id)
        self.assertEqual(body["item"]["access_token"], "tok-new")
        self.assertEqual(body["item"]["quota"], 5)
        self.assertNotIn("refresh_token", body["item"])
        self.assertEqual(self.service.get_account_by_id(account_id)["refresh_token"], "rt-new")

    def test_blank_credentials_do_not_overwrite(self) -> None:
        account_id = self.id_of("tok-a")
        response = self.client.post("/api/accounts/update", json={
            "id": account_id, "access_token": "", "refresh_token": "  ", "quota": 9,
        })
        self.assertEqual(response.status_code, 200)
        stored = self.service.get_account_by_id(account_id)
        self.assertEqual((stored["access_token"], stored["refresh_token"], stored["quota"]), ("tok-a", "rt-a", 9))

    def test_credentials_only_update_is_allowed_and_nothing_to_change_is_rejected(self) -> None:
        account_id = self.id_of("tok-a")
        ok = self.client.post("/api/accounts/update", json={"id": account_id, "refresh_token": "rt-2"})
        empty = self.client.post("/api/accounts/update", json={"id": account_id})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(empty.status_code, 400)

    def test_access_token_owned_by_other_account_conflicts(self) -> None:
        response = self.client.post("/api/accounts/update", json={"id": self.id_of("tok-a"), "access_token": "tok-b"})
        self.assertEqual(response.status_code, 409)

    def test_unknown_id_is_404(self) -> None:
        response = self.client.post("/api/accounts/update", json={"id": "acct_nope", "quota": 1})
        self.assertEqual(response.status_code, 404)

    def test_legacy_access_token_locator_still_works(self) -> None:
        response = self.client.post("/api/accounts/update", json={"access_token": "tok-a", "quota": 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_account("tok-a")["quota"], 7)
        missing = self.client.post("/api/accounts/update", json={"access_token": "nope", "quota": 7})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.client.post("/api/accounts/update", json={"quota": 7}).status_code, 400)

    def test_token_rotation_does_not_break_id_updates(self) -> None:
        account_id = self.id_of("tok-a")
        with patch("services.account_service.log_service"):
            self.service._apply_refreshed_tokens("tok-a", {"access_token": "tok-rotated"}, "test")
        response = self.client.post("/api/accounts/update", json={"id": account_id, "quota": 11})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_account("tok-rotated")["quota"], 11)


class AccountBatchByIdTest(_ApiCase):
    ACCOUNTS = [
        {"access_token": "tok-a", "email": "a@example.test", "group_id": "g1"},
        {"access_token": "tok-b", "email": "b@example.test", "group_id": "g1"},
        {"access_token": "tok-c", "email": "c@example.test", "status": "禁用"},
    ]

    def test_delete_by_ids_and_legacy_tokens(self) -> None:
        by_id = self.client.request("DELETE", "/api/accounts", json={"account_ids": [self.id_of("tok-a"), "acct_nope"]})
        self.assertEqual(by_id.status_code, 200)
        self.assertEqual(by_id.json()["removed"], 1)
        self.assertEqual(by_id.json()["errors"][0]["id"], "acct_nope")
        legacy = self.client.request("DELETE", "/api/accounts", json={"tokens": ["tok-b"]})
        self.assertEqual(legacy.json()["removed"], 1)
        self.assertEqual([item["access_token"] for item in self.service.list_accounts()], ["tok-c"])
        self.assertEqual(self.client.request("DELETE", "/api/accounts", json={}).status_code, 400)

    def test_selection_preview_filter_mode_crosses_pages_and_applies_exclusions(self) -> None:
        excluded = self.id_of("tok-b")
        response = self.client.post("/api/accounts/selection-preview", json={"selection": {
            "mode": "filter", "group_id": "g1", "excluded_account_ids": [excluded, "acct_unrelated"],
        }})
        body = response.json()
        self.assertEqual(body["matching_count"], 2)
        self.assertEqual(body["selected_count"], 1)
        self.assertEqual(body["excluded_account_ids"], [excluded])
        self.assertEqual(body["account_ids"], [self.id_of("tok-a")])

    def test_selection_preview_explicit_reports_missing(self) -> None:
        body = self.client.post("/api/accounts/selection-preview", json={"selection": {
            "mode": "explicit", "account_ids": [self.id_of("tok-a"), "acct_nope"],
        }}).json()
        self.assertEqual(body["selected_count"], 1)
        self.assertEqual(body["errors"][0]["code"], "account_not_found")

    def test_selection_preview_all_and_keyword_filter(self) -> None:
        everything = self.client.post("/api/accounts/selection-preview", json={"selection": {"mode": "all"}}).json()
        self.assertEqual(everything["selected_count"], 3)
        keyword = self.client.post("/api/accounts/selection-preview", json={"selection": {
            "mode": "filter", "keyword": "c@example",
        }}).json()
        self.assertEqual(keyword["account_ids"], [self.id_of("tok-c")])

    def test_batch_update_with_filter_selection(self) -> None:
        response = self.client.post("/api/accounts/batch-update", json={
            "selection": {"mode": "filter", "group_id": "g1", "excluded_account_ids": [self.id_of("tok-a")]},
            "status": "禁用",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)
        self.assertEqual(self.service.get_account("tok-b")["status"], "禁用")
        self.assertEqual(self.service.get_account("tok-a")["status"], "正常")

    def test_batch_update_legacy_tokens_and_empty(self) -> None:
        legacy = self.client.post("/api/accounts/batch-update", json={"access_tokens": ["tok-a", "nope"], "status": "禁用"})
        self.assertEqual(legacy.json()["updated"], 1)
        self.assertEqual(len(legacy.json()["errors"]), 1)
        self.assertEqual(self.client.post("/api/accounts/batch-update", json={"status": "禁用"}).status_code, 400)

    def test_group_bind_by_ids(self) -> None:
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": [{"id": "g2"}]}):
            response = self.client.post("/api/accounts/group", json={
                "account_ids": [self.id_of("tok-c")], "group_id": "g2",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_account("tok-c")["group_id"], "g2")

    def test_clear_auto_pause_by_selection(self) -> None:
        with patch.object(self.service, "clear_auto_pause", side_effect=lambda token: {"access_token": token}) as clear:
            response = self.client.post("/api/accounts/clear-auto-pause", json={
                "selection": {"mode": "filter", "group_id": "g1"},
            })
        self.assertEqual(response.json()["cleared"], 2)
        self.assertEqual(clear.call_count, 2)

    def test_import_cleanup_by_ids(self) -> None:
        response = self.client.post("/api/accounts/import-cleanup", json={
            "account_ids": [self.id_of("tok-a")], "remove": True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checked"], 1)


class AccountCredentialViewTest(_ApiCase):
    ACCOUNTS = [{"access_token": "tok-a", "refresh_token": "rt-a"}, {"access_token": "tok-b"}]

    def test_returns_credentials_without_caching(self) -> None:
        account_id = self.id_of("tok-a")
        access = self.client.get(f"/api/accounts/{account_id}/access-token")
        refresh = self.client.get(f"/api/accounts/{account_id}/refresh-token")
        self.assertEqual(access.json(), {"access_token": "tok-a"})
        self.assertEqual(refresh.json(), {"refresh_token": "rt-a"})
        self.assertEqual(access.headers["cache-control"], "no-store")
        self.assertEqual(refresh.headers["cache-control"], "no-store")

    def test_missing_account_or_credential_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/accounts/acct_nope/access-token").status_code, 404)
        self.assertEqual(self.client.get(f"/api/accounts/{self.id_of('tok-b')}/refresh-token").status_code, 404)

    def test_credential_view_is_audited_without_the_secret(self) -> None:
        self.client.get(f"/api/accounts/{self.id_of('tok-a')}/refresh-token")
        logged = json.dumps(accounts_api.log_service.add.call_args_list, default=str)
        self.assertIn("refresh_token", logged)
        self.assertNotIn("rt-a", logged)


class AccountCredentialAdminOnlyTest(unittest.TestCase):
    def test_requires_admin_without_patching(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        client = TestClient(app)
        for path in ("/api/accounts/acct_x/access-token", "/api/accounts/acct_x/refresh-token"):
            self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(
                client.get(path, headers={"Authorization": "Bearer definitely-wrong"}).status_code, 401,
            )
        self.assertEqual(client.post("/api/accounts/selection-preview", json={"selection": {}}).status_code, 401)
        self.assertEqual(client.post("/api/accounts/codex-oauth/start", json={}).status_code, 401)


class AccountRealTestApiTest(_ApiCase):
    ACCOUNTS = [{"access_token": "tok-a", "email": "a@example.test"}]

    def test_chat_mode_runs_capability_test_by_id(self) -> None:
        result = {"status": "success", "mode": "chat", "content": "pong", "quota_deducted": False}
        with patch.object(accounts_api.account_test_service, "execute", return_value=result) as execute:
            response = self.client.post("/api/accounts/test", json={
                "id": self.id_of("tok-a"), "mode": "chat", "model": "gpt-5", "prompt": "ping",
            })
        self.assertEqual(response.json(), result)
        payload = execute.call_args.args[0]
        self.assertEqual((payload["account_id"], payload["mode"], payload["model"], payload["prompt"]),
                         (self.id_of("tok-a"), "chat", "gpt-5", "ping"))

    def test_path_variant_and_image_mode(self) -> None:
        with patch.object(accounts_api.account_test_service, "execute", return_value={"status": "success"}) as execute:
            response = self.client.post(f"/api/accounts/{self.id_of('tok-a')}/test", json={
                "mode": "image", "model": "gpt-image-1", "prompt": "cat",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(execute.call_args.args[0]["mode"], "image")

    def test_real_modes_require_model_and_prompt(self) -> None:
        response = self.client.post("/api/accounts/test", json={"id": self.id_of("tok-a"), "mode": "chat"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_account_and_missing_locator(self) -> None:
        self.assertEqual(self.client.post("/api/accounts/test", json={"id": "acct_nope", "mode": "chat",
                                                                       "model": "m", "prompt": "p"}).status_code, 404)
        self.assertEqual(self.client.post("/api/accounts/test", json={"mode": "chat"}).status_code, 400)

    def test_probe_mode_by_id_keeps_probe_behaviour(self) -> None:
        account = self.service.get_account("tok-a")
        with patch.object(self.service, "fetch_remote_info", return_value={**account, "quota": 4}) as fetch:
            body = self.client.post("/api/accounts/test", json={"id": self.id_of("tok-a")}).json()
        self.assertTrue(body["ok"])
        fetch.assert_called_once_with("tok-a", "account_batch_test", remove_invalid=False)


class AccountRefreshAccessTokenTest(_ApiCase):
    ACCOUNTS = [
        {"access_token": "tok-a", "refresh_token": "rt-a", "email": "a@example.test"},
        {"access_token": "tok-web", "refresh_token": "rt-w", "source_type": "chatgpt_web"},
        {"access_token": "tok-norefresh"},
    ]

    def test_force_refresh_reports_refreshed_skipped_and_errors(self) -> None:
        def fake_refresh(token: str, *, force: bool = False, event: str = "") -> str:
            self.assertTrue(force)
            self.service.update_account(token, {"last_token_refresh_at": "2026-01-01T00:00:00+00:00"}, quiet=True)
            return token

        with patch.object(self.service, "refresh_access_token", side_effect=fake_refresh):
            result = accounts_api._force_refresh_access_tokens(
                ["tok-a", "tok-web", "tok-norefresh", "tok-missing"], "progress-1",
            )
        self.assertEqual((result["refreshed"], result["skipped"]), (1, 2))
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["error"], "account not found")
        progress = self.service.get_refresh_progress("progress-1")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["processed"], 4)

    def test_failed_refresh_error_is_redacted(self) -> None:
        def failing(token: str, **_kwargs) -> str:
            self.service.update_account(
                token, {"last_token_refresh_error": "bad rt-a for tok-a"}, quiet=True,
            )
            return token

        with patch.object(self.service, "refresh_access_token", side_effect=failing):
            result = accounts_api._force_refresh_access_tokens(["tok-a"], "progress-2")
        self.assertEqual(result["refreshed"], 0)
        self.assertNotIn("rt-a", result["errors"][0]["error"])
        self.assertNotIn("tok-a", result["errors"][0]["error"])

    def test_endpoints_validate_targets_and_return_progress(self) -> None:
        empty = self.client.post("/api/accounts/refresh-access-token", json={})
        self.assertEqual(empty.status_code, 400)
        with patch.object(accounts_api, "_force_refresh_access_tokens", return_value={}):
            started = self.client.post("/api/accounts/refresh-access-token", json={"account_ids": [self.id_of("tok-a")]})
        self.assertEqual(started.status_code, 200)
        self.assertTrue(started.json()["progress_id"])
        self.assertEqual(started.json()["target_ids"], [self.id_of("tok-a")])
        self.assertEqual(self.service.get_refresh_progress(started.json()["progress_id"])["total"], 1)

    def test_sync_is_an_alias_of_refresh(self) -> None:
        with patch.object(self.service, "refresh_accounts", return_value={}) as refresh:
            response = self.client.post("/api/accounts/sync", json={"selection": {"mode": "all"}})
            legacy = self.client.post("/api/accounts/refresh", json={"access_tokens": ["tok-a"]})
            # 刷新在后台任务里跑：必须在 patch 仍然生效时等它们都执行完，
            # 否则 with 退出后模拟被撤掉，后台任务会改调真实函数（机器繁忙时偶发失败）。
            deadline = time.monotonic() + 5
            while len(refresh.call_args_list) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(legacy.status_code, 200)
        calls = sorted(refresh.call_args_list, key=lambda call: len(call.args[0]), reverse=True)
        self.assertEqual(len(calls[0].args[0]), 3)
        self.assertEqual(calls[1].args[0], ["tok-a"])


class AccountExportBackupRestoreTest(_ApiCase):
    ACCOUNTS = [
        {"access_token": "tok-a", "refresh_token": "rt-a", "group_id": "g1", "success": 4},
        {"access_token": "tok-b"},
    ]

    def test_txt_export_lists_selected_access_tokens(self) -> None:
        response = self.client.post("/api/accounts/export", json={
            "format": "txt", "account_ids": [self.id_of("tok-b")],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "tok-b\n")
        self.assertTrue(response.headers["content-disposition"].endswith('.txt"'))
        everything = self.client.post("/api/accounts/export", json={"format": "txt"})
        self.assertEqual(everything.text.split(), ["tok-a", "tok-b"])

    def test_export_with_unknown_id_is_rejected(self) -> None:
        response = self.client.post("/api/accounts/export", json={"format": "txt", "account_ids": ["acct_nope"]})
        self.assertEqual(response.status_code, 400)

    def test_backup_round_trips_through_restore_on_a_fresh_instance(self) -> None:
        exported = self.client.post("/api/accounts/export", json={"format": "backup"})
        self.assertEqual(exported.headers["cache-control"], "no-store")
        payload = exported.json()
        self.assertEqual(payload["format"], accounts_api.BACKUP_FORMAT)
        self.assertEqual({item["access_token"] for item in payload["accounts"]}, {"tok-a", "tok-b"})
        original_id = self.id_of("tok-a")

        fresh = _service([])
        with patch.object(accounts_api, "account_service", fresh):
            restored = self.client.post("/api/accounts", json={
                "accounts": payload["accounts"], "restore": True, "refresh": False, "return_items": False,
            })
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["added"], 2)
        copy = fresh.get_account("tok-a")
        self.assertEqual(copy["management_id"], original_id)
        self.assertEqual((copy["refresh_token"], copy["group_id"], copy["success"]), ("rt-a", "g1", 4))

    def test_plain_import_never_trusts_foreign_ids(self) -> None:
        foreign = "acct_" + "d" * 24
        fresh = _service([])
        with patch.object(accounts_api, "account_service", fresh):
            self.client.post("/api/accounts", json={
                "accounts": [{"access_token": "tok-x", "management_id": foreign}], "refresh": False,
            })
        self.assertNotEqual(fresh.get_account("tok-x")["management_id"], foreign)


class AccountTargetGroupTest(_ApiCase):
    ACCOUNTS = []

    def test_create_assigns_target_group(self) -> None:
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": [{"id": "team-a"}]}):
            response = self.client.post("/api/accounts", json={
                "tokens": ["tok-1"], "accounts": [{"access_token": "tok-2"}],
                "refresh": False, "target_group_id": "Team-A",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_account("tok-1")["group_id"], "team-a")
        self.assertEqual(self.service.get_account("tok-2")["group_id"], "team-a")

    def test_unknown_group_is_rejected_and_none_leaves_group_alone(self) -> None:
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": []}):
            bad = self.client.post("/api/accounts", json={"tokens": ["t"], "refresh": False, "target_group_id": "nope"})
        self.assertEqual(bad.status_code, 400)
        self.assertIsNone(self.service.get_account("t"))
        self.client.post("/api/accounts", json={"tokens": ["t"], "refresh": False})
        self.assertEqual(self.service.get_account("t").get("group_id"), None)

    def test_empty_target_group_means_ungrouped(self) -> None:
        self.client.post("/api/accounts", json={"tokens": ["t"], "refresh": False, "target_group_id": ""})
        self.assertEqual(self.service.get_account("t")["group_id"], "")

    def test_verify_on_import_forwards_group(self) -> None:
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": [{"id": "g"}]}), \
                patch.object(accounts_api, "verify_and_import",
                             return_value={"tokens": [], "added": 0, "verified": 0, "rejected": []}) as verify:
            self.client.post("/api/accounts", json={
                "tokens": ["t"], "verify_on_import": True, "target_group_id": "g",
            })
        self.assertEqual(verify.call_args.args[0], [{"access_token": "t", "source_type": "web", "group_id": "g"}])

    def test_cpa_and_sub2api_imports_forward_group_and_map_conflicts(self) -> None:
        pool = {"id": "p1"}
        server = {"id": "s1"}
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": [{"id": "g"}]}), \
                patch.object(accounts_api.cpa_config, "get_pool", return_value=pool), \
                patch.object(accounts_api.sub2api_config, "get_server", return_value=server), \
                patch.object(accounts_api.cpa_import_service, "start_import", return_value={"job_id": "1"}) as cpa, \
                patch.object(accounts_api.sub2api_import_service, "start_import", return_value={"job_id": "2"}) as sub:
            self.client.post("/api/cpa/pools/p1/import", json={"names": ["a"], "target_group_id": "g"})
            self.client.post("/api/sub2api/servers/s1/import", json={"account_ids": ["1"], "target_group_id": "g"})
            self.assertEqual(cpa.call_args.kwargs["target_group_id"], "g")
            self.assertEqual(sub.call_args.kwargs["target_group_id"], "g")
            cpa.side_effect = RemoteImportConflictError("busy")
            sub.side_effect = RemoteImportConflictError("busy")
            self.assertEqual(self.client.post("/api/cpa/pools/p1/import", json={"names": ["a"]}).status_code, 409)
            self.assertEqual(
                self.client.post("/api/sub2api/servers/s1/import", json={"account_ids": ["1"]}).status_code, 409,
            )

    def test_active_remote_import_endpoint(self) -> None:
        active = {"source": "cpa", "id": "p1", "name": "pool", "job": {"status": "running"}}
        with patch.object(accounts_api, "find_active_remote_import", return_value=active):
            self.assertEqual(self.client.get("/api/accounts/remote-imports/active").json(), {"active": active})
        with patch.object(accounts_api, "find_active_remote_import", return_value=None):
            self.assertEqual(self.client.get("/api/accounts/remote-imports/active").json(), {"active": None})


class CodexOAuthApiTest(_ApiCase):
    ACCOUNTS = [{"access_token": "tok-a", "email": "a@example.test"}]

    def test_start_returns_authorize_url(self) -> None:
        started = {"session_id": "s", "authorize_url": "https://auth.example/authorize"}
        with patch.object(accounts_api.codex_oauth_service, "start", return_value=started) as start:
            response = self.client.post("/api/accounts/codex-oauth/start", json={"email_hint": "a@example.test"})
        self.assertEqual(response.json(), started)
        start.assert_called_once_with("a@example.test")

    def test_finish_binds_to_requested_account_and_maps_errors(self) -> None:
        bound = {"id": "acct_x", "email": "a@example.test"}
        with patch.object(accounts_api.codex_oauth_service, "bind", return_value=bound) as bind:
            ok = self.client.post("/api/accounts/codex-oauth/finish", json={
                "session_id": "s", "callback": "http://localhost:1455/auth/callback?code=c", "account_id": "acct_x",
            })
        self.assertEqual(ok.json(), bound)
        bind.assert_called_once_with("s", "http://localhost:1455/auth/callback?code=c", "acct_x")
        with patch.object(accounts_api.codex_oauth_service, "bind", side_effect=OAuthLoginError("state 不匹配")):
            bad = self.client.post("/api/accounts/codex-oauth/finish", json={"session_id": "s", "callback": "x"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["error"], "state 不匹配")


class Sub2APIFileImportTest(_ApiCase):
    ACCOUNTS = []

    DOCUMENT = {
        "exported_at": "2026-01-01T00:00:00Z",
        "proxies": [],
        "accounts": [
            {"name": "one", "platform": "openai", "credentials": {
                "access_token": "at-1", "refresh_token": "rt-1", "id_token": "id-1", "email": "one@example.test",
                "plan_type": "plus"}},
            {"name": "two", "credentials": json.dumps({"accessToken": "at-2"})},
            {"name": "no-token", "credentials": {"foo": "bar"}},
        ],
    }

    def test_imports_nested_credentials_and_skips_records_without_token(self) -> None:
        response = self.client.post("/api/accounts/import-sub2api-file", json={
            "content": json.dumps(self.DOCUMENT), "refresh": False,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["parsed"], body["added"], body["skipped"]), (2, 2, 0))
        first = self.service.get_account("at-1")
        self.assertEqual((first["refresh_token"], first["id_token"], first["email"], first["type"], first["source_type"]),
                         ("rt-1", "id-1", "one@example.test", "plus", "codex"))
        self.assertIsNotNone(self.service.get_account("at-2"))
        self.assertTrue(first["management_id"])

    def test_reimport_is_idempotent_and_target_group_applies(self) -> None:
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": [{"id": "g"}]}):
            for _ in range(2):
                response = self.client.post("/api/accounts/import-sub2api-file", json={
                    "content": json.dumps(self.DOCUMENT), "refresh": False, "target_group_id": "g",
                })
        self.assertEqual((response.json()["added"], response.json()["skipped"]), (0, 2))
        self.assertEqual(self.service.get_account("at-1")["group_id"], "g")

    def test_refresh_runs_for_imported_tokens(self) -> None:
        with patch.object(self.service, "refresh_accounts", return_value={"refreshed": 2, "errors": []}) as refresh:
            body = self.client.post("/api/accounts/import-sub2api-file", json={"content": json.dumps(self.DOCUMENT)}).json()
        self.assertEqual(body["refreshed"], 2)
        self.assertEqual(refresh.call_args.args[0], ["at-1", "at-2"])

    def test_accepts_bare_list_and_single_account(self) -> None:
        listed = self.client.post("/api/accounts/import-sub2api-file", json={
            "content": json.dumps([{"credentials": {"access_token": "at-l"}}]), "refresh": False}).json()
        single = self.client.post("/api/accounts/import-sub2api-file", json={
            "content": json.dumps({"credentials": {"access_token": "at-s"}}), "refresh": False}).json()
        self.assertEqual((listed["added"], single["added"]), (1, 1))

    def test_invalid_documents_are_rejected(self) -> None:
        self.assertEqual(self.client.post("/api/accounts/import-sub2api-file", json={"content": "not json"}).status_code, 400)
        self.assertEqual(self.client.post("/api/accounts/import-sub2api-file", json={"content": "{}"}).status_code, 400)
        with patch.object(accounts_api, "_account_group_payload", return_value={"groups": []}):
            self.assertEqual(self.client.post("/api/accounts/import-sub2api-file", json={
                "content": json.dumps(self.DOCUMENT), "target_group_id": "nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
