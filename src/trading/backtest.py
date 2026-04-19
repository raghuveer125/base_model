"""Signal replay + backtest harness.

Drives the strategy framework from historical WAL files — no Redis, no Postgres.
For each raw tick in WAL order we:

  1. Normalize via the same adapter used in live ingest.
  2. Index ticks: update spot cache; feed per-(index, timeframe) CandleAggregators;
     emit `on_tick` and any `on_candle_close`.
  3. Option ticks: emit `on_tick`; if within ATM range, compute Greeks against
     the cached spot and emit `on_greeks`.
  4. Signals go through CooldownManager → RiskEngine → BacktestSignalLogger
     (fsync'd jsonl only).

TTE is anchored on the tick's own `ts_exchange`, not wall clock.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import orjson

from trading.adapter import normalize_index_tick, normalize_option_tick
from trading.candles import CandleAggregator
from trading.config import get_settings
from trading.greeks import compute_greeks, time_to_expiry_years
from trading.logging_setup import get_logger
from trading.schemas import (
    FYERS_INDEX_SYMBOL,
    INDEX_STRIKE_STEP,
    TIMEFRAME_MS,
    IndexCandle,
    OptionGreeks,
    Signal,
    Timeframe,
)
from trading.strategies import STRATEGY_REGISTRY
from trading.strategies.base import Strategy, StrategyContext
from trading.strategies.risk import CooldownManager, RiskEngine
from trading.wal import WALReader

log = get_logger(__name__)

DEFAULT_TIMEFRAMES: tuple[Timeframe, ...] = ("1m", "5m", "15m")


@dataclass
class BacktestSummary:
    records_read: int = 0
    ticks_index: int = 0
    ticks_option: int = 0
    candles_closed: dict[str, int] = field(default_factory=dict)
    greeks_computed: int = 0
    signals_emitted: int = 0
    signals_suppressed_cooldown: int = 0
    signals_suppressed_risk: int = 0
    signals_by_strategy: dict[str, int] = field(default_factory=dict)
    signals_by_action: dict[str, int] = field(default_factory=dict)
    ts_min: int | None = None
    ts_max: int | None = None

    def as_dict(self) -> dict:
        return {
            "records_read": self.records_read,
            "ticks_index": self.ticks_index,
            "ticks_option": self.ticks_option,
            "candles_closed": dict(self.candles_closed),
            "greeks_computed": self.greeks_computed,
            "signals_emitted": self.signals_emitted,
            "signals_suppressed_cooldown": self.signals_suppressed_cooldown,
            "signals_suppressed_risk": self.signals_suppressed_risk,
            "signals_by_strategy": dict(self.signals_by_strategy),
            "signals_by_action": dict(self.signals_by_action),
            "ts_range_ms": [self.ts_min, self.ts_max] if self.ts_min is not None else None,
        }


class BacktestSignalLogger:
    """JSONL-only logger. No Postgres, no pub/sub."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab", buffering=0)  # noqa: SIM115
        self._lock = threading.Lock()
        log.info("backtest_signal_logger_open", path=str(self.path))

    def record(self, sig: Signal) -> None:
        line = orjson.dumps(sig.model_dump(mode="json")) + b"\n"
        with self._lock:
            if self._fh is not None:
                self._fh.write(line)
                os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                finally:
                    self._fh.close()
                    self._fh = None  # type: ignore[assignment]


