"""Entry-gate order flip — regime fetched only when signals agree.

Regression guard for the 2026-04-21 finding: the engine was calling
Claude (_regime_for) on every _try_entry even when no signals agreed,
burning tokens for no gain. The flip moves the regime fetch AFTER an
initial combine_signals pre-check with regime_bias="neutral".
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from trading.critical.triggers import Signal, combine_signals


def test_neutral_bias_does_not_veto_either_side():
    """`combine_signals(..., regime_bias="neutral")` must let both CE
    and PE candidates through — that's the property the pre-check
    relies on."""
    ce_sigs = [
        Signal(side="CE", confidence=0.7, reasons=("a",)),
        Signal(side="CE", confidence=0.6, reasons=("b",)),
    ]
    out_ce = combine_signals(ce_sigs, regime_bias="neutral", min_agreement=2)
    assert out_ce is not None and out_ce.side == "CE"

    pe_sigs = [
        Signal(side="PE", confidence=0.7, reasons=("x",)),
        Signal(side="PE", confidence=0.6, reasons=("y",)),
    ]
    out_pe = combine_signals(pe_sigs, regime_bias="neutral", min_agreement=2)
    assert out_pe is not None and out_pe.side == "PE"


def test_neutral_pre_check_none_when_no_agreement():
    """Pre-check must also return None when signals don't align — the
    engine uses that to cheap-exit before consulting Claude."""
    sigs = [
        Signal(side="CE", confidence=0.6, reasons=("a",)),
        Signal(side="PE", confidence=0.6, reasons=("b",)),
    ]
    out = combine_signals(sigs, regime_bias="neutral", min_agreement=2)
    assert out is None


def test_engine_gate_counts_batch_and_flush(monkeypatch):
    """Gate-stage counts must batch in-memory and flush via pipeline
    every ~1s to keep Redis writes under 10-20/s even when bailouts
    happen 600+ times/s."""
    from trading.critical.engine import CriticalEngine

    eng = CriticalEngine.__new__(CriticalEngine)  # skip real __init__
    eng._gate_counts = {}
    eng._gate_last_flush_ms = 0
    eng._GATE_FLUSH_EVERY_MS = 1000

    pipe_calls: list[tuple] = []
    class FakePipeline:
        def hincrby(self, key, field, n): pipe_calls.append(("hincrby", key, field, n))
        def expire(self, key, ttl): pipe_calls.append(("expire", key, ttl))
        def execute(self): pipe_calls.append(("execute",))
    class FakeR:
        def pipeline(self, transaction): return FakePipeline()
    class FakeStore:
        r = FakeR()
    eng.market = MagicMock(_store=FakeStore())

    # Rapidly increment 100 times — should NOT flush (within 1s window).
    import trading.critical.engine as eng_mod
    base_now = 10_000_000
    monkeypatch.setattr(eng_mod, "now_ms", lambda: base_now)
    for _ in range(100):
        eng._gate_tick("NIFTY50", "combine_none")
    # We only call once to break the first-call short-circuit
    # (after this, _gate_last_flush_ms is base_now), so no flush yet.
    assert len(pipe_calls) == 0
    assert eng._gate_counts[("NIFTY50", "combine_none")] == 100

    # Jump 1.5s forward — next tick should flush.
    monkeypatch.setattr(eng_mod, "now_ms", lambda: base_now + 1500)
    eng._gate_tick("NIFTY50", "combine_none")
    assert any(c[0] == "execute" for c in pipe_calls)
    # After flush, the buffer is empty.
    assert eng._gate_counts == {}
    # The execute call wrote one aggregated increment, not 101 individual ones.
    incrs = [c for c in pipe_calls if c[0] == "hincrby"]
    assert len(incrs) == 1
    assert incrs[0][3] == 101   # 100 prior + 1 that triggered flush


def test_engine_gate_count_survives_flush_failure(monkeypatch):
    """If Redis is down at flush time, the counter should silently
    fail and not break the entry pipeline."""
    from trading.critical.engine import CriticalEngine
    import trading.critical.engine as eng_mod

    eng = CriticalEngine.__new__(CriticalEngine)
    eng._gate_counts = {("NIFTY50", "foo"): 3}
    eng._gate_last_flush_ms = 0
    eng._GATE_FLUSH_EVERY_MS = 1000

    class BrokenR:
        def pipeline(self, transaction): raise RuntimeError("redis dead")
    eng.market = MagicMock(_store=MagicMock(r=BrokenR()))
    monkeypatch.setattr(eng_mod, "now_ms", lambda: 2_000)
    # Should not raise.
    eng._flush_gate_counts()
    # Buffer is cleared regardless (we don't want to keep retrying
    # stale counts indefinitely).
    assert eng._gate_counts == {}
