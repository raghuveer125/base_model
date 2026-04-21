"""Tick-freshness gate in `MarketView.get_chain_snapshot`.

Added to stop the engine acting on zombie data lingering in Redis from
prior sessions — the pattern that produced duplicate "NIFTY50 25000 CE
@ 120.50" entries on 2026-04-21 when today's real price was ₹0.45.
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
from trading.storage import LiveStore


@pytest.fixture
def fake_redis():
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    return fakeredis.FakeStrictRedis(decode_responses=False)


def _seed_chain(r, index: str, expiry: str, entries: dict[str, dict]):
    """Write `{strike:side: tick_dict}` into the chain hash."""
    key = f"tpp:chain:{index}:{expiry}"
    for field, tick in entries.items():
        r.hset(key, field, orjson.dumps(tick))


# ────────────────────────────────────────────────────────────────────────


def test_stale_tick_dropped_from_snapshot(fake_redis):
    now_ms = int(time.time() * 1000)
    # Stale: 120s old — past the 60s window.
    # Fresh: 5s old.
    _seed_chain(fake_redis, "NIFTY50", "2026-04-21", {
        "25000:CE": {"ltp": 120.5, "oi": 100, "strike": 25000,
                       "option_type": "CE",
                       "ts_exchange": (now_ms - 120_000) // 1000,
                       "ts_received": now_ms - 120_000},
        "24500:CE": {"ltp": 80.0, "oi": 200, "strike": 24_500,
                       "option_type": "CE",
                       "ts_exchange": (now_ms - 5_000) // 1000,
                       "ts_received": now_ms - 5_000},
    })
    # Seed a spot so build_metrics doesn't blow up.
    fake_redis.set("tpp:spot:NIFTY50", 24_500.0)
    mv = MarketView(store=LiveStore(client=fake_redis), max_tick_age_ms=60_000)
    rows = mv.get_chain_snapshot("NIFTY50", "2026-04-21")
    strikes = sorted(r.strike for r in rows)
    assert 24_500 in strikes
    assert 25_000 not in strikes, (
        "stale 25000 tick must be filtered — this is the zombie-clone guard"
    )


def test_fresh_tick_survives_filter(fake_redis):
    now_ms = int(time.time() * 1000)
    _seed_chain(fake_redis, "NIFTY50", "2026-04-21", {
        "24500:CE": {"ltp": 120.5, "oi": 100, "strike": 24_500,
                       "option_type": "CE",
                       "ts_exchange": (now_ms - 3_000) // 1000,
                       "ts_received": now_ms - 3_000},
    })
    fake_redis.set("tpp:spot:NIFTY50", 24_500.0)
    mv = MarketView(store=LiveStore(client=fake_redis), max_tick_age_ms=60_000)
    rows = mv.get_chain_snapshot("NIFTY50", "2026-04-21")
    assert len(rows) == 1
    assert rows[0].strike == 24_500
    assert (rows[0].ce_tick or {}).get("ltp") == 120.5


def test_missing_timestamp_is_treated_as_fresh(fake_redis):
    # Older test fixtures sometimes lack ts_exchange/ts_received. Don't
    # drop them silently — that would break every existing test.
    _seed_chain(fake_redis, "NIFTY50", "2026-04-21", {
        "24500:CE": {"ltp": 100.0, "oi": 50, "strike": 24_500,
                       "option_type": "CE"},
    })
    fake_redis.set("tpp:spot:NIFTY50", 24_500.0)
    mv = MarketView(store=LiveStore(client=fake_redis), max_tick_age_ms=60_000)
    rows = mv.get_chain_snapshot("NIFTY50", "2026-04-21")
    assert len(rows) == 1
    assert (rows[0].ce_tick or {}).get("ltp") == 100.0


def test_max_tick_age_none_disables_filter(fake_redis):
    now_ms = int(time.time() * 1000)
    _seed_chain(fake_redis, "NIFTY50", "2026-04-21", {
        "25000:CE": {"ltp": 120.5, "oi": 100, "strike": 25_000,
                       "option_type": "CE",
                       "ts_received": now_ms - 3_600_000},  # 1 hour old
    })
    fake_redis.set("tpp:spot:NIFTY50", 24_500.0)
    mv = MarketView(store=LiveStore(client=fake_redis), max_tick_age_ms=None)
    rows = mv.get_chain_snapshot("NIFTY50", "2026-04-21")
    assert len(rows) == 1
    assert rows[0].strike == 25_000


def test_mixed_leg_one_stale_one_fresh_keeps_strike_with_one_leg_none(fake_redis):
    now_ms = int(time.time() * 1000)
    # CE stale, PE fresh → the strike should still appear, but ce_tick
    # gets filtered out. That lets pick_instrument see the PE side only.
    _seed_chain(fake_redis, "NIFTY50", "2026-04-21", {
        "24500:CE": {"ltp": 120.5, "strike": 24_500, "option_type": "CE",
                       "ts_received": now_ms - 120_000},
        "24500:PE": {"ltp": 60.0, "strike": 24_500, "option_type": "PE",
                       "ts_received": now_ms - 2_000},
    })
    fake_redis.set("tpp:spot:NIFTY50", 24_500.0)
    mv = MarketView(store=LiveStore(client=fake_redis), max_tick_age_ms=60_000)
    rows = mv.get_chain_snapshot("NIFTY50", "2026-04-21")
    assert len(rows) == 1
    r = rows[0]
    assert r.strike == 24_500
    assert r.ce_tick is None
    assert (r.pe_tick or {}).get("ltp") == 60.0
