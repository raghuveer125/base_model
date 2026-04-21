"""Tests for the India VIX pipe: storage, market view, prompt rendering."""

from __future__ import annotations

import pytest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

from trading.critical.market_view import MarketView
from trading.critical.regime.prompt import (
    RegimeInput, build_user_prompt,
)
from trading.storage import LiveStore


@pytest.fixture
def live_store():
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    fake = fakeredis.FakeStrictRedis(decode_responses=False)
    return LiveStore(client=fake)


# ----- storage.LiveStore -----

def test_set_vix_get_vix_roundtrip(live_store):
    assert live_store.get_vix() is None
    live_store.set_vix(11.42, ts_ms=1_776_750_000_000)
    assert live_store.get_vix() == 11.42
    assert live_store.vix_ts_ms() == 1_776_750_000_000

def test_vix_ts_is_optional(live_store):
    live_store.set_vix(14.8)
    assert live_store.get_vix() == 14.8
    assert live_store.vix_ts_ms() is None

def test_vix_set_overwrites_previous(live_store):
    live_store.set_vix(11.0)
    live_store.set_vix(15.6)
    assert live_store.get_vix() == 15.6


# ----- market_view.MarketView.get_vix -----

def test_market_view_get_vix_returns_none_when_unset(live_store):
    mv = MarketView(store=live_store)
    assert mv.get_vix() is None

def test_market_view_get_vix_reads_store(live_store):
    live_store.set_vix(13.27)
    mv = MarketView(store=live_store)
    assert mv.get_vix() == 13.27


# ----- prompt rendering -----

def _snap(
    *,
    index: str = "NIFTY50",
    vix: float | None = None,
    atm_iv_ce: float | None = None,
    atm_iv_pe: float | None = None,
) -> RegimeInput:
    return RegimeInput(
        index=index, spot=24_526.0,
        recent_candles=((24_518, 24_528, 24_515, 24_524),),
        recent_ltps=(24_524.3,),
        total_call_oi=0, total_put_oi=0,
        total_call_oi_change=0, total_put_oi_change=0,
        highest_call_oi_strike=None, highest_put_oi_strike=None,
        india_vix=vix, atm_iv_ce=atm_iv_ce, atm_iv_pe=atm_iv_pe,
    )

def test_prompt_renders_nifty_vix_when_present():
    out = build_user_prompt(_snap(vix=11.42))
    assert "India VIX: 11.42" in out

def test_prompt_omits_vix_line_when_not_provided():
    # Per-index isolation: BANKNIFTY / SENSEX should never see a VIX
    # line. When RegimeInput.india_vix is None, the line must not render.
    out = build_user_prompt(_snap(index="BANKNIFTY", vix=None,
                                   atm_iv_ce=14.2, atm_iv_pe=14.7))
    assert "India VIX" not in out
    assert "ATM IV" in out

def test_prompt_renders_atm_iv_with_skew():
    # Percentage input (>= 1.0) passes through unchanged.
    out = build_user_prompt(_snap(atm_iv_ce=12.10, atm_iv_pe=13.40))
    assert "ATM IV (this index): CE=12.10  PE=13.40" in out
    assert "skew PE-CE: +1.30" in out

def test_prompt_converts_decimal_iv_to_percentage():
    # Engine populates IV from the greeks module as a decimal (0.23 = 23%).
    # Prompt must normalise to percentage so SYSTEM_PROMPT band thresholds
    # ("ATM IV > 30", "VIX > 22") line up on a single scale.
    out = build_user_prompt(_snap(atm_iv_ce=0.2307, atm_iv_pe=0.1953))
    assert "ATM IV (this index): CE=23.07  PE=19.53" in out
    assert "skew PE-CE: -3.54" in out

def test_prompt_volatility_block_above_ltps():
    # Structural assertion — volatility is header-level context, so it
    # must appear before the candle/LTP detail the model will reason over.
    out = build_user_prompt(_snap(vix=14.8, atm_iv_ce=12.0, atm_iv_pe=12.4))
    vix_idx = out.find("India VIX:")
    iv_idx = out.find("ATM IV")
    ltps_idx = out.find("Recent LTPs:")
    assert vix_idx < ltps_idx
    assert iv_idx < ltps_idx

def test_prompt_shows_not_available_when_no_gauges():
    out = build_user_prompt(_snap(vix=None, atm_iv_ce=None, atm_iv_pe=None))
    assert "Volatility gauges: not available" in out

def test_prompt_banknifty_never_contains_nifty_vix_value():
    # Even if someone wires VIX into a BANKNIFTY snapshot by mistake,
    # the data isolation rule should catch it at review time. For the
    # engine path, passing None is the only correct call for BANKNIFTY —
    # this test just documents that the engine respects the rule.
    out = build_user_prompt(_snap(index="BANKNIFTY", vix=None))
    assert "India VIX" not in out


# ----- engine.CriticalEngine._atm_iv -----

def test_engine_atm_iv_picks_strike_closest_to_spot():
    from trading.critical.engine import CriticalEngine
    from trading.critical.market_view import ChainRow

    rows = [
        ChainRow(strike=24_400, ce_tick={"ltp": 200}, pe_tick={"ltp": 10},
                 ce_greeks={"iv": 9.9}, pe_greeks={"iv": 10.1}),
        # ATM (closest to spot=24526)
        ChainRow(strike=24_500, ce_tick={"ltp": 120}, pe_tick={"ltp": 50},
                 ce_greeks={"iv": 12.5}, pe_greeks={"iv": 13.0}),
        ChainRow(strike=24_600, ce_tick={"ltp": 60}, pe_tick={"ltp": 100},
                 ce_greeks={"iv": 14.8}, pe_greeks={"iv": 14.2}),
    ]
    ce, pe = CriticalEngine._atm_iv(rows, spot=24_526.0)
    assert ce == 12.5
    assert pe == 13.0

def test_engine_atm_iv_returns_none_on_empty_chain():
    from trading.critical.engine import CriticalEngine
    assert CriticalEngine._atm_iv([], spot=24_526.0) == (None, None)

def test_engine_atm_iv_tolerates_missing_greeks():
    from trading.critical.engine import CriticalEngine
    from trading.critical.market_view import ChainRow

    rows = [
        ChainRow(strike=24_500, ce_tick={"ltp": 120}, pe_tick={"ltp": 50},
                 ce_greeks=None, pe_greeks={"iv": 0}),  # bad/zero IV
    ]
    ce, pe = CriticalEngine._atm_iv(rows, spot=24_500.0)
    assert ce is None
    assert pe is None   # IV=0 is rejected (not a real IV)
