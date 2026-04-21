"""Canonical Pydantic models + constants shared across modules.

All timestamps are Unix epoch MILLISECONDS (int). Dates are `datetime.date` (YYYY-MM-DD).
Every record carries `ts_exchange` (source) and `ts_received` (our clock) so the
pipeline can measure end-to-end latency.
"""

from __future__ import annotations

import time
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Index(StrEnum):
    NIFTY50 = "NIFTY50"
    BANKNIFTY = "BANKNIFTY"
    SENSEX = "SENSEX"


FYERS_INDEX_SYMBOL: dict[str, str] = {
    Index.NIFTY50.value: "NSE:NIFTY50-INDEX",
    Index.BANKNIFTY.value: "NSE:NIFTYBANK-INDEX",
    Index.SENSEX.value: "BSE:SENSEX-INDEX",
}

# India VIX — not one of the indices we scalp, but a volatility signal
# the regime classifier needs. Kept as a standalone symbol so the ingest
# routes it to a dedicated Redis key (`tpp:vix`) without polluting the
# index-tick pipeline (no candle closing, no greeks, no chain lookup).
FYERS_VIX_SYMBOL: str = "NSE:INDIAVIX-INDEX"

INDEX_EXCHANGE: dict[str, str] = {
    Index.NIFTY50.value: "NSE",
    Index.BANKNIFTY.value: "NSE",
    Index.SENSEX.value: "BSE",
}

INDEX_STRIKE_STEP: dict[str, int] = {
    Index.NIFTY50.value: 50,
    Index.BANKNIFTY.value: 100,
    Index.SENSEX.value: 100,
}

INDEX_OPTION_ROOT: dict[str, str] = {
    Index.NIFTY50.value: "NIFTY",
    Index.BANKNIFTY.value: "BANKNIFTY",
    Index.SENSEX.value: "SENSEX",
}

# NSE / BSE index-options minimum price tick in rupees. All fills and
# price-level triggers (target, stop) must land on this grid; otherwise
# the paper engine drifts away from what the real order book would show.
OPTION_TICK_SIZE: float = 0.05


OptionType = Literal["CE", "PE"]


def now_ms() -> int:
    return int(time.time() * 1000)


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
        populate_by_name=True,
    )


class IndexTick(_Strict):
    index: Annotated[str, Field(min_length=1)]
    ltp: Annotated[float, Field(gt=0)]
    ts_exchange: Annotated[int, Field(ge=0)]
    ts_received: Annotated[int, Field(ge=0)]

    @field_validator("index")
    @classmethod
    def _valid_index(cls, v: str) -> str:
        if v not in Index._value2member_map_:
            raise ValueError(f"unknown index: {v}")
        return v


class OptionTick(_Strict):
    index: Annotated[str, Field(min_length=1)]
    strike: Annotated[int, Field(gt=0)]
    option_type: OptionType
    expiry: date
    ltp: Annotated[float, Field(ge=0)]
    oi: Annotated[int, Field(ge=0)] = 0
    oi_change: int = 0
    iv: float | None = None
    # --- microstructure (optional, populated when the feed carries them) ---
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    prev_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    # ------------------------------------------------------------------------
    ts_exchange: Annotated[int, Field(ge=0)]
    ts_received: Annotated[int, Field(ge=0)]

    @field_validator("index")
    @classmethod
    def _valid_index(cls, v: str) -> str:
        if v not in Index._value2member_map_:
            raise ValueError(f"unknown index: {v}")
        return v


class OptionChainSnapshot(_Strict):
    index: str
    spot: float
    atm: int
    expiry: date
    ticks: list[OptionTick]
    ts: int


Timeframe = Literal["1m", "5m", "15m"]

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
}


class IndexCandle(_Strict):
    """OHLC candle built from index ticks. `volume` is 0 for spot indices;
    tick_count is carried separately so the schema stays forward-compatible
    if volume is populated from a real source later."""
    index: Annotated[str, Field(min_length=1)]
    timeframe: Timeframe
    open_ts: Annotated[int, Field(ge=0)]        # bucket start, epoch ms (exchange-time aligned)
    close_ts: Annotated[int, Field(ge=0)]       # bucket end, epoch ms
    open: Annotated[float, Field(gt=0)]
    high: Annotated[float, Field(gt=0)]
    low: Annotated[float, Field(gt=0)]
    close: Annotated[float, Field(gt=0)]
    volume: Annotated[int, Field(ge=0)] = 0
    tick_count: Annotated[int, Field(ge=0)] = 0

    @field_validator("index")
    @classmethod
    def _valid_index(cls, v: str) -> str:
        if v not in Index._value2member_map_:
            raise ValueError(f"unknown index: {v}")
        return v


SignalAction = Literal["BUY", "SELL", "HOLD", "EXIT"]


class Signal(BaseModel):
    """A trading signal emitted by a Strategy.

    Phase 4 does NOT execute signals — this is the logging + risk contract only.
    `instrument` is a Fyers-style symbol (e.g. NSE:NIFTY26O0125000CE) OR an index name.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True,
                              populate_by_name=True)

    strategy: Annotated[str, Field(min_length=1)]
    index: Annotated[str, Field(min_length=1)]
    action: SignalAction
    instrument: Annotated[str, Field(min_length=1)]
    reason: str = ""
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    metadata: dict = Field(default_factory=dict)
    ts: Annotated[int, Field(ge=0)]

    @field_validator("index")
    @classmethod
    def _valid_index(cls, v: str) -> str:
        if v not in Index._value2member_map_:
            raise ValueError(f"unknown index: {v}")
        return v


class OptionGreeks(_Strict):
    """Black-Scholes Greeks for a single option contract.

    Sign conventions:
      delta ∈ [0, 1] for CE, [-1, 0] for PE
      gamma ≥ 0
      theta usually ≤ 0 (per day, i.e. price decay)
      vega  ≥ 0, per 1% absolute change in σ
    """
    index: Annotated[str, Field(min_length=1)]
    strike: Annotated[int, Field(gt=0)]
    option_type: OptionType
    expiry: date
    spot: Annotated[float, Field(gt=0)]
    iv: float | None
    time_to_expiry_years: Annotated[float, Field(ge=0)]
    delta: float
    gamma: float
    theta: float    # per calendar day
    vega: float     # per 1% σ
    # Risk-neutral probability of finishing in-the-money at expiry:
    #   CE: N(d2)        PE: N(-d2)
    # Optional so older cached records still validate.
    itm_prob: float | None = None
    ts: Annotated[int, Field(ge=0)]

    @field_validator("index")
    @classmethod
    def _valid_index(cls, v: str) -> str:
        if v not in Index._value2member_map_:
            raise ValueError(f"unknown index: {v}")
        return v
