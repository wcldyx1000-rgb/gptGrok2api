import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services import editable_file_task_service as eft
from services.editable_file_failure import EditableFileFailureError
from services.image_failure import image_failure
from services.openai_backend_api import EditableFileExportResult
from services.storage.database_storage import DatabaseStorageBackend
from services.storage.editable_file_task_repository import (
    DatabaseEditableFileTaskRepository,
    JsonEditableFileTaskRepository,
)
from utils.file_names import is_safe_public_filename, sanitize_public_filename

IDENTITY = {"id": "key-1", "name": "k", "role": "user"}
OTHER = {"id": "key-2", "name": "o", "role": "user"}


def _task(task_id="t1", owner="o1", **extra):
    base = {
        "id": task_id, "owner_id": owner, "storage_id": f"asset-{task_id}", "status": "queued", "kind": "ppt",
        "model": "m", "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        "created_ts": 1.0, "updated_ts": 1.0,
    }
    base.update(extra)
    return base


class RepositoryContract:
    def make_repo(self):
        raise NotImplementedError

    def test_create_get_update_delete_roundtrip(self) -> None:
        repo = self.make_repo()
        task, created = repo.create(_task())
        self.assertTrue(created)
        again, created = repo.create(_task(status="success"))
        self.assertFalse(created)
        self.assertEqual(again["status"], "queued")
        updated = repo.update("o1", "t1", status="success", result={"primary_url": "/files/x"}, error="", delete_pending=True)
        self.assertEqual(updated["status"], "success")
        self.assertEqual(updated["result"], {"primary_url": "/files/x"})
        self.assertNotIn("error", updated)
        self.assertTrue(repo.get("o1", "t1")["delete_pending"])
        self.assertEqual(len(repo.list_delete_pending()), 1)
        self.assertIsNone(repo.update("o1", "missing", status="x"))
        with self.assertRaises(ValueError):
            repo.update("o1", "t1", storage_id="evil")
        self.assertTrue(repo.delete("o1", "t1"))
        self.assertFalse(repo.delete("o1", "t1"))
        self.assertIsNone(repo.get("o1", "t1"))

    def test_list_filters_owner_ids_limit_and_order(self) -> None:
        repo = self.make_repo()
        for index in range(3):
            repo.create(_task(f"t{index}", updated_at=f"2026-01-01 00:00:0{index}"))
        repo.create(_task("other", owner="o2"))
        listed = repo.list_for_owner("o1")
        self.assertEqual([item["id"] for item in listed], ["t2", "t1", "t0"])
        self.assertEqual([item["id"] for item in repo.list_for_owner("o1", limit=2)], ["t2", "t1"])
        self.assertEqual([item["id"] for item in repo.list_for_owner("o1", task_ids=["t0", "other"])], ["t0"])
        self.assertEqual(repo.list_for_owner("o1", task_ids=[]), [])
        self.assertEqual({item["id"] for item in repo.list_by_status({"queued"})}, {"t0", "t1", "t2", "other"})


class JsonRepositoryTest(RepositoryContract, unittest.TestCase):
    def make_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "tasks.json"
        return JsonEditableFileTaskRepository(self.path)

    def test_persists_across_instances(self) -> None:
        repo = self.make_repo()
        repo.create(_task(result={"zip_url": "/files/z"}))
        reloaded = JsonEditableFileTaskRepository(self.path)
        self.assertEqual(reloaded.get("o1", "t1")["result"], {"zip_url": "/files/z"})


class DatabaseRepositoryTest(RepositoryContract, unittest.TestCase):
    def make_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        backend = DatabaseStorageBackend(f"sqlite:///{(Path(tmp.name) / 'app.db').as_posix()}")
        self.addCleanup(backend.engine.dispose)
        self.backend = backend
        return DatabaseEditableFileTaskRepository(backend)

    def test_persists_across_instances(self) -> None:
        repo = self.make_repo()
        repo.create(_task(result={"zip_url": "/files/z"}))
        reloaded = DatabaseEditableFileTaskRepository(self.backend)
        self.assertEqual(reloaded.get("o1", "t1")["result"], {"zip_url": "/files/z"})


class FileNamesTest(unittest.TestCase):
    def test_safe_public_filename(self) -> None:
        for good in ("deck.pptx", "a b.zip", "中文.psd"):
            self.assertTrue(is_safe_public_filename(good), good)
        for bad in ("", ".", "..", "a/b", "a\\b", "CON.pptx", "x.", "x ", "a:b", "a\x01b", "n" * 300):
            self.assertFalse(is_safe_public_filename(bad), bad)
        self.assertEqual(sanitize_public_filename("../a:b.pptx"), "a_b.pptx")


class EditableFileTaskServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "files"
        self.root.mkdir()
        patcher = patch.object(eft, "EDITABLE_FILE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = JsonEditableFileTaskRepository(Path(tmp.name) / "tasks.json")
        self.service = eft.EditableFileTaskService(repository=self.repo)
        for name in ("_editable_access_token",):
            p = patch.object(eft, name, return_value="tok")
            p.start()
            self.addCleanup(p.stop)
        acct = patch.object(eft, "account_service", MagicMock(get_account=MagicMock(return_value={"email": "a@x.com"})))
        acct.start()
        self.addCleanup(acct.stop)
        log = patch.object(eft, "log_service", MagicMock())
        log.start()
        self.addCleanup(log.stop)

    def _backend(self, fail: Exception | None = None, primary_name="deck.pptx"):
        class Backend:
            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                return False

            def _export(inner, images, prompt, output_dir, timeout_secs=0):
                if fail is not None:
                    raise fail
                out = Path(output_dir)
                out.mkdir(parents=True, exist_ok=True)
                (out / primary_name).write_bytes(b"primary")
                (out / "deck.zip").write_bytes(b"zip")
                return EditableFileExportResult("conv-1", out / primary_name, out / "deck.zip")

            export_ppt_zip = _export
            export_psd_zip = _export

        return patch.object(eft, "OpenAIBackendAPI", lambda token: Backend())

    def _submit(self, identity=IDENTITY, kind="ppt", client_task_id="task-1", **backend_kwargs):
        # 不起后台线程：提交后手动同步执行任务体。
        with patch.object(eft.threading, "Thread") as thread:
            task = getattr(self.service, f"submit_{kind}")(
                identity, client_task_id=client_task_id, prompt="p", base64_images=["QQ=="], base_url="http://h",
            )
        args = thread.call_args.kwargs["args"]
        with self._backend(**backend_kwargs):
            self.service._run_task(*args)
        return task

    def test_success_publishes_random_asset_dir_and_download_path(self) -> None:
        self._submit()
        item = self.service.list_tasks(IDENTITY, ["task-1"])["items"][0]
        self.assertEqual(item["status"], "success")
        self.assertTrue(item["can_download"])
        self.assertTrue(item["can_delete"])
        url = item["result"]["primary_url"]
        self.assertRegex(url, r"^http://h/files/ppt/asset-[0-9a-f-]{36}/deck\.pptx$")
        self.assertNotIn("task-1", url)
        path = self.service.public_file_path(url.split("/files/", 1)[1])
        self.assertEqual(path.read_bytes(), b"primary")
        self.assertFalse((self.root / ".staging" / "ppt").exists() and any((self.root / ".staging" / "ppt").iterdir()))

    def test_public_file_path_rejects_guessable_and_unsafe_paths(self) -> None:
        self._submit()
        storage_id = self.repo.get("key-1", "task-1")["storage_id"]
        for bad in (
            "ppt/task-1/deck.pptx",
            f"ppt/{storage_id}/../x.pptx",
            f"ppt/{storage_id}/deck.exe",
            f"ppt\\{storage_id}\\deck.pptx",
            f"psd/{storage_id}/deck.pptx",
            f"ppt/{storage_id}/missing.pptx",
            "ppt/deck.pptx",
        ):
            with self.assertRaises(FileNotFoundError, msg=bad):
                self.service.public_file_path(bad)

    def test_legacy_task_links_without_storage_id_still_download_and_delete(self) -> None:
        legacy = self.root / "ppt" / "old-task_1"
        legacy.mkdir(parents=True)
        (legacy / "deck.pptx").write_bytes(b"old")
        (self.root / "secret.pptx").write_bytes(b"nope")
        self.repo.create(_task("old-task_1", owner="key-1", storage_id="", status="success"))
        self.assertEqual(self.service.public_file_path("ppt/old-task_1/deck.pptx").read_bytes(), b"old")
        for bad in (
            "ppt/../secret.pptx",
            "ppt/.staging/deck.pptx",
            "ppt/old-task_1/../../secret.pptx",
            "ppt/old-task_1/deck.exe",
            "ppt/old-task_1/missing.pptx",
            "ppt/old task/deck.pptx",
            "psd/old-task_1/deck.pptx",
            "ppt/CON/deck.pptx",
        ):
            with self.assertRaises(FileNotFoundError, msg=bad):
                self.service.public_file_path(bad)
        self.service.delete_task(IDENTITY, "old-task_1")
        self.assertFalse(legacy.exists())

    def test_invalid_assets_are_rejected_and_task_fails_without_leaking(self) -> None:
        self._submit(primary_name="CON.pptx")
        item = self.service.list_tasks(IDENTITY, ["task-1"])["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertTrue(item["can_delete"])
        self.assertFalse(item["can_download"])

    def test_failure_message_is_sanitized(self) -> None:
        failure = EditableFileFailureError(failure=image_failure("auth_invalid"))
        self._submit(fail=failure)
        item = self.service.list_tasks(IDENTITY, ["task-1"])["items"][0]
        self.assertIn("authenticate", item["error"])
        self._submit(client_task_id="task-2", fail=RuntimeError("Bearer sk-secret leaked in upstream body"))
        item2 = self.service.list_tasks(IDENTITY, ["task-2"])["items"][0]
        self.assertNotIn("sk-secret", item2["error"])
        logged = self.service  # raw detail goes to call log only
        _ = logged
        self.assertTrue(eft.log_service.add.called)

    def test_psd_requires_images(self) -> None:
        with patch.object(eft.threading, "Thread") as thread:
            self.service.submit_psd(IDENTITY, client_task_id="p1", prompt="p", base64_images=[], base_url="")
        with self._backend():
            self.service._run_task(*thread.call_args.kwargs["args"])
        item = self.service.list_tasks(IDENTITY, ["p1"])["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertEqual(item["error"], "base64_images is empty")

    def test_delete_requires_terminal_owner_and_removes_files(self) -> None:
        self._submit()
        storage_id = self.repo.get("key-1", "task-1")["storage_id"]
        out = self.root / "ppt" / storage_id
        self.assertTrue(out.is_dir())
        with self.assertRaises(eft.EditableFileTaskNotFoundError):
            self.service.delete_task(OTHER, "task-1")
        self.repo.update("key-1", "task-1", status="running")
        with self.assertRaises(eft.EditableFileTaskNotTerminalError):
            self.service.delete_task(IDENTITY, "task-1")
        self.repo.update("key-1", "task-1", status="success")
        self.assertEqual(self.service.delete_task(IDENTITY, "task-1"), {"task_id": "task-1", "deleted": True})
        self.assertFalse(out.exists())
        self.assertEqual(self.service.list_tasks(IDENTITY, [])["items"], [])
        with self.assertRaises(eft.EditableFileTaskNotFoundError):
            self.service.delete_task(IDENTITY, "task-1")

    def test_delete_cleanup_failure_keeps_pending_and_blocks(self) -> None:
        self._submit()
        with patch.object(eft.EditableFileTaskService, "_remove_directories", return_value=False):
            with self.assertRaises(eft.EditableFileTaskCleanupError):
                self.service.delete_task(IDENTITY, "task-1")
        stored = self.repo.get("key-1", "task-1")
        self.assertTrue(stored["delete_pending"])
        self.assertFalse(self.service.list_tasks(IDENTITY, ["task-1"])["items"][0]["can_download"])
        # 重启恢复：待删除任务会被重新清理并移除。
        eft.EditableFileTaskService(repository=self.repo)
        self.assertIsNone(self.repo.get("key-1", "task-1"))

    def test_client_task_id_validation_and_idempotent_submit(self) -> None:
        for bad in ("../x", "a/b", "a b", "x" * 161, ".."):
            with self.assertRaises(eft.EditableFileTaskInvalidIdError):
                self.service.submit_ppt(IDENTITY, client_task_id=bad, prompt="p", base_url="")
        with patch.object(eft.threading, "Thread") as thread:
            first = self.service.submit_ppt(IDENTITY, client_task_id="same", prompt="p", base_url="")
            second = self.service.submit_ppt(IDENTITY, client_task_id="same", prompt="p", base_url="")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(thread.call_count, 1)

    def test_list_limit_and_isolation(self) -> None:
        with patch.object(eft.threading, "Thread"):
            for index in range(3):
                self.service.submit_ppt(IDENTITY, client_task_id=f"id-{index}", prompt="p", base_url="")
            self.service.submit_ppt(OTHER, client_task_id="foreign", prompt="p", base_url="")
        self.assertEqual(len(self.service.list_tasks(IDENTITY, [], limit=2)["items"]), 2)
        self.assertEqual(len(self.service.list_tasks(IDENTITY, [])["items"]), 3)
        result = self.service.list_tasks(IDENTITY, ["foreign", "id-0"])
        self.assertEqual([i["id"] for i in result["items"]], ["id-0"])
        self.assertEqual(result["missing_ids"], ["foreign"])

    def test_unfinished_tasks_are_interrupted_on_restart(self) -> None:
        with patch.object(eft.threading, "Thread"):
            self.service.submit_ppt(IDENTITY, client_task_id="stuck", prompt="p", base_url="")
        eft.EditableFileTaskService(repository=self.repo)
        item = self.service.list_tasks(IDENTITY, ["stuck"])["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertIn("中断", item["error"])


class EditableFileApiTest(unittest.TestCase):
    def test_routes_exist_with_delete_and_limit(self) -> None:
        from fastapi import FastAPI
        from api import ai

        app = FastAPI()
        app.include_router(ai.create_router())
        paths = {(method, route.path) for route in app.routes for method in getattr(route, "methods", set())}
        self.assertIn(("DELETE", "/v1/editable-file-tasks/{task_id}"), paths)
        self.assertIn(("GET", "/v1/editable-file-tasks"), paths)
        self.assertIn(("GET", "/files/{file_path:path}"), paths)


if __name__ == "__main__":
    unittest.main()
