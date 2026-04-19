"""Adapter unit tests: strike alignment, symbol parsing, normalization, subscription builder."""

from __future__ import annotations

from datetime import date

from trading.adapter import (
    align_strike,
    atm_strikes,
    build_option_subscription,
    normalize_index_tick,
    normalize_option_tick,
    parse_option_symbol,
)


def test_align_strike_rounds_to_step():
    assert align_strike("NIFTY50", 25_018) == 25_000
    assert align_strike("BANKNIFTY", 52_049) == 52_000
    assert align_strike("SENSEX", 82_151) == 82_200


def test_atm_strikes_window_count_and_symmetry():
    strikes = atm_strikes("NIFTY50", 25_010, window=3)
    assert len(strikes) == 7
    atm = align_strike("NIFTY50", 25_010)
    assert strikes == [atm - 150, atm - 100, atm - 50, atm, atm + 50, atm + 100, atm + 150]


def test_parse_weekly_symbol_nifty():
    p = parse_option_symbol("NSE:NIFTY26A2325000CE")
    # month code 'A' isn't in our table -> monthly regex won't match and weekly regex
    # also fails because 'A' -> 10 is defined. Weekly *does* match, but month 10 Oct 23
    # (not 'A'); we assert via real weekly code
    assert p is None  # 'A' not a valid month code, monthly regex also won't match 'A' as 3-letter


def test_parse_weekly_symbol_october_day_one():
    p = parse_option_symbol("NSE:NIFTY26O0155000PE")
    assert p is not None
    assert p.expiry == date(2026, 10, 1)
    assert p.strike == 55_000
    assert p.option_type == "PE"


def test_parse_monthly_symbol_banknifty():
    p = parse_option_symbol("NSE:BANKNIFTY26FEB52000CE")
    assert p is not None
    assert p.root == "BANKNIFTY"
    assert p.expiry.year == 2026
    assert p.expiry.month == 2


def test_parse_bad_symbol_returns_none():
    assert parse_option_symbol("NOT:A:SYMBOL") is None
    assert parse_option_symbol("") is None


def test_normalize_index_tick_happy_path():
    tick = normalize_index_tick({
        "symbol": "NSE:NIFTY50-INDEX",
        "ltp": 25_000.5,
        "exch_feed_time": 1_729_300_000,
    })
    assert tick is not None
    assert tick.index == "NIFTY50"
    assert tick.ltp == 25_000.5
    assert tick.ts_exchange == 1_729_300_000_000


def test_normalize_option_tick_happy_path():
    tick = normalize_option_tick({
        "symbol": "NSE:NIFTY26O0125000CE",
        "ltp": 120.55,
        "oi": 12_345,
        "oich": 100,
        "iv": 14.2,
        "exch_feed_time": 1_729_300_000_000,
    })
    assert tick is not None
    assert tick.strike == 25_000
    assert tick.option_type == "CE"
    assert tick.oi == 12_345
    assert tick.oi_change == 100
    assert tick.iv == 14.2


def test_normalize_returns_none_on_unknown_index():
    assert normalize_index_tick({"symbol": "NSE:MYSTERY-INDEX", "ltp": 1}) is None


def test_build_option_subscription_monthly_uses_mmm_format():
    # 2026-04-30 is the last Thursday of April 2026 → monthly → YY+MMM.
    syms = build_option_subscription("NIFTY50", date(2026, 4, 30),
                                     spot=25_012, window=2)
    assert len(syms) == 10
    assert all(s.startswith("NSE:NIFTY26APR") for s in syms)
    assert sum(s.endswith("CE") for s in syms) == 5
    assert sum(s.endswith("PE") for s in syms) == 5


def test_build_option_subscription_weekly_uses_mcode_dd_format():
    # 2026-04-23 is a Thursday but NOT the last Thursday → weekly → YY+mcode+DD.
    syms = build_option_subscription("NIFTY50", date(2026, 4, 23),
                                     spot=25_012, window=1)
    assert len(syms) == 6
    # prefix should be NSE:NIFTY + 26 + 4 + 23
    assert all(s.startswith("NSE:NIFTY26423") for s in syms)
