"""Pure deterministic scalp triggers.

Every function here is a side-effect-free classifier. No I/O, no clocks,
no LLM. Easy to unit-test, 100% replayable.

The engine assembles these into a final BUY/SELL decision. Bearish
signals still map to BUY — just PE instead of CE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["CE", "PE"]
BuildUp = Literal[
    "long_buildup",     # price ↑ + OI ↑  (bullish conviction)
    "short_buildup",    # price ↓ + OI ↑  (bearish conviction)
    "short_covering",   # price ↑ + OI ↓  (bullish, short exit)
    "long_unwinding",   # price ↓ + OI ↓  (bearish, long exit)
    "flat",
]


@dataclass(frozen=True)
class Signal:
    side: Side            # CE → buy calls (bullish), PE → buy puts (bearish)
    confidence: float     # 0..1
    reasons: tuple[str, ...]


# ---------- primitive classifiers ----------

def classify_buildup(
    price_delta: float | None, oi_delta: int | None,
) -> BuildUp:
    """OI interpretation — standard Indian F&O table."""
    if price_delta is None or oi_delta is None:
        return "flat"
    if price_delta > 0 and oi_delta > 0:
        return "long_buildup"
    if price_delta < 0 and oi_delta > 0:
        return "short_buildup"
    if price_delta > 0 and oi_delta < 0:
        return "short_covering"
    if price_delta < 0 and oi_delta < 0:
        return "long_unwinding"
    return "flat"


def candle_run(candle_closes: list[float], candle_opens: list[float]) -> int:
    """Signed count of trailing candles of the same colour.

    +N means the last N candles closed above their open (green run).
    -N means the last N closed below their open (red run).
      0 means the immediate last candle is a doji / list is empty.
    """
    n = min(len(candle_closes), len(candle_opens))
    if n == 0:
        return 0
    last = candle_closes[-1] - candle_opens[-1]
    if last == 0:
        return 0
    dir_ = 1 if last > 0 else -1
    run = 0
    for i in range(n - 1, -1, -1):
        diff = candle_closes[i] - candle_opens[i]
        if (diff > 0 and dir_ == 1) or (diff < 0 and dir_ == -1):
            run += 1
        else:
            break
    return dir_ * run


# ---------- signal functions ----------

def microstructure_signal(
    *,
    spread_pct: float | None,
    imbalance: float | None,
    ltp_momentum: Literal["up", "down", "flat"],
    max_spread_pct: float,
    imb_threshold: float = 0.3,
) -> Signal | None:
    """Tight spread + sufficient book imbalance + matching tick momentum."""
    if spread_pct is None or imbalance is None:
        return None
    if spread_pct > max_spread_pct:
        return None
    # book wants BUY: bid_qty >> ask_qty AND price ticking up
    if imbalance >= imb_threshold and ltp_momentum == "up":
        return Signal(
            side="CE",
            confidence=min(1.0, abs(imbalance)),
            reasons=(f"micro_bid_imbalance={imbalance:.2f}",
                     f"ltp_momentum={ltp_momentum}",
                     f"spread_pct={spread_pct:.2f}"),
        )
    if imbalance <= -imb_threshold and ltp_momentum == "down":
        return Signal(
            side="PE",
            confidence=min(1.0, abs(imbalance)),
            reasons=(f"micro_ask_imbalance={imbalance:.2f}",
                     f"ltp_momentum={ltp_momentum}",
                     f"spread_pct={spread_pct:.2f}"),
        )
    return None


def buildup_signal(
    *, buildup: BuildUp, confidence: float = 0.7,
) -> Signal | None:
    """OI-buildup based directional signal. No microstructure needed."""
    if buildup == "long_buildup" or buildup == "short_covering":
        return Signal(
            side="CE", confidence=confidence,
            reasons=(f"buildup={buildup}",),
        )
    if buildup == "short_buildup" or buildup == "long_unwinding":
        return Signal(
            side="PE", confidence=confidence,
            reasons=(f"buildup={buildup}",),
        )
    return None


def momentum_signal(
    candle_opens: list[float], candle_closes: list[float],
    *, run_threshold: int = 3,
) -> Signal | None:
    """Three (default) consecutive same-colour closes on the timeframe."""
    run = candle_run(candle_closes, candle_opens)
    if run >= run_threshold:
        return Signal(
            side="CE",
            confidence=min(1.0, 0.4 + 0.2 * (run - run_threshold)),
            reasons=(f"candle_run=+{run}",),
        )
    if run <= -run_threshold:
        return Signal(
            side="PE",
            confidence=min(1.0, 0.4 + 0.2 * (-run - run_threshold)),
            reasons=(f"candle_run={run}",),
        )
    return None


def level_break_signal(
    *,
    spot: float,
    primary_resistance: int | None,
    primary_support:    int | None,
    breaking_ce_walls:  tuple[int, ...] = (),
    breaking_pe_walls:  tuple[int, ...] = (),
) -> Signal | None:
    """Price crosses a wall *and* that wall is actively unwinding — classic
    breakout confirmed by the defenders stepping back."""
    if primary_resistance and spot > primary_resistance and primary_resistance in breaking_ce_walls:
        return Signal(
            side="CE", confidence=0.8,
            reasons=(f"broke_resistance={primary_resistance}",
                     "resistance_wall_unwinding"),
        )
    if primary_support and spot < primary_support and primary_support in breaking_pe_walls:
        return Signal(
            side="PE", confidence=0.8,
            reasons=(f"broke_support={primary_support}",
                     "support_wall_unwinding"),
        )
    return None


# ---------- aggregator ----------

def combine_signals(
    signals: list[Signal | None],
    *,
    regime_bias: Literal["long", "short", "neutral"] = "neutral",
    min_agreement: int = 2,
) -> Signal | None:
    """Require `min_agreement` non-None signals on the same side AND the
    regime not to actively oppose it.

    Confidence is the mean of the agreeing signals, floored by the
    weakest to avoid one wild outlier pushing an otherwise mediocre
    setup into trade territory.
    """
    real = [s for s in signals if s is not None]
    if len(real) < min_agreement:
        return None
    ce = [s for s in real if s.side == "CE"]
    pe = [s for s in real if s.side == "PE"]
    if len(ce) >= min_agreement and (regime_bias != "short"):
        conf = min(s.confidence for s in ce) * 0.5 + \
               (sum(s.confidence for s in ce) / len(ce)) * 0.5
        reasons: list[str] = []
        for s in ce:
            reasons.extend(s.reasons)
        return Signal(side="CE", confidence=conf, reasons=tuple(reasons))
    if len(pe) >= min_agreement and (regime_bias != "long"):
        conf = min(s.confidence for s in pe) * 0.5 + \
               (sum(s.confidence for s in pe) / len(pe)) * 0.5
        reasons = []
        for s in pe:
            reasons.extend(s.reasons)
        return Signal(side="PE", confidence=conf, reasons=tuple(reasons))
    return None
