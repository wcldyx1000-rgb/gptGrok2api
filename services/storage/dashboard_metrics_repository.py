from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import Column, Float, Integer, String, Text, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from services.storage.call_record_repository import RecordBase, read_record_state, write_record_state

_COUNT_FIELDS = (
    "total", "success", "failed", "rate_limited",
    "text_review", "switch_requests", "switch_count", "switch_recovered",
)
_MAP_FIELDS = ("by_endpoint", "by_model", "by_status", "by_error_code", "model_time_counts")
_WRITE_RETRIES = 3
# 成功耗时口径（成功耗时趋势 / 模型耗时）新增的列；旧库在初始化时补列，历史小时桶这些列为 0。
_SUCCESS_MAP_FIELDS = ("model_success", "model_success_total_times", "model_success_time_counts")
_SUCCESS_COLUMN_DDL = {
    "success_duration_total_ms": "FLOAT NOT NULL DEFAULT 0",
    "success_duration_count": "INTEGER NOT NULL DEFAULT 0",
    "model_success": "TEXT NOT NULL DEFAULT '{}'",
    "model_success_total_times": "TEXT NOT NULL DEFAULT '{}'",
    "model_success_time_counts": "TEXT NOT NULL DEFAULT '{}'",
    # 文本回复 / 换号统计列（概览的“切号”曲线与恢复率）。
    "text_review": "INTEGER NOT NULL DEFAULT 0",
    "switch_requests": "INTEGER NOT NULL DEFAULT 0",
    "switch_count": "INTEGER NOT NULL DEFAULT 0",
    "switch_recovered": "INTEGER NOT NULL DEFAULT 0",
}


class DashboardMetricHourModel(RecordBase):
    """看板小时桶；bucket 为 "YYYY-MM-DD HH"（北京时间），日桶由小时桶汇总得到"""
    __tablename__ = "dashboard_metric_hours"

    bucket = Column(String(13), primary_key=True)
    day = Column(String(10), nullable=False, index=True)
    total = Column(Integer, nullable=False, default=0)
    success = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    rate_limited = Column(Integer, nullable=False, default=0)
    text_review = Column(Integer, nullable=False, default=0)
    switch_requests = Column(Integer, nullable=False, default=0)
    switch_count = Column(Integer, nullable=False, default=0)
    switch_recovered = Column(Integer, nullable=False, default=0)
    by_endpoint = Column(Text, nullable=False, default="{}")
    by_model = Column(Text, nullable=False, default="{}")
    by_status = Column(Text, nullable=False, default="{}")
    by_error_code = Column(Text, nullable=False, default="{}")
    model_total_times = Column(Text, nullable=False, default="{}")
    model_time_counts = Column(Text, nullable=False, default="{}")
    success_duration_total_ms = Column(Float, nullable=False, default=0.0)
    success_duration_count = Column(Integer, nullable=False, default=0)
    model_success = Column(Text, nullable=False, default="{}")
    model_success_total_times = Column(Text, nullable=False, default="{}")
    model_success_time_counts = Column(Text, nullable=False, default="{}")


