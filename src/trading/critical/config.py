"""Env-driven config for the critical layer.

Lives in a separate file so deleting `trading.critical` doesn't remove
any knob that the base-model `trading.config.Settings` cares about.
All values have safe defaults — nothing mandatory to run in paper.

Env vars (all optional, prefixed `CRITICAL_`):

| Var | Default | Meaning |
|---|---|---|
| `CRITICAL_LOTS_NIFTY50`     | 2    | Lots per NIFTY50 entry |
| `CRITICAL_LOTS_BANKNIFTY`   | 1    | Lots per BANKNIFTY entry |
| `CRITICAL_LOTS_SENSEX`      | 2    | Lots per SENSEX entry |
| `CRITICAL_MAX_LOSS_RUPEES`  | 1500 | Hard stop per trade — Python kills instantly |
| `CRITICAL_DELTA_MIN`        | 0.55 | Minimum Δ for ITM entry |
| `CRITICAL_DELTA_MAX`        | 0.65 | Maximum Δ for ITM entry |
| `CRITICAL_MAX_CONCURRENT`   | 2    | Max indices with an open position |
| `CRITICAL_COOLDOWN_S`       | 180  | Seconds of no-entry after a losing close |
| `CRITICAL_CIRCUIT_LOSSES`   | 3    | Consecutive losses that halt trading for the day |
| `CRITICAL_REGIME_INTERVAL_S`| 900  | How often to re-classify the regime (15 min default) |
| `CRITICAL_TIME_STOP_S`      | 300  | Max seconds to hold before time-stop exit |
| `CRITICAL_NO_TRADE_OPEN_MIN`| 15   | No entries in the first N minutes after open |
| `CRITICAL_NO_TRADE_CLOSE_MIN`| 30  | No entries in the last N minutes before close |
| `CRITICAL_MAX_SPREAD_PCT`   | 4.0  | Skip entries when spread% exceeds this |
| `CRITICAL_ANTHROPIC_MODEL`  | claude-haiku-4-5-20251001 | Regime classifier model |
| `ANTHROPIC_API_KEY`         | ""   | If unset, regime filter falls back to heuristic |
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(k: str, default: int) -> int:
    v = os.getenv(k)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _get_float(k: str, default: float) -> float:
    v = os.getenv(k)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _get_str(k: str, default: str) -> str:
    v = os.getenv(k)
    return v if v is not None and v != "" else default


@dataclass(frozen=True)
class CriticalConfig:
    lots_per_index: dict[str, int]
    max_loss_rupees: float
    delta_min: float
    delta_max: float
    max_concurrent: int
    cooldown_s: int
    circuit_losses: int
    regime_interval_s: int
    time_stop_s: int
    no_trade_open_min: int
    no_trade_close_min: int
    max_spread_pct: float
    anthropic_model: str
    anthropic_api_key: str


def load_config() -> CriticalConfig:
    return CriticalConfig(
        lots_per_index={
            "NIFTY50":   _get_int("CRITICAL_LOTS_NIFTY50", 2),
            "BANKNIFTY": _get_int("CRITICAL_LOTS_BANKNIFTY", 1),
            "SENSEX":    _get_int("CRITICAL_LOTS_SENSEX", 2),
        },
        max_loss_rupees   = _get_float("CRITICAL_MAX_LOSS_RUPEES", 1500.0),
        delta_min         = _get_float("CRITICAL_DELTA_MIN", 0.55),
        delta_max         = _get_float("CRITICAL_DELTA_MAX", 0.65),
        max_concurrent    = _get_int  ("CRITICAL_MAX_CONCURRENT", 2),
        cooldown_s        = _get_int  ("CRITICAL_COOLDOWN_S", 180),
        circuit_losses    = _get_int  ("CRITICAL_CIRCUIT_LOSSES", 3),
        regime_interval_s = _get_int  ("CRITICAL_REGIME_INTERVAL_S", 900),
        time_stop_s       = _get_int  ("CRITICAL_TIME_STOP_S", 300),
        no_trade_open_min = _get_int  ("CRITICAL_NO_TRADE_OPEN_MIN", 15),
        no_trade_close_min= _get_int  ("CRITICAL_NO_TRADE_CLOSE_MIN", 30),
        max_spread_pct    = _get_float("CRITICAL_MAX_SPREAD_PCT", 4.0),
        anthropic_model   = _get_str  ("CRITICAL_ANTHROPIC_MODEL",
                                        "claude-haiku-4-5-20251001"),
        anthropic_api_key = _get_str  ("ANTHROPIC_API_KEY", ""),
    )
