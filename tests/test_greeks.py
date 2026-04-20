"""Greeks unit tests — Black-Scholes invariants, edge cases, and TTE helper."""

from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from trading.greeks import (
    compute_greeks,
    compute_greeks_full,
    solve_iv,
    time_to_expiry_years,
)


R = 0.065
IV = 0.15
T = 30 / 365.25
K = 25_000


def test_call_delta_in_unit_interval_for_range_of_spots():
    for spot in (20_000, 23_000, 25_000, 27_000, 30_000):
        d, _, _, _ = compute_greeks("CE", spot, K, IV, T, R)
        assert 0.0 <= d <= 1.0


def test_put_delta_in_negative_unit_interval():
    for spot in (20_000, 23_000, 25_000, 27_000, 30_000):
        d, _, _, _ = compute_greeks("PE", spot, K, IV, T, R)
        assert -1.0 <= d <= 0.0


def test_gamma_non_negative_and_ce_pe_symmetric():
    for spot in (24_000, 25_000, 26_000):
        _, gc, _, _ = compute_greeks("CE", spot, K, IV, T, R)
        _, gp, _, _ = compute_greeks("PE", spot, K, IV, T, R)
        assert gc >= 0.0 and gp >= 0.0
        assert math.isclose(gc, gp, rel_tol=1e-6)


def test_vega_non_negative_and_ce_pe_equal():
    spot = 25_000
    _, _, _, vc = compute_greeks("CE", spot, K, IV, T, R)
    _, _, _, vp = compute_greeks("PE", spot, K, IV, T, R)
    assert vc >= 0.0
    assert math.isclose(vc, vp, rel_tol=1e-6)


def test_theta_non_positive_near_atm():
    spot = 25_000
    _, _, tc, _ = compute_greeks("CE", spot, K, IV, T, R)
    _, _, tp, _ = compute_greeks("PE", spot, K, IV, T, R)
    assert tc <= 0.0 and tp <= 0.0


def test_deep_itm_call_delta_near_one_and_deep_otm_near_zero():
    d_itm, _, _, _ = compute_greeks("CE", 30_000, K, IV, T, R)
    d_otm, _, _, _ = compute_greeks("CE", 20_000, K, IV, T, R)
    assert d_itm > 0.95
    assert d_otm < 0.05


def test_put_call_delta_parity():
    """Δ_call − Δ_put = 1 (BS identity for non-dividend spot)."""
    for spot in (24_000, 25_000, 26_000):
        dc, *_ = compute_greeks("CE", spot, K, IV, T, R)
        dp, *_ = compute_greeks("PE", spot, K, IV, T, R)
        assert math.isclose(dc - dp, 1.0, abs_tol=1e-6)


def test_zero_time_returns_intrinsic_delta_and_zero_other():
    d_itm, g, th, v = compute_greeks("CE", 26_000, K, IV, 0.0, R)
    d_otm, *_ = compute_greeks("CE", 24_000, K, IV, 0.0, R)
    assert d_itm == 1.0
    assert d_otm == 0.0
    assert g == 0.0 and th == 0.0 and v == 0.0


def test_zero_vol_degenerate_returns_finite():
    d, g, th, v = compute_greeks("CE", 26_000, K, 0.0, T, R)
    assert d == 1.0
    assert g == 0.0 and th == 0.0 and v == 0.0


def test_time_to_expiry_uses_15_30_ist():
    expiry = date(2026, 4, 30)
    tz = ZoneInfo("Asia/Kolkata")
    close = datetime.combine(expiry, dtime(15, 30), tzinfo=tz)
    close_ms = int(close.timestamp() * 1000)
    tte = time_to_expiry_years(expiry, now_ms_=close_ms)
    assert abs(tte) < 1e-9


def test_time_to_expiry_is_positive_before_close():
    expiry = date(2026, 4, 30)
    tz = ZoneInfo("Asia/Kolkata")
    before = datetime.combine(expiry, dtime(15, 30), tzinfo=tz).timestamp() * 1000 - 86_400_000
    tte = time_to_expiry_years(expiry, now_ms_=int(before))
    assert tte > 0
    assert abs(tte - (1 / 365.25)) < 1e-3


# ----- ITM probability (N(d2) / N(-d2)) -----

def test_itm_prob_in_unit_interval():
    for spot in (20_000, 25_000, 30_000):
        _, _, _, _, p = compute_greeks_full("CE", spot, K, IV, T, R)
        assert 0.0 <= p <= 1.0
        _, _, _, _, p = compute_greeks_full("PE", spot, K, IV, T, R)
        assert 0.0 <= p <= 1.0


def test_itm_prob_call_plus_put_equals_one():
    # N(d2) + N(-d2) == 1 by symmetry of the standard normal.
    for spot in (24_000, 25_000, 26_000):
        _, _, _, _, pc = compute_greeks_full("CE", spot, K, IV, T, R)
        _, _, _, _, pp = compute_greeks_full("PE", spot, K, IV, T, R)
        assert math.isclose(pc + pp, 1.0, abs_tol=1e-6)


def test_itm_prob_monotone_in_moneyness_for_call():
    _, _, _, _, p_low = compute_greeks_full("CE", 22_000, K, IV, T, R)
    _, _, _, _, p_atm = compute_greeks_full("CE", 25_000, K, IV, T, R)
    _, _, _, _, p_hi  = compute_greeks_full("CE", 28_000, K, IV, T, R)
    assert p_low < p_atm < p_hi


def test_itm_prob_zero_time_collapses_to_binary():
    _, _, _, _, p_itm = compute_greeks_full("CE", 26_000, K, IV, 0.0, R)
    _, _, _, _, p_otm = compute_greeks_full("CE", 24_000, K, IV, 0.0, R)
    assert p_itm == 1.0 and p_otm == 0.0


# ----- IV solver (BS inversion) -----

from trading.greeks import _bs_price    # noqa: E402 — internal helper for round-trip tests


def test_solve_iv_round_trip_recovers_sigma():
    """Forward: price = BS(σ). Solve: σ' = solve_iv(price). Expect σ ≈ σ'."""
    spot, K_ = 25_000, 25_000
    T_ = 30 / 365.25
    for true_sigma in (0.10, 0.18, 0.30, 0.50):
        price, _ = _bs_price("CE", spot, K_, true_sigma, T_, R)
        solved = solve_iv("CE", spot, K_, price, T_, R)
        assert math.isclose(solved, true_sigma, abs_tol=1e-3)
        price_p, _ = _bs_price("PE", spot, K_, true_sigma, T_, R)
        solved_p = solve_iv("PE", spot, K_, price_p, T_, R)
        assert math.isclose(solved_p, true_sigma, abs_tol=1e-3)


def test_solve_iv_below_intrinsic_returns_zero():
    # Deep-ITM call, price quoted below intrinsic → stale / crossed → no answer.
    # Spot 26_000, K 25_000 → intrinsic = 1000. Price = 500 (< 1000) must bail.
    assert solve_iv("CE", 26_000, 25_000, 500.0, 30 / 365.25, R) == 0.0


def test_solve_iv_zero_inputs_safe():
    assert solve_iv("CE", 0, 25_000, 100.0, 0.1, R) == 0.0
    assert solve_iv("CE", 25_000, 25_000, 0.0, 0.1, R) == 0.0
    assert solve_iv("CE", 25_000, 25_000, 100.0, 0.0, R) == 0.0
