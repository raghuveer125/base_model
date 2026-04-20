"""Pure scalper-oriented derived metrics built from tick + greeks + spot.

No I/O, no state. All inputs explicit so the functions stay trivially testable
and safe to call from inside hot code paths.

Conventions:
  * All floats are in market units (rupees, shares, %).
  * `option_type` is "CE" or "PE".
  * Any missing/unusable input returns `None` rather than a sentinel — callers
    decide whether to hide the field or render "—".
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

OptionType = Literal["CE", "PE"]

# Liquidity thresholds — tuned for Indian index options, ATM ± a few strikes.
# A spread of >4% of mid or fewer than 100 contracts traded is "low liquidity"
# for scalping purposes (round-trip slippage dominates edge).
LOW_LIQ_SPREAD_PCT = 4.0
LOW_LIQ_MIN_VOLUME = 100


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """Percent spread relative to mid. None if quotes missing or mid <= 0."""
    if bid is None or ask is None:
        return None
    if ask <= 0 or bid < 0 or ask < bid:
        return None
    mid = (bid + ask) * 0.5
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def intrinsic_value(option_type: OptionType, spot: float, strike: float) -> float:
    """Intrinsic value of the option — never negative."""
    if option_type == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def time_value(
    option_type: OptionType, ltp: float, spot: float, strike: float,
) -> float:
    """LTP − intrinsic; clamped at 0 to stay non-negative under noisy quotes."""
    return max(ltp - intrinsic_value(option_type, spot, strike), 0.0)


def vol_oi_ratio(volume: int | None, oi: int | None) -> float | None:
    """Volume / OI — "churn". None when OI is zero or either input missing."""
    if volume is None or oi is None or oi <= 0:
        return None
    return volume / oi


def imbalance(
    bid_qty: int | None, ask_qty: int | None,
) -> float | None:
    """Top-of-book bid/ask size imbalance in [-1, 1].

    +1 = all bid (buying pressure), -1 = all ask (selling pressure).
    """
    if bid_qty is None or ask_qty is None:
        return None
    total = bid_qty + ask_qty
    if total <= 0:
        return None
    return (bid_qty - ask_qty) / total


def is_low_liquidity(
    spread_pct_val: float | None, volume: int | None,
    *,
    spread_threshold: float = LOW_LIQ_SPREAD_PCT,
    min_volume: int = LOW_LIQ_MIN_VOLUME,
) -> bool:
    """True if the strike is too thin / wide to scalp safely."""
    if spread_pct_val is not None and spread_pct_val > spread_threshold:
        return True
    if volume is not None and volume < min_volume:
        return True
    return False


def build_metrics(
    tick: Mapping[str, Any],
    greeks: Mapping[str, Any] | None,
    spot: float | None,
) -> dict[str, Any]:
    """Assemble the per-row scalper metrics dict.

    `tick` and `greeks` are the raw dicts as serialized by Pydantic
    (OptionTick.model_dump / OptionGreeks.model_dump). We read loosely so
    the function stays forward-compatible if extra fields appear.
    """
    ot: OptionType | None = tick.get("option_type")  # type: ignore[assignment]
    if ot not in ("CE", "PE"):
        return {}

    strike = tick.get("strike")
    ltp = tick.get("ltp")
    bid = tick.get("bid")
    ask = tick.get("ask")
    bid_qty = tick.get("bid_qty")
    ask_qty = tick.get("ask_qty")
    volume = tick.get("volume")
    oi = tick.get("oi")

    sp = spread_pct(bid, ask)
    imb = imbalance(bid_qty, ask_qty)
    v_oi = vol_oi_ratio(volume, oi)

    intrinsic: float | None = None
    tv: float | None = None
    if isinstance(strike, (int, float)) and isinstance(ltp, (int, float)) and (
        isinstance(spot, (int, float)) and spot > 0
    ):
        intrinsic = intrinsic_value(ot, float(spot), float(strike))
        tv = time_value(ot, float(ltp), float(spot), float(strike))

    # itm_prob is authored upstream in greeks.py (cached). We mirror it here
    # so a single `metrics` dict fully describes the row for the UI.
    itm_prob = None
    if greeks:
        gv = greeks.get("itm_prob")
        if isinstance(gv, (int, float)):
            itm_prob = float(gv)

    return {
        "spread_pct": sp,
        "intrinsic": intrinsic,
        "time_value": tv,
        "vol_oi": v_oi,
        "imbalance": imb,
        "itm_prob": itm_prob,
        "low_liq": is_low_liquidity(sp, volume),
    }
