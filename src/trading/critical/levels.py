"""Dynamic support/resistance + OI-wall migration.

The "levels" of the day aren't hardcoded — they're computed live from
the top OI walls. A wall flips meaning when price crosses it, so we
also track *migration* between observations: which walls are forming
(OI growing fast) vs breaking (OI unwinding fast).
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.critical.market_view import Wall

# A strike's OI changing by this ratio between observations is considered
# "significant" — smaller moves are just noise on a big OI base.
DEFAULT_FORMING_THRESHOLD  = 0.20   # +20% OI growth
DEFAULT_BREAKING_THRESHOLD = 0.20   # -20% OI unwind


@dataclass(frozen=True)
class Levels:
    """Dynamic levels derived from OI walls at a moment in time."""
    resistance_strikes: tuple[int, ...]    # CE walls, desc by OI
    support_strikes:    tuple[int, ...]    # PE walls, desc by OI
    primary_resistance: int | None
    primary_support:    int | None

    @property
    def has_levels(self) -> bool:
        return self.primary_resistance is not None and self.primary_support is not None


def compute_levels(walls: dict[str, list[Wall]]) -> Levels:
    ce = walls.get("CE", [])
    pe = walls.get("PE", [])
    return Levels(
        resistance_strikes=tuple(w.strike for w in ce),
        support_strikes=tuple(w.strike for w in pe),
        primary_resistance=ce[0].strike if ce else None,
        primary_support=pe[0].strike if pe else None,
    )


@dataclass(frozen=True)
class WallMigration:
    forming: tuple[int, ...]    # strikes whose OI is growing fast (walls forming)
    breaking: tuple[int, ...]   # strikes whose OI is unwinding fast (walls breaking)


def detect_migration(
    prev: dict[int, int],
    curr: dict[int, int],
    *,
    forming_threshold: float = DEFAULT_FORMING_THRESHOLD,
    breaking_threshold: float = DEFAULT_BREAKING_THRESHOLD,
) -> WallMigration:
    """Compare two OI-by-strike dicts and tag each significant move.

    `prev` / `curr` are the raw strike -> OI maps for one side (CE or PE).
    Returns `WallMigration(forming=…, breaking=…)`.
    """
    forming: list[int] = []
    breaking: list[int] = []
    for strike, new_oi in curr.items():
        old = prev.get(strike)
        if old is None or old <= 0:
            continue
        ratio = (new_oi - old) / old
        if ratio >= forming_threshold:
            forming.append(strike)
        elif ratio <= -breaking_threshold:
            breaking.append(strike)
    forming.sort()
    breaking.sort()
    return WallMigration(forming=tuple(forming), breaking=tuple(breaking))
