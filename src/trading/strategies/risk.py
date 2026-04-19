"""Cooldown + RiskEngine. Both are pure in-memory, thread-safe."""

from __future__ import annotations

import threading
from collections import deque

from trading.schemas import Signal, now_ms


class CooldownManager:
    """Per-(strategy, instrument) minimum interval between emitted signals."""

    def __init__(self, cooldown_seconds: int) -> None:
        self.cooldown_ms = cooldown_seconds * 1000
        self._last: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def should_suppress(self, sig: Signal) -> bool:
        key = (sig.strategy, sig.instrument)
        with self._lock:
            last = self._last.get(key, 0)
            if sig.ts - last < self.cooldown_ms:
                return True
            self._last[key] = sig.ts
            return False

    def reset(self, strategy: str | None = None, instrument: str | None = None) -> None:
        with self._lock:
            if strategy is None and instrument is None:
                self._last.clear()
                return
            for k in list(self._last.keys()):
                if (strategy in (None, k[0])) and (instrument in (None, k[1])):
                    self._last.pop(k, None)


class RiskEngine:
    """Rolling-window rate limits + index/action allowlist.

    Windows use epoch-ms `sig.ts` for admission check; internal timestamps use
    `now_ms()` for cleanup. Hour + day are tracked together in one deque per strategy.
    """

    _HOUR_MS = 60 * 60 * 1000
    _DAY_MS = 24 * 60 * 60 * 1000

    def __init__(
        self,
        *,
        max_per_hour: int,
        max_per_day: int,
        allowed_indices: set[str] | None = None,
        allowed_actions: set[str] | None = None,
    ) -> None:
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.allowed_indices = allowed_indices
        self.allowed_actions = allowed_actions or {"BUY", "SELL", "HOLD", "EXIT"}
        self._events_by_strategy: dict[str, deque[int]] = {}
        self._lock = threading.Lock()

    def allows(self, sig: Signal) -> tuple[bool, str]:
        if self.allowed_indices is not None and sig.index not in self.allowed_indices:
            return False, f"index {sig.index!r} not allowed"
        if sig.action not in self.allowed_actions:
            return False, f"action {sig.action!r} not allowed"
        now = now_ms()
        with self._lock:
            q = self._events_by_strategy.setdefault(sig.strategy, deque())
            day_cutoff = now - self._DAY_MS
            while q and q[0] < day_cutoff:
                q.popleft()
            hour_cutoff = now - self._HOUR_MS
            in_hour = sum(1 for t in q if t >= hour_cutoff)
            in_day = len(q)
            if in_hour >= self.max_per_hour:
                return False, f"hour cap {self.max_per_hour} hit (in_hour={in_hour})"
            if in_day >= self.max_per_day:
                return False, f"day cap {self.max_per_day} hit (in_day={in_day})"
            q.append(sig.ts)
        return True, ""

    def snapshot(self) -> dict[str, dict[str, int]]:
        now = now_ms()
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            for strat, q in self._events_by_strategy.items():
                hour_cutoff = now - self._HOUR_MS
                out[strat] = {
                    "in_hour": sum(1 for t in q if t >= hour_cutoff),
                    "in_day": len(q),
                }
        return out
