from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Column, Index, Integer, String, Text, case, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.declarative import declarative_base

from utils.log import logger

# 调用记录与看板指标共用的表元数据；与账号表分开，便于独立 create_all。
RecordBase = declarative_base()

_IN_CHUNK = 500


class CallRecordModel(RecordBase):
    """日志记录（调用日志与账号日志），payload 为完整 JSON，其余列用于过滤与统计"""
    __tablename__ = "log_records"

    sequence = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String(64), nullable=False, unique=True)
    event_time = Column(String(32), nullable=False)
    event_day = Column(String(10), nullable=False)
    type = Column(String(32), nullable=False, default="")
    status = Column(String(64), nullable=False, default="")
    status_lower = Column(String(64), nullable=False, default="")
    endpoint = Column(String(255), nullable=False, default="")
    model = Column(String(255), nullable=False, default="")
    account_email = Column(String(320), nullable=False, default="")
    conversation_id = Column(String(255), nullable=False, default="")
    is_failed = Column(Integer, nullable=False, default=0)
    is_limited = Column(Integer, nullable=False, default=0)
    is_image = Column(Integer, nullable=False, default=0)
    # 调用结果归类（success / partial_success / text_review / ...）；旧记录为空串。
    outcome = Column(String(32), nullable=False, default="", server_default="")
    search_text = Column(Text, nullable=False, default="")
    payload = Column(Text, nullable=False)

    __table_args__ = (
        Index("ix_log_records_time", "event_time", "sequence"),
        Index("ix_log_records_type_time", "type", "event_time"),
        Index("ix_log_records_day", "event_day"),
        Index("ix_log_records_endpoint", "endpoint"),
        Index("ix_log_records_model", "model"),
        Index("ix_log_records_account", "account_email"),
        Index("ix_log_records_conversation", "conversation_id"),
    )


class RecordStateModel(RecordBase):
    """一次性迁移等状态标记（key → JSON）"""
    __tablename__ = "record_store_state"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False, default="")


@dataclass(frozen=True)
class CallRecordWrite:
    """一条待写入的记录：payload 原样保存，其余字段由 LogService 按现有过滤语义预先算好"""

    payload: dict[str, Any]
    status: str = ""
    endpoint: str = ""
    model: str = ""
    account_email: str = ""
    conversation_id: str = ""
    is_failed: bool = False
    is_limited: bool = False
    is_image: bool = False
    search_text: str = ""
    outcome: str = ""


@dataclass(frozen=True)
class CallRecordQuery:
    type: str = ""
    start_date: str = ""
    end_date: str = ""
    status: str = ""
    endpoint: str = ""
    model: str = ""
    account: str = ""
    conversation_id: str = ""
    search: str = ""


def _clean(value: object) -> str:
    return str(value or "").strip()


