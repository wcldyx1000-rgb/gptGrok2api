import unittest
from unittest.mock import patch

from services import remote_import_job_status as status
from services.cpa_service import CPAImportService
from services.sub2api_service import Sub2APIImportService


class _Config:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def list_pools(self) -> list[dict]:
        return self.items

    def list_servers(self) -> list[dict]:
        return self.items

    def set_import_job(self, *_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("job must not be reserved while another import is active")


class RemoteImportGuardTests(unittest.TestCase):
    def patch_configs(self, pools: list[dict], servers: list[dict]):
        return (
            patch("services.cpa_service.cpa_config", _Config(pools)),
            patch("services.sub2api_service.sub2api_config", _Config(servers)),
        )

    def test_active_statuses(self) -> None:
        self.assertTrue(status.import_job_is_active({"status": "running"}))
        self.assertTrue(status.import_job_is_active({"status": "Pending"}))
        self.assertFalse(status.import_job_is_active({"status": "completed"}))
        self.assertFalse(status.import_job_is_active(None))

    def test_finds_active_job_in_either_source(self) -> None:
        cpa, sub = self.patch_configs(
            [{"id": "p1", "name": "pool", "import_job": {"status": "completed"}}],
            [{"id": "s1", "name": "srv", "import_job": {"status": "running", "total": 3}}],
        )
        with cpa, sub:
            active = status.find_active_remote_import()
            self.assertEqual((active["source"], active["id"], active["job"]["total"]), ("sub2api", "s1", 3))
            with self.assertRaises(status.RemoteImportConflictError):
                status.ensure_no_active_remote_import()

    def test_no_active_job(self) -> None:
        cpa, sub = self.patch_configs([{"id": "p1", "import_job": None}], [])
        with cpa, sub:
            self.assertIsNone(status.find_active_remote_import())
            status.ensure_no_active_remote_import()

    def test_both_importers_refuse_to_start_while_another_runs(self) -> None:
        cpa, sub = self.patch_configs([{"id": "p1", "import_job": {"status": "pending"}}], [])
        with cpa, sub:
            with self.assertRaises(status.RemoteImportConflictError):
                CPAImportService(_Config([])).start_import({"id": "p2"}, ["a.json"])
            with self.assertRaises(status.RemoteImportConflictError):
                Sub2APIImportService(_Config([])).start_import({"id": "s2"}, ["1"], create_account_groups=False)

    def test_conflict_is_a_value_error_for_legacy_callers(self) -> None:
        self.assertTrue(issubclass(status.RemoteImportConflictError, ValueError))


if __name__ == "__main__":
    unittest.main()
