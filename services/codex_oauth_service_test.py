import time
import unittest
from unittest.mock import patch

from services import codex_oauth_service as module
from services.codex_oauth_service import CodexOAuthService
from services.oauth_login_service import OAuthLoginError

_TOKENS = {"access_token": "cx-at", "refresh_token": "cx-rt", "id_token": "cx-id"}


def _claims_for(token: str) -> dict:
    if token == "cx-id":
        return {"email": "A@Example.test"}
    if token == "cx-at":
        return {"exp": 2_000_000_000, "https://api.openai.com/auth": {
            "chatgpt_account_id": "chatgpt-1", "chatgpt_plan_type": "plus"}}
    return {}


class CodexOAuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(module, "account_service")
        self.accounts = patcher.start()
        self.addCleanup(patcher.stop)
        self.accounts._decode_jwt_payload.side_effect = _claims_for

    def test_start_builds_codex_cli_authorize_url(self) -> None:
        started = CodexOAuthService().start("a@example.test")
        self.assertIn("client_id=app_EMoamEEZ73f0CkXaXp7hrann", started["authorize_url"])
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback", started["authorize_url"])
        self.assertIn("code_challenge_method=S256", started["authorize_url"])
        self.assertIn("login_hint=a%40example.test", started["authorize_url"])

    def test_bind_to_explicit_account_stores_codex_fields_only(self) -> None:
        self.accounts.get_account_by_id.return_value = {
            "access_token": "web-at", "management_id": "acct_x", "email": "a@example.test"}
        service = CodexOAuthService()
        with patch.object(service, "finish", return_value=dict(_TOKENS)):
            result = service.bind("sid", "cb", "acct_x")
        token, fields = self.accounts.update_account.call_args.args
        self.assertEqual(token, "web-at")
        self.assertEqual(set(fields), {
            "codex_access_token", "codex_refresh_token", "codex_account_id", "codex_plan_type",
            "codex_token_expires_at", "codex_bound_at"})
        self.assertEqual((fields["codex_access_token"], fields["codex_account_id"], fields["codex_plan_type"]),
                         ("cx-at", "chatgpt-1", "plus"))
        self.assertNotIn("access_token", fields)
        self.assertNotIn("refresh_token", fields)
        self.assertEqual(result["id"], "acct_x")
        self.assertNotIn("cx-at", str(result))
        self.assertNotIn("cx-rt", str(result))

    def test_bind_rejects_email_mismatch_and_unknown_account(self) -> None:
        service = CodexOAuthService()
        self.accounts.get_account_by_id.return_value = {"access_token": "w", "email": "other@example.test"}
        with patch.object(service, "finish", return_value=dict(_TOKENS)), self.assertRaises(OAuthLoginError):
            service.bind("sid", "cb", "acct_x")
        self.accounts.get_account_by_id.return_value = None
        with patch.object(service, "finish", return_value=dict(_TOKENS)), self.assertRaises(OAuthLoginError):
            service.bind("sid", "cb", "acct_x")
        self.accounts.update_account.assert_not_called()

    def test_bind_without_account_id_matches_by_email(self) -> None:
        self.accounts.list_accounts.return_value = [
            {"access_token": "other", "email": "z@example.test"},
            {"access_token": "web-at", "email": "a@example.test", "management_id": "acct_x"},
        ]
        service = CodexOAuthService()
        with patch.object(service, "finish", return_value=dict(_TOKENS)):
            service.bind("sid", "cb")
        self.assertEqual(self.accounts.update_account.call_args.args[0], "web-at")
        self.accounts.list_accounts.return_value = []
        with patch.object(service, "finish", return_value=dict(_TOKENS)), self.assertRaises(OAuthLoginError):
            service.bind("sid", "cb")

    def test_ensure_fresh_returns_none_when_not_bound(self) -> None:
        self.accounts.get_account.return_value = {"access_token": "web-at"}
        self.assertIsNone(CodexOAuthService().ensure_fresh("web-at"))

    def test_ensure_fresh_uses_current_codex_token_without_refreshing(self) -> None:
        self.accounts.get_account.return_value = {
            "access_token": "web-at", "codex_access_token": "cx-at", "codex_account_id": "chatgpt-1",
            "codex_token_expires_at": int(time.time()) + 3600}
        with patch.object(module, "_post_token") as post:
            self.assertEqual(CodexOAuthService().ensure_fresh("web-at"), ("cx-at", "chatgpt-1"))
        post.assert_not_called()

    def test_ensure_fresh_refreshes_expiring_token_and_persists_rotation(self) -> None:
        self.accounts.get_account.return_value = {
            "access_token": "web-at", "email": "a@example.test", "codex_access_token": "old",
            "codex_refresh_token": "old-rt", "codex_account_id": "chatgpt-1",
            "codex_token_expires_at": int(time.time()) + 30}
        with patch.object(module, "_post_token", return_value={"access_token": "cx-at", "refresh_token": "", "id_token": ""}) as post:
            creds = CodexOAuthService().ensure_fresh("web-at")
        self.assertEqual(creds[0], "cx-at")
        self.assertEqual(post.call_args.args[0]["grant_type"], "refresh_token")
        persisted = self.accounts.update_account.call_args.args[1]
        self.assertEqual(persisted["codex_refresh_token"], "old-rt")
        self.assertEqual(persisted["codex_access_token"], "cx-at")

    def test_expired_without_refresh_token_asks_for_new_login(self) -> None:
        self.accounts.get_account.return_value = {
            "access_token": "web-at", "codex_access_token": "old", "codex_token_expires_at": int(time.time()) - 5}
        with self.assertRaises(OAuthLoginError):
            CodexOAuthService().ensure_fresh("web-at")


class BackendCodexCredentialTests(unittest.TestCase):
    def test_codex_headers_use_bound_token(self) -> None:
        from services.openai_backend_api import OpenAIBackendAPI
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "web-at"
        backend.session_id = "sess"
        with patch("services.codex_oauth_service.codex_oauth_service.ensure_fresh", return_value=("cx-at", "chatgpt-1")):
            headers = backend._codex_responses_headers()
        self.assertEqual(headers["Authorization"], "Bearer cx-at")
        self.assertEqual(headers["chatgpt-account-id"], "chatgpt-1")
        self.assertEqual(headers["originator"], "codex_cli_rs")

    def test_codex_headers_fall_back_to_account_token(self) -> None:
        from services.openai_backend_api import OpenAIBackendAPI
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "codex-source-at"
        with patch("services.codex_oauth_service.codex_oauth_service.ensure_fresh", return_value=None):
            headers = backend._codex_responses_headers()
        self.assertEqual(headers, {"Authorization": "Bearer codex-source-at", "Content-Type": "application/json"})

    def test_bound_account_passes_codex_source_check(self) -> None:
        from services.openai_backend_api import OpenAIBackendAPI
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "web-at"
        with patch("services.openai_backend_api.account_service") as accounts:
            accounts.get_account.return_value = {"source_type": "web", "codex_access_token": "cx"}
            backend._ensure_codex_source_account()
            accounts.get_account.return_value = {"source_type": "web"}
            with self.assertRaises(RuntimeError):
                backend._ensure_codex_source_account()


if __name__ == "__main__":
    unittest.main()