def _loads(raw: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class CallRecordRepository:
    """数据库中的日志记录；复用账号存储后端的引擎与 SQLite 串行锁，避免第二个连接争锁。"""

    def __init__(self, backend: Any):
        self.backend = backend
        RecordBase.metadata.create_all(backend.engine)
        self._ensure_outcome_column()
        self._writer = _BatchWriter(self.append_many)

    def _ensure_outcome_column(self) -> None:
        """旧版本建的 log_records 没有 outcome 列：补列，历史记录该列为空串（不参与部分成功/文本筛选）。"""
        table = CallRecordModel.__tablename__
        try:
            existing = {column["name"] for column in inspect(self.backend.engine).get_columns(table)}
        except Exception:
            return
        if "outcome" in existing:
            return
        with self.backend.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN outcome VARCHAR(32) NOT NULL DEFAULT ''"))

    # ── 写入 ──────────────────────────────────────────────

    def enqueue(self, record: CallRecordWrite) -> None:
        """异步写入：请求路径只入队，后台线程批量落库。"""
        self._writer.put(record)

    def flush(self, timeout: float = 10.0) -> bool:
        return self._writer.flush(timeout)

    def close(self, timeout: float = 5.0) -> None:
        self._writer.close(timeout)

    def append_many(self, records: Iterable[CallRecordWrite]) -> int:
        """批量写入，按 id 去重（已存在的跳过），因此重复导入是幂等的。"""
        pending: dict[str, CallRecordWrite] = {}
        for record in records:
            record_id = _clean(record.payload.get("id"))
            event_time = _clean(record.payload.get("time"))
            if record_id and event_time:
                pending.setdefault(record_id, record)
        if not pending:
            return 0
        inserted = 0
        with self.backend.session() as session:
            try:
                ids = list(pending)
                for start in range(0, len(ids), _IN_CHUNK):
                    chunk = ids[start:start + _IN_CHUNK]
                    existing = set(session.scalars(select(CallRecordModel.id).where(CallRecordModel.id.in_(chunk))))
                    for record_id in chunk:
                        if record_id in existing:
                            continue
                        session.add(self._row(pending[record_id]))
                        inserted += 1
                session.commit()
            except IntegrityError:
                # 另一个实例同时写入了相同 id：回滚后逐条重试，跳过冲突行。
                session.rollback()
                inserted = self._append_one_by_one(list(pending.values()))
            except Exception:
                session.rollback()
                raise
        return inserted

    def _append_one_by_one(self, records: list[CallRecordWrite]) -> int:
        inserted = 0
        for record in records:
            with self.backend.session() as session:
                try:
                    session.add(self._row(record))
                    session.commit()
                    inserted += 1
                except IntegrityError:
                    session.rollback()
        return inserted

    @staticmethod
    def _row(record: CallRecordWrite) -> CallRecordModel:
        payload = record.payload
        event_time = _clean(payload.get("time"))
        status = _clean(record.status) or "unknown"
        return CallRecordModel(
            id=_clean(payload.get("id")),
            event_time=event_time,
            event_day=event_time[:10],
            type=_clean(payload.get("type")),
            status=status[:64],
            status_lower=status.lower()[:64],
            endpoint=_clean(record.endpoint)[:255],
            model=_clean(record.model)[:255],
            account_email=_clean(record.account_email)[:320],
            conversation_id=_clean(record.conversation_id)[:255],
            is_failed=1 if record.is_failed else 0,
            is_limited=1 if record.is_limited else 0,
            is_image=1 if record.is_image else 0,
            outcome=_clean(record.outcome).lower()[:32],
            search_text=_clean(record.search_text).lower(),
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    # ── 查询 ──────────────────────────────────────────────

    @staticmethod
    def _filters(query: CallRecordQuery) -> list[Any]:
        filters: list[Any] = []
        if query.type:
            filters.append(CallRecordModel.type == query.type)
        if query.start_date:
            filters.append(CallRecordModel.event_day >= query.start_date)
        if query.end_date:
            filters.append(CallRecordModel.event_day <= query.end_date)
        status = _clean(query.status).lower()
        if status == "success":
            filters.append(CallRecordModel.status_lower == "success")
        elif status == "failed":
            filters.append(CallRecordModel.is_failed == 1)
        elif status == "limited":
            filters.append(CallRecordModel.is_limited == 1)
        elif status in {"partial", "partial_success"}:
            filters.append(CallRecordModel.outcome == "partial_success")
        elif status in {"text", "text_review"}:
            filters.append(CallRecordModel.outcome == "text_review")
        if query.endpoint:
            filters.append(CallRecordModel.endpoint == query.endpoint)
        if query.model:
            filters.append(CallRecordModel.model == query.model)
        if query.account:
            filters.append(CallRecordModel.account_email == query.account)
        if query.conversation_id:
            filters.append(CallRecordModel.conversation_id == query.conversation_id)
        search = _clean(query.search).lower()
        if search:
            filters.append(CallRecordModel.search_text.contains(search, autoescape=True))
        return filters

    @staticmethod
    def _newest_first():
        return CallRecordModel.event_time.desc(), CallRecordModel.sequence.desc()

    @staticmethod
    def _counts(session: Any, column: Any, filters: list[Any]) -> dict[str, int]:
        rows = session.execute(
            select(column, func.count(CallRecordModel.sequence)).where(*filters).group_by(column)
        ).all()
        return {str(key): int(count) for key, count in rows if _clean(key)}

    def list(self, *, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        filters = self._filters(CallRecordQuery(type=type, start_date=start_date, end_date=end_date))
        with self.backend.session() as session:
            rows = session.scalars(
                select(CallRecordModel.payload).where(*filters).order_by(*self._newest_first()).limit(max(1, int(limit)))
            ).all()
        return [item for item in (_loads(raw) for raw in rows) if item is not None]

    def list_page(self, query: CallRecordQuery, *, limit: int, offset: int) -> dict[str, Any]:
        filters = self._filters(query)
        with self.backend.session() as session:
            totals = session.execute(
                select(
                    func.count(CallRecordModel.sequence),
                    func.sum(case((CallRecordModel.status_lower == "success", 1), else_=0)),
                    func.sum(CallRecordModel.is_failed),
                    func.sum(CallRecordModel.is_limited),
                    func.sum(CallRecordModel.is_image),
                    func.sum(case((CallRecordModel.outcome == "partial_success", 1), else_=0)),
                    func.sum(case((CallRecordModel.outcome == "text_review", 1), else_=0)),
                ).where(*filters)
            ).one()
            rows = session.scalars(
                select(CallRecordModel.payload)
                .where(*filters)
                .order_by(*self._newest_first())
                .offset(max(0, int(offset)))
                .limit(max(1, int(limit)))
            ).all()
            facets = {
                "statuses": self._counts(session, CallRecordModel.status, filters),
                "endpoints": self._counts(session, CallRecordModel.endpoint, filters),
                "models": self._counts(session, CallRecordModel.model, filters),
                "accounts": self._counts(session, CallRecordModel.account_email, filters),
            }
        total = int(totals[0] or 0)
        items = [item for item in (_loads(raw) for raw in rows) if item is not None]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "facets": facets,
            "stats": {
                "total": total,
                "success": int(totals[1] or 0),
                "failed": int(totals[2] or 0),
                "limited": int(totals[3] or 0),
                "image": int(totals[4] or 0),
                "partial_success": int(totals[5] or 0),
                "text_review": int(totals[6] or 0),
            },
        }

    def get(self, log_id: str) -> dict[str, Any] | None:
        key = _clean(log_id)
        if not key:
            return None
        with self.backend.session() as session:
            raw = session.scalar(select(CallRecordModel.payload).where(CallRecordModel.id == key).limit(1))
        return _loads(raw) if raw is not None else None

    def iter_records(self, *, type: str = "", batch_size: int = 1000) -> Iterator[dict[str, Any]]:
        """按 sequence 升序分批遍历（每批独立会话，不长期占用 SQLite 锁）。"""
        last = 0
        while True:
            filters: list[Any] = [CallRecordModel.sequence > last]
            if type:
                filters.append(CallRecordModel.type == type)
            with self.backend.session() as session:
                rows = session.execute(
                    select(CallRecordModel.sequence, CallRecordModel.payload)
                    .where(*filters)
                    .order_by(CallRecordModel.sequence.asc())
                    .limit(batch_size)
                ).all()
            if not rows:
                return
            for sequence, raw in rows:
                last = int(sequence)
                item = _loads(raw)
                if item is not None:
                    yield item

    def count(self) -> int:
        with self.backend.session() as session:
            return int(session.scalar(select(func.count(CallRecordModel.sequence))) or 0)

    # ── 删除与清理 ────────────────────────────────────────

    def delete(self, ids: Iterable[str]) -> int:
        targets = [key for key in dict.fromkeys(_clean(item) for item in ids) if key]
        if not targets:
            return 0
        removed = 0
        with self.backend.session() as session:
            try:
                for start in range(0, len(targets), _IN_CHUNK):
                    chunk = targets[start:start + _IN_CHUNK]
                    removed += session.query(CallRecordModel).filter(CallRecordModel.id.in_(chunk)).delete(
                        synchronize_session=False
                    )
                session.commit()
            except Exception:
                session.rollback()
                raise
        return int(removed)

    def cleanup_before(self, cutoff_time: str, *, dry_run: bool) -> dict[str, int | bool]:
        """删除 event_time 早于 cutoff_time（同为 "YYYY-MM-DD HH:MM:SS"）的记录。"""
        older = CallRecordModel.event_time < cutoff_time
        with self.backend.session() as session:
            try:
                removed, removed_size = session.execute(
                    select(func.count(CallRecordModel.sequence), func.sum(func.length(CallRecordModel.payload))).where(older)
                ).one()
                total = int(session.scalar(select(func.count(CallRecordModel.sequence))) or 0)
                removed = int(removed or 0)
                if not dry_run and removed:
                    session.query(CallRecordModel).filter(older).delete(synchronize_session=False)
                    session.commit()
            except Exception:
                session.rollback()
                raise
        return {
            "removed": removed,
            "kept": total - removed,
            "removed_size_bytes": int(removed_size or 0),
            "dry_run": dry_run,
        }

    # ── 状态标记 ──────────────────────────────────────────

    def get_state(self, key: str) -> dict[str, Any] | None:
        return read_record_state(self.backend, key)

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        write_record_state(self.backend, key, value)


def read_record_state(backend: Any, key: str) -> dict[str, Any] | None:
    with backend.session() as session:
        row = session.get(RecordStateModel, key)
        return _loads(row.value) if row is not None else None


def write_record_state(backend: Any, key: str, value: dict[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False)
    with backend.session() as session:
        try:
            row = session.get(RecordStateModel, key)
            if row is None:
                session.add(RecordStateModel(key=key, value=raw))
            else:
                row.value = raw
            session.commit()
        except IntegrityError:
            session.rollback()
        except Exception:
            session.rollback()
            raise


class _BatchWriter:
    """后台批量写入：请求线程只做入队，失败时保留队列稍后重试。"""

    MAX_PENDING = 50000
    BATCH_SIZE = 500
    INTERVAL_SECONDS = 0.5

    def __init__(self, sink):
        self._sink = sink
        self._queue: deque[CallRecordWrite] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._inflight = 0
        self._closed = False
        self._dropped = 0

    def put(self, record: CallRecordWrite) -> None:
        with self._cond:
            if len(self._queue) >= self.MAX_PENDING:
                self._queue.popleft()
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 1000 == 0:
                    logger.error({"event": "call_record_queue_overflow", "dropped": self._dropped})
            self._queue.append(record)
            if self._thread is None or not self._thread.is_alive():
                self._closed = False
                self._thread = threading.Thread(target=self._run, daemon=True, name="call-record-writer")
                self._thread.start()
            if len(self._queue) >= self.BATCH_SIZE:
                self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._queue:
                    if self._closed:
                        return
                    self._cond.wait(self.INTERVAL_SECONDS)
                    if not self._queue:
                        if self._closed:
                            return
                        continue
                batch = [self._queue.popleft() for _ in range(min(self.BATCH_SIZE, len(self._queue)))]
                self._inflight += len(batch)
            failed = False
            try:
                self._sink(batch)
            except Exception as exc:
                failed = True
                logger.error({"event": "call_record_write_failed", "count": len(batch), "error": str(exc)})
            with self._cond:
                self._inflight -= len(batch)
                if failed:
                    self._queue.extendleft(reversed(batch))
                self._cond.notify_all()
            if failed:
                time.sleep(1.0)

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            self._cond.notify_all()
            while self._queue or self._inflight:
                if self._thread is None or not self._thread.is_alive():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.1))
            if self._queue and (self._thread is None or not self._thread.is_alive()):
                batch = list(self._queue)
                self._queue.clear()
            else:
                return True
        # 写线程已经退出（例如进程收尾），由调用方线程同步落库。
        self._sink(batch)
        return True

    def close(self, timeout: float) -> None:
        self.flush(timeout)
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
