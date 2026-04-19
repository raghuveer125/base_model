"""In-process metrics — counters + rolling latency window, snapshotted to Redis.

Redis hash `tpp:metrics` fields:
  ticks_total              cumulative tick count
  tick_rate_per_s          rolling-window rate (float, 2dp)
  ingest_latency_p50_ms    rolling p50 of (ts_received - ts_exchange)
  ingest_latency_p95_ms    rolling p95
  ingest_latency_max_ms    rolling max
  gap_count                cumulative GapDetector firings
  reconnect_count          cumulative WS reconnect attempts
  dedup_drops              cumulative duplicate ticks dropped
  wal_appends              cumulative WAL records
  pg_flushes               cumulative BatchBuffer flushes
  pg_rows_flushed          cumulative rows inserted into Postgres

Key `tpp:metrics:ts` holds last-publish epoch-ms so staleness is detectable.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from trading.logging_setup import get_logger
from trading.storage import get_redis

log = get_logger(__name__)


def _percentile(values: list[int], p: int) -> int:
    if not values:
        return 0
    vs = sorted(values)
    idx = int(round((p / 100.0) * (len(vs) - 1)))
    return vs[max(0, min(len(vs) - 1, idx))]


class Metrics:
    def __init__(self, window: int = 2000) -> None:
        self._lock = threading.Lock()
        self._lat: deque[int] = deque(maxlen=window)
        self._tick_times_ms: deque[int] = deque(maxlen=window)
        self.ticks_total = 0
        self.gap_count = 0
        self.reconnect_count = 0
        self.dedup_drops = 0
        self.wal_appends = 0
        self.pg_flushes = 0
        self.pg_rows_flushed = 0
        self.candles_closed = 0

    def observe_tick(self, ts_exchange_ms: int, ts_received_ms: int) -> None:
        lat = ts_received_ms - ts_exchange_ms
        if lat < 0:
            lat = 0  # clock skew guard
        with self._lock:
            self.ticks_total += 1
            self._lat.append(lat)
            self._tick_times_ms.append(ts_received_ms)

    def incr_gap(self) -> None:
        with self._lock:
            self.gap_count += 1

    def incr_reconnect(self) -> None:
        with self._lock:
            self.reconnect_count += 1

    def incr_dedup_drop(self) -> None:
        with self._lock:
            self.dedup_drops += 1

    def incr_wal_append(self) -> None:
        with self._lock:
            self.wal_appends += 1

    def incr_pg_flush(self, rows: int) -> None:
        with self._lock:
            self.pg_flushes += 1
            self.pg_rows_flushed += rows

    def incr_candle_close(self) -> None:
        with self._lock:
            self.candles_closed += 1

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            lats = list(self._lat)
            times = list(self._tick_times_ms)
            ticks_total = self.ticks_total
            gap_count = self.gap_count
            reconnect_count = self.reconnect_count
            dedup_drops = self.dedup_drops
            wal_appends = self.wal_appends
            pg_flushes = self.pg_flushes
            pg_rows_flushed = self.pg_rows_flushed
            candles_closed = self.candles_closed

        if len(times) >= 2:
            span_s = (times[-1] - times[0]) / 1000.0
            rate = (len(times) - 1) / span_s if span_s > 0 else 0.0
        else:
            rate = 0.0

        return {
            "ticks_total": ticks_total,
            "tick_rate_per_s": round(rate, 2),
            "ingest_latency_p50_ms": _percentile(lats, 50),
            "ingest_latency_p95_ms": _percentile(lats, 95),
            "ingest_latency_max_ms": max(lats) if lats else 0,
            "gap_count": gap_count,
            "reconnect_count": reconnect_count,
            "dedup_drops": dedup_drops,
            "wal_appends": wal_appends,
            "pg_flushes": pg_flushes,
            "pg_rows_flushed": pg_rows_flushed,
            "candles_closed": candles_closed,
        }


metrics = Metrics()


class MetricsPublisher:
    """Background loop that writes snapshots to Redis and logs them."""

    def __init__(self, interval_s: int = 5) -> None:
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="metrics-publisher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._publish_once()

    def _publish_once(self) -> None:
        try:
            snap = metrics.snapshot()
            client = get_redis()
            pipe = client.pipeline(transaction=False)
            pipe.hset("tpp:metrics", mapping={k: str(v) for k, v in snap.items()})
            pipe.set("tpp:metrics:ts", int(time.time() * 1000))
            pipe.execute()
            log.info("metrics_snapshot", **snap)
        except Exception as e:  # noqa: BLE001
            log.warning("metrics_publish_failed", error=str(e))
