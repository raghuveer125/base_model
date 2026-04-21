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


# ----- wall-break hysteresis + same-wall skip -----

def _pe_pos_with_entry_resistance(
    entry_resistance: int | None, entry_ltp: float = 100.0,
) -> Position:
    return Position(
        index="NIFTY50", expiry_iso="2026-04-28", strike=24_500,
        option_type="PE", lots=1, lot_size=75,
        entry_ltp=entry_ltp, entry_ts_ms=0,
        target_ltp=entry_ltp + 10, stop_ltp=entry_ltp - 20,
        time_stop_ms=1_000_000_000,
        entry_primary_resistance=entry_resistance,
    )

def test_wall_break_skipped_when_same_wall_as_entry():
    # PE opened when resistance was already 24500 (spot drifted to 24498
    # then bounced to 24502 within the soft-exit-gate window). That wall
    # was priced in at entry — don't panic-exit on it.
    pos = _pe_pos_with_entry_resistance(24_500, entry_ltp=100.0)
    d = evaluate_exit(pos, ltp=99.0, now_ms=10_000,
                       regime_bias="neutral", spot=24_502,
                       primary_resistance=24_500, primary_support=None,
                       wall_break_hysteresis_pts=5.0)
    assert not d.should_exit

def test_wall_break_fires_on_new_wall():
    # A freshly-migrated resistance that DIDN'T exist at entry. Spot
    # clearly above it past the hysteresis buffer → exit.
    pos = _pe_pos_with_entry_resistance(24_500, entry_ltp=100.0)
    d = evaluate_exit(pos, ltp=99.0, now_ms=10_000,
                       regime_bias="neutral", spot=24_556,
                       primary_resistance=24_550, primary_support=None,
                       wall_break_hysteresis_pts=5.0)
    assert d.should_exit and d.reason == "wall_break"

def test_wall_break_suppressed_by_hysteresis_flicker():
    # Newly migrated wall at 24_550 but spot only barely above (24_552 —
    # less than the 5-pt hysteresis). One-tick flicker, don't kill.
    pos = _pe_pos_with_entry_resistance(24_500, entry_ltp=100.0)
    d = evaluate_exit(pos, ltp=99.0, now_ms=10_000,
                       regime_bias="neutral", spot=24_552,
                       primary_resistance=24_550, primary_support=None,
                       wall_break_hysteresis_pts=5.0)
    assert not d.should_exit

def test_wall_break_ce_symmetric_same_support():
    # CE opened when support was 24_500; spot dips to 24_498 and recovers.
    # Same-wall skip should apply to the support side just like resistance.
    pos = Position(
        index="NIFTY50", expiry_iso="2026-04-28", strike=24_500,
        option_type="CE", lots=1, lot_size=75,
        entry_ltp=100.0, entry_ts_ms=0,
        target_ltp=110.0, stop_ltp=90.0, time_stop_ms=1_000_000_000,
        entry_primary_support=24_500,
    )
    d = evaluate_exit(pos, ltp=99.0, now_ms=10_000,
                       regime_bias="neutral", spot=24_498,
                       primary_resistance=None, primary_support=24_500,
                       wall_break_hysteresis_pts=5.0)
    assert not d.should_exit

def test_wall_break_legacy_behaviour_when_entry_wall_unset():
    # Positions from before the entry-wall-tracking change have no
    # entry_primary_resistance/support. Old behaviour must still work.
    pos = Position(
        index="NIFTY50", expiry_iso="2026-04-28", strike=24_500,
        option_type="PE", lots=1, lot_size=75,
        entry_ltp=100.0, entry_ts_ms=0,
        target_ltp=110.0, stop_ltp=80.0, time_stop_ms=1_000_000_000,
    )
    # No hysteresis, no entry wall → behaves like pre-change code.
    d = evaluate_exit(pos, ltp=99.0, now_ms=10_000,
                       regime_bias="neutral", spot=24_555,
                       primary_resistance=24_500, primary_support=None,
                       wall_break_hysteresis_pts=0.0)
    assert d.should_exit and d.reason == "wall_break"


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


def test_build_exit_levels_snaps_target_and_stop_to_tick():
    # Entry on a messy price — target/stop must land on ₹0.05 grid.
    # Stop floors (wider safety zone), target ceils (conservative take-profit).
    target, stop, _ = build_exit_levels(
        entry_ltp=666.57, spread=1.0, max_loss_rupees=1500,
        lots=2, lot_size=20, time_stop_s=300, now_ms=0,
    )
    # Raw stop_buffer = max(5, 1500/40=37.5) = 37.5 → raw stop = 629.07 → floor = 629.05
    # Raw target = 666.57 + 1.5 * 37.5 = 722.82 → ceil = 722.85
    assert stop == 629.05
    assert target == 722.85
    # Grid invariant — must be an exact multiple of 0.05
    assert abs(stop / 0.05 - round(stop / 0.05)) < 1e-6
    assert abs(target / 0.05 - round(target / 0.05)) < 1e-6


def test_build_exit_levels_preserves_min_stop_buffer_after_snap():
    # When rupee-cap buffer is just above 5.0 points, flooring the stop
    # must NOT pull the buffer below the min-stop floor of 5.0 points.
    target, stop, _ = build_exit_levels(
        entry_ltp=120.03, spread=0.0, max_loss_rupees=765,
        lots=1, lot_size=150, time_stop_s=300, now_ms=0,
    )
    # rupee_cap_buffer = 765/150 = 5.1; stop_buffer = max(5, 5.1) = 5.1
    # raw stop = 120.03 - 5.1 = 114.93 → floor = 114.90
    # buffer after snap = 120.03 - 114.90 = 5.13 >= 5.0 ✓
    assert (120.03 - stop) >= 5.0
    assert stop == 114.90


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
