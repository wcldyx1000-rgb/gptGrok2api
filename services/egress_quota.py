"""按出口限速：每个出口（代理地址）每天最多注册几个号、两次之间至少隔多久。

同一个 IP 短时间内连续注册很像批量操作；把注册摊到多个出口、拉开间隔。
每个出口当天用量和下次可用时间存在 data/egress_usage.json 里，重启不丢。
默认不限制（上限 0、间隔 0），只有在注册配置里设置了才生效。
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.json_file import read_json_object, write_json_file
from services.proxy_service import _redact_url_credentials, normalize_proxy_url

USAGE_FILE = DATA_DIR / "egress_usage.json"
_lock = threading.Lock()


def _today(now: float | None = None) -> str:
    # 按北京时间切日，和控制台其它“今日”统计一致。
    moment = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc) + timedelta(hours=8)
    return moment.strftime("%Y-%m-%d")


def _key(proxy: str) -> str:
    return normalize_proxy_url(str(proxy or "").strip()) or "direct"


def _load(path: Path) -> dict[str, Any]:
    return read_json_object(path, name=path.name) if path.exists() else {}


def normalize_settings(raw: object) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    try:
        daily_limit = max(0, min(10000, int(source.get("daily_limit") or 0)))
    except (TypeError, ValueError):
        daily_limit = 0
    try:
        min_gap_secs = max(0.0, min(86400.0, float(source.get("min_gap_secs") or 0)))
    except (TypeError, ValueError):
        min_gap_secs = 0.0
    return {"daily_limit": daily_limit, "min_gap_secs": min_gap_secs}


def is_active(settings: dict[str, Any]) -> bool:
    return int(settings.get("daily_limit") or 0) > 0 or float(settings.get("min_gap_secs") or 0) > 0


def acquire(
    candidates: list[str],
    *,
    daily_limit: int,
    min_gap_secs: float,
    path: Path | None = None,
    now: float | None = None,
) -> tuple[str | None, float | None]:
    """从候选出口里挑一个现在能用的并记一次用量。

    返回 (出口, None)；都在间隔期返回 (None, 最短还要等几秒)；都已到当天上限返回 (None, None)。
    """
    if not candidates:
        return None, None
    store_path = path or USAGE_FILE
    current = time.time() if now is None else now
    today = _today(current)
    with _lock:
        store = _load(store_path)
        best: tuple[int, float, int] | None = None
        soonest: float | None = None
        for index, proxy in enumerate(candidates):
            usage = store.get(_key(proxy)) or {}
            count = int(usage.get("count") or 0) if usage.get("day") == today else 0
            if daily_limit > 0 and count >= daily_limit:
                continue
            wait = float(usage.get("next_at") or 0) - current
            if wait > 0:
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            # 当天用得最少、最久没用的出口优先，把注册摊开。
            rank = (count, float(usage.get("last_at") or 0), index)
            if best is None or rank < best:
                best = rank
        if best is None:
            return None, soonest
        proxy = candidates[best[2]]
        usage = store.get(_key(proxy)) or {}
        count = int(usage.get("count") or 0) if usage.get("day") == today else 0
        store[_key(proxy)] = {
            "day": today,
            "count": count + 1,
            "last_at": current,
            # 间隔带随机浮动（1～1.6 倍），不要每次整整齐齐。
            "next_at": current + max(0.0, float(min_gap_secs)) * random.uniform(1.0, 1.6),
        }
        write_json_file(store_path, store, backup=False)
        return proxy, None


def usage_today(proxy: str, *, path: Path | None = None, now: float | None = None) -> int:
    usage = _load(path or USAGE_FILE).get(_key(proxy)) or {}
    return int(usage.get("count") or 0) if usage.get("day") == _today(now) else 0


def snapshot(*, path: Path | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """各出口今日用量（地址已脱敏），供页面展示。"""
    current = time.time() if now is None else now
    today = _today(current)
    rows = []
    for key, usage in _load(path or USAGE_FILE).items():
        if not isinstance(usage, dict):
            continue
        rows.append({
            "proxy": "直连" if key == "direct" else _redact_url_credentials(key),
            "count_today": int(usage.get("count") or 0) if usage.get("day") == today else 0,
            "next_available_in": max(0, int(float(usage.get("next_at") or 0) - current)),
        })
    return sorted(rows, key=lambda item: -item["count_today"])
