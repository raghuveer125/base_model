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

import math
from dataclasses import dataclass
from typing import Literal

from trading.critical.state import Position
from trading.schemas import OPTION_TICK_SIZE

ExitReason = Literal[
    "stop", "dollar_stop", "time", "target", "regime_flip", "wall_break",
]


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    detail: str = ""


# Minimum position age (ms) before "soft" exits may fire. The hard stop
# and time stop are ALWAYS evaluated — they're the safety net. Regime flip
# and wall break are noisy near entry time (a wall already broken when we
# entered shouldn't insta-close us; a regime flicker shouldn't either), so
# they're gated on both age and adverse P&L.
_MIN_SOFT_EXIT_AGE_MS = 5_000


def evaluate(
    pos: Position,
    *,
    ltp: float,
    now_ms: int,
    regime_bias: Literal["long", "short", "neutral"],
    spot: float | None = None,
    primary_resistance: int | None = None,
    primary_support: int | None = None,
    max_loss_rupees: float | None = None,
    wall_break_hysteresis_pts: float = 0.0,
) -> ExitDecision:
    age_ms = now_ms - pos.entry_ts_ms
    adverse = ltp < pos.entry_ltp

    # 0. DOLLAR-CAP STOP — highest priority. Fires the instant realized
    # loss in rupees meets or exceeds `max_loss_rupees`, even if the tick
    # gapped through `stop_ltp` (which is only a *price-level* trigger).
    # Without this, a fast adverse move between ticks can realize a 4-5×
    # loss vs the intended cap (observed live 2026-04-20: SENSEX -₹6,408
    # and NIFTY50 -₹7,134 on a ₹1,500 cap).
    if max_loss_rupees is not None:
        qty = max(pos.lots * pos.lot_size, 1)
        rupee_loss = (pos.entry_ltp - ltp) * qty
        if rupee_loss >= max_loss_rupees:
            return ExitDecision(
                True, "dollar_stop",
                f"loss={rupee_loss:.0f}>=cap={max_loss_rupees:.0f} "
                f"(entry={pos.entry_ltp:.2f} ltp={ltp:.2f} qty={qty})",
            )

    # 1. hard stop — always on
    if ltp <= pos.stop_ltp:
        return ExitDecision(
            True, "stop",
            f"ltp={ltp:.2f}<=stop={pos.stop_ltp:.2f}",
        )
    # 2. time stop — always on
    if now_ms >= pos.time_stop_ms:
        return ExitDecision(
            True, "time",
            f"now={now_ms}>=time_stop={pos.time_stop_ms}",
        )
    # 3. profit target — always on
    if ltp >= pos.target_ltp:
        return ExitDecision(
            True, "target",
            f"ltp={ltp:.2f}>=target={pos.target_ltp:.2f}",
        )

    # Gate the "soft" exits until the position has aged AND is actually
    # losing money — avoids the insta-close when a condition was already
    # true at entry time (stale OI wall, flickering regime).
    if age_ms < _MIN_SOFT_EXIT_AGE_MS or not adverse:
        return ExitDecision(False)

    # 4. regime flip
    if pos.option_type == "CE" and regime_bias == "short":
        return ExitDecision(True, "regime_flip", "regime=short, held CE")
    if pos.option_type == "PE" and regime_bias == "long":
        return ExitDecision(True, "regime_flip", "regime=long, held PE")
    # 5. wall break against position
    #
    # Two guards added vs v1:
    #
    #   a) same-wall skip: if the breaking wall is the SAME level that was
    #      already identified at entry time, that break was already priced
    #      in — exiting on it would be a tautological panic. Only exit when
    #      a DIFFERENT (newly migrated) wall is broken.
    #
    #   b) hysteresis buffer: require the spot to be clear of the wall by
    #      `wall_break_hysteresis_pts` points, so a 1-tick flicker past a
    #      round-number level doesn't kill the position.
    if (spot is not None and pos.option_type == "CE" and primary_support
            and spot < primary_support - wall_break_hysteresis_pts
            and primary_support != pos.entry_primary_support):
        return ExitDecision(
            True, "wall_break",
            f"spot={spot}<primary_support={primary_support}"
            f" (hysteresis={wall_break_hysteresis_pts})",
        )
    if (spot is not None and pos.option_type == "PE" and primary_resistance
            and spot > primary_resistance + wall_break_hysteresis_pts
            and primary_resistance != pos.entry_primary_resistance):
        return ExitDecision(
            True, "wall_break",
            f"spot={spot}>primary_resistance={primary_resistance}"
            f" (hysteresis={wall_break_hysteresis_pts})",
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

    # Snap onto the ₹0.05 option tick grid so stops/targets match what a
    # real order book would trigger on. Direction is chosen conservatively:
    # floor the stop (wider safety buffer, fires later) and ceil the target
    # (harder to hit, slightly lower realized profit). This preserves the
    # `stop_buffer >= min_stop_points` invariant — flooring only widens.
    stop_ltp = math.floor(stop_ltp / OPTION_TICK_SIZE) * OPTION_TICK_SIZE
    target_ltp = math.ceil(target_ltp / OPTION_TICK_SIZE) * OPTION_TICK_SIZE

    time_stop_ms = now_ms + time_stop_s * 1000
    return round(target_ltp, 2), round(stop_ltp, 2), time_stop_ms
