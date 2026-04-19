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
