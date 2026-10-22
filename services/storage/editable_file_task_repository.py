# Ported from yukkcat/chatgpt2api (AGPL-3.0-only); storage reworked to the pluggable JSON/database backends.
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Boolean, Column, Float, String, Text
from sqlalchemy.exc import IntegrityError

from services.json_file import read_json_file, write_json_file
from services.storage.call_record_repository import RecordBase

_UPDATE_FIELDS = {
    "status",
    "updated_at",
    "updated_ts",
    "started_ts",
    "ended_ts",
    "result",
    "error",
    "account_email",
    "delete_pending",
}


def _clean(value: object) -> str:
    return str(value or "").strip()


class EditableFileTaskRepository(Protocol):
    """PPT/PSD 任务存储接口：JSON 文件与数据库两种实现可互换。"""

    def get(self, owner_id: str, task_id: str) -> dict[str, Any] | None: ...

    def list_for_owner(self, owner_id: str, *, task_ids: list[str] | None = None, limit: int = 0) -> list[dict[str, Any]]: ...

    def list_by_status(self, statuses: set[str]) -> list[dict[str, Any]]: ...

    def list_delete_pending(self) -> list[dict[str, Any]]: ...

    def create(self, task: dict[str, Any]) -> tuple[dict[str, Any], bool]: ...

    def update(self, owner_id: str, task_id: str, **updates: Any) -> dict[str, Any] | None: ...

    def delete(self, owner_id: str, task_id: str) -> bool: ...


def _check_updates(updates: dict[str, Any]) -> None:
    unknown = set(updates) - _UPDATE_FIELDS
    if unknown:
        raise ValueError(f"unsupported editable file task fields: {sorted(unknown)}")


