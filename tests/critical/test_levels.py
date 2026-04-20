"""Tests for dynamic S/R + OI-wall migration."""

from __future__ import annotations

from trading.critical.levels import (
    compute_levels, detect_migration,
)
from trading.critical.market_view import Wall


def test_compute_levels_from_walls():
    walls = {
        "CE": [Wall(25_000, "CE", 10_000_000, 0),
               Wall(25_100, "CE",  5_000_000, 0)],
        "PE": [Wall(24_800, "PE", 12_000_000, 0)],
    }
    lv = compute_levels(walls)
    assert lv.primary_resistance == 25_000
    assert lv.primary_support == 24_800
    assert lv.resistance_strikes == (25_000, 25_100)
    assert lv.support_strikes == (24_800,)
    assert lv.has_levels is True


def test_compute_levels_missing_sides():
    walls = {"CE": [], "PE": []}
    lv = compute_levels(walls)
    assert lv.primary_resistance is None
    assert lv.primary_support is None
    assert lv.has_levels is False


def test_detect_migration_flags_forming_and_breaking():
    prev = {25_000: 1_000_000, 25_100: 2_000_000, 24_800: 5_000_000}
    curr = {25_000: 1_300_000,   # +30% → forming
            25_100: 1_550_000,   # -22.5% → breaking
            24_800: 5_050_000}   # +1% → noise
    m = detect_migration(prev, curr,
                          forming_threshold=0.2, breaking_threshold=0.2)
    assert 25_000 in m.forming
    assert 25_100 in m.breaking
    assert 24_800 not in m.forming
    assert 24_800 not in m.breaking


def test_detect_migration_ignores_unknown_previous():
    prev: dict[int, int] = {}
    curr = {25_000: 1_000_000}
    m = detect_migration(prev, curr)
    assert m.forming == () and m.breaking == ()
