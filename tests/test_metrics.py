"""Metrics unit tests — counters, latency percentiles, snapshot shape."""

from __future__ import annotations

from trading.metrics import Metrics


def test_counters_increment_and_isolate():
    m = Metrics()
    m.incr_gap()
    m.incr_gap()
    m.incr_reconnect()
    m.incr_dedup_drop()
    m.incr_wal_append()
    m.incr_pg_flush(rows=10)
    m.incr_pg_flush(rows=5)
    snap = m.snapshot()
    assert snap["gap_count"] == 2
    assert snap["reconnect_count"] == 1
    assert snap["dedup_drops"] == 1
    assert snap["wal_appends"] == 1
    assert snap["pg_flushes"] == 2
    assert snap["pg_rows_flushed"] == 15


def test_latency_percentiles_and_total():
    m = Metrics()
    for lat_ms in range(1, 101):
        m.observe_tick(ts_exchange_ms=0, ts_received_ms=lat_ms)
    snap = m.snapshot()
    assert snap["ticks_total"] == 100
    assert 49 <= snap["ingest_latency_p50_ms"] <= 51
    assert 94 <= snap["ingest_latency_p95_ms"] <= 96
    assert snap["ingest_latency_max_ms"] == 100


def test_negative_latency_clamped_to_zero():
    m = Metrics()
    m.observe_tick(ts_exchange_ms=1_000, ts_received_ms=500)
    snap = m.snapshot()
    assert snap["ingest_latency_max_ms"] == 0


def test_tick_rate_rolling_window():
    m = Metrics()
    for i in range(11):
        m.observe_tick(ts_exchange_ms=0, ts_received_ms=i * 100)
    snap = m.snapshot()
    assert snap["tick_rate_per_s"] == 10.0


def test_snapshot_contains_all_expected_keys():
    m = Metrics()
    snap = m.snapshot()
    expected = {
        "ticks_total", "tick_rate_per_s",
        "ingest_latency_p50_ms", "ingest_latency_p95_ms", "ingest_latency_max_ms",
        "gap_count", "reconnect_count", "dedup_drops",
        "wal_appends", "pg_flushes", "pg_rows_flushed",
    }
    assert set(snap.keys()) == expected
