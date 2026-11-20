import base64
import json
import unittest
from unittest.mock import patch

from services.account_service import AccountService


def _jwt(user_id: str, account_id: str = "ws-1", iat: int = 1_700_000_000, salt: str = "") -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    claims = {
        "iat": iat,
        "exp": iat + 864000,
        "https://api.openai.com/auth": {"chatgpt_user_id": user_id, "chatgpt_account_id": account_id},
        "salt": salt,
    }
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = [dict(item) for item in accounts]


def _make_service(accounts: list[dict]) -> tuple[AccountService, _MemoryStorage]:
    storage = _MemoryStorage(accounts)
    with patch.object(AccountService, "_load_cumulative_total", return_value=0), \
            patch.object(AccountService, "_save_cumulative_total", return_value=None), \
            patch("services.account_service.log_service"):
        service = AccountService(storage)
    return service, storage


class AccountIdentityTests(unittest.TestCase):
    def test_identity_includes_workspace(self) -> None:
        self.assertEqual(AccountService.account_identity(_jwt("u1", "ws-a")), "u1|ws-a")
        self.assertNotEqual(
            AccountService.account_identity(_jwt("u1", "ws-a")),
            AccountService.account_identity(_jwt("u1", "ws-b")),
        )

    def test_non_jwt_has_no_identity(self) -> None:
        self.assertEqual(AccountService.account_identity("opaque-token"), "")


class ImportDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("services.account_service.log_service")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reimport_with_newer_tokens_merges_into_existing_record(self) -> None:
        old = _jwt("u1", iat=1_700_000_000)
        new = _jwt("u1", iat=1_700_100_000)
        service, _ = _make_service([])
        service.add_account_items([{"access_token": old, "refresh_token": "rt-old", "email": "a@x.com", "success": 3}])
        management_id = service.get_account(old)["management_id"]

        result = service.add_account_items([
            {"access_token": new, "refresh_token": "rt-new", "password": "pw", "totp_secret": "S"},
        ], return_items=False)

        self.assertEqual((result["added"], result["merged"]), (0, 1))
        accounts = service.list_accounts()
        self.assertEqual(len(accounts), 1)
        merged = accounts[0]
        self.assertEqual(merged["access_token"], new)
        self.assertEqual(merged["refresh_token"], "rt-new")
        self.assertEqual(merged["password"], "pw")
        self.assertEqual(merged["email"], "a@x.com")
        self.assertEqual(merged["success"], 3)
        self.assertEqual(merged["management_id"], management_id)
        # 旧令牌仍能解析到合并后的记录（在途请求不受影响）。
        self.assertEqual(service.resolve_access_token(old), new)

    def test_reimport_with_older_tokens_keeps_newer_credentials_and_fills_password(self) -> None:
        old = _jwt("u1", iat=1_700_000_000)
        new = _jwt("u1", iat=1_700_100_000)
        service, _ = _make_service([])
        service.add_account_items([{"access_token": new, "refresh_token": "rt-new"}])
        service.add_account_items([{"access_token": old, "refresh_token": "rt-old", "password": "pw"}])

        accounts = service.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["access_token"], new)
        self.assertEqual(accounts[0]["refresh_token"], "rt-new")
        self.assertEqual(accounts[0]["password"], "pw")

    def test_newer_access_token_without_refresh_token_keeps_existing_rt(self) -> None:
        old = _jwt("u1", iat=1_700_000_000)
        new = _jwt("u1", iat=1_700_100_000)
        service, _ = _make_service([])
        service.add_account_items([{"access_token": old, "refresh_token": "rt-old"}])
        service.add_accounts([new])

        accounts = service.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["access_token"], new)
        self.assertEqual(accounts[0]["refresh_token"], "rt-old")

    def test_different_workspaces_are_separate_accounts(self) -> None:
        service, _ = _make_service([])
        result = service.add_accounts([_jwt("u1", "personal"), _jwt("u1", "team")], return_items=False)
        self.assertEqual((result["added"], result["merged"]), (2, 0))
        self.assertEqual(len(service.list_accounts()), 2)

    def test_same_account_twice_in_one_batch_is_merged(self) -> None:
        service, _ = _make_service([])
        result = service.add_accounts(
            [_jwt("u1", iat=1_700_000_000), _jwt("u1", iat=1_700_100_000)], return_items=False,
        )
        self.assertEqual((result["added"], result["merged"]), (1, 1))
        self.assertEqual(len(service.list_accounts()), 1)

    def test_opaque_tokens_are_not_merged(self) -> None:
        service, _ = _make_service([])
        result = service.add_accounts(["tok-a", "tok-b"], return_items=False)
        self.assertEqual(result["added"], 2)


class StartupMergeTests(unittest.TestCase):
    def test_existing_duplicates_are_merged_on_load(self) -> None:
        token_only = _jwt("u1", iat=1_700_000_000, salt="a")
        with_password = _jwt("u1", iat=1_700_100_000, salt="b")
        other = _jwt("u2")
        service, storage = _make_service([
            {"access_token": token_only, "created_at": "2026-10-21T14:35:00+00:00", "success": 2, "fail": 1},
            {"access_token": with_password, "created_at": "2026-10-22T02:52:00+00:00", "password": "pw",
             "totp_secret": "S", "refresh_token": "rt", "success": 5},
            {"access_token": other},
        ])
        accounts = {item["access_token"]: item for item in service.list_accounts()}
        self.assertEqual(set(accounts), {with_password, other})
        merged = accounts[with_password]
        self.assertEqual(merged["password"], "pw")
        self.assertEqual((merged["success"], merged["fail"]), (7, 1))
        self.assertEqual(merged["created_at"], "2026-10-21T14:35:00+00:00")
        self.assertEqual(len(storage.accounts), 2)

    def test_no_duplicates_no_save(self) -> None:
        service, _ = _make_service([{"access_token": _jwt("u1")}, {"access_token": _jwt("u2")}])
        self.assertEqual(service.merge_duplicate_accounts(), {"groups": 0, "removed": 0})


if __name__ == "__main__":
    unittest.main()
