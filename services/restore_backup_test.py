"""Tests for scripts/restore_backup.py (verify-only default, --apply with pre-restore snapshot)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import services.backup_service as backup_module

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "restore_backup.py"
_spec = importlib.util.spec_from_file_location("restore_backup_script", _SCRIPT)
restore = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(restore)


def _sqlite_bytes(directory: Path) -> bytes:
    path = directory / "src.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        database.execute("create table t (v text)")
        database.execute("insert into t values ('kept')")
        database.commit()
    return path.read_bytes()


class RestoreBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "src" / "data"
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "register.json").write_text('{"k": 1}', encoding="utf-8")

    def _build(self, backend=None, include=None) -> bytes:
        backend = backend or SimpleNamespace(get_backend_info=lambda: {"type": "json"})
        service = backup_module.BackupService()
        with patch.object(backup_module, "DATA_DIR", self.data_dir), \
                patch.object(backup_module.config, "get_storage_backend", return_value=backend):
            return service._build_backup_archive({"include": include or {"register": True}}, trigger="test")

    def _write_archive(self, payload: bytes, name: str = "backup-1.tar.gz") -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    @staticmethod
    def _retar(members: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, payload in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_verify_only_lists_contents_and_writes_nothing(self) -> None:
        archive = self._write_archive(self._build())
        target = self.root / "target"
        code, out, _ = self._run(str(archive), "--target", str(target))
        self.assertEqual(code, restore.EXIT_OK)
        self.assertIn("data/register.json", out)
        self.assertIn("未写入任何文件", out)
        self.assertFalse(target.exists())

    def test_tampered_member_fails_hash_check(self) -> None:
        files = restore.unpack(self._build())
        files["data/register.json"] = b'{"k": 2}'
        archive = self._write_archive(self._retar(files))
        code, _, err = self._run(str(archive))
        self.assertEqual(code, restore.EXIT_INVALID)
        self.assertIn("哈希不匹配", err)

    def test_missing_manifest_member_is_reported(self) -> None:
        files = restore.unpack(self._build())
        del files["data/register.json"]
        code, _, err = self._run(str(self._write_archive(self._retar(files))))
        self.assertEqual(code, restore.EXIT_INVALID)
        self.assertIn("缺失", err)

    def test_old_backup_without_manifest_warns_but_passes(self) -> None:
        files = restore.unpack(self._build())
        del files["backup-manifest.json"]
        code, out, _ = self._run(str(self._write_archive(self._retar(files))))
        self.assertEqual(code, restore.EXIT_OK)
        self.assertIn("警告", out)

    def test_non_backup_archive_is_rejected(self) -> None:
        code, _, err = self._run(str(self._write_archive(self._retar({"x.txt": b"1"}))))
        self.assertEqual(code, restore.EXIT_INVALID)
        self.assertIn("backup-metadata.json", err)

    def test_path_traversal_member_is_rejected(self) -> None:
        for bad in ("../evil.txt", "/abs.txt", "data/../../evil.txt", "C:/evil.txt"):
            payload = self._retar({"backup-metadata.json": b"{}", bad: b"x"})
            with self.assertRaises(restore.RestoreError, msg=bad):
                restore.unpack(payload)

    def test_corrupt_archive_is_rejected(self) -> None:
        code, _, err = self._run(str(self._write_archive(b"not a tarball")))
        self.assertEqual(code, restore.EXIT_INVALID)
        self.assertIn("损坏", err)

    def test_encrypted_archive_requires_passphrase(self) -> None:
        archive = self._write_archive(b"ciphertext", "backup-1.tar.gz.enc")
        code, _, err = self._run(str(archive), "--passphrase-env", "NO_SUCH_PASSPHRASE_VAR")
        self.assertEqual(code, restore.EXIT_INVALID)
        self.assertIn("口令", err)

    def test_encrypted_archive_roundtrip_with_mocked_openssl(self) -> None:
        plain = self._build()
        archive = self._write_archive(b"ciphertext", "backup-1.tar.gz.enc")
        with patch.dict("os.environ", {"MY_PASS": "secret"}), \
                patch.object(restore, "decrypt_openssl", return_value=plain) as decrypt:
            code, out, _ = self._run(str(archive), "--passphrase-env", "MY_PASS")
        self.assertEqual(code, restore.EXIT_OK)
        decrypt.assert_called_once_with(b"ciphertext", "secret")
        self.assertIn("data/register.json", out)

    def test_apply_snapshots_existing_data_then_restores(self) -> None:
        archive = self._write_archive(self._build())
        target = self.root / "target"
        (target / "data").mkdir(parents=True)
        (target / "data" / "register.json").write_text("OLD", encoding="utf-8")
        (target / "data" / "keep.txt").write_text("keep", encoding="utf-8")
        (target / "config.json").write_text("{}", encoding="utf-8")

        code, out, _ = self._run(str(archive), "--target", str(target), "--apply")
        self.assertEqual(code, restore.EXIT_OK, out)
        self.assertEqual((target / "data" / "register.json").read_text(encoding="utf-8"), '{"k": 1}')
        self.assertEqual((target / "data" / "keep.txt").read_text(encoding="utf-8"), "keep")
        snapshots = sorted(path.name for path in target.glob("data.pre-restore-*"))
        self.assertEqual(len(snapshots), 1)
        snapshot_dir = target / snapshots[0]
        self.assertEqual((snapshot_dir / "register.json").read_text(encoding="utf-8"), "OLD")
        self.assertTrue(list(target.glob("config.json.pre-restore-*")))
        self.assertEqual(list(target.rglob("*.restore.tmp")), [])

    def test_sqlite_snapshot_not_written_over_live_db_by_default(self) -> None:
        backend = SimpleNamespace(
            database_url="sqlite:///" + (self.data_dir / "accounts.db").as_posix(),
            get_backend_info=lambda: {"type": "sqlite"},
        )
        db = self.data_dir / "accounts.db"
        db.write_bytes(_sqlite_bytes(self.root))
        archive = self._write_archive(self._build(backend, include={}))
        target = self.root / "target"
        live = target / "data" / "accounts.db"
        live.parent.mkdir(parents=True)
        live.write_bytes(b"LIVE")

        code, out, _ = self._run(str(archive), "--target", str(target), "--apply")
        self.assertEqual(code, restore.EXIT_OK, out)
        self.assertEqual(live.read_bytes(), b"LIVE")  # live DB untouched
        restored = target / "data" / "application-database.sqlite3"
        with closing(sqlite3.connect(restored)) as database:
            self.assertEqual(database.execute("select v from t").fetchone(), ("kept",))
        self.assertIn("停止服务", out)

        sqlite_target = target / "data" / "accounts.db"
        (target / "data" / "accounts.db-wal").write_bytes(b"stale")
        code, out, _ = self._run(str(archive), "--target", str(target), "--apply", "--sqlite-target", str(sqlite_target))
        self.assertEqual(code, restore.EXIT_OK, out)
        with closing(sqlite3.connect(sqlite_target)) as database:
            self.assertEqual(database.execute("select v from t").fetchone(), ("kept",))
        self.assertFalse((target / "data" / "accounts.db-wal").exists())

    def test_postgres_dump_is_never_restored_automatically(self) -> None:
        backend = SimpleNamespace(database_url="postgresql://u:p@localhost/db", get_backend_info=lambda: {})
        with patch.object(backup_module.subprocess, "run", return_value=SimpleNamespace(stdout=b"PGDMP", stderr=b"")):
            archive = self._write_archive(self._build(backend, include={}))
        target = self.root / "target"
        with patch.object(restore.subprocess, "run", side_effect=AssertionError("must not run pg_restore")):
            code, out, _ = self._run(str(archive), "--target", str(target), "--apply")
        self.assertEqual(code, restore.EXIT_OK, out)
        self.assertIn("pg_restore", out)
        self.assertEqual((target / "data" / "application-database.pgdump").read_bytes(), b"PGDMP")

    def test_database_snapshot_hash_mismatch_with_metadata_is_detected(self) -> None:
        backend = SimpleNamespace(database_url="postgresql://u:p@localhost/db", get_backend_info=lambda: {})
        with patch.object(backup_module.subprocess, "run", return_value=SimpleNamespace(stdout=b"PGDMP", stderr=b"")):
            files = restore.unpack(self._build(backend, include={}))
        files["data/application-database.pgdump"] = b"changed"
        manifest = json.loads(files["backup-manifest.json"])
        manifest["files"].pop("data/application-database.pgdump")
        files["backup-manifest.json"] = json.dumps(manifest).encode()
        with self.assertRaises(restore.RestoreError):
            restore.verify(files)

    def test_real_openssl_roundtrip_when_available(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl not installed")
        plain = self._build()
        encrypted = backup_module._openssl_encrypt(plain, "pw")
        self.assertEqual(restore.decrypt_openssl(encrypted, "pw"), plain)
        with self.assertRaises(restore.RestoreError):
            restore.decrypt_openssl(encrypted, "wrong")


if __name__ == "__main__":
    unittest.main()
