"""HeartbeatStrategy — observer that logs event counts, emits nothing.

Exists to prove the framework wiring end-to-end without any trading logic.
"""

from __future__ import annotations

from collections import Counter

from trading.logging_setup import get_logger
from trading.schemas import IndexCandle, OptionGreeks
from trading.strategies import register
from trading.strategies.base import Strategy, StrategyContext

log = get_logger(__name__)

LOG_EVERY = 500


@register("heartbeat")
class HeartbeatStrategy(Strategy):
    def __init__(self, indices: list[str]) -> None:
        super().__init__(indices)
        self.ticks: Counter[str] = Counter()
        self.candles: Counter[tuple[str, str]] = Counter()
        self.greeks: Counter[str] = Counter()
        self._events_since_log = 0

    def on_start(self) -> None:
        log.info("heartbeat_start", indices=self.indices)

    def on_tick(self, ctx: StrategyContext, channel: str, data: dict) -> None:
        idx = data.get("index") or "?"
        kind = "index" if channel.startswith("ticks.index.") else "option"
        self.ticks[f"{idx}:{kind}"] += 1
        self._maybe_log()

    def on_candle_close(self, ctx: StrategyContext, candle: IndexCandle) -> None:
        self.candles[(candle.index, candle.timeframe)] += 1
        log.info(
            "heartbeat_candle_close",
            index=candle.index, timeframe=candle.timeframe,
            ohlc=(candle.open, candle.high, candle.low, candle.close),
            ticks=candle.tick_count,
        )

    def on_greeks(self, ctx: StrategyContext, greeks: OptionGreeks) -> None:
        self.greeks[greeks.index] += 1
        self._maybe_log()

    def on_stop(self) -> None:
        log.info(
            "heartbeat_stop",
            ticks=dict(self.ticks),
            candles={f"{i}:{tf}": c for (i, tf), c in self.candles.items()},
            greeks=dict(self.greeks),
        )

    def _maybe_log(self) -> None:
        self._events_since_log += 1
        if self._events_since_log >= LOG_EVERY:
            self._events_since_log = 0
            log.info("heartbeat_summary",
                     ticks=dict(self.ticks), greeks=dict(self.greeks))
