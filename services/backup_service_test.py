from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import services.backup_service as backup_module


class GrokRuntimeBackupTest(unittest.TestCase):
    def test_register_backup_contains_consistent_grok_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            runtime_dir = data_dir / "grok_runtime"
            runtime_dir.mkdir(parents=True)
            (data_dir / "register.json").write_text("{}\n", encoding="utf-8")
            (data_dir / "grok_accounts.json").write_text("[]\n", encoding="utf-8")
            (runtime_dir / "config.toml").write_text('[proxy.egress]\nmode = "direct"\n', encoding="utf-8")

            database_path = runtime_dir / "accounts.db"
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("create table accounts (token text primary key, status text not null)")
                database.execute("insert into accounts values (?, ?)", ("test-token", "disabled"))
                database.commit()

            with patch.object(backup_module, "DATA_DIR", data_dir):
                payload = backup_module.BackupService()._build_backup_archive(
                    {"include": {"register": True}},
                    trigger="test",
                )

            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                names = {member.name for member in archive.getmembers() if member.isfile()}
                self.assertIn("data/grok_runtime/config.toml", names)
                self.assertIn("data/grok_runtime/accounts.db", names)
                extracted = archive.extractfile("data/grok_runtime/accounts.db")
                self.assertIsNotNone(extracted)
                database_bytes = extracted.read() if extracted is not None else b""

            restored_path = Path(directory) / "restored.db"
            restored_path.write_bytes(database_bytes)
            with closing(sqlite3.connect(restored_path)) as restored:
                self.assertEqual(
                    restored.execute("select token, status from accounts").fetchone(),
                    ("test-token", "disabled"),
                )


class BackupStateAndLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.data_dir = Path(self._dir.name)
        self.state: dict = {}
        patches = [
            patch.object(backup_module, "DATA_DIR", self.data_dir),
            patch.object(backup_module, "load_backup_state", side_effect=lambda: dict(self.state)),
            patch.object(backup_module, "save_backup_state", side_effect=self._save),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _save(self, value):
        self.state = dict(value)
        return self.state

    def test_run_backup_marks_running_then_success(self) -> None:
        service = backup_module.BackupService()
        seen: list[str] = []

        def fake_once(*, trigger):
            seen.append(self.state["last_status"])
            return {"key": "k1"}

        with patch.object(service, "_run_backup_once", side_effect=fake_once):
            service.run_backup()
        self.assertEqual(seen, ["running"])
        self.assertEqual(self.state["last_status"], "success")
        lock = backup_module.acquire_backup_process_lock()
        self.assertIsNotNone(lock)
        backup_module.release_backup_process_lock(lock)

    def test_failed_backup_records_error_and_releases_lock(self) -> None:
        service = backup_module.BackupService()
        with patch.object(service, "_run_backup_once", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                service.run_backup()
        self.assertEqual(self.state["last_status"], "error")
        self.assertEqual(self.state["last_error"], "boom")
        lock = backup_module.acquire_backup_process_lock()
        self.assertIsNotNone(lock)
        backup_module.release_backup_process_lock(lock)

    def test_backup_rejected_while_other_process_holds_lock(self) -> None:
        held = backup_module.acquire_backup_process_lock()
        self.assertIsNotNone(held)
        try:
            self.assertIsNone(backup_module.acquire_backup_process_lock())
            service = backup_module.BackupService()
            with patch.object(service, "_run_backup_once") as once:
                with self.assertRaises(backup_module.BackupError):
                    service.run_backup()
            once.assert_not_called()
        finally:
            backup_module.release_backup_process_lock(held)

    def test_startup_recovers_stale_running_state(self) -> None:
        self.state = {"last_status": "running", "last_started_at": "2026-01-01T00:00:00Z"}
        self.assertTrue(backup_module.BackupService().recover_interrupted_state())
        self.assertEqual(self.state["last_status"], "error")
        self.assertIn("退出", self.state["last_error"])

    def test_startup_does_not_touch_state_when_another_process_is_backing_up(self) -> None:
        self.state = {"last_status": "running"}
        held = backup_module.acquire_backup_process_lock()
        try:
            self.assertFalse(backup_module.BackupService().recover_interrupted_state())
            self.assertEqual(self.state["last_status"], "running")
        finally:
            backup_module.release_backup_process_lock(held)


class DatabaseSnapshotBackupTest(unittest.TestCase):
    """STORAGE_BACKEND=sqlite/postgres: the archive must carry a consistent DB snapshot."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()

    def _archive(self, backend, include=None):
        service = backup_module.BackupService()
        with patch.object(backup_module, "DATA_DIR", self.data_dir), \
                patch.object(backup_module.config, "get_storage_backend", return_value=backend):
            payload = service._build_backup_archive({"include": include or {}}, trigger="test")
        return service, payload

    @staticmethod
    def _members(payload: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            return {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}

    def test_sqlite_snapshot_contains_all_tables_and_matches_manifest(self) -> None:
        from services.storage.database_storage import DatabaseStorageBackend

        db_path = self.data_dir / "accounts.db"
        backend = DatabaseStorageBackend(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(backend.engine.dispose)
        backend.save_accounts([{"access_token": "tok", "status": "正常"}])
        # A table owned by another repository (call records / dashboard metrics) in the same file.
        with backend.engine.begin() as connection:
            connection.exec_driver_sql("create table call_records_demo (id integer primary key, body text)")
            connection.exec_driver_sql("insert into call_records_demo (body) values ('hello')")

        _, payload = self._archive(backend)
        members = self._members(payload)
        self.assertIn("data/application-database.sqlite3", members)
        restored = self.root / "restored.sqlite3"
        restored.write_bytes(members["data/application-database.sqlite3"])
        with closing(sqlite3.connect(restored)) as database:
            self.assertEqual(database.execute("select body from call_records_demo").fetchone(), ("hello",))
            self.assertEqual(database.execute("select count(*) from accounts").fetchone(), (1,))

        metadata = json.loads(members["backup-metadata.json"])
        snapshot = metadata["database_snapshot"]
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["sha256"], hashlib.sha256(members["data/application-database.sqlite3"]).hexdigest())
        manifest = json.loads(members["backup-manifest.json"])["files"]
        self.assertEqual(manifest["data/application-database.sqlite3"]["size"], len(members["data/application-database.sqlite3"]))

    def test_sqlite_snapshot_is_consistent_while_writes_continue(self) -> None:
        from services.storage.database_storage import DatabaseStorageBackend

        backend = DatabaseStorageBackend(f"sqlite:///{(self.data_dir / 'accounts.db').as_posix()}")
        self.addCleanup(backend.engine.dispose)
        stop = threading.Event()
        errors: list[Exception] = []

        def writer() -> None:
            index = 0
            while not stop.is_set():
                try:
                    backend.save_accounts([{"access_token": f"t{index}", "status": "正常"}])
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)
                    return
                index += 1

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(3):
                _, payload = self._archive(backend)
                database = self.root / "snap.sqlite3"
                database.write_bytes(self._members(payload)["data/application-database.sqlite3"])
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute("pragma integrity_check").fetchone(), ("ok",))
        finally:
            stop.set()
            thread.join(10)
        self.assertEqual(errors, [])

    def test_postgres_uses_pg_dump_with_password_in_env_only(self) -> None:
        backend = SimpleNamespace(
            database_url="postgresql+psycopg2://app_user:s3cret%40pw@db.internal:5433/appdb?sslmode=require",
            get_backend_info=lambda: {"type": "postgresql"},
        )
        calls: list[tuple[list[str], dict]] = []

        def fake_run(command, **kwargs):
            calls.append((list(command), kwargs))
            return SimpleNamespace(stdout=b"PGDMP-bytes", stderr=b"")

        with patch.object(backup_module.subprocess, "run", side_effect=fake_run):
            service, payload = self._archive(backend)
        command, kwargs = calls[0]
        self.assertEqual(command[0], "pg_dump")
        self.assertIn("--format=custom", command)
        self.assertEqual(command[command.index("--host") + 1], "db.internal")
        self.assertEqual(command[command.index("--port") + 1], "5433")
        self.assertEqual(command[command.index("--username") + 1], "app_user")
        self.assertEqual(command[command.index("--dbname") + 1], "appdb")
        self.assertFalse(any("s3cret" in part for part in command))
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "s3cret@pw")
        self.assertEqual(kwargs["env"]["PGSSLMODE"], "require")
        members = self._members(payload)
        self.assertEqual(members["data/application-database.pgdump"], b"PGDMP-bytes")
        self.assertEqual(service._last_warnings, [])

    def test_missing_pg_dump_skips_snapshot_with_chinese_warning(self) -> None:
        backend = SimpleNamespace(
            database_url="postgresql://u:p@localhost/db",
            get_backend_info=lambda: {"type": "postgresql"},
        )
        (self.data_dir / "register.json").write_text("{}", encoding="utf-8")
        with patch.object(backup_module.subprocess, "run", side_effect=FileNotFoundError("pg_dump")):
            service, payload = self._archive(backend, include={"register": True})
        members = self._members(payload)
        self.assertNotIn("data/application-database.pgdump", members)
        self.assertIn("data/register.json", members)  # the rest of the backup still succeeds
        self.assertEqual(len(service._last_warnings), 1)
        self.assertIn("pg_dump", service._last_warnings[0])
        self.assertIn("跳过", service._last_warnings[0])
        metadata = json.loads(members["backup-metadata.json"])
        self.assertEqual(metadata["database_snapshot"]["status"], "skipped")

    def test_pg_dump_failure_fails_the_backup(self) -> None:
        backend = SimpleNamespace(database_url="postgresql://u:p@localhost/db", get_backend_info=lambda: {})
        error = subprocess.CalledProcessError(1, "pg_dump", stderr="connection refused".encode())
        with patch.object(backup_module.subprocess, "run", side_effect=error):
            with self.assertRaises(backup_module.BackupError) as ctx:
                self._archive(backend)
        self.assertIn("connection refused", str(ctx.exception))

    def test_json_backend_adds_no_database_snapshot(self) -> None:
        backend = SimpleNamespace(get_backend_info=lambda: {"type": "json"})
        _, payload = self._archive(backend)
        members = self._members(payload)
        self.assertNotIn("database_snapshot", json.loads(members["backup-metadata.json"]))
        self.assertFalse([name for name in members if name.startswith("data/application-database")])

    def test_file_members_are_hashed_into_the_manifest(self) -> None:
        backend = SimpleNamespace(get_backend_info=lambda: {})
        (self.data_dir / "register.json").write_text('{"a": 1}', encoding="utf-8")
        _, payload = self._archive(backend, include={"register": True})
        members = self._members(payload)
        manifest = json.loads(members["backup-manifest.json"])["files"]
        self.assertEqual(manifest["data/register.json"]["sha256"], hashlib.sha256(members["data/register.json"]).hexdigest())

    def test_run_backup_surfaces_warnings_in_state(self) -> None:
        state: dict = {}
        service = backup_module.BackupService()
        result = {"key": "k", "warnings": ["已跳过 PostgreSQL 数据库快照"]}
        with patch.object(backup_module, "DATA_DIR", self.data_dir), \
                patch.object(backup_module, "load_backup_state", side_effect=lambda: dict(state)), \
                patch.object(backup_module, "save_backup_state", side_effect=lambda v: state.update(v) or state), \
                patch.object(service, "_run_backup_once", return_value=result):
            service.run_backup()
        self.assertEqual(state["last_status"], "success")
        self.assertIn("跳过", state["last_error"])

    def test_decode_detail_hides_manifest_and_exposes_database_snapshot(self) -> None:
        backend = SimpleNamespace(database_url="postgresql://u:p@localhost/db", get_backend_info=lambda: {})
        with patch.object(backup_module.subprocess, "run", return_value=SimpleNamespace(stdout=b"x", stderr=b"")):
            service, payload = self._archive(backend)
        detail = service._decode_archive_detail(payload)
        self.assertEqual(detail["database_snapshot"]["status"], "ok")
        self.assertNotIn("backup-manifest.json", [item["name"] for item in detail["files"]])


if __name__ == "__main__":
    unittest.main()
