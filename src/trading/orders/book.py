"""PositionBook — signed net position + average entry price per instrument.

Average-cost accounting:
- Opening / adding same direction: blend avg_price with the new fill.
- Opposite direction: realize PnL on the closing portion; flip the remainder
  (if any) to the opposite side at the fill price.
- Fees always reduce realized PnL.

State survives restart via Redis:
  tpp:pos:{instrument}  = Position JSON
  tpp:pnl:realized      = session cumulative realized PnL (float)
"""

from __future__ import annotations

import threading

import orjson

from trading.logging_setup import get_logger
from trading.orders.base import Fill, Position
from trading.storage import get_redis

log = get_logger(__name__)

_KEY_POS_FMT = "tpp:pos:{instrument}"
_KEY_REALIZED = "tpp:pnl:realized"


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._realized_pnl: float = 0.0
        self._lock = threading.Lock()
        self._hydrate()

    def _hydrate(self) -> None:
        try:
            r = get_redis()
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor=cursor, match="tpp:pos:*", count=64)
                for raw in keys or []:
                    key = raw.decode() if isinstance(raw, bytes) else raw
                    body = r.get(key)
                    if not body:
                        continue
                    try:
                        pos = Position.model_validate(orjson.loads(body))
                        self._positions[pos.instrument] = pos
                    except Exception as e:  # noqa: BLE001
                        log.warning("pos_hydrate_skip", key=key, error=str(e))
                if cursor == 0:
                    break
            pnl_raw = r.get(_KEY_REALIZED)
            self._realized_pnl = float(pnl_raw) if pnl_raw is not None else 0.0
        except Exception as e:  # noqa: BLE001
            log.warning("book_hydrate_failed", error=str(e))

    def _persist_pos(self, pos: Position) -> None:
        try:
            get_redis().set(
                _KEY_POS_FMT.format(instrument=pos.instrument),
                orjson.dumps(pos.model_dump(mode="json")),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("pos_persist_failed", error=str(e))

    def _persist_realized(self) -> None:
        try:
            get_redis().set(_KEY_REALIZED, self._realized_pnl)
        except Exception as e:  # noqa: BLE001
            log.warning("pnl_persist_failed", error=str(e))

    def apply_fill(self, fill: Fill) -> dict:
        """Update book from a fill. Returns {position, delta_realized}."""
        with self._lock:
            pos = self._positions.get(fill.instrument) or Position(
                instrument=fill.instrument, index=fill.index,
                qty=0, avg_price=0.0, realized_pnl=0.0,
                last_update_ms=fill.ts_ms,
            )
            signed = fill.qty if fill.side == "B" else -fill.qty
            same_dir = (pos.qty == 0
                        or (pos.qty > 0 and signed > 0)
                        or (pos.qty < 0 and signed < 0))

            delta_real = 0.0
            if not same_dir:
                closing_qty = min(abs(pos.qty), fill.qty)
                if pos.qty > 0:
                    delta_real = (fill.fill_price - pos.avg_price) * closing_qty
                else:
                    delta_real = (pos.avg_price - fill.fill_price) * closing_qty
            delta_real -= fill.fees

            if same_dir:
                abs_pos = abs(pos.qty)
                total_cost = pos.avg_price * abs_pos + fill.fill_price * fill.qty
                new_abs = abs_pos + fill.qty
                new_avg = total_cost / new_abs if new_abs else 0.0
                new_qty = pos.qty + signed
            else:
                if fill.qty > abs(pos.qty):
                    remaining = fill.qty - abs(pos.qty)
                    new_qty = remaining if fill.side == "B" else -remaining
                    new_avg = fill.fill_price
                else:
                    new_qty = pos.qty + signed
                    new_avg = pos.avg_price if new_qty != 0 else 0.0

            pos = pos.model_copy(update={
                "qty": new_qty,
                "avg_price": round(new_avg, 6),
                "realized_pnl": round(pos.realized_pnl + delta_real, 4),
                "last_update_ms": fill.ts_ms,
            })
            self._positions[fill.instrument] = pos
            self._realized_pnl += delta_real
            self._persist_pos(pos)
            self._persist_realized()
            return {"position": pos, "delta_realized": round(delta_real, 4)}

    def get(self, instrument: str) -> Position | None:
        with self._lock:
            return self._positions.get(instrument)

    def all_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def realized_pnl(self) -> float:
        with self._lock:
            return round(self._realized_pnl, 4)
