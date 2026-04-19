"""ReplayEngine — generalized replay runner.

One code path for WAL-only, Postgres-only, and merged sources. Strategies
receive the same `on_tick` / `on_candle_close` / `on_greeks` callbacks whether
live or replayed. No wall-clock calls on the replay path — bucket alignment
and TTE use each event's own timestamp.

Output directory `{LOG_DIR}/replays/{run_id}/`:
  signals.jsonl    one Signal JSON per line, fsync'd
  summary.json     ReplaySummary.as_dict (sorted keys)
  manifest.json    run metadata (source, strategies, config, started/finished)

`run_id` is a 16-hex SHA-256 of the source fingerprint, sorted strategy names,
sorted indices, sorted timeframes, and config knobs. Same inputs → same run_id
→ byte-identical signals.jsonl and summary.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import orjson

from trading.candles import CandleAggregator
from trading.config import get_settings
from trading.greeks import compute_greeks, time_to_expiry_years
from trading.logging_setup import get_logger
from trading.replay.sources import EventKind, EventSource, ReplayEvent
from trading.schemas import (
    INDEX_STRIKE_STEP,
    TIMEFRAME_MS,
    IndexCandle,
    IndexTick,
    OptionGreeks,
    OptionTick,
    Signal,
    Timeframe,
)
from trading.strategies import STRATEGY_REGISTRY
from trading.strategies.base import Strategy, StrategyContext
from trading.strategies.risk import CooldownManager, RiskEngine

log = get_logger(__name__)

DEFAULT_TIMEFRAMES: tuple[Timeframe, ...] = ("1m", "5m", "15m")


@dataclass
class ReplaySummary:
    run_id: str = ""
    records_read: int = 0
    ticks_index: int = 0
    ticks_option: int = 0
    candles_from_source: int = 0
    candles_synthesized: int = 0
    greeks_computed: int = 0
    signals_emitted: int = 0
    signals_suppressed_cooldown: int = 0
    signals_suppressed_risk: int = 0
    signals_by_strategy: dict[str, int] = field(default_factory=dict)
    signals_by_action: dict[str, int] = field(default_factory=dict)
    candles_closed_by_tf: dict[str, int] = field(default_factory=dict)
    ts_min: int | None = None
    ts_max: int | None = None
    wall_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "records_read": self.records_read,
            "ticks_index": self.ticks_index,
            "ticks_option": self.ticks_option,
            "candles_from_source": self.candles_from_source,
            "candles_synthesized": self.candles_synthesized,
            "candles_closed_by_tf": dict(self.candles_closed_by_tf),
            "greeks_computed": self.greeks_computed,
            "signals_emitted": self.signals_emitted,
            "signals_suppressed_cooldown": self.signals_suppressed_cooldown,
            "signals_suppressed_risk": self.signals_suppressed_risk,
            "signals_by_strategy": dict(self.signals_by_strategy),
            "signals_by_action": dict(self.signals_by_action),
            "ts_range_ms": [self.ts_min, self.ts_max] if self.ts_min is not None else None,
            "wall_seconds": round(self.wall_seconds, 3),
        }


class _JsonlSignalLogger:
    """fsync'd jsonl writer. Replay-only — never touches PG or pub/sub."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab", buffering=0)  # noqa: SIM115
        self._lock = threading.Lock()

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


def _derive_run_id(
    source: EventSource,
    strategy_names: list[str],
    indices: list[str],
    timeframes: list[str],
    atm_range: int,
    risk_free_rate: float,
    cooldown_seconds: int,
    apply_cooldown: bool,
    apply_risk: bool,
) -> str:
    h = hashlib.sha256(b"replay|")
    h.update(source.fingerprint().encode())
    h.update(b"|")
    h.update("|".join(sorted(strategy_names)).encode())
    h.update(b"|")
    h.update("|".join(sorted(indices)).encode())
    h.update(b"|")
    h.update("|".join(sorted(timeframes)).encode())
    h.update(f"|atm={atm_range}|rfr={risk_free_rate}|cd={cooldown_seconds}".encode())
    h.update(f"|cd_on={apply_cooldown}|risk_on={apply_risk}".encode())
    return h.hexdigest()[:16]


