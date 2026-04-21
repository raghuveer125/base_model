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

- Volatility inputs are index-ISOLATED. You will see one or both of:
    * "India VIX: X.XX" — present ONLY for NIFTY50 calls (NSE derives
      it from NIFTY ATM options; it does not describe BANKNIFTY or
      SENSEX and will be absent from those prompts by design).
    * "ATM IV (this index): CE=X.XX  PE=X.XX  (skew PE-CE: +/-Y.YY)" —
      present for all indices when greeks are available. This is the
      INDEX'S OWN implied volatility. Prefer this over any cross-index
      reasoning.
  Use them for conviction sizing:
    * Low vol (NIFTY VIX < 12, or per-index ATM IV < 10) — option
      premia decay fast; the time-stop becomes a trap for
      non-directional entries. Set bias="neutral" with confidence < 50
      UNLESS the candles show a clean directional run.
    * Normal vol (NIFTY VIX 12-18, or ATM IV 10-20) — judge on price
      action alone.
    * Elevated vol (NIFTY VIX 18-22, or ATM IV 20-30) — widen your
      tolerance for colour flips before declaring "volatile"; whippy
      price is expected in this band.
    * High vol (NIFTY VIX > 22, or ATM IV > 30) — classify "volatile"
      and set bias="neutral" regardless of candle pattern.
    * IV skew (PE - CE): materially positive (+3 or more) suggests
      bearish pressure; materially negative (-3 or less) suggests
      bullish pressure. Use as a tiebreaker, not a primary driver.
  If "Volatility gauges: not available" appears, ignore these rules
  and classify on the other inputs alone.

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
    """Compact snapshot — everything the model needs, nothing it doesn't.

    Data isolation rule: every field in this dataclass for a given
    `index` must be derived from THAT index's own data source. We do
    not cross-map NIFTY's IV onto BANKNIFTY, etc. Where a gauge is only
    meaningful for NIFTY50 (India VIX), it is passed only for NIFTY50
    and left None for other indices.
    """
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
    # India VIX — authoritative ONLY for NIFTY50 (NSE derives it from
    # NIFTY ATM options). Intentionally None for BANKNIFTY / SENSEX so we
    # don't cross-pollute their prompt with NIFTY-specific volatility.
    india_vix: float | None = None
    # Per-index ATM option IV — each index's OWN volatility gauge. CE and
    # PE are kept separate because IV skew (PE_iv - CE_iv) is meaningful.
    atm_iv_ce: float | None = None
    atm_iv_pe: float | None = None
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
    vol_block = _render_volatility(snap)
    feedback_block = _render_feedback(snap.feedback)
    return f"""Index: {snap.index}
Spot: {snap.spot:.2f}
{vol_block}
Recent LTPs: [{ltps}]
Recent 1m candles (oldest first):
{candles}
Total CE OI: {snap.total_call_oi}   (change: {snap.total_call_oi_change:+d})
Total PE OI: {snap.total_put_oi}   (change: {snap.total_put_oi_change:+d})
Highest CE-OI strike: {snap.highest_call_oi_strike}
Highest PE-OI strike: {snap.highest_put_oi_strike}
{feedback_block}
Classify the regime. Respond with JSON only."""


def _render_volatility(snap: RegimeInput) -> str:
    """Build the volatility context block for this index.

    India VIX only appears for NIFTY50 (where it is the native measure).
    ATM IV is per-index — always from this index's own option chain.

    IV unit normalisation: the greeks module stores IV as an annualised
    decimal (e.g. 0.23 = 23%). VIX is quoted as a percentage. To keep
    the SYSTEM_PROMPT's band thresholds consistent ("VIX > 22", "ATM
    IV > 30"), we render IV in percentage form here. If an IV value is
    already >= 1.0 (someone passed a percentage by mistake) we render it
    as-is to stay robust.
    """
    lines: list[str] = []
    if snap.india_vix is not None:
        lines.append(f"India VIX: {snap.india_vix:.2f}")
    if snap.atm_iv_ce is not None or snap.atm_iv_pe is not None:
        ce_pct = _iv_to_pct(snap.atm_iv_ce)
        pe_pct = _iv_to_pct(snap.atm_iv_pe)
        ce = f"{ce_pct:.2f}" if ce_pct is not None else "n/a"
        pe = f"{pe_pct:.2f}" if pe_pct is not None else "n/a"
        skew = (
            f"  (skew PE-CE: {pe_pct - ce_pct:+.2f})"
            if ce_pct is not None and pe_pct is not None
            else ""
        )
        lines.append(f"ATM IV (this index): CE={ce}  PE={pe}{skew}")
    if not lines:
        return "Volatility gauges: not available"
    return "\n".join(lines)


def _iv_to_pct(iv: float | None) -> float | None:
    """Convert a greeks-module IV (decimal) to percentage. Values already
    >= 1.0 are assumed to be percentages and passed through unchanged."""
    if iv is None:
        return None
    return iv * 100 if iv < 1.0 else iv


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
