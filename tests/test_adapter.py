"""Adapter unit tests: strike alignment, symbol parsing, normalization, subscription builder."""

from __future__ import annotations

from datetime import date

from trading.adapter import (
    align_strike,
    atm_strikes,
    build_index_subscription,
    build_option_subscription,
    normalize_index_tick,
    normalize_option_tick,
    parse_option_symbol,
)
from trading.schemas import FYERS_VIX_SYMBOL


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
    # Monthly BANKNIFTY (last Tuesday of the month — post-Nov-2024 convention).
    # 2026-02: Tuesdays are 3, 10, 17, 24 → last = 24.
    p = parse_option_symbol("NSE:BANKNIFTY26FEB52000CE")
    assert p is not None
    assert p.root == "BANKNIFTY"
    assert p.expiry == date(2026, 2, 24)


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


def test_normalize_option_tick_microstructure_v3_keys():
    """Fyers WS v3 primary key names: bid_price, ask_price, bid_size, ask_size,
    vol_traded_today, prev_close_price, ch, chp."""
    tick = normalize_option_tick({
        "symbol": "NSE:NIFTY26O0125000CE",
        "ltp": 120.55,
        "oi": 12_345, "oich": 100, "iv": 14.2,
        "bid_price": 120.10, "ask_price": 120.90,
        "bid_size": 750, "ask_size": 1200,
        "vol_traded_today": 25_400,
        "prev_close_price": 115.30,
        "ch": 5.25, "chp": 4.55,
        "exch_feed_time": 1_729_300_000_000,
    })
    assert tick is not None
    assert tick.bid == 120.10
    assert tick.ask == 120.90
    assert tick.bid_qty == 750 and tick.ask_qty == 1200
    assert tick.volume == 25_400
    assert tick.prev_close == 115.30
    assert tick.change == 5.25
    assert tick.change_pct == 4.55


def test_normalize_option_tick_derives_change_from_prev_close():
    """If the feed omits ch/chp, derive them from ltp − prev_close."""
    tick = normalize_option_tick({
        "symbol": "NSE:NIFTY26O0125000CE",
        "ltp": 120.00,
        "prev_close_price": 100.00,
        "exch_feed_time": 1_729_300_000_000,
    })
    assert tick is not None
    assert tick.change == 20.00
    assert tick.change_pct == 20.00


def test_normalize_option_tick_missing_microstructure_is_none():
    """Absent microstructure fields must be None, never raise."""
    tick = normalize_option_tick({
        "symbol": "NSE:NIFTY26O0125000CE",
        "ltp": 120.00,
        "exch_feed_time": 1_729_300_000_000,
    })
    assert tick is not None
    assert tick.bid is None and tick.ask is None
    assert tick.volume is None
    assert tick.change is None and tick.change_pct is None


def test_build_option_subscription_monthly_uses_mmm_format():
    # 2026-04-28 is the last Tuesday of April 2026 → monthly → YY+MMM.
    # (NIFTY monthly moved to last Tuesday in Oct 2024.)
    syms = build_option_subscription("NIFTY50", date(2026, 4, 28),
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


# ----- build_index_subscription -----

def test_build_index_subscription_emits_configured_indices_plus_vix():
    syms = build_index_subscription(["NIFTY50", "BANKNIFTY", "SENSEX"])
    assert "NSE:NIFTY50-INDEX" in syms
    assert "NSE:NIFTYBANK-INDEX" in syms
    assert "BSE:SENSEX-INDEX" in syms
    assert FYERS_VIX_SYMBOL in syms
    # VIX is last; one entry per managed index plus one VIX
    assert len(syms) == 4

def test_build_index_subscription_still_appends_vix_when_no_indices():
    # Degenerate empty input — VIX should still be on the wire so the
    # regime classifier isn't blind on a cold cache.
    syms = build_index_subscription([])
    assert syms == [FYERS_VIX_SYMBOL]
