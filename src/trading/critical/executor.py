"""Thin adapter around `trading.orders.PaperExecutor`.

Translates `Signal + EntryCandidate + Position` into the `Order` /
`Fill` protocol used by the existing paper engine, then emits a JSON
record on `scalp.{INDEX}` for observability.

No state lives here — caller (engine) is responsible for updating
`CriticalState` with the returned position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Literal

import orjson

from trading.critical.entry import EntryCandidate
from trading.critical.state import Position
from trading.critical.triggers import Signal
from trading.events import EventBus
from trading.logging_setup import get_logger
from trading.orders.base import Fill, Order
from trading.orders.paper import PaperExecutor
from trading.schemas import FYERS_INDEX_SYMBOL, INDEX_OPTION_ROOT, now_ms

log = get_logger(__name__)

# Append-only JSONL that captures every entry/exit/rejection as it
# happens. Read back by the UI to show a full trade table. Gitignored
# under `logs/critical/` like the other outputs of this package.
_TRADES_JSONL = Path("logs/critical/trades.jsonl").resolve()
_TRADES_LOCK = Lock()

# Lot sizes as of the SEBI Nov-2024 revision. Could be moved to config
# later if exchanges revise again.
LOT_SIZES: dict[str, int] = {
    "NIFTY50":   75,
    "BANKNIFTY": 30,
    "SENSEX":    20,
}

STRATEGY_NAME = "critical_scalp"

ScalpEvent = Literal["entry", "exit"]


@dataclass
class ExecutionOutcome:
    ok: bool
    position: Position | None
    fill: Fill | None
    reason: str = ""


def _instrument_symbol(index: str, expiry_d: date, strike: int, ot: str) -> str:
    """Best-effort instrument string for the paper executor.

    Uses the same monthly format helper that `adapter.build_option_subscription`
    uses — keeping both ends of the pipeline symmetric. The paper
    executor doesn't actually care about the format (it just echoes
    back the string on fills), but making it look real means the
    audit log is usable for reconciliation later.
    """
    exch = "NSE" if index != "SENSEX" else "BSE"
    root = INDEX_OPTION_ROOT.get(index, index)
    yy = expiry_d.strftime("%y")
    # We always pass "monthly-style" for simplicity in this adapter —
    # the critical layer doesn't need to distinguish weekly vs monthly
    # for audit purposes.
    return f"{exch}:{root}{yy}{expiry_d.strftime('%b').upper()}{strike}{ot}"


class ScalpExecutor:
    def __init__(
        self,
        paper: PaperExecutor | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._paper = paper or PaperExecutor()
        self._bus = bus or EventBus()

    def enter(
        self, *, index: str, expiry_d: date, cand: EntryCandidate,
        signal: Signal, lots: int, target_ltp: float, stop_ltp: float,
        time_stop_ms: int, signal_ts_ms: int,
        entry_primary_resistance: int | None = None,
        entry_primary_support: int | None = None,
    ) -> ExecutionOutcome:
        lot_size = LOT_SIZES.get(index, 1)
        qty = max(lots * lot_size, 1)
        instrument = _instrument_symbol(index, expiry_d, cand.strike, cand.side)
        order = Order(
            strategy=STRATEGY_NAME,
            index=index,
            instrument=instrument,
            action="BUY",
            qty=qty,
            ref_price=float(cand.ltp),
            signal_ts=signal_ts_ms,
            ordered_ts=now_ms(),
            signal_reason=" | ".join(signal.reasons),
            signal_confidence=float(signal.confidence),
        )
        result = self._paper.execute(order)
        if not result.ok or result.fill is None:
            self._publish_event("entry_rejected", index, {
                "strike": cand.strike, "side": cand.side,
                "reason": result.reason,
            })
            return ExecutionOutcome(False, None, None, result.reason)

        pos = Position(
            index=index,
            expiry_iso=expiry_d.isoformat(),
            strike=cand.strike,
            option_type=cand.side,  # type: ignore[arg-type]
            lots=lots, lot_size=lot_size,
            entry_ltp=result.fill.fill_price,
            entry_ts_ms=result.fill.ts_ms,
            target_ltp=target_ltp,
            stop_ltp=stop_ltp,
            time_stop_ms=time_stop_ms,
            reason=" | ".join(signal.reasons),
            entry_primary_resistance=entry_primary_resistance,
            entry_primary_support=entry_primary_support,
        )
        self._publish_event("entry", index, {
            "strike": pos.strike, "side": pos.option_type,
            "lots": pos.lots, "qty": qty,
            "entry_ltp": pos.entry_ltp,
            "target": pos.target_ltp, "stop": pos.stop_ltp,
            "confidence": signal.confidence,
            "reasons": list(signal.reasons),
            "instrument": instrument,
        })
        return ExecutionOutcome(True, pos, result.fill)

    def exit(
        self, *, pos: Position, current_ltp: float,
        reason: str, signal_ts_ms: int,
    ) -> ExecutionOutcome:
        qty = pos.lots * pos.lot_size
        instrument = _instrument_symbol(
            pos.index,
            # Position only stores expiry_iso — convert back just for the
            # audit-log instrument string.
            date.fromisoformat(pos.expiry_iso),
            pos.strike, pos.option_type,
        )
        order = Order(
            strategy=STRATEGY_NAME, index=pos.index,
            instrument=instrument, action="SELL", qty=qty,
            ref_price=float(current_ltp),
            signal_ts=signal_ts_ms, ordered_ts=now_ms(),
            signal_reason=reason, signal_confidence=0.0,
        )
        result = self._paper.execute(order)
        if not result.ok or result.fill is None:
            return ExecutionOutcome(False, pos, None, result.reason)
        pnl = (result.fill.fill_price - pos.entry_ltp) * qty - result.fill.fees
        self._publish_event("exit", pos.index, {
            "strike": pos.strike, "side": pos.option_type,
            "entry_ltp": pos.entry_ltp,
            "exit_ltp": result.fill.fill_price,
            "pnl": pnl, "reason": reason,
            "held_ms": result.fill.ts_ms - pos.entry_ts_ms,
            "instrument": instrument,
        })
        return ExecutionOutcome(True, None, result.fill, reason)

    # ---- internals ----

    def _publish_event(self, kind: ScalpEvent | str, index: str,
                        data: dict) -> None:
        payload = {"kind": kind, "ts": now_ms(), "index": index, **data}
        # Zombie-clone tracer (2026-04-21): fingerprint of the persistent
        # NIFTY50 25000 CE @ 120.50 phantom entry that keeps appearing
        # despite Redis showing the real strike at ₹0.25-0.45. When the
        # pattern tries to write, dump a stack + payload to a dedicated
        # file — stderr goes to a minimised tpp-up window (invisible).
        # Harmless for normal trading; fires only on exact fingerprint.
        # Remove once root cause lands.
        if (kind == "entry"
                and index == "NIFTY50"
                and data.get("strike") == 25000
                and abs(float(data.get("entry_ltp") or 0) - 120.5) < 0.01):
            try:
                import traceback
                from datetime import datetime as _dt
                trace_path = _TRADES_JSONL.parent / "zombie_tracer.log"
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                with open(trace_path, "a", encoding="utf-8") as tf:
                    tf.write(f"\n=== {_dt.now().isoformat()} ===\n")
                    tf.write(f"payload: {payload}\n")
                    tf.write(f"caller stack (last 8 frames):\n")
                    tf.write("".join(traceback.format_stack()[-8:]))
            except Exception:   # noqa: BLE001
                pass
        # 1) live pub/sub for the UI's signal feed
        try:
            self._bus.publish(f"scalp.{index}", payload)
        except Exception:   # noqa: BLE001 — observability must never block trading
            pass
        # 2) append to the persistent trade log so the "Paper" tab can
        #    replay the full day even after a UI reload
        try:
            _TRADES_JSONL.parent.mkdir(parents=True, exist_ok=True)
            line = orjson.dumps(payload) + b"\n"
            with _TRADES_LOCK, open(_TRADES_JSONL, "ab") as f:
                f.write(line)
        except Exception:   # noqa: BLE001
            pass


# Exported helper for tests / external callers who want to know what
# instrument string a given (index, expiry, strike, side) would produce.
__all__ = ["ScalpExecutor", "ExecutionOutcome", "LOT_SIZES", "STRATEGY_NAME",
           "_instrument_symbol"]
