"""System + user prompt builders for the regime classifier.

System prompt is static so it stays in the Anthropic prompt-cache window
(5-minute TTL). User prompt holds the per-call data.
"""

from __future__ import annotations

from dataclasses import dataclass


SYSTEM_PROMPT = """You are a risk-neutral market-regime classifier AND adaptive
co-pilot for intraday Indian options scalping. On each call you receive
a short snapshot of an index's recent price + open-interest behaviour
and must decide whether the prevailing regime is trending, ranging, or
volatile, and whether the short-term bias leans long, short, or neutral.

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
the side of "ranging" when the picture is mixed.

=== ADAPTIVE CO-PILOT FEEDBACK RULES ===
You operate inside a system that reports how recent trades have closed.
Use that context to cool down or warm up your gate:

- If the recent feedback shows the dominant exit reason is TIME_STOP
  (e.g. 5 of 7 trades timed out) AND hit-rate is below 35%, treat this
  as evidence that the local signal engine is firing into non-moves.
  When you classify the regime as RANGING in this state, you MUST set
  confidence below 50 UNLESS an imbalance signal (imbalance > 0.8) in
  the snapshot clearly points to a directional move.

- India VIX is provided as a numeric value (or "not available" if the
  ingest hasn't populated it yet). Use it to size your conviction:
    * VIX < 12  → low-vol regime. Option premia decay fast; the 300s
      time-stop becomes a trap for non-directional entries. Set bias =
      "neutral" with confidence < 50 UNLESS the candles show a clean
      directional run (trending with few flips).
    * 12 ≤ VIX ≤ 18 → normal regime; judge on price action alone.
    * VIX > 18 → elevated volatility. Widen your tolerance for colour
      flips before declaring "volatile" — whippy price action is
      expected and not the same as a structural volatility breakout.
    * VIX > 22 → classify as "volatile" and set bias = "neutral"
      regardless of candle pattern. The regime gate will veto entries.
  If VIX is "not available", ignore these rules and classify on the
  other inputs alone.

- If the last 3 trades on this index all exited via the same reason
  ("time", "wall_break", or "stop"), treat that as evidence your own
  recent regime calls have been wrong. Consider the OPPOSITE bias or
  return bias = "neutral" with lowered confidence.

- If the feedback reports no recent trades, behave normally (use the
  rules of thumb above without adaptive dampening).

The block labelled "Recent session feedback" in the user message is
your short-term memory. Treat it as authoritative about what already
happened this session, but not predictive — your classification of the
current snapshot is still the primary output."""


@dataclass(frozen=True)
class SessionFeedback:
    """Short-term trade outcome memory injected into the user prompt.

    Populated by the engine from the paper-trading audit log so Claude
    can dampen / warm up its gate based on how recently-fired trades
    actually closed. All fields optional — empty `last_3` means no
    recent trades on this index today.
    """
    dominant_exit_reason: str | None = None
    hit_rate: float | None = None       # 0..1
    trades_today: int = 0
    # Short one-line summaries: "NIFTY50 24550PE exited via time -929"
    last_3: tuple[str, ...] = ()


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
    # India VIX spot — if None, the prompt renders "not available" and
    # VIX-sensitive SYSTEM_PROMPT rules should not fire.
    india_vix: float | None = None
    # Optional adaptive-memory block; when absent, SYSTEM_PROMPT's rules
    # say "behave normally" so this stays a pure additive extension.
    feedback: SessionFeedback | None = None


def build_user_prompt(snap: RegimeInput) -> str:
    """Format the snapshot as the minimal string the model needs."""
    candles = "\n".join(
        f"  {i+1}: O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}"
        for i, (o, h, l, c) in enumerate(snap.recent_candles)
    ) or "  (no candles)"
    ltps = ", ".join(f"{x:.2f}" for x in snap.recent_ltps) or "(empty)"
    vix_line = (
        f"India VIX: {snap.india_vix:.2f}"
        if snap.india_vix is not None
        else "India VIX: not available"
    )
    feedback_block = _render_feedback(snap.feedback)
    return f"""Index: {snap.index}
Spot: {snap.spot:.2f}
{vix_line}
Recent LTPs: [{ltps}]
Recent 1m candles (oldest first):
{candles}
Total CE OI: {snap.total_call_oi}   (change: {snap.total_call_oi_change:+d})
Total PE OI: {snap.total_put_oi}   (change: {snap.total_put_oi_change:+d})
Highest CE-OI strike: {snap.highest_call_oi_strike}
Highest PE-OI strike: {snap.highest_put_oi_strike}
{feedback_block}
Classify the regime. Respond with JSON only."""


def _render_feedback(fb: SessionFeedback | None) -> str:
    if fb is None or fb.trades_today == 0:
        return "\nRecent session feedback: (no trades yet today on this index)\n"
    hr_pct = f"{fb.hit_rate * 100:.0f}%" if fb.hit_rate is not None else "n/a"
    lines = [
        "",
        "Recent session feedback:",
        f"  trades_today = {fb.trades_today}",
        f"  hit_rate = {hr_pct}",
        f"  dominant_exit_reason = {fb.dominant_exit_reason or 'n/a'}",
    ]
    if fb.last_3:
        lines.append("  last 3 trades:")
        for s in fb.last_3:
            lines.append(f"    - {s}")
    lines.append("")
    return "\n".join(lines)