class BacktestRunner:
    def __init__(
        self,
        *,
        wal_dir: Path | None = None,
        date: str | None = None,
        indices: Iterable[str] | None = None,
        strategy_names: Iterable[str] = (),
        output_path: Path | None = None,
        timeframes: Iterable[Timeframe] = DEFAULT_TIMEFRAMES,
        atm_range: int | None = None,
        apply_cooldown: bool = True,
        apply_risk: bool = True,
    ) -> None:
        s = get_settings()
        self.reader = WALReader(wal_dir) if wal_dir else WALReader()
        self.date = date
        self.indices = list(indices) if indices is not None else s.index_list
        self.timeframes: list[Timeframe] = list(timeframes)
        self.atm_range = atm_range if atm_range is not None else s.atm_strike_window
        self.output_path = Path(output_path) if output_path is not None else (
            s.log_dir / f"backtest_signals_{(date or 'all')}.jsonl"
        )

        self.strategies: list[Strategy] = []
        for name in strategy_names:
            cls = STRATEGY_REGISTRY.get(name)
            if cls is None:
                raise KeyError(
                    f"strategy {name!r} not registered "
                    f"(known: {sorted(STRATEGY_REGISTRY)})"
                )
            self.strategies.append(cls(indices=self.indices))

        self.contexts: dict[str, StrategyContext] = {
            strat.name: StrategyContext(on_signal=self._handle_signal,
                                        state=strat.state)
            for strat in self.strategies
        }

        self.cooldown = (
            CooldownManager(s.strategy_cooldown_seconds) if apply_cooldown else None
        )
        self.risk = (
            RiskEngine(
                max_per_hour=s.strategy_max_signals_per_hour,
                max_per_day=s.strategy_max_signals_per_day,
                allowed_indices=set(self.indices),
            ) if apply_risk else None
        )
        self.logger = BacktestSignalLogger(self.output_path)

        self.aggs: dict[tuple[str, Timeframe], CandleAggregator] = {}
        for idx in self.indices:
            for tf in self.timeframes:
                self.aggs[(idx, tf)] = CandleAggregator(idx, tf, TIMEFRAME_MS[tf])
        self.spot: dict[str, float] = {}

        self.summary = BacktestSummary()
        self.summary.candles_closed = Counter()
        self.summary.signals_by_strategy = Counter()
        self.summary.signals_by_action = Counter()

    def _handle_signal(self, sig: Signal) -> None:
        if self.cooldown is not None and self.cooldown.should_suppress(sig):
            self.summary.signals_suppressed_cooldown += 1
            return
        if self.risk is not None:
            ok, _ = self.risk.allows(sig)
            if not ok:
                self.summary.signals_suppressed_risk += 1
                return
        self.logger.record(sig)
        self.summary.signals_emitted += 1
        self.summary.signals_by_strategy[sig.strategy] += 1  # type: ignore[index]
        self.summary.signals_by_action[sig.action] += 1      # type: ignore[index]

    def _dispatch_index(self, tick) -> None:
        channel = f"ticks.index.{tick.index}"
        data = tick.model_dump(mode="json")
        for strat in self.strategies:
            try:
                strat.on_tick(self.contexts[strat.name], channel, data)
            except Exception as e:  # noqa: BLE001
                log.warning("bt_strategy_tick_failed",
                            strategy=strat.name, error=str(e))

        self.spot[tick.index] = tick.ltp

        for tf in self.timeframes:
            agg = self.aggs[(tick.index, tf)]
            closed = agg.ingest(tick.ts_exchange, tick.ltp)
            if closed is not None:
                self._emit_candle(closed)

    def _emit_candle(self, candle: IndexCandle) -> None:
        self.summary.candles_closed[candle.timeframe] = (
            self.summary.candles_closed.get(candle.timeframe, 0) + 1
        )
        for strat in self.strategies:
            try:
                strat.on_candle_close(self.contexts[strat.name], candle)
            except Exception as e:  # noqa: BLE001
                log.warning("bt_strategy_candle_failed",
                            strategy=strat.name, error=str(e))

    def _dispatch_option(self, tick) -> None:
        channel = f"ticks.option.{tick.index}"
        data = tick.model_dump(mode="json")
        for strat in self.strategies:
            try:
                strat.on_tick(self.contexts[strat.name], channel, data)
            except Exception as e:  # noqa: BLE001
                log.warning("bt_strategy_tick_failed",
                            strategy=strat.name, error=str(e))

        spot = self.spot.get(tick.index)
        if spot is None or spot <= 0:
            return
        step = INDEX_STRIKE_STEP.get(tick.index)
        if step is None or abs(tick.strike - spot) > self.atm_range * step:
            return
        T = time_to_expiry_years(tick.expiry, now_ms_=tick.ts_exchange)
        if T <= 0:
            return
        iv = float(tick.iv) if tick.iv is not None else 0.0
        d, g, th, v = compute_greeks(
            tick.option_type, spot, tick.strike, iv, T,
            get_settings().risk_free_rate,
        )
        greeks = OptionGreeks(
            index=tick.index, strike=tick.strike, option_type=tick.option_type,
            expiry=tick.expiry, spot=spot, iv=tick.iv,
            time_to_expiry_years=T, delta=d, gamma=g, theta=th, vega=v,
            ts=tick.ts_exchange,
        )
        self.summary.greeks_computed += 1
        for strat in self.strategies:
            try:
                strat.on_greeks(self.contexts[strat.name], greeks)
            except Exception as e:  # noqa: BLE001
                log.warning("bt_strategy_greeks_failed",
                            strategy=strat.name, error=str(e))

    def run(self) -> dict:
        log.info("backtest_start",
                 date=self.date or "all",
                 indices=self.indices,
                 strategies=[s.name for s in self.strategies],
                 output=str(self.output_path))
        for strat in self.strategies:
            try:
                strat.on_start()
            except Exception as e:  # noqa: BLE001
                log.warning("bt_on_start_failed", strategy=strat.name, error=str(e))

        last_ts: int | None = None
        try:
            for rec in self.reader.iter_records(date=self.date):
                self.summary.records_read += 1
                if rec.get("kind") != "raw_tick":
                    continue
                payload = rec.get("data") or {}
                sym = payload.get("symbol") or payload.get("sym") or ""
                if sym in FYERS_INDEX_SYMBOL.values():
                    tick = normalize_index_tick(payload)
                    if tick is None:
                        continue
                    self.summary.ticks_index += 1
                    last_ts = tick.ts_exchange
                    self._track_ts(last_ts)
                    self._dispatch_index(tick)
                else:
                    tick = normalize_option_tick(payload)
                    if tick is None:
                        continue
                    self.summary.ticks_option += 1
                    last_ts = tick.ts_exchange
                    self._track_ts(last_ts)
                    self._dispatch_option(tick)

            if last_ts is not None:
                for agg in self.aggs.values():
                    closed = agg.maybe_close_stale(last_ts + agg.bucket_ms)
                    if closed is not None:
                        self._emit_candle(closed)
        finally:
            for strat in self.strategies:
                try:
                    strat.on_stop()
                except Exception as e:  # noqa: BLE001
                    log.warning("bt_on_stop_failed", strategy=strat.name, error=str(e))
            self.logger.close()

        log.info("backtest_done", summary=self.summary.as_dict())
        return self.summary.as_dict()

    def _track_ts(self, ts: int) -> None:
        if self.summary.ts_min is None or ts < self.summary.ts_min:
            self.summary.ts_min = ts
        if self.summary.ts_max is None or ts > self.summary.ts_max:
            self.summary.ts_max = ts
