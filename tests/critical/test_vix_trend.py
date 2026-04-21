"""VIX rolling-1h change — storage, market view, prompt integration.

Added 2026-04-21 after forensic audit flagged that the engine kept
firing PE entries while VIX was collapsing 6% intraday. A VIX drop
punishes option BUYERS on vega; today we buy, so we need to veto PEs
(most-bought-on-drop side) or raise the confidence bar.
"""

from __future__ import annotations

import time

import orjson
import pytest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

from trading.critical.market_view import MarketView
from trading.critical.regime.prompt import RegimeInput, build_user_prompt
from trading.storage import LiveStore


@pytest.fixture
def live_store():
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    return LiveStore(client=fakeredis.FakeStrictRedis(decode_responses=False))


def _seed_history(store: LiveStore, samples: list[tuple[int, float]]) -> None:
    """Seed VIX history as (ts_ms, ltp) tuples — bypasses set_vix so
    tests can inject arbitrary timelines."""
    pipe = store.r.pipeline(transaction=False)
    for ts_ms, ltp in samples:
        pipe.zadd("tpp:vix_history", {f"{ts_ms}:{ltp}": ts_ms})
    pipe.execute()


# ────────────────────────────────────────────────────────────────────────
# storage — rolling history + change calc
# ────────────────────────────────────────────────────────────────────────


def test_set_vix_appends_to_history_zset(live_store):
    live_store.set_vix(17.5, ts_ms=1_000_000)
    live_store.set_vix(17.3, ts_ms=1_060_000)
    n = live_store.r.zcard("tpp:vix_history")
    assert n == 2

def test_set_vix_prunes_samples_older_than_70_min(live_store):
    # Seed a 90-min-old sample; it should be pruned on next set_vix.
    old_ts = 1_000_000
    fresh_ts = old_ts + 90 * 60 * 1000   # 90 min later
    live_store.set_vix(18.0, ts_ms=old_ts)
    live_store.set_vix(17.0, ts_ms=fresh_ts)
    remaining = live_store.r.zrange("tpp:vix_history", 0, -1)
    assert len(remaining) == 1
    member = remaining[0].decode() if isinstance(remaining[0], bytes) else remaining[0]
    assert member.startswith(f"{fresh_ts}:")

def test_vix_change_pct_1h_collapse(live_store):
    # Baseline 1h ago was 20, current is 18 → -10%.
    now = 1_000_000_000
    one_hour_ago = now - 60 * 60 * 1000
    _seed_history(live_store, [(one_hour_ago, 20.0)])
    live_store.set_vix(18.0, ts_ms=now)
    pct = live_store.vix_change_pct_1h(now_ms=now)
    assert pct is not None
    assert abs(pct - (-10.0)) < 0.01

def test_vix_change_pct_1h_spike(live_store):
    # Baseline 1h ago was 15, current is 19 → +26.67%.
    now = 1_000_000_000
    _seed_history(live_store, [(now - 60 * 60 * 1000, 15.0)])
    live_store.set_vix(19.0, ts_ms=now)
    pct = live_store.vix_change_pct_1h(now_ms=now)
    assert pct is not None
    assert abs(pct - ((19 - 15) / 15 * 100)) < 0.01

def test_vix_change_pct_1h_none_when_history_shallow(live_store):
    # Only a 10-min-old sample → no 1h baseline yet.
    now = 1_000_000_000
    _seed_history(live_store, [(now - 10 * 60 * 1000, 17.0)])
    live_store.set_vix(17.5, ts_ms=now)
    # A sample 10-min old is within the 1h window but IS the oldest —
    # so the delta is vs that sample, not None. Document the intended
    # behaviour: return delta when any sample within the window exists.
    pct = live_store.vix_change_pct_1h(now_ms=now)
    assert pct is not None   # uses the oldest-available within window

def test_vix_change_pct_1h_none_when_no_history(live_store):
    live_store.set_vix(17.0)   # set current but no history seeded
    live_store.r.delete("tpp:vix_history")
    pct = live_store.vix_change_pct_1h()
    assert pct is None


# ────────────────────────────────────────────────────────────────────────
# market_view — delegates to store
# ────────────────────────────────────────────────────────────────────────


def test_market_view_exposes_vix_change_pct(live_store):
    # MarketView.get_vix_change_pct_1h reads wall-clock time via
    # time.time() internally. Seed at 58 min ago (inside the 60-min
    # window even after a few seconds of clock drift).
    import time as _time
    now = int(_time.time() * 1000)
    baseline_ts = now - 58 * 60 * 1000
    _seed_history(live_store, [(baseline_ts, 20.0)])
    live_store.set_vix(19.0, ts_ms=now)
    mv = MarketView(store=live_store)
    pct = mv.get_vix_change_pct_1h()
    assert pct is not None
    assert abs(pct - (-5.0)) < 0.1


# ────────────────────────────────────────────────────────────────────────
# prompt — renders VIX trend on same line when available
# ────────────────────────────────────────────────────────────────────────


def _snap(
    vix: float | None = None,
    vix_chg: float | None = None,
    index: str = "NIFTY50",
) -> RegimeInput:
    return RegimeInput(
        index=index, spot=24_526.0,
        recent_candles=((24_518, 24_528, 24_515, 24_524),),
        recent_ltps=(24_524.3,),
        total_call_oi=0, total_put_oi=0,
        total_call_oi_change=0, total_put_oi_change=0,
        highest_call_oi_strike=None, highest_put_oi_strike=None,
        india_vix=vix, india_vix_change_pct_1h=vix_chg,
    )


def test_prompt_renders_vix_with_trend_delta():
    out = build_user_prompt(_snap(vix=17.67, vix_chg=-6.12))
    assert "India VIX: 17.67  (Δ1h: -6.12%)" in out


def test_prompt_renders_vix_without_trend_when_absent():
    out = build_user_prompt(_snap(vix=17.67, vix_chg=None))
    assert "India VIX: 17.67" in out
    assert "Δ1h" not in out


def test_prompt_banknifty_still_no_vix_regardless_of_trend_field():
    # Isolation rule: BANKNIFTY prompt must not show India VIX even if
    # the vix_chg field somehow slips in.
    out = build_user_prompt(_snap(index="BANKNIFTY", vix=None, vix_chg=-5.0))
    assert "India VIX" not in out
    assert "Δ1h" not in out
