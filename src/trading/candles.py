"""Candle engine — independent process that builds 1m/5m/15m OHLC candles.

- Subscribes to Redis pub/sub `ticks.index.*` (IndexTick JSON).
- Bucket alignment uses `ts_exchange` (exchange-authoritative), NOT our wall clock.
- Per (index, timeframe) a CandleAggregator holds the in-progress bucket.
- A background closer thread sweeps every second to close buckets whose end has
  passed with no further ticks.
- In-progress state is persisted to Redis after each tick so a restart resumes
  exactly where we left off; any bucket whose end has passed is closed immediately
  on recovery.

Zero coupling to ingest — candles.py reads only from the Redis pub/sub bus.
"""

from __future__ import annotations

import math
import threading
from typing import Iterable

from trading.events import ALL_INGEST_CHANNELS, EventBus, ch_candle
from trading.logging_setup import get_logger
from trading.metrics import MetricsPublisher, metrics
from trading.schemas import TIMEFRAME_MS, IndexCandle, Timeframe, now_ms
from trading.storage import LiveStore, insert_candles

log = get_logger(__name__)

DEFAULT_TIMEFRAMES: tuple[Timeframe, ...] = ("1m", "5m", "15m")


class CandleAggregator:
    """Per (index, timeframe) in-progress OHLC state. Not thread-safe — one per key."""

    def __init__(self, index: str, timeframe: Timeframe, bucket_ms: int) -> None:
        self.index = index
        self.timeframe: Timeframe = timeframe
        self.bucket_ms = bucket_ms
        self.open_ts: int | None = None
        self.close_ts: int | None = None
        self.open_price: float | None = None
        self.high: float = -math.inf
        self.low: float = math.inf
        self.close_price: float | None = None
        self.tick_count: int = 0

    def bucket_for(self, ts_ms: int) -> tuple[int, int]:
        start = (ts_ms // self.bucket_ms) * self.bucket_ms
        return start, start + self.bucket_ms

    def ingest(self, ts_exchange_ms: int, ltp: float) -> IndexCandle | None:
        """Absorb a tick. Returns the closed candle if this tick rolls the bucket, else None."""
        start, end = self.bucket_for(ts_exchange_ms)
        if self.open_ts is None:
            self._begin_bucket(start, end, ltp)
            return None
        if start > self.open_ts:
            closed = self._snapshot()
            self._begin_bucket(start, end, ltp)
            return closed
        if start < self.open_ts:
            log.warning(
                "candle_out_of_order",
                index=self.index, timeframe=self.timeframe,
                tick_bucket=start, current_bucket=self.open_ts,
            )
            return None
        self._update_bucket(ltp)
        return None

    def maybe_close_stale(self, now_ms_: int) -> IndexCandle | None:
        if self.open_ts is None or self.close_ts is None:
            return None
        if now_ms_ >= self.close_ts:
            closed = self._snapshot()
            self.reset()
            return closed
        return None

    def _begin_bucket(self, start: int, end: int, ltp: float) -> None:
        self.open_ts = start
        self.close_ts = end
        self.open_price = ltp
        self.high = ltp
        self.low = ltp
        self.close_price = ltp
        self.tick_count = 1

    def _update_bucket(self, ltp: float) -> None:
        if ltp > self.high:
            self.high = ltp
        if ltp < self.low:
            self.low = ltp
        self.close_price = ltp
        self.tick_count += 1

    def _snapshot(self) -> IndexCandle:
        assert self.open_ts is not None and self.close_ts is not None
        assert self.open_price is not None and self.close_price is not None
        return IndexCandle(
            index=self.index,
            timeframe=self.timeframe,
            open_ts=self.open_ts,
            close_ts=self.close_ts,
            open=self.open_price,
            high=self.high,
            low=self.low,
            close=self.close_price,
            volume=0,
            tick_count=self.tick_count,
        )

    def reset(self) -> None:
        self.open_ts = None
        self.close_ts = None
        self.open_price = None
        self.close_price = None
        self.high = -math.inf
        self.low = math.inf
        self.tick_count = 0

    def to_state(self) -> dict:
        return {
            "index": self.index,
            "timeframe": self.timeframe,
            "bucket_ms": self.bucket_ms,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "open": self.open_price,
            "high": None if self.high == -math.inf else self.high,
            "low": None if self.low == math.inf else self.low,
            "close": self.close_price,
            "tick_count": self.tick_count,
        }

    @classmethod
    def from_state(cls, state: dict) -> "CandleAggregator":
        agg = cls(
            index=state["index"],
            timeframe=state["timeframe"],
            bucket_ms=int(state["bucket_ms"]),
        )
        agg.open_ts = state.get("open_ts")
        agg.close_ts = state.get("close_ts")
        agg.open_price = state.get("open")
        high = state.get("high")
        low = state.get("low")
        agg.high = -math.inf if high is None else float(high)
        agg.low = math.inf if low is None else float(low)
        agg.close_price = state.get("close")
        agg.tick_count = int(state.get("tick_count", 0))
        return agg


class CandleEngine:
    """Subscribe-and-aggregate engine."""

    def __init__(
        self,
        indices: Iterable[str],
        timeframes: Iterable[Timeframe] = DEFAULT_TIMEFRAMES,
        *,
        bus: EventBus | None = None,
        store: LiveStore | None = None,
        closer_interval_s: float = 1.0,
    ) -> None:
        self.indices = list(indices)
        self.timeframes: list[Timeframe] = list(timeframes)
        for tf in self.timeframes:
            if tf not in TIMEFRAME_MS:
                raise ValueError(f"unsupported timeframe: {tf}")

        self.bus = bus or EventBus()
        self.store = store or LiveStore()
        self.metrics_pub = MetricsPublisher()
        self.closer_interval_s = closer_interval_s

        self.aggs: dict[tuple[str, Timeframe], CandleAggregator] = {}
        self._persist_lock = threading.Lock()
        self._stop = threading.Event()
        self._closer_thread: threading.Thread | None = None

        for idx in self.indices:
            for tf in self.timeframes:
                self.aggs[(idx, tf)] = CandleAggregator(idx, tf, TIMEFRAME_MS[tf])

        self._recover_from_redis()

    def _recover_from_redis(self) -> None:
        now = now_ms()
        for key, agg in list(self.aggs.items()):
            idx, tf = key
            state = self.store.get_in_progress_candle(idx, tf)
            if not state:
                continue
            try:
                restored = CandleAggregator.from_state(state)
            except Exception as e:  # noqa: BLE001
                log.warning("candle_recover_failed", index=idx, timeframe=tf, error=str(e))
                self.store.clear_in_progress_candle(idx, tf)
                continue
            stale = restored.maybe_close_stale(now)
            if stale is not None:
                log.info("candle_recovered_and_closed",
                         index=idx, timeframe=tf, open_ts=stale.open_ts)
                self._emit_closed(stale)
            else:
                self.aggs[key] = restored
                log.info("candle_recovered_in_progress",
                         index=idx, timeframe=tf,
                         open_ts=restored.open_ts, tick_count=restored.tick_count)

    def _emit_closed(self, candle: IndexCandle) -> None:
        try:
            insert_candles([candle])
        except Exception as e:  # noqa: BLE001
            log.error("candle_pg_insert_failed",
                      index=candle.index, timeframe=candle.timeframe, error=str(e))
        try:
            self.store.set_last_close_candle(candle)
            self.store.clear_in_progress_candle(candle.index, candle.timeframe)
        except Exception as e:  # noqa: BLE001
            log.error("candle_redis_persist_failed", error=str(e))
        try:
            self.bus.publish(ch_candle(candle.index, candle.timeframe),
                             candle.model_dump(mode="json"))
        except Exception as e:  # noqa: BLE001
            log.error("candle_publish_failed", error=str(e))
        metrics.incr_candle_close()
        log.info("candle_closed",
                 index=candle.index, timeframe=candle.timeframe,
                 open_ts=candle.open_ts, close_ts=candle.close_ts,
                 ohlc=(candle.open, candle.high, candle.low, candle.close),
                 ticks=candle.tick_count)

    def _persist_in_progress(self, agg: CandleAggregator) -> None:
        if agg.open_ts is None:
            return
        try:
            self.store.set_in_progress_candle(agg.index, agg.timeframe, agg.to_state())
        except Exception as e:  # noqa: BLE001
            log.warning("candle_in_progress_persist_failed",
                        index=agg.index, timeframe=agg.timeframe, error=str(e))

    def on_tick(self, channel: str, data: dict) -> None:
        _ = channel
        try:
            idx = data.get("index")
            ts_ex = int(data.get("ts_exchange") or 0)
            ltp = float(data.get("ltp") or 0.0)
            if not idx or ts_ex <= 0 or ltp <= 0:
                return
            for tf in self.timeframes:
                agg = self.aggs.get((idx, tf))
                if agg is None:
                    continue
                with self._persist_lock:
                    closed = agg.ingest(ts_ex, ltp)
                    if closed is not None:
                        self._emit_closed(closed)
                    self._persist_in_progress(agg)
        except Exception as e:  # noqa: BLE001
            log.error("candle_on_tick_failed", error=str(e))

    def _closer_loop(self) -> None:
        while not self._stop.wait(self.closer_interval_s):
            try:
                now = now_ms()
                with self._persist_lock:
                    for agg in list(self.aggs.values()):
                        closed = agg.maybe_close_stale(now)
                        if closed is not None:
                            self._emit_closed(closed)
            except Exception as e:  # noqa: BLE001
                log.error("candle_closer_failed", error=str(e))

    def run(self) -> None:
        log.info("candle_engine_start",
                 indices=self.indices, timeframes=list(self.timeframes))
        self.metrics_pub.start()
        self._closer_thread = threading.Thread(
            target=self._closer_loop, daemon=True, name="candle-closer",
        )
        self._closer_thread.start()
        try:
            self.bus.subscribe([ALL_INGEST_CHANNELS[0]], self.on_tick)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        log.info("candle_engine_shutdown_begin")
        self._stop.set()
        try:
            if self._closer_thread:
                self._closer_thread.join(timeout=2)
        finally:
            self.metrics_pub.stop()
            try:
                self.bus.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("candle_engine_shutdown_ok", final=metrics.snapshot())
