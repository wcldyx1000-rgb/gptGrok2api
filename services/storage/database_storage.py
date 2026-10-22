from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator

from sqlalchemy import Column, String, Text, create_engine, Integer, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

from services.storage.base import (
    StorageBackend,
    StorageMutation,
    StorageMutationResult,
    StorageRevisionConflictError,
    StorageSnapshot,
)
from utils.sqlite_runtime import sqlite_connection_guard

Base = declarative_base()

_ACCOUNTS = "accounts"
_AUTH_KEYS = "auth_keys"
_IN_CHUNK = 500


class AccountModel(Base):
    """账号数据模型"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(2048), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)  # JSON 格式存储完整账号数据


class AuthKeyModel(Base):
    """鉴权密钥数据模型"""
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class StorageRevisionModel(Base):
    """集合修订号：每次写入 +1，用于乐观并发检查和跳过无变化的重新加载"""
    __tablename__ = "storage_revisions"

    collection = Column(String(64), primary_key=True)
    revision = Column(Integer, nullable=False, default=0)


def _env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


def sqlite_busy_timeout_seconds() -> float:
    """SQLITE_BUSY_TIMEOUT_SECONDS（默认 30，最小 1）：写锁冲突时等待多久才报 database is locked。"""
    return _env_float("SQLITE_BUSY_TIMEOUT_SECONDS", 30.0)


def postgres_pool_options() -> dict[str, int]:
    """PostgreSQL 有界连接池：DATABASE_POOL_SIZE / DATABASE_MAX_OVERFLOW / DATABASE_POOL_TIMEOUT_SECONDS。"""
    return {
        "pool_size": _env_int("DATABASE_POOL_SIZE", 10, minimum=1),
        "max_overflow": _env_int("DATABASE_MAX_OVERFLOW", 20, minimum=0),
        "pool_timeout": _env_int("DATABASE_POOL_TIMEOUT_SECONDS", 30, minimum=1),
    }


def _configure_sqlite_connection(dbapi_connection, busy_timeout_ms: int) -> None:
    """每个新 SQLite 连接：外键、busy_timeout、WAL、synchronous=NORMAL（仅 SQLite 调用）。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _dumps(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False)


