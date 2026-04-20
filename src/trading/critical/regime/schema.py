"""Shape of the regime-filter response.

Any non-conforming LLM output is rejected — the engine falls back to the
deterministic heuristic rather than trust a malformed verdict.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Regime = Literal["trending", "ranging", "volatile"]
Bias   = Literal["long", "short", "neutral"]


class RegimeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    regime: Regime
    bias:   Bias
    confidence: int = Field(ge=0, le=100)
    # Optional: set by cache layer so the engine knows whether this came
    # from a fresh LLM call, a replay of a historical call, or the
    # deterministic fallback.
    source: Literal["llm", "cache", "fallback"] = "llm"


def allow_entry(d: RegimeDecision, side: Literal["CE", "PE"],
                *, min_confidence: int = 50) -> bool:
    """Gate logic — the Python Sniper's entry is allowed only if the
    regime confidence is high enough AND the bias isn't actively
    against the trade side."""
    if d.confidence < min_confidence:
        return False
    if d.regime == "volatile":
        # too chaotic to trust any directional scalp
        return False
    if side == "CE" and d.bias == "short":
        return False
    if side == "PE" and d.bias == "long":
        return False
    return True
