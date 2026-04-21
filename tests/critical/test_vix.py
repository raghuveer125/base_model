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

def _snap(vix: float | None) -> RegimeInput:
    return RegimeInput(
        index="NIFTY50", spot=24_526.0,
        recent_candles=((24_518, 24_528, 24_515, 24_524),),
        recent_ltps=(24_524.3,),
        total_call_oi=0, total_put_oi=0,
        total_call_oi_change=0, total_put_oi_change=0,
        highest_call_oi_strike=None, highest_put_oi_strike=None,
        india_vix=vix,
    )

def test_prompt_renders_vix_when_present():
    out = build_user_prompt(_snap(11.42))
    assert "India VIX: 11.42" in out

def test_prompt_shows_not_available_when_vix_missing():
    out = build_user_prompt(_snap(None))
    assert "India VIX: not available" in out

def test_prompt_vix_line_sits_above_ltps_block():
    # Structural assertion — VIX is header-level context, so it must
    # appear before the candle/LTP detail the model will reason over.
    out = build_user_prompt(_snap(14.8))
    vix_idx = out.find("India VIX:")
    ltps_idx = out.find("Recent LTPs:")
    assert vix_idx >= 0 and ltps_idx >= 0 and vix_idx < ltps_idx
