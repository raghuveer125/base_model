"""Paper executor — deterministic slippage + fees, no real order placement."""

from __future__ import annotations

import math

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.orders.base import ExecutionResult, Fill, Order
from trading.schemas import OPTION_TICK_SIZE, now_ms

log = get_logger(__name__)


def _snap_fill(price: float, side: str) -> float:
    """Snap a post-slippage fill price onto the ₹0.05 option tick grid.

    Direction matches real-market microstructure: a BUY fills at the next
    higher tick (book walks up against you), a SELL fills at the next
    lower tick. Keeps paper PnL conservative and aligns audit prices with
    what a real order book would show.
    """
    if side == "B":
        return round(math.ceil(price / OPTION_TICK_SIZE) * OPTION_TICK_SIZE, 2)
    return round(math.floor(price / OPTION_TICK_SIZE) * OPTION_TICK_SIZE, 2)


class PaperExecutor:
    name = "paper"

    def __init__(
        self,
        slippage_bps: float | None = None,
        fee_bps: float | None = None,
        flat_fee: float | None = None,
    ) -> None:
        s = get_settings()
        self.slippage_bps = slippage_bps if slippage_bps is not None else s.paper_slippage_bps
        self.fee_bps = fee_bps if fee_bps is not None else s.paper_fee_bps
        self.flat_fee = flat_fee if flat_fee is not None else s.paper_flat_fee

    def execute(self, order: Order) -> ExecutionResult:
        if order.ref_price <= 0:
            return ExecutionResult(ok=False, fill=None,
                                   reason="ref_price unavailable or non-positive")
        if order.qty <= 0:
            return ExecutionResult(ok=False, fill=None, reason="non-positive qty")
        if order.action == "HOLD":
            return ExecutionResult(ok=False, fill=None, reason="HOLD is a no-op")

        slip = self.slippage_bps / 10_000.0
        # EXIT should arrive as a concrete BUY or SELL from the engine (based on
        # the current position). If it somehow still reads as EXIT here, treat
        # it as SELL (close long) as a safe default.
        if order.action == "BUY":
            fill_price = order.ref_price * (1 + slip)
            side = "B"
        else:  # SELL or EXIT
            fill_price = order.ref_price * (1 - slip)
            side = "S"

        fill_price = _snap_fill(fill_price, side)

        gross = fill_price * order.qty
        fee = self.flat_fee + gross * (self.fee_bps / 10_000.0)

        fill = Fill(
            order_id=None,
            strategy=order.strategy,
            index=order.index,
            instrument=order.instrument,
            side=side,  # type: ignore[arg-type]
            qty=order.qty,
            fill_price=fill_price,
            fees=round(fee, 4),
            slippage_bps=float(self.slippage_bps),
            ts_ms=now_ms(),
        )
        return ExecutionResult(ok=True, fill=fill, reason="")
