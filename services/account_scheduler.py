"""Weighted fair scheduling for Upstream Account selection.

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Stride scheduling: every account carries a virtual "pass". Picking an account
advances its pass by ``1 / weight``, and the next pick takes the smallest pass,
so over time each account is chosen in proportion to its weight while picks
stay interleaved instead of draining one account in a burst.

Each pool (one set of selection filters) keeps a virtual clock equal to the
smallest pass among its members. The clock is computed over every ready member,
including accounts that are busy or excluded from this particular pick, so a
heavily weighted account that is momentarily at its concurrency cap keeps its
credit. Accounts that rejoin the pool, or are new to it, start at the clock and
cannot bank credit while they were away.

The scheduler is runtime-only state. Callers serialize access with their own
lock; nothing here is persisted, and a restart simply starts a fresh rotation.
"""

from __future__ import annotations

from typing import Collection, Hashable, Iterable, Sequence

_MIN_WEIGHT = 1e-3
# An account never sits more than this many of its current strides ahead of the
# clock. Weights change (a drained account gets its quota back), and a pass
# earned under a tiny old weight must not starve the account afterwards. Three
# strides still covers one normal pick plus a failure penalty.
_MAX_LEAD_STRIDES = 3.0
# A preferred account (e.g. one with a warmed-up session) ranks as if it were
# this many strides earlier: it wins near-ties but cannot skip far ahead.
_PREFERRED_BONUS_STRIDES = 1.0


class WeightedFairScheduler:
    def __init__(self) -> None:
        self._pass: dict[str, float] = {}
        self._last_pick: dict[str, int] = {}
        self._clocks: dict[Hashable, float] = {}
        self._ticks = 0

    def pick(
        self,
        candidates: Sequence[tuple[str, float, int]],
        *,
        members: Iterable[str],
        pool: Hashable = "",
        preferred: Collection[str] = (),
    ) -> str | None:
        """Choose one of ``(key, weight, inflight)`` and record the lease.

        ``members`` lists every ready account of the pool, including the ones
        that cannot take this lease. Ordering: fewest in-flight leases, then
        smallest virtual pass, then the account picked longest ago, then caller
        order.
        """
        clock = self._advance_clock(pool, members)
        best_rank: tuple | None = None
        best: tuple[str, float, float] | None = None
        for order, (key, weight, inflight) in enumerate(candidates):
            stride = 1.0 / max(float(weight), _MIN_WEIGHT)
            start = max(self._pass.get(key, clock), clock)
            lead_limit = clock + _MAX_LEAD_STRIDES * stride
            if start > lead_limit:
                # Persist the cap: a cap that moved with the clock would keep the
                # account a fixed distance ahead forever.
                start = self._pass[key] = lead_limit
            ranked_start = start - (_PREFERRED_BONUS_STRIDES * stride if key in preferred else 0.0)
            # Rounding keeps float drift (3 * 1/30 < 1/10) from breaking ties.
            rank = (int(inflight), round(ranked_start, 9), self._last_pick.get(key, -1), order)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best = (key, start, stride)
        if best is None:
            return None
        key, start, stride = best
        self._pass[key] = start + stride
        self._ticks += 1
        self._last_pick[key] = self._ticks
        return key

    def penalize(self, key: str, weight: float, strides: float = 2.0) -> None:
        """Push an account back after a failed attempt so it cools down."""
        base = max([self._pass.get(key, 0.0), *self._clocks.values()])
        self._pass[key] = base + strides / max(float(weight), _MIN_WEIGHT)

    def rename(self, old_key: str, new_key: str) -> None:
        """Carry an account's place in the rotation over to a new key (token rotation)."""
        if old_key == new_key:
            return
        if old_key in self._pass:
            self._pass[new_key] = self._pass.pop(old_key)
        if old_key in self._last_pick:
            self._last_pick[new_key] = self._last_pick.pop(old_key)

    def prune(self, live_keys: Iterable[str]) -> None:
        live = set(live_keys)
        self._pass = {key: value for key, value in self._pass.items() if key in live}
        self._last_pick = {key: value for key, value in self._last_pick.items() if key in live}

    def __len__(self) -> int:
        return len(self._pass)

    def _advance_clock(self, pool: Hashable, members: Iterable[str]) -> float:
        clock = self._clocks.get(pool, 0.0)
        # Members without a pass yet are new to the rotation and sit at the clock.
        floor = min((self._pass.get(key, clock) for key in members), default=clock)
        clock = max(clock, floor)
        self._clocks[pool] = clock
        return clock
