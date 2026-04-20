"""Deterministic regime classifier — used when LLM is unreachable / not
configured, and also as the baseline for replay-determinism tests.

Simple rules from first principles:

  * Realized volatility (stdev of 1m close returns) relative to its own
    recent history → volatile vs not.
  * Count of candle-colour flips in the window → ranging (many flips)
    vs trending (few flips).
  * Sign of the session so far (first close → last close) → bias.
  * Confidence scales with how clean the signals are (e.g., a strong
    one-directional run with low vol → high conf trending).

The output is a plain dict with the same keys as `RegimeDecision`, so
both paths (LLM and fallback) feed the same validator.
"""

from __future__ import annotations

import math
from typing import Literal

from trading.critical.regime.prompt import RegimeInput


def _realized_vol(closes: list[float]) -> float:
    if len(closes) < 2:
        return 0.0
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes)) if closes[i - 1] > 0
    ]
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var)


def _candle_flips(opens: list[float], closes: list[float]) -> int:
    """Number of times consecutive candle colours differ."""
    n = min(len(opens), len(closes))
    flips = 0
    prev_dir = 0
    for i in range(n):
        diff = closes[i] - opens[i]
        d = 1 if diff > 0 else (-1 if diff < 0 else 0)
        if d != 0 and prev_dir != 0 and d != prev_dir:
            flips += 1
        if d != 0:
            prev_dir = d
    return flips


def classify(snap: RegimeInput) -> dict:
    candles = snap.recent_candles
    if not candles:
        return {"regime": "ranging", "bias": "neutral",
                "confidence": 20, "source": "fallback"}

    opens  = [c[0] for c in candles]
    closes = [c[3] for c in candles]
    n = len(candles)

    vol = _realized_vol(closes)
    flips = _candle_flips(opens, closes)
    session_change = closes[-1] - closes[0]

    # Volatility threshold ≈ 0.25% per-minute stdev on the underlying
    # → anything bigger is "volatile". Tunable via future config.
    regime: Literal["trending", "ranging", "volatile"]
    if vol >= 0.0025:
        regime = "volatile"
    elif flips >= max(2, n // 3):
        regime = "ranging"
    else:
        regime = "trending"

    bias: Literal["long", "short", "neutral"]
    if regime == "volatile":
        bias = "neutral"
    elif session_change > 0 and flips <= n // 4:
        bias = "long"
    elif session_change < 0 and flips <= n // 4:
        bias = "short"
    else:
        bias = "neutral"

    # Confidence — 40 baseline, +20 if trending cleanly, -10 if volatile.
    conf = 40
    if regime == "trending" and flips == 0:
        conf = 70
    elif regime == "trending":
        conf = 55
    elif regime == "volatile":
        conf = 30

    return {
        "regime": regime, "bias": bias,
        "confidence": conf, "source": "fallback",
    }
