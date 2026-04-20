"""Pre-trade risk gate.

All checks are pure — the engine calls `allow_entry()` before placing
any paper order. Failures return a short reason string that gets logged
to `logs/critical/signals-*.jsonl` so the validator can track why a
setup was rejected (tuning input).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from trading.critical.state import CriticalState

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN  = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)


@dataclass(frozen=True)
class RiskGate:
    allowed: bool
    reason: str = ""


def _ist_now_from_ms(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=_IST)


def within_entry_window(
    ts_ms: int,
    *, no_trade_open_min: int, no_trade_close_min: int,
) -> bool:
    """True only if we're inside [open + no_trade_open_min,
    close - no_trade_close_min]. Lunch hour is allowed here; the
    regime filter is expected to handle dead-tape zones separately."""
    t = _ist_now_from_ms(ts_ms).time()
    open_plus = (datetime.combine(datetime(2000,1,1), _MARKET_OPEN)
                  .replace(hour=_MARKET_OPEN.hour,
                           minute=_MARKET_OPEN.minute + no_trade_open_min)
                  .time())
    close_minus = (datetime.combine(datetime(2000,1,1), _MARKET_CLOSE)
                    .replace(hour=_MARKET_CLOSE.hour,
                             minute=_MARKET_CLOSE.minute - no_trade_close_min)
                    .time())
    return open_plus <= t <= close_minus


def allow_entry(
    state: CriticalState, index: str,
    *, ts_ms: int,
    max_concurrent: int,
    cooldown_s: int,
    circuit_losses: int,
    no_trade_open_min: int,
    no_trade_close_min: int,
) -> RiskGate:
    if not within_entry_window(
        ts_ms,
        no_trade_open_min=no_trade_open_min,
        no_trade_close_min=no_trade_close_min,
    ):
        return RiskGate(False, "outside_entry_window")

    idx_state = state.get(index)
    if idx_state.position is not None:
        return RiskGate(False, "index_already_open")
    if idx_state.halted_today:
        return RiskGate(False, f"halted_today ({idx_state.halted_reason})")
    if state.concurrent_open() >= max_concurrent:
        return RiskGate(False, "max_concurrent_reached")
    if idx_state.consecutive_losses >= circuit_losses:
        return RiskGate(False, "daily_circuit_breaker")
    if (idx_state.last_loss_ts_ms
            and ts_ms - idx_state.last_loss_ts_ms < cooldown_s * 1000):
        return RiskGate(
            False,
            f"cooldown_active (since {idx_state.last_loss_ts_ms})",
        )
    return RiskGate(True, "ok")
