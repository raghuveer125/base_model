"""Pure-function tests for the scalp triggers — the heart of the layer."""

from __future__ import annotations

import pytest

from trading.critical.triggers import (
    buildup_signal, candle_run, classify_buildup, combine_signals,
    level_break_signal, microstructure_signal, momentum_signal,
)


# ----- buildup classifier -----

def test_classify_buildup_four_quadrants():
    assert classify_buildup(1.0,  1000)  == "long_buildup"
    assert classify_buildup(-1.0, 1000)  == "short_buildup"
    assert classify_buildup(1.0,  -1000) == "short_covering"
    assert classify_buildup(-1.0, -1000) == "long_unwinding"

def test_classify_buildup_none_when_missing():
    assert classify_buildup(None, 1000) == "flat"
    assert classify_buildup(1.0, None)  == "flat"
    assert classify_buildup(0.0, 0)     == "flat"


# ----- candle run -----

def test_candle_run_green_streak():
    opens  = [100, 101, 102, 103]
    closes = [101, 102, 103, 104]
    assert candle_run(closes, opens) == 4

def test_candle_run_red_streak():
    opens  = [104, 103, 102, 101]
    closes = [103, 102, 101, 100]
    assert candle_run(closes, opens) == -4

def test_candle_run_stops_at_reversal():
    opens  = [100, 101, 103, 104]
    closes = [101, 102, 102, 105]  # 3rd is a red
    assert candle_run(closes, opens) == 1      # only the last candle counts

def test_candle_run_doji_returns_zero():
    opens  = [100]
    closes = [100]
    assert candle_run(closes, opens) == 0

def test_candle_run_empty():
    assert candle_run([], []) == 0


# ----- microstructure -----

def test_microstructure_buy_when_bid_imbalance_and_momentum_up():
    s = microstructure_signal(
        spread_pct=1.0, imbalance=0.5, ltp_momentum="up",
        max_spread_pct=4.0,
    )
    assert s is not None and s.side == "CE"

def test_microstructure_sell_when_ask_imbalance_and_momentum_down():
    s = microstructure_signal(
        spread_pct=1.0, imbalance=-0.5, ltp_momentum="down",
        max_spread_pct=4.0,
    )
    assert s is not None and s.side == "PE"

def test_microstructure_blocks_wide_spread():
    s = microstructure_signal(
        spread_pct=6.0, imbalance=0.8, ltp_momentum="up",
        max_spread_pct=4.0,
    )
    assert s is None

def test_microstructure_requires_imbalance_and_momentum_agreement():
    s = microstructure_signal(
        spread_pct=1.0, imbalance=0.5, ltp_momentum="down",
        max_spread_pct=4.0,
    )
    assert s is None

def test_microstructure_missing_inputs_none():
    assert microstructure_signal(spread_pct=None, imbalance=0.5,
                                  ltp_momentum="up", max_spread_pct=4.0) is None
    assert microstructure_signal(spread_pct=1.0, imbalance=None,
                                  ltp_momentum="up", max_spread_pct=4.0) is None


# ----- buildup signal -----

@pytest.mark.parametrize("buildup,expected_side", [
    ("long_buildup",   "CE"),
    ("short_covering", "CE"),
    ("short_buildup",  "PE"),
    ("long_unwinding", "PE"),
    ("flat", None),
])
def test_buildup_signal_direction(buildup, expected_side):
    s = buildup_signal(buildup=buildup)
    if expected_side is None:
        assert s is None
    else:
        assert s is not None
        assert s.side == expected_side


# ----- momentum -----

def test_momentum_signal_three_green_fires_ce():
    opens  = [100, 101, 102]
    closes = [101, 102, 103]
    s = momentum_signal(opens, closes, run_threshold=3)
    assert s is not None and s.side == "CE"

def test_momentum_signal_two_green_does_not_fire():
    opens  = [100, 101]
    closes = [101, 102]
    s = momentum_signal(opens, closes, run_threshold=3)
    assert s is None


# ----- level break -----

def test_level_break_fires_when_resistance_breaks_and_wall_unwinds():
    s = level_break_signal(
        spot=25_050, primary_resistance=25_000, primary_support=24_800,
        breaking_ce_walls=(25_000,), breaking_pe_walls=(),
    )
    assert s is not None and s.side == "CE"

def test_level_break_does_not_fire_if_wall_not_unwinding():
    s = level_break_signal(
        spot=25_050, primary_resistance=25_000, primary_support=24_800,
        breaking_ce_walls=(), breaking_pe_walls=(),
    )
    assert s is None

def test_level_break_fires_put_on_support_break():
    s = level_break_signal(
        spot=24_750, primary_resistance=25_000, primary_support=24_800,
        breaking_ce_walls=(), breaking_pe_walls=(24_800,),
    )
    assert s is not None and s.side == "PE"


# ----- combine_signals -----

def test_combine_requires_agreement():
    from trading.critical.triggers import Signal
    ce_strong = Signal(side="CE", confidence=0.9, reasons=("a",))
    pe_weak   = Signal(side="PE", confidence=0.3, reasons=("b",))
    # only one signal → no agreement, no trade
    assert combine_signals([ce_strong, None, None], min_agreement=2) is None
    # one CE + one PE: still no agreement (no side has 2 votes)
    assert combine_signals([ce_strong, pe_weak], min_agreement=2) is None

def test_combine_yields_agreed_side():
    from trading.critical.triggers import Signal
    a = Signal(side="CE", confidence=0.7, reasons=("a",))
    b = Signal(side="CE", confidence=0.6, reasons=("b",))
    final = combine_signals([a, b, None], min_agreement=2)
    assert final is not None and final.side == "CE"
    assert "a" in final.reasons and "b" in final.reasons

def test_combine_vetoed_by_opposite_regime_bias():
    from trading.critical.triggers import Signal
    a = Signal(side="CE", confidence=0.7, reasons=("a",))
    b = Signal(side="CE", confidence=0.6, reasons=("b",))
    assert combine_signals([a, b], regime_bias="short", min_agreement=2) is None
