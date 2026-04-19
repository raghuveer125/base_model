"""Strategy ABC + emit helper."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Callable

from trading.schemas import IndexCandle, OptionGreeks, Signal, SignalAction, now_ms


@dataclass
class StrategyContext:
    """Per-callback context. Strategies never construct this themselves."""
    on_signal: Callable[[Signal], None]
    state: dict = field(default_factory=dict)

    def emit(
        self,
        *,
        strategy: str,
        index: str,
        action: SignalAction,
        instrument: str,
        reason: str = "",
        confidence: float = 0.0,
        metadata: dict | None = None,
        ts: int | None = None,
    ) -> Signal:
        sig = Signal(
            strategy=strategy,
            index=index,
            action=action,
            instrument=instrument,
            reason=reason,
            confidence=confidence,
            metadata=metadata or {},
            ts=ts or now_ms(),
        )
        self.on_signal(sig)
        return sig


class Strategy(ABC):
    """Base class for all strategies. Default callbacks are no-ops."""

    name: str = ""

    def __init__(self, indices: list[str]) -> None:
        self.indices = indices
        self.state: dict = {}

    def on_start(self) -> None:
        return None

    def on_stop(self) -> None:
        return None

    def on_tick(self, ctx: StrategyContext, channel: str, data: dict) -> None:
        return None

    def on_candle_close(self, ctx: StrategyContext, candle: IndexCandle) -> None:
        return None

    def on_greeks(self, ctx: StrategyContext, greeks: OptionGreeks) -> None:
        return None
