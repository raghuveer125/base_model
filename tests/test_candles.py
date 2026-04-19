"""Candle aggregator unit tests — alignment, OHLC, rollover, stale close, recovery."""

from __future__ import annotations

from trading.candles import CandleAggregator
from trading.schemas import TIMEFRAME_MS


def _new(tf: str = "1m") -> CandleAggregator:
    return CandleAggregator("NIFTY50", tf, TIMEFRAME_MS[tf])  # type: ignore[arg-type]


def test_bucket_alignment_is_floor_of_step():
    a = _new("1m")
    start, end = a.bucket_for(1_729_300_012_345)
    assert start % 60_000 == 0
    assert end - start == 60_000
    assert start <= 1_729_300_012_345 < end


def test_bucket_alignment_5m_and_15m():
    for tf, step in (("5m", 300_000), ("15m", 900_000)):
        a = _new(tf)
        start, end = a.bucket_for(1_729_300_000_500)
        assert start % step == 0
        assert end - start == step


def test_first_tick_opens_bucket_no_close():
    a = _new("1m")
    closed = a.ingest(1_000_000, 100.0)
    assert closed is None
    assert a.open_price == 100.0
    assert a.high == 100.0
    assert a.low == 100.0
    assert a.close_price == 100.0
    assert a.tick_count == 1


def test_ohlc_updates_within_bucket():
    a = _new("1m")
    a.ingest(60_000, 100.0)
    a.ingest(60_500, 105.0)
    a.ingest(60_800, 98.5)
    a.ingest(60_999, 102.0)
    assert a.open_price == 100.0
    assert a.high == 105.0
    assert a.low == 98.5
    assert a.close_price == 102.0
    assert a.tick_count == 4


def test_rollover_returns_closed_candle_and_starts_new():
    a = _new("1m")
    a.ingest(60_000, 100.0)
    a.ingest(60_500, 110.0)
    a.ingest(60_900, 95.0)
    closed = a.ingest(120_000, 97.0)
    assert closed is not None
    assert closed.open == 100.0
    assert closed.high == 110.0
    assert closed.low == 95.0
    assert closed.close == 95.0
    assert closed.tick_count == 3
    assert closed.open_ts == 60_000
    assert closed.close_ts == 120_000
    assert a.open_ts == 120_000
    assert a.open_price == 97.0
    assert a.tick_count == 1


def test_out_of_order_tick_is_dropped():
    a = _new("1m")
    a.ingest(120_000, 100.0)
    before = (a.tick_count, a.open_price, a.high, a.low, a.close_price)
    closed = a.ingest(60_500, 999.0)
    assert closed is None
    assert (a.tick_count, a.open_price, a.high, a.low, a.close_price) == before


def test_maybe_close_stale_fires_when_end_passed():
    a = _new("1m")
    a.ingest(60_000, 100.0)
    assert a.maybe_close_stale(119_999) is None
    closed = a.maybe_close_stale(120_000)
    assert closed is not None
    assert closed.close_ts == 120_000
    assert a.open_ts is None
    assert a.tick_count == 0


def test_state_round_trip_preserves_ohlc_and_counter():
    a = _new("5m")
    a.ingest(300_000, 100.0)
    a.ingest(300_500, 105.0)
    a.ingest(301_000, 99.0)
    state = a.to_state()
    b = CandleAggregator.from_state(state)
    assert b.index == a.index
    assert b.timeframe == a.timeframe
    assert b.bucket_ms == a.bucket_ms
    assert b.open_ts == a.open_ts
    assert b.close_ts == a.close_ts
    assert b.open_price == a.open_price
    assert b.high == a.high
    assert b.low == a.low
    assert b.close_price == a.close_price
    assert b.tick_count == a.tick_count


def test_state_round_trip_on_empty_aggregator():
    a = _new("1m")
    state = a.to_state()
    b = CandleAggregator.from_state(state)
    assert b.open_ts is None
    assert b.close_ts is None
    assert b.tick_count == 0
