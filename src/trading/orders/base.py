"""Order, Fill, Position schemas + Executor protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

OrderAction = Literal["BUY", "SELL", "EXIT", "HOLD"]
FillSide = Literal["B", "S"]


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, populate_by_name=True,
    )


class Order(_Strict):
    strategy: Annotated[str, Field(min_length=1)]
    index: Annotated[str, Field(min_length=1)]
    instrument: Annotated[str, Field(min_length=1)]
    action: OrderAction
    qty: Annotated[int, Field(gt=0)]
    ref_price: Annotated[float, Field(ge=0)]
    signal_ts: Annotated[int, Field(ge=0)]
    ordered_ts: Annotated[int, Field(ge=0)]
    signal_reason: str = ""
    signal_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class Fill(_Strict):
    order_id: int | None = None
    strategy: Annotated[str, Field(min_length=1)]
    index: Annotated[str, Field(min_length=1)]
    instrument: Annotated[str, Field(min_length=1)]
    side: FillSide
    qty: Annotated[int, Field(gt=0)]
    fill_price: Annotated[float, Field(gt=0)]
    fees: Annotated[float, Field(ge=0)] = 0.0
    slippage_bps: float = 0.0
    ts_ms: Annotated[int, Field(ge=0)]


class Position(_Strict):
    instrument: str
    index: str
    qty: int                             # signed: positive = long, negative = short
    avg_price: Annotated[float, Field(ge=0)]
    realized_pnl: float = 0.0
    last_update_ms: Annotated[int, Field(ge=0)]


@dataclass
class ExecutionResult:
    ok: bool
    fill: Fill | None
    reason: str = ""


class Executor(Protocol):
    name: str
    def execute(self, order: Order) -> ExecutionResult: ...
