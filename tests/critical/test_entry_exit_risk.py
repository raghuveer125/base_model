"""Tests for the entry picker, exit decisions, and risk gate."""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from trading.critical.entry import pick_instrument
from trading.critical.exit import build_exit_levels, evaluate as evaluate_exit
from trading.critical.market_view import ChainRow
from trading.critical.risk import allow_entry, within_entry_window
from trading.critical.state import CriticalState, Position


_IST = ZoneInfo("Asia/Kolkata")


def _row(strike, ce_ltp, ce_delta, pe_ltp, pe_delta, ce_spread=1.0, pe_spread=1.0):
    return ChainRow(
        strike=strike,
        ce_tick={"ltp": ce_ltp, "oi": 1000},
        pe_tick={"ltp": pe_ltp, "oi": 1000},
        ce_greeks={"delta": ce_delta},
        pe_greeks={"delta": pe_delta},
        ce_metrics={"spread_pct": ce_spread},
        pe_metrics={"spread_pct": pe_spread},
    )


# ----- entry -----

def test_entry_picks_ce_in_delta_window():
    rows = [
        _row(24_900, ce_ltp=200, ce_delta=0.75, pe_ltp=10,  pe_delta=-0.25),
        _row(25_000, ce_ltp=120, ce_delta=0.60, pe_ltp=50,  pe_delta=-0.40),  # target
        _row(25_100, ce_ltp= 60, ce_delta=0.40, pe_ltp=100, pe_delta=-0.60),
    ]
    c = pick_instrument(rows, spot=25_020, side="CE",
                        delta_min=0.55, delta_max=0.65, max_spread_pct=4.0)
    assert c is not None
    assert c.strike == 25_000
    assert c.side == "CE"

def test_entry_skips_wide_spread():
    rows = [
        _row(25_000, ce_ltp=120, ce_delta=0.60, pe_ltp=50, pe_delta=-0.40,
             ce_spread=10.0),   # too wide
    ]
    c = pick_instrument(rows, spot=25_020, side="CE",
                        delta_min=0.55, delta_max=0.65, max_spread_pct=4.0)
    assert c is None

def test_entry_falls_back_to_atm_when_no_delta_match():
    rows = [
        _row(25_000, ce_ltp=120, ce_delta=0.90, pe_ltp=5, pe_delta=-0.10),
        _row(25_100, ce_ltp=100, ce_delta=0.80, pe_ltp=3, pe_delta=-0.05),
    ]
    c = pick_instrument(rows, spot=25_050, side="CE",
                        delta_min=0.55, delta_max=0.65, max_spread_pct=4.0)
    assert c is not None
    assert c.strike in (25_000, 25_100)     # closest to spot
    assert c.reason == "atm_fallback"


# ----- exit -----

def _pos(entry=100.0, target=115.0, stop=90.0, time_stop_ms=1_000_000):
    return Position(
        index="NIFTY50", expiry_iso="2026-04-28",
        strike=25_000, option_type="CE",
        lots=2, lot_size=75,
        entry_ltp=entry, entry_ts_ms=0,
        target_ltp=target, stop_ltp=stop, time_stop_ms=time_stop_ms,
    )

def test_exit_hard_stop_fires_first():
    d = evaluate_exit(_pos(), ltp=89.0, now_ms=10,
                       regime_bias="long")
    assert d.should_exit and d.reason == "stop"

def test_exit_target_fires_when_reached():
    d = evaluate_exit(_pos(), ltp=116.0, now_ms=10,
                       regime_bias="long")
    assert d.should_exit and d.reason == "target"

def test_exit_time_stop_fires_after_deadline():
    d = evaluate_exit(_pos(time_stop_ms=100),
                       ltp=110.0, now_ms=500,
                       regime_bias="long")
    assert d.should_exit and d.reason == "time"