def _loads(raw: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class DatabaseStorageBackend(StorageBackend):
    """数据库存储后端（支持 SQLite、PostgreSQL、MySQL 等）"""

    supports_account_mutations = True

    def __init__(self, database_url: str):
        self.database_url = database_url
        url = make_url(database_url)
        self._is_sqlite = url.get_backend_name() == "sqlite"
        # SQLite 只允许一个写入者；进程内串行化可以避免 "database is locked"，
        # 也让下面的单连接池不会出现并发打开/关闭连接。
        self._sqlite_lock = threading.RLock() if self._is_sqlite else None
        if self._is_sqlite:
            in_memory = url.database in (None, "", ":memory:")
            busy_timeout = sqlite_busy_timeout_seconds()
            self.engine = create_engine(
                database_url,
                connect_args={"check_same_thread": False, "timeout": busy_timeout},
                pool_pre_ping=True,
                **(
                    {"poolclass": StaticPool}
                    if in_memory
                    # 单个长期连接：不按时回收，避免运行期间反复打开/关闭 SQLite 连接
                    # （8e7fe7d 修过并发打开/关闭导致的死锁）。
                    else {"poolclass": QueuePool, "pool_size": 1, "max_overflow": 0, "pool_recycle": -1}
                ),
            )

            @event.listens_for(self.engine, "do_connect")
            def _guarded_connect(dialect, conn_rec, cargs, cparams):  # noqa: ANN001
                with sqlite_connection_guard:
                    return sqlite3.connect(*cargs, **cparams)

            busy_timeout_ms = int(busy_timeout * 1000)

            @event.listens_for(self.engine, "connect")
            def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
                _configure_sqlite_connection(dbapi_connection, busy_timeout_ms)
        else:
            self.engine = create_engine(
                database_url,
                pool_pre_ping=True,  # 自动检测连接是否有效
                pool_recycle=3600,   # 1小时回收连接
                **postgres_pool_options(),
            )
        # create_all 只创建缺失的表，老库启动时会自动补上 storage_revisions。
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        for collection in (_ACCOUNTS, _AUTH_KEYS):
            self._ensure_revision_row(collection)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self._sqlite_lock if self._sqlite_lock is not None else nullcontext():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

    def session(self):
        """供同库的其它仓储（调用记录、看板指标）复用引擎与 SQLite 串行锁。"""
        return self._session()

    def _ensure_revision_row(self, collection: str) -> None:
        with self._session() as session:
            if session.get(StorageRevisionModel, collection) is not None:
                return
            session.add(StorageRevisionModel(collection=collection, revision=0))
            try:
                session.commit()
            except IntegrityError:
                # 另一个进程同时完成了迁移
                session.rollback()

    @staticmethod
    def _read_revision(session: Session, collection: str) -> str | None:
        value = session.execute(
            text("SELECT revision FROM storage_revisions WHERE collection = :c"),
            {"c": collection},
        ).scalar()
        return None if value is None else str(value)

    def _bump_revision(self, session: Session, collection: str, expected: str | None) -> str:
        """修订号 +1；expected 不为 None 时只在修订号未变时成功（CAS）。

        UPDATE 本身会拿到行锁/写锁，所以必须放在同一事务的最前面。
        """
        if expected is None:
            result = session.execute(
                text("UPDATE storage_revisions SET revision = revision + 1 WHERE collection = :c"),
                {"c": collection},
            )
        else:
            try:
                expected_int = int(expected)
            except (TypeError, ValueError):
                raise StorageRevisionConflictError(collection, expected, self._read_revision(session, collection))
            result = session.execute(
                text(
                    "UPDATE storage_revisions SET revision = revision + 1 "
                    "WHERE collection = :c AND revision = :e"
                ),
                {"c": collection, "e": expected_int},
            )
        if result.rowcount == 0:
            current = self._read_revision(session, collection)
            if current is None and expected is None:
                session.add(StorageRevisionModel(collection=collection, revision=1))
                session.flush()
                return "1"
            raise StorageRevisionConflictError(collection, expected, current)
        return self._read_revision(session, collection) or "0"

    @staticmethod
    def _key_column(model: type[AccountModel] | type[AuthKeyModel]):
        return model.access_token if model is AccountModel else model.key_id

    def _apply_rows(
        self,
        session: Session,
        model: type[AccountModel] | type[AuthKeyModel],
        upserts: dict[str, dict[str, Any]],
        delete_keys: list[str],
    ) -> tuple[int, int, int]:
        key_column = self._key_column(model)
        key_attr = key_column.key
        inserted = updated = deleted = 0
        keys = list(upserts)
        for start in range(0, len(keys), _IN_CHUNK):
            chunk = keys[start:start + _IN_CHUNK]
            existing = {
                getattr(row, key_attr): row
                for row in session.query(model).filter(key_column.in_(chunk)).all()
            }
            for key in chunk:
                item = upserts[key]
                row = existing.get(key)
                if row is None:
                    session.add(model(**{key_attr: key}, data=_dumps(item)))
                    inserted += 1
                elif _loads(row.data) != item:
                    row.data = _dumps(item)
                    updated += 1
        for start in range(0, len(delete_keys), _IN_CHUNK):
            chunk = delete_keys[start:start + _IN_CHUNK]
            deleted += session.query(model).filter(key_column.in_(chunk)).delete(synchronize_session=False)
        return inserted, updated, deleted

    def load_accounts(self) -> list[dict[str, Any]]:
        """从数据库加载账号数据"""
        return self._load_rows(AccountModel)

    def load_accounts_snapshot(self) -> StorageSnapshot:
        with self._session() as session:
            # 先读修订号再读数据：中间若有写入，下一次 CAS 会冲突并重新加载，不会丢更新。
            revision = self._read_revision(session, _ACCOUNTS)
            items = self._rows_from_session(session, AccountModel)
        return StorageSnapshot(items=items, revision=revision)

    def load_accounts_revision(self) -> str | None:
        with self._session() as session:
            return self._read_revision(session, _ACCOUNTS)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到数据库（与库中现有数据比对，只写有变化的行）"""
        self._save_rows(AccountModel, _ACCOUNTS, accounts, "access_token")

    def mutate_accounts(self, mutation: StorageMutation) -> StorageMutationResult:
        upserts: dict[str, dict[str, Any]] = {}
        for item in mutation.upserts:
            key = str(item.get("access_token") or "").strip() if isinstance(item, dict) else ""
            if key:
                upserts[key] = item
        delete_keys = [key for key in dict.fromkeys(str(k or "").strip() for k in mutation.delete_keys) if key]
        with self._session() as session:
            try:
                revision = self._bump_revision(session, _ACCOUNTS, mutation.expected_revision)
                inserted, updated, deleted = self._apply_rows(session, AccountModel, upserts, delete_keys)
                session.commit()
            except Exception:
                session.rollback()
                raise
        return StorageMutationResult(revision=revision, inserted=inserted, updated=updated, deleted=deleted)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从数据库加载鉴权密钥数据"""
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到数据库"""
        self._save_rows(AuthKeyModel, _AUTH_KEYS, auth_keys, "id")

    @staticmethod
    def _rows_from_session(session: Session, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        items = []
        for (raw,) in session.query(model.data).order_by(model.id).all():
            item = _loads(raw)
            if item is not None:
                items.append(item)
        return items

    def _load_rows(self, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        with self._session() as session:
            return self._rows_from_session(session, model)

    def _save_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        collection: str,
        items: list[dict[str, Any]],
        source_key: str,
    ) -> None:
        desired: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            key_value = str(item.get(source_key) or "").strip()
            if key_value:
                desired[key_value] = item
        key_column = self._key_column(model)
        with self._session() as session:
            try:
                existing = {key: _loads(raw) for key, raw in session.query(key_column, model.data).all()}
                upserts = {key: item for key, item in desired.items() if existing.get(key) != item}
                delete_keys = [key for key in existing if key not in desired]
                if not upserts and not delete_keys:
                    return
                self._bump_revision(session, collection, None)
                self._apply_rows(session, model, upserts, delete_keys)
                session.commit()
            except Exception:
                session.rollback()
                raise

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            with self._session() as session:
                # 尝试执行简单查询
                session.execute(text("SELECT 1"))
                count = session.query(AccountModel).count()
                auth_key_count = session.query(AuthKeyModel).count()
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": count,
                    "auth_key_count": auth_key_count,
                    "accounts_revision": self._read_revision(session, _ACCOUNTS),
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"

        return {
            "type": "database",
            "db_type": db_type,
            "description": f"数据库存储 ({db_type})",
            "database_url": self._mask_password(self.database_url),
        }

    @staticmethod
    def _mask_password(url: str) -> str:
        """隐藏数据库连接字符串中的密码"""
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url
