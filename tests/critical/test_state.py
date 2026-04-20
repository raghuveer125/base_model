"""Rolling-state tests — deques dedupe, momentum classifier, circuit reset."""

from __future__ import annotations

from datetime import date

from trading.critical.state import CriticalState, StrikeHistory


def test_strike_history_dedupes_consecutive_duplicates():
    h = StrikeHistory()
    h.observe(100.0, 1000)
    h.observe(100.0, 1000)   # no-op
    h.observe(101.0, 1100)
    assert list(h.ltps) == [100.0, 101.0]
    assert list(h.ois)  == [1000, 1100]

def test_strike_history_momentum_up_down_flat():
    h = StrikeHistory()
    for v in (100.0, 101.0, 102.0):
        h.observe(v, None)
    assert h.ltp_momentum() == "up"

    h2 = StrikeHistory()
    for v in (100.0, 99.0, 98.0):
        h2.observe(v, None)
    assert h2.ltp_momentum() == "down"

    h3 = StrikeHistory()
    h3.observe(100.0, None)
    assert h3.ltp_momentum() == "flat"

def test_strike_history_oi_delta_signed():
    h = StrikeHistory()
    h.observe(None, 1000)
    h.observe(None, 1500)
    assert h.oi_delta() == 500

def test_strike_history_oi_delta_requires_two():
    h = StrikeHistory()
    h.observe(None, 1000)
    assert h.oi_delta() is None


def test_critical_state_day_reset_clears_consecutive_losses():
    st = CriticalState()
    s = st.get("NIFTY50")
    s.consecutive_losses = 3
    s.last_loss_ts_ms = 123
    s.day_reset_date = "2026-04-19"
    st.reset_if_new_day(date(2026, 4, 20))
    assert s.consecutive_losses == 0
    assert s.last_loss_ts_ms == 0

def test_critical_state_concurrent_open_counts_positions():
    st = CriticalState()
    # no positions
    assert st.concurrent_open() == 0
    # fake two open positions
    st.get("NIFTY50").position = object()   # type: ignore[assignment]
    st.get("BANKNIFTY").position = object()  # type: ignore[assignment]
    st.get("SENSEX").position = None
    assert st.concurrent_open() == 2