def test_exit_regime_flip_closes_call_on_short_regime():
    # Soft exits require age >= 5s AND ltp < entry — both hold here.
    d = evaluate_exit(_pos(entry=110.0), ltp=99.0, now_ms=6_000,
                       regime_bias="short")
    assert d.should_exit and d.reason == "regime_flip"

def test_exit_regime_flip_blocked_if_position_too_young():
    d = evaluate_exit(_pos(entry=110.0), ltp=99.0, now_ms=1_000,
                       regime_bias="short")
    assert not d.should_exit     # age < 5s

def test_exit_regime_flip_blocked_if_not_adverse():
    d = evaluate_exit(_pos(entry=100.0), ltp=105.0, now_ms=6_000,
                       regime_bias="short")
    assert not d.should_exit     # in profit; soft exits skip

def test_exit_wall_break_closes_call_on_support_break():
    d = evaluate_exit(_pos(entry=110.0), ltp=99.0, now_ms=6_000,
                       regime_bias="long",
                       spot=24_700, primary_support=24_800)
    assert d.should_exit and d.reason == "wall_break"

def test_exit_none_when_inside_guardrails():
    d = evaluate_exit(_pos(), ltp=105.0, now_ms=10,
                       regime_bias="long",
                       spot=25_050, primary_support=24_800,
                       primary_resistance=25_100)
    assert not d.should_exit


# ----- dollar-cap stop (priority over price-level stop) -----

def test_dollar_stop_fires_before_price_stop_on_gap_through():
    # 2 lots × 75 = 150 qty. entry=100, ltp gapped to 80 (-20/unit ×
    # 150 = 3000 loss) — well past the 1500 dollar cap even though
    # the price-level stop_ltp=90 wasn't reached in isolation.
    pos = Position(
        index="NIFTY50", expiry_iso="2026-04-28", strike=25_000,
        option_type="CE", lots=2, lot_size=75,
        entry_ltp=100.0, entry_ts_ms=0,
        target_ltp=115.0, stop_ltp=90.0, time_stop_ms=1_000_000,
    )
    d = evaluate_exit(pos, ltp=80.0, now_ms=10,
                       regime_bias="long", max_loss_rupees=1500.0)
    assert d.should_exit and d.reason == "dollar_stop"

def test_dollar_stop_does_not_fire_inside_cap():
    pos = Position(
        index="NIFTY50", expiry_iso="2026-04-28", strike=25_000,
        option_type="CE", lots=2, lot_size=75,
        entry_ltp=100.0, entry_ts_ms=0,
        target_ltp=115.0, stop_ltp=90.0, time_stop_ms=1_000_000,
    )
    # -5 per unit × 150 = 750 loss, under 1500 cap → price-level stop
    # hasn't fired either → no exit.
    d = evaluate_exit(pos, ltp=95.0, now_ms=10,
                       regime_bias="long", max_loss_rupees=1500.0)
    assert not d.should_exit


# ----- build_exit_levels -----

def test_build_exit_levels_respects_rupee_cap():
    # 2 lots × 75 = 150 qty. Cap 1500 rupees → 10/point stop.
    target, stop, ts = build_exit_levels(
        entry_ltp=100.0, spread=1.0, max_loss_rupees=1500,
        lots=2, lot_size=75, time_stop_s=300, now_ms=0,
    )
    assert abs(stop - 90.0) < 0.01
    assert target > 100
    assert ts == 300 * 1000

def test_build_exit_levels_has_min_stop_floor():
    # Tiny position but still 5-point floor should apply.
    target, stop, _ = build_exit_levels(
        entry_ltp=100.0, spread=1.0, max_loss_rupees=1500,
        lots=1, lot_size=1, time_stop_s=300, now_ms=0,
    )
    # 1500 / 1 = 1500, much bigger than min 5; here cap dominates → stop very low.
    assert stop == 0.0    # clamped at zero


# ----- within entry window -----

