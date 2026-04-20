"""System + user prompt builders for the regime classifier.

System prompt is static so it stays in the Anthropic prompt-cache window
(5-minute TTL). User prompt holds the per-call data.
"""

from __future__ import annotations

from dataclasses import dataclass


SYSTEM_PROMPT = """You are a risk-neutral market-regime classifier for intraday Indian
options scalping. On each call you receive a short snapshot of an index's
recent price + open-interest behaviour and must decide whether the
prevailing regime is trending, ranging, or volatile, and whether the
short-term bias leans long, short, or neutral.

You are NOT an entry/exit signal. Do NOT recommend trades. Only classify.

Respond with STRICT JSON and NOTHING ELSE. The JSON keys are:

  regime: "trending" | "ranging" | "volatile"
  bias:   "long" | "short" | "neutral"
  confidence: integer 0-100 (your own certainty in this classification)

Rules of thumb (guidance, not rigid):
- trending: candles mostly same colour, price moving in one direction,
  small reversals.
- ranging: candles alternate colour, price oscillates around a level,
  tight high-low band.
- volatile: large candle bodies, frequent colour flips, wide wicks,
  rising realized vol.

A lower confidence is preferable to a confidently wrong regime. Err on
the side of "ranging" when the picture is mixed."""


@dataclass(frozen=True)
class RegimeInput:
    """Compact snapshot — everything the model needs, nothing it doesn't."""
    index: str
    spot: float
    # last ~15 min of 1-min candles: list of (open, high, low, close)
    recent_candles: tuple[tuple[float, float, float, float], ...]
    # last ~3 min of spot LTPs
    recent_ltps: tuple[float, ...]
    total_call_oi: int
    total_put_oi: int
    total_call_oi_change: int
    total_put_oi_change: int
    highest_call_oi_strike: int | None
    highest_put_oi_strike: int | None


def build_user_prompt(snap: RegimeInput) -> str:
    """Format the snapshot as the minimal string the model needs."""
    candles = "\n".join(
        f"  {i+1}: O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}"
        for i, (o, h, l, c) in enumerate(snap.recent_candles)
    ) or "  (no candles)"
    ltps = ", ".join(f"{x:.2f}" for x in snap.recent_ltps) or "(empty)"
    return f"""Index: {snap.index}
Spot: {snap.spot:.2f}
Recent LTPs: [{ltps}]
Recent 1m candles (oldest first):
{candles}
Total CE OI: {snap.total_call_oi}   (change: {snap.total_call_oi_change:+d})
Total PE OI: {snap.total_put_oi}   (change: {snap.total_put_oi_change:+d})
Highest CE-OI strike: {snap.highest_call_oi_strike}
Highest PE-OI strike: {snap.highest_put_oi_strike}

Classify the regime. Respond with JSON only."""