class ReplayEngine:
    def __init__(
        self,
        source: EventSource,
        *,
        strategy_names: Iterable[str],
        indices: Iterable[str] | None = None,
        output_dir: Path | None = None,
        timeframes: Iterable[Timeframe] = DEFAULT_TIMEFRAMES,
        atm_range: int | None = None,
        apply_cooldown: bool = True,
        apply_risk: bool = True,
    ) -> None:
        s = get_settings()
        self.source = source
        self.indices = list(indices) if indices is not None else s.index_list
        self.timeframes: list[Timeframe] = list(timeframes)
        self.atm_range = atm_range if atm_range is not None else s.atm_strike_window

        self.strategies: list[Strategy] = []
        for name in strategy_names:
            cls = STRATEGY_REGISTRY.get(name)
            if cls is None:
                raise KeyError(
                    f"strategy {name!r} not registered "
                    f"(known: {sorted(STRATEGY_REGISTRY)})"
                )
            self.strategies.append(cls(indices=self.indices))

        self.run_id = _derive_run_id(
            source, [st.name for st in self.strategies], self.indices,
            list(self.timeframes), self.atm_range,
            s.risk_free_rate, s.strategy_cooldown_seconds,
            apply_cooldown, apply_risk,
        )
        self.output_dir = (
            Path(output_dir) if output_dir is not None
            else s.log_dir / "replays" / self.run_id
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

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
        self.logger = _JsonlSignalLogger(self.output_dir / "signals.jsonl")

        self.aggs: dict[tuple[str, Timeframe], CandleAggregator] = {
            (idx, tf): CandleAggregator(idx, tf, TIMEFRAME_MS[tf])
            for idx in self.indices for tf in self.timeframes
        }
        self.spot: dict[str, float] = {}

        self.summary = ReplaySummary(run_id=self.run_id)
        self.summary.signals_by_strategy = Counter()
        self.summary.signals_by_action = Counter()
        self.summary.candles_closed_by_tf = Counter()

        self._started_at: float = 0.0

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

    def _on_index_tick(self, event: ReplayEvent) -> None:
        tick = IndexTick.model_validate(event.payload)
        self.summary.ticks_index += 1
        channel = f"ticks.index.{tick.index}"
        for strat in self.strategies:
            try:
                strat.on_tick(self.contexts[strat.name], channel, event.payload)
            except Exception as e:  # noqa: BLE001
                log.warning("replay_strategy_tick_failed",
                            strategy=strat.name, error=str(e))
        self.spot[tick.index] = tick.ltp
        for tf in self.timeframes:
            agg = self.aggs[(tick.index, tf)]
            closed = agg.ingest(tick.ts_exchange, tick.ltp)
            if closed is not None:
                self.summary.candles_synthesized += 1
                self._dispatch_candle(closed)

    def _on_option_tick(self, event: ReplayEvent) -> None:
        tick = OptionTick.model_validate(event.payload)
        self.summary.ticks_option += 1
        channel = f"ticks.option.{tick.index}"
        for strat in self.strategies:
            try:
                strat.on_tick(self.contexts[strat.name], channel, event.payload)
            except Exception as e:  # noqa: BLE001
                log.warning("replay_strategy_tick_failed",
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
                log.warning("replay_strategy_greeks_failed",
                            strategy=strat.name, error=str(e))

    def _on_candle(self, event: ReplayEvent) -> None:
        candle = IndexCandle.model_validate(event.payload)
        self.summary.candles_from_source += 1
        self._dispatch_candle(candle)

    def _dispatch_candle(self, candle: IndexCandle) -> None:
        self.summary.candles_closed_by_tf[candle.timeframe] = (
            self.summary.candles_closed_by_tf.get(candle.timeframe, 0) + 1
        )
        for strat in self.strategies:
            try:
                strat.on_candle_close(self.contexts[strat.name], candle)
            except Exception as e:  # noqa: BLE001
                log.warning("replay_strategy_candle_failed",
                            strategy=strat.name, error=str(e))

    def run(self) -> dict:
        self._started_at = time.time()
        log.info("replay_start",
                 run_id=self.run_id,
                 source=self.source.description(),
                 strategies=[s.name for s in self.strategies],
                 output=str(self.output_dir))
        for strat in self.strategies:
            try:
                strat.on_start()
            except Exception as e:  # noqa: BLE001
                log.warning("replay_on_start_failed",
                            strategy=strat.name, error=str(e))

        last_ts: int | None = None
        try:
            for ev in self.source.iter_events():
                self.summary.records_read += 1
                if self.summary.ts_min is None or ev.ts < self.summary.ts_min:
                    self.summary.ts_min = ev.ts
                if self.summary.ts_max is None or ev.ts > self.summary.ts_max:
                    self.summary.ts_max = ev.ts
                last_ts = ev.ts
                if ev.kind == EventKind.INDEX_TICK:
                    self._on_index_tick(ev)
                elif ev.kind == EventKind.OPTION_TICK:
                    self._on_option_tick(ev)
                elif ev.kind == EventKind.CANDLE:
                    self._on_candle(ev)

            if last_ts is not None:
                for agg in self.aggs.values():
                    closed = agg.maybe_close_stale(last_ts + agg.bucket_ms)
                    if closed is not None:
                        self.summary.candles_synthesized += 1
                        self._dispatch_candle(closed)
        finally:
            for strat in self.strategies:
                try:
                    strat.on_stop()
                except Exception as e:  # noqa: BLE001
                    log.warning("replay_on_stop_failed",
                                strategy=strat.name, error=str(e))
            self.logger.close()

        self.summary.wall_seconds = time.time() - self._started_at
        summary_dict = self.summary.as_dict()
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary_dict, indent=2, sort_keys=True)
        )
        (self.output_dir / "manifest.json").write_text(json.dumps({
            "run_id": self.run_id,
            "source": self.source.description(),
            "strategies": [s.name for s in self.strategies],
            "indices": self.indices,
            "timeframes": list(self.timeframes),
            "atm_range": self.atm_range,
            "apply_cooldown": self.cooldown is not None,
            "apply_risk": self.risk is not None,
            "started_at": datetime.fromtimestamp(self._started_at, tz=timezone.utc).isoformat(),
            "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        }, indent=2, sort_keys=True))

        log.info("replay_done", run_id=self.run_id, summary=summary_dict)
        return summary_dict
