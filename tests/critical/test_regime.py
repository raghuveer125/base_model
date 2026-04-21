"""Regime filter — schema gate, deterministic fallback, cache determinism."""

from __future__ import annotations

from trading.critical.regime import cache as cache_mod
from trading.critical.regime.fallback import classify as fb_classify
from trading.critical.regime.prompt import (
    RegimeInput, SessionFeedback, build_user_prompt,
)
from trading.critical.regime.schema import RegimeDecision, allow_entry


# ----- schema gate -----

def _d(regime, bias, confidence=60, source="llm") -> RegimeDecision:
    return RegimeDecision(regime=regime, bias=bias,
                          confidence=confidence, source=source)

def test_allow_entry_blocks_low_confidence():
    assert not allow_entry(_d("trending", "long", 40), "CE", min_confidence=50)

def test_allow_entry_blocks_volatile_regardless():
    assert not allow_entry(_d("volatile", "long", 95), "CE", min_confidence=50)

def test_allow_entry_blocks_opposing_bias():
    assert not allow_entry(_d("trending", "short", 80), "CE", min_confidence=50)
    assert not allow_entry(_d("trending", "long",  80), "PE", min_confidence=50)

def test_allow_entry_green_when_aligned():
    assert allow_entry(_d("trending", "long", 80), "CE", min_confidence=50)
    assert allow_entry(_d("ranging",  "neutral", 55), "CE", min_confidence=50)


# ----- fallback classifier -----

def _snap(candles) -> RegimeInput:
    return RegimeInput(
        index="NIFTY50", spot=25_000.0,
        recent_candles=tuple(candles),
        recent_ltps=(),
        total_call_oi=0, total_put_oi=0,
        total_call_oi_change=0, total_put_oi_change=0,
        highest_call_oi_strike=None, highest_put_oi_strike=None,
    )

def test_fallback_trending_on_clean_uptrend():
    # 8 green candles, tight body — trending long, high confidence
    candles = [
        (100+i*0.1, 100+i*0.1+0.05, 100+i*0.1-0.02, 100+i*0.1+0.04)
        for i in range(8)
    ]
    out = fb_classify(_snap(candles))
    assert out["regime"] == "trending"
    assert out["bias"] == "long"
    assert out["source"] == "fallback"

def test_fallback_ranging_on_alternating_candles():
    # 10 candles, flip colour every bar, tiny bodies (realized vol stays
    # below the "volatile" threshold) → ranging.
    candles = []
    for i in range(10):
        if i % 2 == 0:
            candles.append((100.00, 100.03, 99.99, 100.02))   # green
        else:
            candles.append((100.02, 100.03, 99.99, 100.00))   # red
    out = fb_classify(_snap(candles))
    assert out["regime"] == "ranging"

def test_fallback_volatile_on_big_moves():
    # 6 candles, big body, big moves → volatile
    candles = [
        (100,  110, 99, 109),
        (109,  112, 95, 96),
        (96,   105, 95, 104),
        (104,  110, 90, 92),
        (92,   100, 88, 99),
        (99,   108, 92, 93),
    ]
    out = fb_classify(_snap(candles))
    assert out["regime"] == "volatile"
    assert out["bias"] == "neutral"

def test_fallback_empty_candles_returns_low_confidence_ranging():
    out = fb_classify(_snap([]))
    assert out["regime"] == "ranging"
    assert out["confidence"] <= 30


# ----- feedback injection -----

def test_user_prompt_includes_no_trades_marker_when_empty_feedback():
    snap = _snap([(100, 101, 99, 100)])
    out = build_user_prompt(snap)
    assert "(no trades yet today on this index)" in out

def test_user_prompt_renders_feedback_stats():
    fb = SessionFeedback(
        dominant_exit_reason="time",
        hit_rate=0.29,
        trades_today=8,
        last_3=(
            "NIFTY50 24550PE exited via time -929",
            "NIFTY50 24450CE exited via time -705",
            "NIFTY50 24500CE exited via target +1672",
        ),
    )
    snap = RegimeInput(
        index="NIFTY50", spot=24_526.0,
        recent_candles=((24524, 24528, 24515, 24524),),
        recent_ltps=(24524.3,),
        total_call_oi=0, total_put_oi=0,
        total_call_oi_change=0, total_put_oi_change=0,
        highest_call_oi_strike=None, highest_put_oi_strike=None,
        feedback=fb,
    )
    out = build_user_prompt(snap)
    assert "trades_today = 8" in out
    assert "hit_rate = 29%" in out
    assert "dominant_exit_reason = time" in out
    assert "24550PE" in out


# ----- cache determinism -----

def test_cache_key_is_stable_within_bucket():
    snap = _snap([(100, 101, 99, 100)])
    prompt = build_user_prompt(snap)
    # two timestamps 30 s apart in the same 15-min bucket → same key
    b1 = cache_mod.minute_bucket(1_000_000_000_000, 900)
    b2 = cache_mod.minute_bucket(1_000_000_030_000, 900)
    k1 = cache_mod.cache_key("NIFTY50", b1, prompt)
    k2 = cache_mod.cache_key("NIFTY50", b2, prompt)
    assert k1 == k2

def test_cache_key_rolls_over_bucket():
    snap = _snap([(100, 101, 99, 100)])
    prompt = build_user_prompt(snap)
    b1 = cache_mod.minute_bucket(1_000_000_000_000, 900)   # 15-min window
    b2 = cache_mod.minute_bucket(1_000_000_000_000 + 15 * 60 * 1000, 900)
    assert b1 != b2
    assert cache_mod.cache_key("NIFTY50", b1, prompt) != \
           cache_mod.cache_key("NIFTY50", b2, prompt)
