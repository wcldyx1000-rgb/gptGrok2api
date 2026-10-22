import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService
from services.storage.base import StorageMutation, StorageRevisionConflictError
from services.storage.database_storage import AccountModel, DatabaseStorageBackend


def _account(token: str, **extra) -> dict:
    return {"access_token": token, "status": "正常", "quota": 10, "email": f"{token}@example.test", **extra}


class _TempDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "accounts.db"
        self.url = f"sqlite:///{self.db_path.as_posix()}"

    def make_backend(self) -> DatabaseStorageBackend:
        backend = DatabaseStorageBackend(self.url)
        self.addCleanup(backend.engine.dispose)
        return backend

    def row_ids(self, backend: DatabaseStorageBackend) -> dict[str, int]:
        session = backend.Session()
        try:
            return {row.access_token: row.id for row in session.query(AccountModel).all()}
        finally:
            session.close()


class DatabaseStorageIncrementalTests(_TempDbCase):
    def test_save_accounts_only_rewrites_changed_rows(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b"), _account("c")])
        ids_before = self.row_ids(backend)
        revision_before = backend.load_accounts_revision()

        backend.save_accounts([_account("a"), _account("b", quota=3), _account("c")])

        # 行没有被删除重建：主键 id 保持不变
        self.assertEqual(self.row_ids(backend), ids_before)
        self.assertEqual(int(backend.load_accounts_revision()), int(revision_before) + 1)
        by_token = {item["access_token"]: item for item in backend.load_accounts()}
        self.assertEqual(by_token["b"]["quota"], 3)

    def test_unchanged_save_keeps_revision(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a")])
        revision = backend.load_accounts_revision()
        backend.save_accounts([_account("a")])
        self.assertEqual(backend.load_accounts_revision(), revision)

    def test_save_accounts_deletes_removed_rows(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b")])
        backend.save_accounts([_account("a")])
        self.assertEqual([item["access_token"] for item in backend.load_accounts()], ["a"])

    def test_mutate_accounts_upserts_and_deletes(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b")])
        ids_before = self.row_ids(backend)
        snapshot = backend.load_accounts_snapshot()

        result = backend.mutate_accounts(
            StorageMutation(
                upserts=(_account("a", quota=1), _account("c")),
                delete_keys=("b",),
                expected_revision=snapshot.revision,
            )
        )

        self.assertEqual((result.inserted, result.updated, result.deleted), (1, 1, 1))
        self.assertEqual(int(result.revision), int(snapshot.revision) + 1)
        ids_after = self.row_ids(backend)
        self.assertEqual(ids_after["a"], ids_before["a"])
        self.assertNotIn("b", ids_after)
        self.assertIn("c", ids_after)

    def test_stale_revision_is_rejected_without_writing(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a")])
        stale = backend.load_accounts_revision()
        backend.mutate_accounts(StorageMutation(upserts=(_account("a", quota=5),), expected_revision=stale))

        with self.assertRaises(StorageRevisionConflictError):
            backend.mutate_accounts(StorageMutation(upserts=(_account("a", quota=9),), expected_revision=stale))
        self.assertEqual(backend.load_accounts()[0]["quota"], 5)

    def test_legacy_database_is_migrated_on_startup(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "access_token VARCHAR(2048) NOT NULL UNIQUE, data TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE auth_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "key_id VARCHAR(255) NOT NULL UNIQUE, data TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO accounts (access_token, data) VALUES (?, ?)",
                ("legacy", '{"access_token": "legacy", "status": "正常"}'),
            )
        conn.close()

        backend = self.make_backend()
        self.assertEqual(backend.load_accounts_revision(), "0")
        self.assertEqual(backend.load_accounts()[0]["access_token"], "legacy")
        # 再次启动不会重复迁移
        backend.engine.dispose()
        self.assertEqual(self.make_backend().load_accounts_revision(), "0")

    def test_auth_keys_save_is_incremental(self) -> None:
        backend = self.make_backend()
        backend.save_auth_keys([{"id": "k1", "name": "one"}, {"id": "k2", "name": "two"}])
        backend.save_auth_keys([{"id": "k1", "name": "uno"}])
        self.assertEqual(backend.load_auth_keys(), [{"id": "k1", "name": "uno"}])

    def test_concurrent_writers_do_not_deadlock(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account(f"t{i}") for i in range(8)])
        errors: list[BaseException] = []

        def writer(index: int) -> None:
            try:
                for step in range(40):
                    backend.mutate_accounts(StorageMutation(upserts=(_account(f"t{index}", quota=step),)))
                    backend.load_accounts_revision()
                    if step % 10 == 0:
                        backend.load_accounts_snapshot()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads), "writer threads deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(int(backend.load_accounts_revision()), 1 + 8 * 40)
        quotas = {item["access_token"]: item["quota"] for item in backend.load_accounts()}
        self.assertEqual(set(quotas.values()), {39})


class AccountServicePersistenceTests(_TempDbCase):
    def setUp(self) -> None:
        super().setUp()
        self._log = patch("services.account_service.log_service")
        self._log.start()
        self.addCleanup(self._log.stop)

    def make_service(self, backend) -> AccountService:
        with patch.object(AccountService, "_load_cumulative_total", return_value=0):
            return AccountService(backend)

    def test_legacy_accounts_get_management_ids_persisted_once(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b")])
        self.make_service(backend)
        ids = {item["access_token"]: item.get("management_id") for item in backend.load_accounts()}
        self.assertTrue(all(str(value).startswith("acct_") for value in ids.values()))
        with patch.object(backend, "mutate_accounts", wraps=backend.mutate_accounts) as mutate:
            second = self.make_service(backend)
        mutate.assert_not_called()
        self.assertEqual(second.get_account("a")["management_id"], ids["a"])

    def test_single_account_update_only_writes_that_account(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b"), _account("c")])
        service = self.make_service(backend)

        with patch.object(backend, "mutate_accounts", wraps=backend.mutate_accounts) as mutate, \
                patch.object(backend, "save_accounts", wraps=backend.save_accounts) as save_all:
            service.update_account("b", {"quota": 2}, quiet=True)

        save_all.assert_not_called()
        mutation = mutate.call_args.args[0]
        self.assertEqual([item["access_token"] for item in mutation.upserts], ["b"])
        self.assertEqual(mutation.delete_keys, ())

    def test_no_change_skips_storage(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a")])
        service = self.make_service(backend)
        with patch.object(backend, "mutate_accounts") as mutate:
            with service._lock:
                service._save_accounts()
        mutate.assert_not_called()

    def test_delete_is_persisted_as_delete_key(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b")])
        service = self.make_service(backend)
        with patch.object(backend, "mutate_accounts", wraps=backend.mutate_accounts) as mutate:
            service.delete_accounts(["a"], return_items=False)
        mutation = mutate.call_args.args[0]
        self.assertEqual(mutation.upserts, ())
        self.assertEqual(mutation.delete_keys, ("a",))
        self.assertEqual([item["access_token"] for item in backend.load_accounts()], ["b"])

    def test_conflict_merges_changes_from_other_writer(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a"), _account("b")])
        first = self.make_service(backend)
        second = self.make_service(backend)

        first.update_account("a", {"quota": 1}, quiet=True)
        # second 的修订号已过期：冲突后合并远端，再写自己的变更，不能覆盖 first 的修改
        second.update_account("b", {"quota": 2}, quiet=True)

        stored = {item["access_token"]: item["quota"] for item in backend.load_accounts()}
        self.assertEqual(stored, {"a": 1, "b": 2})
        with second._lock:
            self.assertEqual(second._accounts["a"]["quota"], 1)

    def test_sync_skips_reload_when_revision_unchanged(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a")])
        service = self.make_service(backend)
        service._accounts_revision_checked_at = 0.0

        with patch.object(backend, "load_accounts_snapshot", wraps=backend.load_accounts_snapshot) as snapshot:
            self.assertFalse(service.sync_accounts_from_storage())
        snapshot.assert_not_called()

    def test_sync_reloads_after_external_write(self) -> None:
        backend = self.make_backend()
        backend.save_accounts([_account("a")])
        service = self.make_service(backend)
        other = self.make_service(backend)
        other.update_account("a", {"quota": 4}, quiet=True)

        service._accounts_revision_checked_at = 0.0
        self.assertTrue(service.sync_accounts_from_storage())
        with service._lock:
            self.assertEqual(service._accounts["a"]["quota"], 4)

    def test_non_incremental_backend_falls_back_to_full_save(self) -> None:
        class _MemoryStorage:
            def __init__(self) -> None:
                # 已带管理 ID 的账号：启动时无需补齐落盘，所以首次空保存不触碰存储。
                self.accounts = [
                    {**_account("a"), "management_id": "acct_" + "a" * 24},
                    {**_account("b"), "management_id": "acct_" + "b" * 24},
                ]
                self.saves = 0

            def load_accounts(self) -> list[dict]:
                return [dict(item) for item in self.accounts]

            def save_accounts(self, accounts: list[dict]) -> None:
                self.saves += 1
                self.accounts = [dict(item) for item in accounts]

        storage = _MemoryStorage()
        service = self.make_service(storage)
        with service._lock:
            service._save_accounts()
        self.assertEqual(storage.saves, 0)

        service.update_account("a", {"quota": 1}, quiet=True)
        self.assertEqual(storage.saves, 1)
        self.assertEqual({item["access_token"] for item in storage.accounts}, {"a", "b"})
        self.assertFalse(service.sync_accounts_from_storage())


class SqliteConnectionTuningTests(_TempDbCase):
    """WAL / foreign_keys / synchronous=NORMAL on SQLite, env-driven busy timeout and PG pool."""

    def _pragma(self, backend: DatabaseStorageBackend, name: str):
        with backend.engine.connect() as connection:
            return connection.exec_driver_sql(f"PRAGMA {name}").scalar()

    def test_sqlite_connections_use_wal_foreign_keys_and_normal_sync(self) -> None:
        backend = self.make_backend()
        self.assertEqual(str(self._pragma(backend, "journal_mode")).lower(), "wal")
        self.assertEqual(self._pragma(backend, "foreign_keys"), 1)
        self.assertEqual(self._pragma(backend, "synchronous"), 1)  # NORMAL
        self.assertEqual(self._pragma(backend, "busy_timeout"), 30000)

    def test_busy_timeout_comes_from_environment(self) -> None:
        with patch.dict("os.environ", {"SQLITE_BUSY_TIMEOUT_SECONDS": "7"}):
            backend = self.make_backend()
            self.assertEqual(self._pragma(backend, "busy_timeout"), 7000)

    def test_invalid_busy_timeout_falls_back_to_default(self) -> None:
        from services.storage import database_storage as module

        with patch.dict("os.environ", {"SQLITE_BUSY_TIMEOUT_SECONDS": "abc"}):
            self.assertEqual(module.sqlite_busy_timeout_seconds(), 30.0)
        with patch.dict("os.environ", {"SQLITE_BUSY_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(module.sqlite_busy_timeout_seconds(), 1.0)

    def test_sqlite_keeps_single_connection_pool(self) -> None:
        # DATABASE_POOL_SIZE must not widen the SQLite pool (writes are serialised by design).
        with patch.dict("os.environ", {"DATABASE_POOL_SIZE": "9"}):
            backend = self.make_backend()
        self.assertEqual(backend.engine.pool.size(), 1)

    def test_postgres_pool_options_are_bounded_and_env_driven(self) -> None:
        from services.storage import database_storage as module

        self.assertEqual(
            module.postgres_pool_options(),
            {"pool_size": 10, "max_overflow": 20, "pool_timeout": 30},
        )
        env = {"DATABASE_POOL_SIZE": "3", "DATABASE_MAX_OVERFLOW": "0", "DATABASE_POOL_TIMEOUT_SECONDS": "5"}
        with patch.dict("os.environ", env):
            self.assertEqual(
                module.postgres_pool_options(),
                {"pool_size": 3, "max_overflow": 0, "pool_timeout": 5},
            )
        with patch.dict("os.environ", {"DATABASE_POOL_SIZE": "0", "DATABASE_POOL_TIMEOUT_SECONDS": "x"}):
            options = module.postgres_pool_options()
            self.assertEqual(options["pool_size"], 1)
            self.assertEqual(options["pool_timeout"], 30)

    def test_postgres_engine_receives_pool_options(self) -> None:
        from services.storage import database_storage as module

        captured: dict = {}

        def fake_create_engine(url, **kwargs):
            captured.update(kwargs)
            return object()

        with patch.dict("os.environ", {"DATABASE_POOL_SIZE": "4"}), \
                patch.object(module, "create_engine", side_effect=fake_create_engine), \
                patch.object(module.Base.metadata, "create_all"), \
                patch.object(module, "sessionmaker"), \
                patch.object(module.DatabaseStorageBackend, "_ensure_revision_row"):
            module.DatabaseStorageBackend("postgresql://u:p@localhost/db")
        self.assertEqual(captured["pool_size"], 4)
        self.assertEqual(captured["max_overflow"], 20)
        self.assertEqual(captured["pool_timeout"], 30)
        self.assertTrue(captured["pool_pre_ping"])

    def test_concurrent_writers_do_not_deadlock_or_report_locked(self) -> None:
        backend = self.make_backend()
        errors: list[Exception] = []

        def work(seed: int) -> None:
            try:
                for index in range(15):
                    backend.save_accounts([_account(f"w{seed}-{index}")])
                    backend.load_accounts()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
