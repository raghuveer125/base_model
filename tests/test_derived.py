"""Unit tests for trading.derived — scalper metrics."""

from __future__ import annotations

import math

from trading.derived import (
    build_metrics,
    imbalance,
    intrinsic_value,
    is_low_liquidity,
    spread_pct,
    time_value,
    vol_oi_ratio,
)


# ----- spread_pct -----

def test_spread_pct_basic():
    # bid=99, ask=101 → mid=100, spread=2% of mid
    assert math.isclose(spread_pct(99.0, 101.0), 2.0, abs_tol=1e-9)


def test_spread_pct_zero_for_crossed_or_zero():
    assert spread_pct(100.0, 100.0) == 0.0
    assert spread_pct(0.0, 0.0) is None          # mid == 0
    assert spread_pct(101.0, 99.0) is None       # crossed
    assert spread_pct(None, 100.0) is None


# ----- intrinsic / time value -----

def test_intrinsic_call_and_put():
    assert intrinsic_value("CE", 25_100, 25_000) == 100.0
    assert intrinsic_value("CE", 24_900, 25_000) == 0.0         # OTM
    assert intrinsic_value("PE", 24_900, 25_000) == 100.0
    assert intrinsic_value("PE", 25_100, 25_000) == 0.0         # OTM


def test_time_value_atm_is_all_premium():
    # ATM call, spot==strike, LTP 120 → intrinsic 0, TV 120
    assert time_value("CE", 120.0, 25_000, 25_000) == 120.0


def test_time_value_deep_itm_clamps_nonnegative():
    # Deep-ITM call trading BELOW intrinsic (possible with stale prints) →
    # clamp to 0 rather than showing negative TV.
    tv = time_value("CE", 50.0, 25_500, 25_000)   # intrinsic 500, ltp 50
    assert tv == 0.0


# ----- vol/OI -----

def test_vol_oi_ratio_and_zero_oi():
    assert vol_oi_ratio(10_000, 50_000) == 0.2
    assert vol_oi_ratio(10_000, 0) is None
    assert vol_oi_ratio(None, 50_000) is None


# ----- imbalance -----

def test_imbalance_all_bid_all_ask_and_balanced():
    assert imbalance(1000, 0) == 1.0
    assert imbalance(0, 1000) == -1.0
    assert imbalance(500, 500) == 0.0
    assert imbalance(0, 0) is None


# ----- low liquidity flag -----

def test_low_liquidity_flags_wide_spread():
    assert is_low_liquidity(spread_pct_val=5.0, volume=10_000) is True


def test_low_liquidity_flags_thin_volume():
    assert is_low_liquidity(spread_pct_val=0.5, volume=10) is True


def test_low_liquidity_passes_tight_active():
    assert is_low_liquidity(spread_pct_val=0.5, volume=10_000) is False


# ----- build_metrics integration -----

def test_build_metrics_full_row():
    tick = {
        "strike": 25_000, "option_type": "CE", "ltp": 150.0,
        "bid": 149.0, "ask": 151.0, "bid_qty": 500, "ask_qty": 300,
        "volume": 20_000, "oi": 40_000,
    }
    greeks = {"itm_prob": 0.48}
    m = build_metrics(tick, greeks, spot=25_100.0)
    assert math.isclose(m["spread_pct"], 2 / 150 * 100, abs_tol=1e-6)
    assert math.isclose(m["intrinsic"], 100.0, abs_tol=1e-9)
    assert math.isclose(m["time_value"], 50.0, abs_tol=1e-9)
    assert m["vol_oi"] == 0.5
    assert math.isclose(m["imbalance"], 0.25, abs_tol=1e-9)
    assert m["itm_prob"] == 0.48
    assert m["low_liq"] is False


def test_build_metrics_missing_spot_leaves_intrinsic_none():
    tick = {"strike": 25_000, "option_type": "CE", "ltp": 150.0}
    m = build_metrics(tick, None, spot=None)
    assert m["intrinsic"] is None and m["time_value"] is None
