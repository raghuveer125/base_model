"""ITM strike picker — selects the contract to buy on an entry signal.

Rules (v1):

  * CE (bullish)  → pick the highest-strike CE whose |Δ| ∈ [delta_min,
    delta_max]. That's the slightly-ITM call with good directional
    sensitivity.
  * PE (bearish)  → pick the lowest-strike PE whose |Δ| ∈ [delta_min,
    delta_max].
  * If no strike in the window has Δ in range (e.g., Δs not yet
    computed / stale IV), fall back to the ATM strike.
  * Strike must also clear the per-index liquidity bar (spread_pct
    within config limit).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trading.critical.market_view import ChainRow

Side = Literal["CE", "PE"]


@dataclass(frozen=True)
class EntryCandidate:
    strike: int
    side: Side
    ltp: float
    delta: float
    spread_pct: float
    reason: str


def pick_instrument(
    rows: list[ChainRow], spot: float, side: Side,
    *, delta_min: float, delta_max: float, max_spread_pct: float,
) -> EntryCandidate | None:
    if not rows:
        return None

    candidates: list[EntryCandidate] = []
    for r in rows:
        leg_tick = r.ce_tick if side == "CE" else r.pe_tick
        leg_greeks = r.ce_greeks if side == "CE" else r.pe_greeks
        leg_metrics = r.ce_metrics if side == "CE" else r.pe_metrics
        if leg_tick is None or leg_greeks is None:
            continue
        ltp = leg_tick.get("ltp")
        delta = leg_greeks.get("delta")
        spread = leg_metrics.get("spread_pct")
        if not isinstance(ltp, (int, float)) or ltp <= 0:
            continue
        if not isinstance(delta, (int, float)):
            continue
        if spread is not None and spread > max_spread_pct:
            continue
        abs_delta = abs(delta)
        if delta_min <= abs_delta <= delta_max:
            candidates.append(EntryCandidate(
                strike=r.strike, side=side,
                ltp=float(ltp), delta=float(delta),
                spread_pct=float(spread) if spread is not None else float("nan"),
                reason=f"delta={abs_delta:.3f}",
            ))

    if candidates:
        # Prefer the strike closest to spot — less premium at risk for a
        # similar delta.
        candidates.sort(key=lambda c: abs(c.strike - spot))
        return candidates[0]

    # Fallback: plain ATM regardless of delta (might be stale greeks).
    atm_row = min(rows, key=lambda r: abs(r.strike - spot))
    leg_tick = atm_row.ce_tick if side == "CE" else atm_row.pe_tick
    leg_metrics = atm_row.ce_metrics if side == "CE" else atm_row.pe_metrics
    if leg_tick is None:
        return None
    ltp = leg_tick.get("ltp")
    if not isinstance(ltp, (int, float)) or ltp <= 0:
        return None
    spread = leg_metrics.get("spread_pct")
    if spread is not None and spread > max_spread_pct:
        return None
    return EntryCandidate(
        strike=atm_row.strike, side=side,
        ltp=float(ltp), delta=float("nan"),
        spread_pct=float(spread) if spread is not None else float("nan"),
        reason="atm_fallback",
    )
