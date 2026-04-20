"""Exit logic — consulted on every tick of a held position.

Five exit paths, checked in this order (first match wins):

  1. Hard stop  (ltp ≤ stop_ltp)                         — reason "stop"
  2. Time stop  (now > time_stop_ms)                     — reason "time"
  3. Profit    (ltp ≥ target_ltp)                        — reason "target"
  4. Regime flip (new regime bias opposes the side)      — reason "regime_flip"
  5. Wall break against us (primary S/R broken adverse)  — reason "wall_break"

Pure function — takes numbers, returns a decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trading.critical.state import Position

ExitReason = Literal["stop", "time", "target", "regime_flip", "wall_break"]


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    detail: str = ""


def evaluate(
    pos: Position,
    *,
    ltp: float,
    now_ms: int,
    regime_bias: Literal["long", "short", "neutral"],
    spot: float | None = None,
    primary_resistance: int | None = None,
    primary_support: int | None = None,
) -> ExitDecision:
    # 1. hard stop
    if ltp <= pos.stop_ltp:
        return ExitDecision(
            True, "stop",
            f"ltp={ltp:.2f}<=stop={pos.stop_ltp:.2f}",
        )
    # 2. time stop
    if now_ms >= pos.time_stop_ms:
        return ExitDecision(
            True, "time",
            f"now={now_ms}>=time_stop={pos.time_stop_ms}",
        )
    # 3. profit target
    if ltp >= pos.target_ltp:
        return ExitDecision(
            True, "target",
            f"ltp={ltp:.2f}>=target={pos.target_ltp:.2f}",
        )
    # 4. regime flip — only triggers when regime bias is now opposite
    if pos.option_type == "CE" and regime_bias == "short":
        return ExitDecision(True, "regime_flip", "regime=short, held CE")
    if pos.option_type == "PE" and regime_bias == "long":
        return ExitDecision(True, "regime_flip", "regime=long, held PE")
    # 5. wall break against position
    if (spot is not None and pos.option_type == "CE" and primary_support
            and spot < primary_support):
        return ExitDecision(
            True, "wall_break", f"spot={spot}<primary_support={primary_support}",
        )
    if (spot is not None and pos.option_type == "PE" and primary_resistance
            and spot > primary_resistance):
        return ExitDecision(
            True, "wall_break",
            f"spot={spot}>primary_resistance={primary_resistance}",
        )
    return ExitDecision(False)


def build_exit_levels(
    *,
    entry_ltp: float,
    spread: float | None,
    max_loss_rupees: float,
    lots: int, lot_size: int,
    time_stop_s: int,
    now_ms: int,
    target_multiple: float = 1.5,    # 1.5× of the greater of (spread, 5)
    min_stop_points: float = 5.0,
) -> tuple[float, float, int]:
    """Compute (target, stop, time_stop_ms) given entry context.

    Stop is the TIGHTER of:
      - Max-loss-in-rupees cap ÷ (lots × lot_size), AND
      - A floor of `min_stop_points` from entry so we don't get wicked
        out on micro-moves.

    Target is `target_multiple` × the price buffer we just defined as
    stop — a basic 1:1.5 R:R to start.
    """
    qty = max(lots * lot_size, 1)
    rupee_cap_buffer = max_loss_rupees / qty
    stop_buffer = max(min_stop_points, rupee_cap_buffer)
    stop_ltp = max(0.0, entry_ltp - stop_buffer)

    base = max((spread or 0.0), stop_buffer)
    target_ltp = entry_ltp + target_multiple * base

    time_stop_ms = now_ms + time_stop_s * 1000
    return target_ltp, stop_ltp, time_stop_ms