def _sorted_recent(tasks: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    # 与数据库实现一致：updated_at 倒序，同时间按 id 升序。
    ordered = sorted(tasks, key=lambda item: str(item.get("id") or ""))
    ordered.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return ordered[:limit] if limit > 0 else ordered


class JsonEditableFileTaskRepository:
    """默认实现：沿用 data/editable_file_tasks.json（{"tasks": [...]}）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[tuple[str, str], dict[str, Any]] = self._load()

    def _load(self) -> dict[tuple[str, str], dict[str, Any]]:
        raw = read_json_file(
            self.path,
            name=self.path.name,
            default_factory=dict,
            expected_types=(dict, list),
        )
        tasks: dict[tuple[str, str], dict[str, Any]] = {}
        for item in (raw.get("tasks") if isinstance(raw, dict) else raw) or []:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            task = copy.deepcopy(item)
            task["id"], task["owner_id"] = task_id, owner
            task["kind"] = "psd" if item.get("kind") == "psd" else "ppt"
            task["status"] = _clean(item.get("status")) or "error"
            tasks[(owner, task_id)] = task
        return tasks

    def _save(self) -> None:
        write_json_file(self.path, {"tasks": _sorted_recent(list(self._tasks.values()), 0)})

    def get(self, owner_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get((owner_id, task_id))
            return copy.deepcopy(task) if task is not None else None

    def list_for_owner(self, owner_id: str, *, task_ids: list[str] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            if task_ids is not None and not task_ids:
                return []
            wanted = set(task_ids) if task_ids is not None else None
            items = [
                task for (owner, task_id), task in self._tasks.items()
                if owner == owner_id and (wanted is None or task_id in wanted)
            ]
            return copy.deepcopy(_sorted_recent(items, limit))

    def list_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy([task for task in self._tasks.values() if task.get("status") in statuses])

    def list_delete_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy([task for task in self._tasks.values() if task.get("delete_pending")])

    def create(self, task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        owner, task_id = _clean(task.get("owner_id")), _clean(task.get("id"))
        if not owner or not task_id:
            raise ValueError("editable file task owner_id and id are required")
        with self._lock:
            existing = self._tasks.get((owner, task_id))
            if existing is not None:
                return copy.deepcopy(existing), False
            self._tasks[(owner, task_id)] = copy.deepcopy(task)
            self._save()
            return copy.deepcopy(task), True

    def update(self, owner_id: str, task_id: str, **updates: Any) -> dict[str, Any] | None:
        _check_updates(updates)
        with self._lock:
            task = self._tasks.get((owner_id, task_id))
            if task is None:
                return None
            for name, value in updates.items():
                if name in {"error", "account_email"} and not value:
                    task.pop(name, None)
                elif name == "delete_pending" and not value:
                    task.pop(name, None)
                elif name in {"started_ts", "ended_ts"} and value is None:
                    task.pop(name, None)
                else:
                    task[name] = copy.deepcopy(value)
            self._save()
            return copy.deepcopy(task)

    def delete(self, owner_id: str, task_id: str) -> bool:
        with self._lock:
            if self._tasks.pop((owner_id, task_id), None) is None:
                return False
            self._save()
            return True


class EditableFileTaskModel(RecordBase):
    __tablename__ = "editable_file_tasks"

    owner_id = Column(String(256), primary_key=True)
    task_id = Column(String(160), primary_key=True)
    storage_id = Column(String(64), nullable=False, default="")
    status = Column(String(24), nullable=False, index=True)
    kind = Column(String(8), nullable=False)
    model = Column(String(128), nullable=True)
    created_at = Column(String(64), nullable=False)
    updated_at = Column(String(64), nullable=False, index=True)
    created_ts = Column(Float, nullable=False, default=0.0)
    updated_ts = Column(Float, nullable=False, default=0.0)
    started_ts = Column(Float, nullable=True)
    ended_ts = Column(Float, nullable=True)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    account_email = Column(String(320), nullable=True)
    delete_pending = Column(Boolean, nullable=False, default=False, index=True)


class DatabaseEditableFileTaskRepository:
    """存储后端为 SQLite/PostgreSQL 时使用：复用账号存储的引擎和 SQLite 串行锁。"""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        RecordBase.metadata.create_all(backend.engine, tables=[EditableFileTaskModel.__table__])

    @staticmethod
    def _task_from_row(row: EditableFileTaskModel) -> dict[str, Any]:
        task: dict[str, Any] = {
            "id": row.task_id,
            "storage_id": row.storage_id,
            "owner_id": row.owner_id,
            "status": row.status,
            "kind": row.kind,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "created_ts": float(row.created_ts or 0),
            "updated_ts": float(row.updated_ts or 0),
        }
        for name in ("model", "started_ts", "ended_ts", "error", "account_email"):
            value = getattr(row, name)
            if value not in (None, ""):
                task[name] = value
        if row.result:
            try:
                value = json.loads(row.result)
            except (TypeError, ValueError):
                value = None
            if isinstance(value, dict) and value:
                task["result"] = value
        if row.delete_pending:
            task["delete_pending"] = True
        return task

    @staticmethod
    def _required(task: dict[str, Any], name: str) -> str:
        value = _clean(task.get(name))
        if not value:
            raise ValueError(f"editable file task {name} is required")
        return value

    @classmethod
    def _row_from_task(cls, task: dict[str, Any]) -> EditableFileTaskModel:
        result = task.get("result")
        return EditableFileTaskModel(
            owner_id=cls._required(task, "owner_id"),
            task_id=cls._required(task, "id"),
            storage_id=_clean(task.get("storage_id")),
            status=cls._required(task, "status"),
            kind=cls._required(task, "kind"),
            model=_clean(task.get("model")) or None,
            created_at=cls._required(task, "created_at"),
            updated_at=cls._required(task, "updated_at"),
            created_ts=float(task.get("created_ts") or 0),
            updated_ts=float(task.get("updated_ts") or 0),
            started_ts=float(task["started_ts"]) if task.get("started_ts") is not None else None,
            ended_ts=float(task["ended_ts"]) if task.get("ended_ts") is not None else None,
            result=json.dumps(result, ensure_ascii=False) if isinstance(result, dict) and result else None,
            error=str(task.get("error") or "") or None,
            account_email=_clean(task.get("account_email")) or None,
            delete_pending=bool(task.get("delete_pending")),
        )

    def get(self, owner_id: str, task_id: str) -> dict[str, Any] | None:
        with self.backend.session() as session:
            row = session.get(EditableFileTaskModel, (owner_id, task_id))
            return self._task_from_row(row) if row is not None else None

    def list_for_owner(self, owner_id: str, *, task_ids: list[str] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        with self.backend.session() as session:
            query = session.query(EditableFileTaskModel).filter(EditableFileTaskModel.owner_id == owner_id)
            if task_ids is not None:
                if not task_ids:
                    return []
                query = query.filter(EditableFileTaskModel.task_id.in_(task_ids))
            query = query.order_by(EditableFileTaskModel.updated_at.desc(), EditableFileTaskModel.task_id.asc())
            if limit > 0:
                query = query.limit(limit)
            return [self._task_from_row(row) for row in query.all()]

    def list_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        with self.backend.session() as session:
            rows = session.query(EditableFileTaskModel).filter(
                EditableFileTaskModel.status.in_(sorted(statuses))
            ).all()
            return [self._task_from_row(row) for row in rows]

    def list_delete_pending(self) -> list[dict[str, Any]]:
        with self.backend.session() as session:
            rows = session.query(EditableFileTaskModel).filter(
                EditableFileTaskModel.delete_pending.is_(True)
            ).all()
            return [self._task_from_row(row) for row in rows]

    def create(self, task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        row = self._row_from_task(task)
        with self.backend.session() as session:
            try:
                session.add(row)
                session.commit()
                return self._task_from_row(row), True
            except IntegrityError:
                session.rollback()
                existing = session.get(EditableFileTaskModel, (row.owner_id, row.task_id))
                if existing is None:
                    raise
                return self._task_from_row(existing), False
            except Exception:
                session.rollback()
                raise

    def update(self, owner_id: str, task_id: str, **updates: Any) -> dict[str, Any] | None:
        _check_updates(updates)
        with self.backend.session() as session:
            try:
                row = session.get(EditableFileTaskModel, (owner_id, task_id))
                if row is None:
                    return None
                for name, value in updates.items():
                    if name == "result":
                        value = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) and value else None
                    elif name in {"started_ts", "ended_ts", "updated_ts"}:
                        value = float(value) if value is not None else None
                    elif name == "delete_pending":
                        value = bool(value)
                    elif name in {"error", "account_email"}:
                        value = str(value or "") or None
                    setattr(row, name, value)
                session.commit()
                return self._task_from_row(row)
            except Exception:
                session.rollback()
                raise

    def delete(self, owner_id: str, task_id: str) -> bool:
        with self.backend.session() as session:
            try:
                row = session.get(EditableFileTaskModel, (owner_id, task_id))
                if row is None:
                    return False
                session.delete(row)
                session.commit()
                return True
            except Exception:
                session.rollback()
                raise


def resolve_editable_file_task_repository(json_path: Path) -> EditableFileTaskRepository:
    """存储后端是数据库时任务落库，否则沿用 JSON 文件。"""
    from services.storage.factory import database_backend_or_none

    backend = database_backend_or_none()
    if backend is not None:
        return DatabaseEditableFileTaskRepository(backend)
    return JsonEditableFileTaskRepository(json_path)
