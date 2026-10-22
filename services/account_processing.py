"""Shared concurrency control for account batch work (refresh, check, import).

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

The limit comes from ``config.account_processing_concurrency`` (default 30,
range 1-100) and is re-read on every acquisition, so changing the setting takes
effect without a restart.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from threading import Condition, Lock, local
from typing import Iterator


_DEFAULT_CONCURRENCY = 30
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 100


def account_processing_concurrency() -> int:
    """Return the current configured global account-processing limit."""
    try:
        from services.config import config

        value = int(config.account_processing_concurrency)
    except (AttributeError, TypeError, ValueError):
        value = _DEFAULT_CONCURRENCY
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, value))


def account_processing_worker_count(item_count: int) -> int:
    """Size a local worker pool without exceeding the shared global limit."""
    count = max(0, int(item_count or 0))
    return min(count, account_processing_concurrency())


class AccountProcessingLimiter:
    """A process-wide, dynamically sized and thread-reentrant limiter."""

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._active = 0
        self._local = local()

    @contextmanager
    def slot(self) -> Iterator[None]:
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        with self._condition:
            while self._active >= account_processing_concurrency():
                self._condition.wait(timeout=0.5)
            self._active += 1
            self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._active = max(0, self._active - 1)
                self._condition.notify_all()


account_processing_limiter = AccountProcessingLimiter()


def account_processing_slot() -> AbstractContextManager[None]:
    """Acquire one shared slot for an upstream account-maintenance request."""
    return account_processing_limiter.slot()
