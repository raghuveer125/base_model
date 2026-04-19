"""StrategyEngine — single-process runner that wires the strategy framework.

Flow:
  pub/sub → _dispatch → strategy callbacks → ctx.emit → _handle_signal
                                                    │
                                            cooldown → risk → logger
"""

from __future__ import annotations

import threading
from typing import Iterable

from trading.config import get_settings
from trading.events import (
    ALL_CANDLE_CHANNELS,
    ALL_GREEKS_CHANNELS,
    ALL_INGEST_CHANNELS,
    EventBus,
)
from trading.logging_setup import get_logger
from trading.metrics import MetricsPublisher, metrics
from trading.schemas import IndexCandle, OptionGreeks, Signal
from trading.strategies import STRATEGY_REGISTRY
from trading.strategies.base import Strategy, StrategyContext
from trading.strategies.logger import SignalLogger
from trading.strategies.risk import CooldownManager, RiskEngine

log = get_logger(__name__)


class StrategyEngine:
    def __init__(
        self,
        indices: Iterable[str],
        strategy_names: Iterable[str],
        *,
        bus: EventBus | None = None,
        cooldown: CooldownManager | None = None,
        risk: RiskEngine | None = None,
        signal_logger: SignalLogger | None = None,
    ) -> None:
        s = get_settings()
        self.indices = list(indices)
        self.bus = bus or EventBus()
        self.cooldown = cooldown or CooldownManager(s.strategy_cooldown_seconds)
        self.risk = risk or RiskEngine(
            max_per_hour=s.strategy_max_signals_per_hour,
            max_per_day=s.strategy_max_signals_per_day,
            allowed_indices=set(self.indices),
        )
        self.signal_logger = signal_logger or SignalLogger(bus=self.bus)
        self.metrics_pub = MetricsPublisher()

        self.strategies: list[Strategy] = []
        for name in strategy_names:
            cls = STRATEGY_REGISTRY.get(name)
            if cls is None:
                raise KeyError(
                    f"strategy {name!r} not registered "
                    f"(known: {sorted(STRATEGY_REGISTRY)})"
                )
            self.strategies.append(cls(indices=self.indices))
        self._contexts: dict[str, StrategyContext] = {
            strat.name: StrategyContext(on_signal=self._handle_signal,
                                        state=strat.state)
            for strat in self.strategies
        }
        self._stop = threading.Event()

    def _handle_signal(self, sig: Signal) -> None:
        if self.cooldown.should_suppress(sig):
            metrics.incr_signal_cooldown()
            log.info("signal_suppressed_cooldown",
                     strategy=sig.strategy, instrument=sig.instrument)
            return
        ok, reason = self.risk.allows(sig)
        if not ok:
            metrics.incr_signal_risk()
            log.warning("signal_suppressed_risk",
                        strategy=sig.strategy, reason=reason)
            return
        self.signal_logger.record(sig)
        metrics.incr_signal_emit()

    def _dispatch(self, channel: str, data: dict) -> None:
        try:
            if channel.startswith("ticks."):
                self._fanout_tick(channel, data)
            elif channel.startswith("candles."):
                self._fanout_candle(data)
            elif channel.startswith("greeks."):
                self._fanout_greeks(data)
        except Exception as e:  # noqa: BLE001
            log.error("strategy_dispatch_failed", channel=channel, error=str(e))

    def _fanout_tick(self, channel: str, data: dict) -> None:
        for strat in self.strategies:
            try:
                strat.on_tick(self._contexts[strat.name], channel, data)
            except Exception as e:  # noqa: BLE001
                log.error("strategy_on_tick_failed",
                          strategy=strat.name, error=str(e))

    def _fanout_candle(self, data: dict) -> None:
        try:
            candle = IndexCandle.model_validate(data)
        except Exception as e:  # noqa: BLE001
            log.warning("candle_payload_invalid", error=str(e))
            return
        for strat in self.strategies:
            try:
                strat.on_candle_close(self._contexts[strat.name], candle)
            except Exception as e:  # noqa: BLE001
                log.error("strategy_on_candle_failed",
                          strategy=strat.name, error=str(e))

    def _fanout_greeks(self, data: dict) -> None:
        try:
            g = OptionGreeks.model_validate(data)
        except Exception as e:  # noqa: BLE001
            log.warning("greeks_payload_invalid", error=str(e))
            return
        for strat in self.strategies:
            try:
                strat.on_greeks(self._contexts[strat.name], g)
            except Exception as e:  # noqa: BLE001
                log.error("strategy_on_greeks_failed",
                          strategy=strat.name, error=str(e))

    def run(self) -> None:
        log.info("strategy_engine_start",
                 indices=self.indices,
                 strategies=[s.name for s in self.strategies])
        for strat in self.strategies:
            try:
                strat.on_start()
            except Exception as e:  # noqa: BLE001
                log.error("strategy_on_start_failed",
                          strategy=strat.name, error=str(e))
        self.metrics_pub.start()
        patterns = list(ALL_INGEST_CHANNELS) + list(ALL_CANDLE_CHANNELS) + list(ALL_GREEKS_CHANNELS)
        try:
            self.bus.subscribe(patterns, self._dispatch)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for strat in self.strategies:
            try:
                strat.on_stop()
            except Exception as e:  # noqa: BLE001
                log.error("strategy_on_stop_failed",
                          strategy=strat.name, error=str(e))
        self.metrics_pub.stop()
        try:
            self.signal_logger.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("strategy_engine_shutdown_ok",
                 risk=self.risk.snapshot(), final=metrics.snapshot())
