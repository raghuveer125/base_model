"""Expiry-mode auto-detection + override application + defensive gates.

Tightened defaults come from a 60-yr veteran-scalper review on
2026-04-21. Values land in `config.py`; behaviour lands in
`engine.py::_try_entry` via `_effective_params` / `_is_expiry_day` /
`_near_wall` / straddle veto.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from trading.critical.config import load_config
from trading.critical.engine import CriticalEngine, EffectiveParams

_IST = ZoneInfo("Asia/Kolkata")


# ────────────────────────────────────────────────────────────────────────
# _is_expiry_day
# ────────────────────────────────────────────────────────────────────────


def _eng_with_expiries(expiries: dict[str, date]) -> CriticalEngine:
    """Build an engine without running its heavy __init__. We only need
    `_expiries` populated for these unit tests."""
    eng = CriticalEngine.__new__(CriticalEngine)
    eng._expiries = expiries
    eng.cfg = load_config()
    return eng


def test_is_expiry_day_true_when_today_matches():
    today_ist = datetime.now(_IST).date()
    eng = _eng_with_expiries({"NIFTY50": today_ist})
    assert eng._is_expiry_day("NIFTY50") is True

def test_is_expiry_day_false_when_today_differs():
    tomorrow = datetime.now(_IST).date() + timedelta(days=1)
    eng = _eng_with_expiries({"NIFTY50": tomorrow})
    assert eng._is_expiry_day("NIFTY50") is False

def test_is_expiry_day_false_when_no_expiry_configured():
    eng = _eng_with_expiries({})
    assert eng._is_expiry_day("NIFTY50") is False

def test_is_expiry_day_independent_per_index():
    """Common real-world case: NIFTY weekly expires today but
    BANKNIFTY monthly expires later."""
    today_ist = datetime.now(_IST).date()
    later = today_ist + timedelta(days=7)
    eng = _eng_with_expiries({
        "NIFTY50": today_ist,
        "BANKNIFTY": later,
    })
    assert eng._is_expiry_day("NIFTY50") is True
    assert eng._is_expiry_day("BANKNIFTY") is False


# ────────────────────────────────────────────────────────────────────────
# _effective_params
# ────────────────────────────────────────────────────────────────────────


def test_effective_params_normal_day_uses_cfg_defaults():
    tomorrow = datetime.now(_IST).date() + timedelta(days=1)
    eng = _eng_with_expiries({"NIFTY50": tomorrow})
    p = eng._effective_params("NIFTY50")
    assert p.is_expiry is False
    assert p.time_stop_s == eng.cfg.time_stop_s
    assert p.delta_min == eng.cfg.delta_min
    assert p.delta_max == eng.cfg.delta_max
    assert p.min_regime_conf == eng.cfg.regime_min_confidence

def test_effective_params_expiry_day_swaps_in_overrides():
    today_ist = datetime.now(_IST).date()
    eng = _eng_with_expiries({"NIFTY50": today_ist})
    p = eng._effective_params("NIFTY50")
    assert p.is_expiry is True
    assert p.time_stop_s == eng.cfg.expiry_time_stop_s
    assert p.time_stop_s == 120   # expert-approved default
    assert p.target_multiple == eng.cfg.expiry_target_multiple
    assert p.target_multiple == 1.2
    assert p.min_regime_conf == eng.cfg.expiry_min_regime_conf
    assert p.min_regime_conf == 65
    assert p.delta_min == 0.40
    assert p.delta_max == 0.55
    assert p.wall_proximity_veto_pct == 0.10

def test_effective_params_type_is_frozen_dataclass():
    # Regression: EffectiveParams must be immutable so it can't be
    # mutated mid-tick.
    today_ist = datetime.now(_IST).date()
    eng = _eng_with_expiries({"NIFTY50": today_ist})
    p = eng._effective_params("NIFTY50")
    with pytest.raises(Exception):
        p.time_stop_s = 999  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# _near_wall
# ────────────────────────────────────────────────────────────────────────


def test_near_wall_flags_spot_within_band_of_resistance():
    # NIFTY 24525 vs resistance 24500, band = 0.10% of spot = ~24.5 pts.
    # Distance = 25 → outside 24.5-pt band, NOT near.
    assert CriticalEngine._near_wall(
        spot=24_525, primary_resistance=24_500,
        primary_support=None, tolerance_pct=0.10,
    ) is False
    # NIFTY 24510 vs resistance 24500, distance 10 pts → within 24.5 pts.
    assert CriticalEngine._near_wall(
        spot=24_510, primary_resistance=24_500,
        primary_support=None, tolerance_pct=0.10,
    ) is True

def test_near_wall_flags_spot_within_band_of_support():
    # NIFTY 24505 vs support 24500 → within 0.10% band.
    assert CriticalEngine._near_wall(
        spot=24_505, primary_resistance=None,
        primary_support=24_500, tolerance_pct=0.10,
    ) is True

def test_near_wall_zero_tolerance_always_false():
    # Tolerance 0 disables the gate (normal-day default).
    assert CriticalEngine._near_wall(
        spot=24_500, primary_resistance=24_500,
        primary_support=24_500, tolerance_pct=0.0,
    ) is False

def test_near_wall_handles_missing_walls():
    # Either wall can be None — shouldn't crash, shouldn't flag.
    assert CriticalEngine._near_wall(
        spot=24_525, primary_resistance=None,
        primary_support=None, tolerance_pct=0.10,
    ) is False

def test_near_wall_uses_percent_of_spot_not_absolute_points():
    # 0.15% of 57000 (BANKNIFTY) = ~85 pts. Support 57000, spot 56950 →
    # 50 pts < 85 pts → near.
    assert CriticalEngine._near_wall(
        spot=56_950, primary_resistance=None,
        primary_support=57_000, tolerance_pct=0.15,
    ) is True
    # 0.15% of 57000 = 85 pts. Spot 56850 → 150 pts > 85 → NOT near.
    assert CriticalEngine._near_wall(
        spot=56_850, primary_resistance=None,
        primary_support=57_000, tolerance_pct=0.15,
    ) is False


# ────────────────────────────────────────────────────────────────────────
# Config env-override surface
# ────────────────────────────────────────────────────────────────────────


def test_expiry_overrides_can_be_tuned_via_env(monkeypatch):
    monkeypatch.setenv("CRITICAL_EXPIRY_TIME_STOP_S", "90")
    monkeypatch.setenv("CRITICAL_EXPIRY_TARGET_MULT", "1.1")
    monkeypatch.setenv("CRITICAL_EXPIRY_MIN_REGIME_CONF", "70")
    monkeypatch.setenv("CRITICAL_EXPIRY_DELTA_MIN", "0.45")
    monkeypatch.setenv("CRITICAL_EXPIRY_DELTA_MAX", "0.60")
    monkeypatch.setenv("CRITICAL_EXPIRY_WALL_PROXIMITY_VETO_PCT", "0.05")
    cfg = load_config()
    assert cfg.expiry_time_stop_s == 90
    assert cfg.expiry_target_multiple == 1.1
    assert cfg.expiry_min_regime_conf == 70
    assert cfg.expiry_delta_min == 0.45
    assert cfg.expiry_delta_max == 0.60
    assert cfg.expiry_wall_proximity_veto_pct == 0.05
