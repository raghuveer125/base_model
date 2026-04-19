"""Fyers live executor — stub. Not implemented yet.

When implemented, this class will:
  - read the cached access token from LiveStore (same token used by ingest WS)
  - place orders via fyersModel.place_order({...})
  - poll order status and wrap the eventual trade into a `Fill`
  - respect the same fee bookkeeping so Position/PnL semantics match paper

Until then, constructing this raises so misconfigured envs fail fast.
"""

from __future__ import annotations

from trading.orders.base import ExecutionResult, Order


class FyersExecutor:
    name = "fyers"

    def __init__(self) -> None:
        raise NotImplementedError(
            "Live Fyers order placement is not yet implemented. "
            "Run with ORDERS_MODE=paper to use the simulated executor."
        )

    def execute(self, order: Order) -> ExecutionResult:  # pragma: no cover
        raise NotImplementedError
