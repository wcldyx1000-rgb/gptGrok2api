"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Canonical status semantics for remote account-import jobs, plus the global
single-task guard shared by the CPA and Sub2API importers.
"""
from __future__ import annotations

import threading

REMOTE_IMPORT_ACTIVE_STATUSES = frozenset({"pending", "running"})

# 远程导入全局只允许一个任务：检查与预留 import_job 必须在同一把锁内完成。
remote_import_start_lock = threading.RLock()


class RemoteImportConflictError(ValueError):
    """已有远程导入任务在运行（API 层翻译成 409）。"""


def import_job_is_active(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    status = str(raw.get("status") or "").strip().lower()
    return status in REMOTE_IMPORT_ACTIVE_STATUSES


def find_active_remote_import() -> dict | None:
    """返回当前进行中的远程导入 {source, id, name, job}；没有则 None。"""
    from services.cpa_service import cpa_config
    from services.sub2api_service import sub2api_config

    for pool in cpa_config.list_pools():
        if import_job_is_active(pool.get("import_job")):
            return {
                "source": "cpa",
                "id": str(pool.get("id") or ""),
                "name": str(pool.get("name") or ""),
                "job": dict(pool["import_job"]),
            }
    for server in sub2api_config.list_servers():
        if import_job_is_active(server.get("import_job")):
            return {
                "source": "sub2api",
                "id": str(server.get("id") or ""),
                "name": str(server.get("name") or ""),
                "job": dict(server["import_job"]),
            }
    return None


def ensure_no_active_remote_import() -> None:
    active = find_active_remote_import()
    if active is not None:
        label = active.get("name") or active.get("id") or active.get("source")
        raise RemoteImportConflictError(f"已有远程导入任务正在运行（{label}），请等待完成后再试")
