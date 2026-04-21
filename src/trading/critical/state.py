"""Rolling in-memory state for the scalper.

Per-index windows of LTP / OI / candles + open position + risk counters.
Lives entirely in-process — deliberately NOT mirrored to Redis in v1 so
the critical layer has zero side effects on the base pipeline.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

OptionType = Literal["CE", "PE"]
Momentum = Literal["up", "down", "flat"]


@dataclass
class StrikeHistory:
    """Rolling LTP / OI window for a single (strike, CE|PE) contract."""
    ltps: deque = field(default_factory=lambda: deque(maxlen=5))
    ois: deque = field(default_factory=lambda: deque(maxlen=3))

    def observe(self, ltp: float | None, oi: int | None) -> None:
        if isinstance(ltp, (int, float)) and ltp > 0:
            if not self.ltps or self.ltps[-1] != ltp:
                self.ltps.append(float(ltp))
        if isinstance(oi, (int, float)) and oi > 0:
            if not self.ois or self.ois[-1] != oi:
                self.ois.append(int(oi))

    def ltp_momentum(self) -> Momentum:
        if len(self.ltps) < 2:
            return "flat"
        first, last = self.ltps[0], self.ltps[-1]
        if last > first:
            return "up"
        if last < first:
            return "down"
        return "flat"

    def oi_delta(self) -> int | None:
        """Signed delta between oldest and newest cached OI value.

        None when fewer than 2 observations — caller should treat as
        "unknown", not "zero".
        """
        if len(self.ois) < 2:
            return None
        return int(self.ois[-1] - self.ois[0])


@dataclass
class Position:
    """A paper-trade open position tracked by the critical layer."""
    index: str
    expiry_iso: str
    strike: int
    option_type: OptionType
    lots: int
    lot_size: int
    entry_ltp: float
    entry_ts_ms: int
    # precomputed exit levels so the engine can bail instantly without
    # the LLM and without recomputing on every tick
    target_ltp: float
    stop_ltp: float
    time_stop_ms: int
    reason: str = ""
    # Snapshot of the OI-derived S/R levels at entry time. Used by the
    # wall-break exit to reject "false flag" exits that fire on the SAME
    # wall that was already broken when the trade was opened. None means
    # no wall was identified at that moment (fall back to old behaviour).
    entry_primary_resistance: int | None = None
    entry_primary_support: int | None = None


@dataclass
class IndexState:
    index: str
    strikes: dict[tuple[int, str], StrikeHistory] = field(default_factory=dict)
    candles_1m: deque = field(default_factory=lambda: deque(maxlen=30))
    candles_5m: deque = field(default_factory=lambda: deque(maxlen=30))
    position: Position | None = None
    last_loss_ts_ms: int = 0
    consecutive_losses: int = 0
    day_reset_date: str = ""
    # Set by the engine when this index should be frozen for the rest of
    # the session — e.g., a single loss exceeded `big_loss_rupees`, or the
    # circuit-breaker consecutive-loss cap was hit. Cleared on day reset.
    halted_today: bool = False
    halted_reason: str = ""

    def get_strike(self, strike: int, ot: OptionType) -> StrikeHistory:
        key = (strike, ot)
        h = self.strikes.get(key)
        if h is None:
            h = StrikeHistory()
            self.strikes[key] = h
        return self.strikes[key]

    def reset_for_day(self, today_iso: str) -> None:
        """Called at first event of a new trading day — flushes counters."""
        if self.day_reset_date == today_iso:
            return
        self.day_reset_date = today_iso
        self.consecutive_losses = 0
        self.last_loss_ts_ms = 0
        self.halted_today = False
        self.halted_reason = ""


@dataclass
class CriticalState:
    """Top-level state object passed through the engine."""
    per_index: dict[str, IndexState] = field(default_factory=dict)
    last_regime_update_ms: dict[str, int] = field(default_factory=dict)
    last_regime: dict[str, dict] = field(default_factory=dict)

    def get(self, index: str) -> IndexState:
        s = self.per_index.get(index)
        if s is None:
            s = IndexState(index=index)
            self.per_index[index] = s
        return s

    def concurrent_open(self) -> int:
        return sum(1 for s in self.per_index.values() if s.position is not None)

    def reset_if_new_day(self, today: date) -> None:
        today_iso = today.isoformat()
        for s in self.per_index.values():
            s.reset_for_day(today_iso)
