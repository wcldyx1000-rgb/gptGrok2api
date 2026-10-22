import unittest
from unittest.mock import patch

from services.account_service import AccountService


class _MemoryStorage:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts
        self.saves = 0

    def load_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts]

    def save_accounts(self, accounts: list[dict]) -> None:
        self.saves += 1
        self.accounts = [dict(item) for item in accounts]


def _make_service(accounts: list[dict]) -> tuple[AccountService, _MemoryStorage]:
    storage = _MemoryStorage(accounts)
    with patch.object(AccountService, "_load_cumulative_total", return_value=0), \
            patch.object(AccountService, "_save_cumulative_total", return_value=None), \
            patch("services.account_service.log_service"):
        service = AccountService(storage)
    return service, storage


class ManagementIdMigrationTests(unittest.TestCase):
    def test_legacy_accounts_get_ids_and_are_persisted_on_first_load(self) -> None:
        service, storage = _make_service([{"access_token": "tok-a"}, {"access_token": "tok-b"}])
        ids = {item["access_token"]: item["management_id"] for item in service.list_accounts()}
        self.assertTrue(all(value.startswith("acct_") and len(value) == 29 for value in ids.values()))
        self.assertEqual(len(set(ids.values())), 2)
        self.assertEqual(storage.saves, 1)
        self.assertEqual({item["access_token"]: item["management_id"] for item in storage.accounts}, ids)

    def test_ids_are_stable_across_reloads_and_not_rewritten(self) -> None:
        _service, storage = _make_service([{"access_token": "tok-a"}])
        first = storage.accounts[0]["management_id"]
        saves = storage.saves
        reloaded, storage2 = _make_service(storage.accounts)
        self.assertEqual(reloaded.get_account("tok-a")["management_id"], first)
        self.assertEqual(storage2.saves, 0)
        self.assertEqual(saves, 1)

    def test_duplicate_ids_on_disk_are_resolved(self) -> None:
        same = "acct_" + "a" * 24
        service, _storage = _make_service([
            {"access_token": "tok-a", "management_id": same},
            {"access_token": "tok-b", "management_id": same},
        ])
        ids = [item["management_id"] for item in service.list_accounts()]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids[0], same)

    def test_invalid_id_is_replaced(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a", "management_id": "bogus"}])
        self.assertTrue(service.get_account("tok-a")["management_id"].startswith("acct_"))


class ManagementIdLookupTests(unittest.TestCase):
    def test_get_by_id_and_resolve_ids(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a"}, {"access_token": "tok-b"}])
        account_id = service.get_account("tok-b")["management_id"]
        self.assertEqual(service.get_account_by_id(account_id.upper())["access_token"], "tok-b")
        self.assertIsNone(service.get_account_by_id("acct_missing"))
        tokens, missing = service.resolve_account_ids([account_id, account_id, "acct_nope", " "])
        self.assertEqual(tokens, ["tok-b"])
        self.assertEqual(missing, ["acct_nope"])

    def test_id_survives_token_rotation_and_old_token_still_resolves(self) -> None:
        service, _storage = _make_service([{"access_token": "old-at", "refresh_token": "rt"}])
        account_id = service.get_account("old-at")["management_id"]
        with patch("services.account_service.log_service"):
            service._apply_refreshed_tokens("old-at", {"access_token": "new-at", "refresh_token": "rt2"}, "test")
        rotated = service.get_account_by_id(account_id)
        self.assertEqual(rotated["access_token"], "new-at")
        self.assertEqual(rotated["management_id"], account_id)
        self.assertEqual(service.get_account("old-at")["management_id"], account_id)
        tokens, _missing = service.resolve_account_ids([account_id])
        self.assertEqual(tokens, ["new-at"])

    def test_new_account_reusing_a_rotated_token_gets_a_distinct_id(self) -> None:
        service, _storage = _make_service([{"access_token": "old-at", "refresh_token": "rt"}])
        original_id = service.get_account("old-at")["management_id"]
        with patch("services.account_service.log_service"):
            service._apply_refreshed_tokens("old-at", {"access_token": "new-at"}, "test")
            service.add_account_items([{"access_token": "old-at"}], return_items=False)
        ids = [item["management_id"] for item in service.list_accounts()]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(service.get_account_by_id(original_id)["access_token"], "new-at")


class ImportIdTests(unittest.TestCase):
    def test_external_ids_are_ignored_unless_restoring(self) -> None:
        foreign = "acct_" + "b" * 24
        service, _storage = _make_service([])
        with patch("services.account_service.log_service"):
            service.add_account_items([{"access_token": "tok-a", "management_id": foreign}], return_items=False)
            service.add_account_items(
                [{"access_token": "tok-b", "management_id": foreign}], return_items=False, restore=True,
            )
        self.assertNotEqual(service.get_account("tok-a")["management_id"], foreign)
        self.assertEqual(service.get_account("tok-b")["management_id"], foreign)

    def test_update_account_cannot_change_id(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a"}])
        before = service.get_account("tok-a")["management_id"]
        with patch("services.account_service.log_service"):
            service.update_account("tok-a", {"management_id": "acct_" + "c" * 24, "quota": 3})
        self.assertEqual(service.get_account("tok-a")["management_id"], before)

    def test_backup_records_are_complete_copies(self) -> None:
        service, _storage = _make_service([
            {"access_token": "tok-a", "refresh_token": "rt-a", "group_id": "g1"},
            {"access_token": "tok-b"},
        ])
        records = service.build_backup_records(["tok-a"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["refresh_token"], "rt-a")
        self.assertEqual(records[0]["group_id"], "g1")
        self.assertTrue(records[0]["management_id"])
        records[0]["refresh_token"] = "mutated"
        self.assertEqual(service.get_account("tok-a")["refresh_token"], "rt-a")


class UpdateCredentialsTests(unittest.TestCase):
    def test_changing_access_token_keeps_id_and_aliases_old_token(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a", "refresh_token": "rt"}])
        account_id = service.get_account("tok-a")["management_id"]
        result = service.update_credentials(account_id, access_token="tok-new", refresh_token="rt-new")
        self.assertEqual(result["access_token"], "tok-new")
        self.assertEqual(result["refresh_token"], "rt-new")
        self.assertEqual(result["management_id"], account_id)
        self.assertEqual(service.get_account("tok-a")["access_token"], "tok-new")

    def test_blank_values_do_not_overwrite(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a", "refresh_token": "rt"}])
        account_id = service.get_account("tok-a")["management_id"]
        result = service.update_credentials(account_id, access_token="  ", refresh_token="")
        self.assertEqual(result["access_token"], "tok-a")
        self.assertEqual(result["refresh_token"], "rt")

    def test_access_token_owned_by_another_account_is_rejected(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a"}, {"access_token": "tok-b"}])
        account_id = service.get_account("tok-a")["management_id"]
        with self.assertRaises(ValueError):
            service.update_credentials(account_id, access_token="tok-b")
        self.assertEqual(service.get_account_by_id(account_id)["access_token"], "tok-a")

    def test_unknown_id_returns_none(self) -> None:
        service, _storage = _make_service([{"access_token": "tok-a"}])
        self.assertIsNone(service.update_credentials("acct_missing", access_token="x"))


class CodexImageSelectionTests(unittest.TestCase):
    def test_codex_login_bound_accounts_match_any_plan_and_legacy_codex_needs_paid_plan(self) -> None:
        match = AccountService._account_matches_source_type
        source = AccountService.CODEX_IMAGE_SOURCE
        self.assertTrue(match({"source_type": "web", "type": "free", "codex_access_token": "c"}, source))
        self.assertTrue(match({"source_type": "codex", "type": "plus"}, source))
        self.assertFalse(match({"source_type": "codex", "type": "free"}, source))
        self.assertFalse(match({"source_type": "web", "type": "plus"}, source))
        self.assertTrue(match({"source_type": "codex", "type": "free"}, "codex"))


if __name__ == "__main__":
    unittest.main()