def _map(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _iter_hours(data: dict[str, Any]):
    days = data.get("days") if isinstance(data.get("days"), dict) else {}
    for day_key, day in days.items():
        if not isinstance(day, dict):
            continue
        hours = day.get("hours") if isinstance(day.get("hours"), dict) else {}
        for hour_key, hour in hours.items():
            if isinstance(hour, dict):
                yield str(day_key), str(hour_key).zfill(2), hour


class DashboardMetricsRepository:
    """把 DashboardMetricsService 的 {"days": {day: {..., "hours": {...}}}} 结构增量落到数据库。"""

    def __init__(self, backend: Any):
        self.backend = backend
        RecordBase.metadata.create_all(backend.engine)
        self._ensure_success_columns()

    def _ensure_success_columns(self) -> None:
        """旧版本建的 dashboard_metric_hours 没有成功耗时列：缺哪列补哪列。"""
        table = DashboardMetricHourModel.__tablename__
        try:
            existing = {column["name"] for column in inspect(self.backend.engine).get_columns(table)}
        except Exception:
            return
        missing = [name for name in _SUCCESS_COLUMN_DDL if name not in existing]
        if not missing:
            return
        with self.backend.engine.begin() as connection:
            for name in missing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {_SUCCESS_COLUMN_DDL[name]}"))

    def get_state(self, key: str) -> dict[str, Any] | None:
        return read_record_state(self.backend, key)

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        write_record_state(self.backend, key, value)

    def is_empty(self) -> bool:
        with self.backend.session() as session:
            return session.scalar(select(func.count()).select_from(DashboardMetricHourModel)) == 0

    def merge(self, data: dict[str, Any], merge_bucket: Callable[[dict[str, Any], dict[str, Any]], None]) -> None:
        """把待写入的小时桶加到库里（同一事务，失败整体回滚）。"""
        hours = list(_iter_hours(data))
        if not hours:
            return
        for attempt in range(_WRITE_RETRIES):
            with self.backend.session() as session:
                try:
                    for day_key, hour_key, source in hours:
                        bucket_key = f"{day_key} {hour_key}"
                        row = session.get(DashboardMetricHourModel, bucket_key, with_for_update=True)
                        if row is None:
                            row = DashboardMetricHourModel(bucket=bucket_key, day=day_key)
                            session.add(row)
                            current: dict[str, Any] = {}
                        else:
                            current = self._payload(row)
                        merge_bucket(current, source)
                        self._assign(row, current)
                    session.commit()
                    return
                except IntegrityError:
                    # 另一个实例同时插入了同一个新桶：重读后再加一次。
                    session.rollback()
                    if attempt == _WRITE_RETRIES - 1:
                        raise
                except Exception:
                    session.rollback()
                    raise

    def replace_all(self, data: dict[str, Any]) -> None:
        """重建投影：清空后整体写入。"""
        with self.backend.session() as session:
            try:
                session.query(DashboardMetricHourModel).delete(synchronize_session=False)
                for day_key, hour_key, source in _iter_hours(data):
                    row = DashboardMetricHourModel(bucket=f"{day_key} {hour_key}", day=day_key)
                    self._assign(row, source)
                    session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise

    def load(self, since_day: str) -> dict[str, Any]:
        """返回 since_day（含）之后的 {"days": {day: {"hours": {...}}}}，日桶留给调用方汇总。"""
        with self.backend.session() as session:
            rows = session.scalars(
                select(DashboardMetricHourModel).where(DashboardMetricHourModel.day >= since_day)
            ).all()
            hours = [(row.day, row.bucket[-2:], self._payload(row)) for row in rows]
        days: dict[str, Any] = {}
        for day_key, hour_key, payload in hours:
            days.setdefault(day_key, {}).setdefault("hours", {})[hour_key] = payload
        return {"version": 1, "days": days}

    def prune(self, first_day: str, last_day: str) -> int:
        with self.backend.session() as session:
            try:
                removed = session.query(DashboardMetricHourModel).filter(
                    (DashboardMetricHourModel.day < first_day) | (DashboardMetricHourModel.day > last_day)
                ).delete(synchronize_session=False)
                session.commit()
            except Exception:
                session.rollback()
                raise
        return int(removed or 0)

    @staticmethod
    def _payload(row: DashboardMetricHourModel) -> dict[str, Any]:
        payload: dict[str, Any] = {key: int(getattr(row, key) or 0) for key in _COUNT_FIELDS}
        for key in _MAP_FIELDS:
            payload[key] = {name: int(value or 0) for name, value in _map(getattr(row, key)).items()}
        payload["model_total_times"] = {name: float(value or 0.0) for name, value in _map(row.model_total_times).items()}
        payload["success_duration_total_ms"] = float(row.success_duration_total_ms or 0.0)
        payload["success_duration_count"] = int(row.success_duration_count or 0)
        payload["model_success"] = {name: int(value or 0) for name, value in _map(row.model_success).items()}
        payload["model_success_total_times"] = {
            name: float(value or 0.0) for name, value in _map(row.model_success_total_times).items()
        }
        payload["model_success_time_counts"] = {
            name: int(value or 0) for name, value in _map(row.model_success_time_counts).items()
        }
        return payload

    @staticmethod
    def _assign(row: DashboardMetricHourModel, payload: dict[str, Any]) -> None:
        for key in _COUNT_FIELDS:
            setattr(row, key, int(payload.get(key, 0) or 0))
        for key in (*_MAP_FIELDS, "model_total_times", *_SUCCESS_MAP_FIELDS):
            value = payload.get(key)
            setattr(row, key, _dump(value if isinstance(value, dict) else {}))
        row.success_duration_total_ms = float(payload.get("success_duration_total_ms", 0.0) or 0.0)
        row.success_duration_count = int(payload.get("success_duration_count", 0) or 0)
