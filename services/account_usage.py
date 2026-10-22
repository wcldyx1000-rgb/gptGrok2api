"""Runtime image usage of Upstream Accounts and Account Pool selection pressure.

Sliding windows fed by the image scheduler: per-account attempts, recent
results, upstream throttling and refusals, plus the pool's lease hold times,
waits, timeouts and empty selections. Like the scheduler this is per process
and starts empty after a restart.

Not thread safe; the account service serializes access with its own lock.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

_HOUR = 3600.0
_DAY = 86400.0
_RECENT_RESULTS = 20
# Bounds memory for an account that is hammered with the cooldown disabled.
_MAX_TRACKED_ATTEMPTS = 20_000


def _prune(times: deque, horizon: float) -> None:
    while times and times[0] < horizon:
        times.popleft()


def _prune_pairs(items: deque, horizon: float) -> None:
    while items and items[0][0] < horizon:
        items.popleft()


@dataclass
class _AccountUsage:
    # Separate hourly and daily windows keep every read O(1) per account, so
    # snapshots taken under the scheduler lock stay cheap.
    hour: deque = field(default_factory=lambda: deque(maxlen=_MAX_TRACKED_ATTEMPTS))
    day: deque = field(default_factory=lambda: deque(maxlen=_MAX_TRACKED_ATTEMPTS))
    results: deque = field(default_factory=lambda: deque(maxlen=_RECENT_RESULTS))
    consecutive_failures: int = 0
    rate_limited: deque = field(default_factory=deque)
    forbidden: deque = field(default_factory=deque)
    leases: deque = field(default_factory=deque)

    def prune(self, now: float) -> None:
        _prune(self.hour, now - _HOUR)
        _prune(self.day, now - _DAY)
        _prune(self.rate_limited, now - _HOUR)
        _prune(self.forbidden, now - _DAY)


class ImageUsageTracker:
    """Sliding-window usage of image accounts and the pool's selection pressure."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at = clock()
        self._accounts: dict[str, _AccountUsage] = {}
        self._holds: deque[tuple[float, float]] = deque()
        self._waits: deque[tuple[float, float]] = deque()
        self._timeouts: deque[float] = deque()
        self._unavailable: deque[float] = deque()

    def record_lease(self, key: str) -> None:
        self._accounts.setdefault(key, _AccountUsage()).leases.append(self._clock())

    def record_release(self, key: str) -> float | None:
        """Close the oldest open lease of ``key`` and return how long it was held."""
        usage = self._accounts.get(key)
        if usage is None or not usage.leases:
            return None
        return max(0.0, self._clock() - usage.leases.popleft())

    def record_attempt(
        self,
        key: str,
        *,
        success: bool,
        account_failure: bool,
        failure_code: str = "",
        status_code: int = 0,
        held_seconds: float | None = None,
    ) -> None:
        """Record one upstream image attempt that ran on ``key``.

        ``account_failure`` marks failures that count against the account;
        request-level results (content policy, text replies) only count as use.
        """
        now = self._clock()
        usage = self._accounts.setdefault(key, _AccountUsage())
        usage.prune(now)
        usage.hour.append(now)
        usage.day.append(now)
        if success:
            usage.results.append(True)
            usage.consecutive_failures = 0
        elif account_failure:
            usage.results.append(False)
            usage.consecutive_failures += 1
        if failure_code in {"upstream_rate_limited", "file_upload_throttled"} or status_code == 429:
            usage.rate_limited.append(now)
        if status_code == 403:
            usage.forbidden.append(now)
        if held_seconds is not None:
            _prune_pairs(self._holds, now - _HOUR)
            self._holds.append((now, held_seconds))

    def record_wait(self, seconds: float) -> None:
        now = self._clock()
        _prune_pairs(self._waits, now - _HOUR)
        self._waits.append((now, max(0.0, seconds)))

    def record_timeout(self) -> None:
        now = self._clock()
        _prune(self._timeouts, now - _HOUR)
        self._timeouts.append(now)

    def record_unavailable(self) -> None:
        now = self._clock()
        _prune(self._unavailable, now - _HOUR)
        self._unavailable.append(now)

    def prune(self, live_keys: Iterable[str]) -> None:
        live = set(live_keys)
        self._accounts = {key: value for key, value in self._accounts.items() if key in live}

    def __len__(self) -> int:
        return len(self._accounts)

    def account_stats(self, key: str) -> dict[str, Any]:
        usage = self._accounts.get(key)
        if usage is None:
            return {
                "uses_1h": 0,
                "uses_24h": 0,
                "recent_results": 0,
                "recent_failures": 0,
                "consecutive_failures": 0,
                "rate_limited_1h": 0,
                "forbidden_24h": 0,
            }
        usage.prune(self._clock())
        return {
            "uses_1h": len(usage.hour),
            "uses_24h": len(usage.day),
            "recent_results": len(usage.results),
            "recent_failures": sum(1 for ok in usage.results if not ok),
            "consecutive_failures": usage.consecutive_failures,
            "rate_limited_1h": len(usage.rate_limited),
            "forbidden_24h": len(usage.forbidden),
        }

    def pool_stats(self) -> dict[str, Any]:
        now = self._clock()
        horizon = now - _HOUR
        _prune_pairs(self._holds, horizon)
        _prune_pairs(self._waits, horizon)
        _prune(self._timeouts, horizon)
        _prune(self._unavailable, horizon)
        attempts_1h = 0
        for usage in self._accounts.values():
            usage.prune(now)
            attempts_1h += len(usage.hour)
        holds = [seconds for _, seconds in self._holds]
        waits = [seconds for _, seconds in self._waits]
        return {
            "attempts_1h": attempts_1h,
            "avg_hold_seconds": sum(holds) / len(holds) if holds else None,
            "waits_1h": len(waits),
            "avg_wait_seconds": sum(waits) / len(waits) if waits else 0.0,
            "timeouts_1h": len(self._timeouts),
            "unavailable_1h": len(self._unavailable),
            "observed_seconds": min(_HOUR, max(0.0, now - self._started_at)),
        }