def test_within_entry_window_rejects_preopen_and_postclose():
    # 09:00 IST on Mon 2026-04-20 — pre-open
    ist = datetime(2026, 4, 20, 9, 0, tzinfo=_IST)
    ts_ms = int(ist.timestamp() * 1000)
    assert within_entry_window(ts_ms,
                                no_trade_open_min=15,
                                no_trade_close_min=30) is False
    # 15:15 IST — inside the last-30-min closeout
    ist = datetime(2026, 4, 20, 15, 15, tzinfo=_IST)
    assert within_entry_window(int(ist.timestamp() * 1000),
                                no_trade_open_min=15,
                                no_trade_close_min=30) is False
    # 11:30 IST — inside window
    ist = datetime(2026, 4, 20, 11, 30, tzinfo=_IST)
    assert within_entry_window(int(ist.timestamp() * 1000),
                                no_trade_open_min=15,
                                no_trade_close_min=30) is True


# ----- allow_entry risk gate -----

def _midday_ms() -> int:
    return int(datetime(2026, 4, 20, 11, 30, tzinfo=_IST).timestamp() * 1000)

def test_allow_entry_blocked_by_existing_position():
    st = CriticalState()
    st.get("NIFTY50").position = object()   # type: ignore[assignment]
    g = allow_entry(st, "NIFTY50",
                     ts_ms=_midday_ms(),
                     max_concurrent=2, cooldown_s=180,
                     circuit_losses=3,
                     no_trade_open_min=15, no_trade_close_min=30)
    assert not g.allowed
    assert g.reason == "index_already_open"

def test_allow_entry_blocked_by_concurrent_cap():
    st = CriticalState()
    st.get("NIFTY50").position = object()    # type: ignore[assignment]
    st.get("BANKNIFTY").position = object()  # type: ignore[assignment]
    g = allow_entry(st, "SENSEX",
                     ts_ms=_midday_ms(),
                     max_concurrent=2, cooldown_s=180,
                     circuit_losses=3,
                     no_trade_open_min=15, no_trade_close_min=30)
    assert not g.allowed
    assert g.reason == "max_concurrent_reached"

def test_allow_entry_blocked_by_circuit_breaker():
    st = CriticalState()
    st.get("NIFTY50").consecutive_losses = 3
    g = allow_entry(st, "NIFTY50",
                     ts_ms=_midday_ms(),
                     max_concurrent=2, cooldown_s=180,
                     circuit_losses=3,
                     no_trade_open_min=15, no_trade_close_min=30)
    assert not g.allowed
    assert g.reason == "daily_circuit_breaker"

def test_allow_entry_blocked_by_cooldown():
    now = _midday_ms()
    st = CriticalState()
    st.get("NIFTY50").last_loss_ts_ms = now - 30 * 1000   # 30 s ago
    g = allow_entry(st, "NIFTY50",
                     ts_ms=now,
                     max_concurrent=2, cooldown_s=180,
                     circuit_losses=3,
                     no_trade_open_min=15, no_trade_close_min=30)
    assert not g.allowed
    assert g.reason.startswith("cooldown_active")

def test_allow_entry_allowed_on_clean_state():
    g = allow_entry(CriticalState(), "NIFTY50",
                     ts_ms=_midday_ms(),
                     max_concurrent=2, cooldown_s=180,
                     circuit_losses=3,
                     no_trade_open_min=15, no_trade_close_min=30)
    assert g.allowed
    assert g.reason == "ok"

def test_allow_entry_blocked_by_halted_today():
    st = CriticalState()
    s = st.get("NIFTY50")
    s.halted_today = True
    s.halted_reason = "big_loss=5200"
    g = allow_entry(st, "NIFTY50",
                     ts_ms=_midday_ms(),
                     max_concurrent=2, cooldown_s=180,
                     circuit_losses=3,
                     no_trade_open_min=15, no_trade_close_min=30)
    assert not g.allowed
    assert g.reason.startswith("halted_today")
