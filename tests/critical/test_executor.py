"""Executor adapter smoke-tests — `_instrument_symbol` + entry/exit glue."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from trading.critical import executor as exec_mod
from trading.critical.entry import EntryCandidate
from trading.critical.executor import (
    LOT_SIZES, ScalpExecutor, _instrument_symbol,
)
from trading.critical.triggers import Signal


@pytest.fixture(autouse=True)
def isolate_trades_log(tmp_path, monkeypatch):
    """Every test in this file exercises ScalpExecutor, which appends
    to `_TRADES_JSONL`. Without this fixture the tests would write to
    the REAL `logs/critical/trades.jsonl` on every pytest run — that's
    how we produced the 2026-04-21 'zombie clone' phantom entries
    (same test data, written to production audit log on each run).
    """
    monkeypatch.setattr(
        exec_mod, "_TRADES_JSONL", tmp_path / "trades.jsonl",
    )


def test_instrument_symbol_nse_monthly():
    sym = _instrument_symbol("NIFTY50", date(2026, 4, 28), 25_000, "CE")
    assert sym == "NSE:NIFTY26APR25000CE"

def test_instrument_symbol_bse_monthly():
    sym = _instrument_symbol("SENSEX", date(2026, 4, 28), 82_000, "PE")
    assert sym == "BSE:SENSEX26APR82000PE"


def test_enter_propagates_through_paper_executor():
    fake_fill = MagicMock(fill_price=120.5, ts_ms=1_234_567, fees=1.2)
    paper = MagicMock()
    paper.execute.return_value = MagicMock(ok=True, fill=fake_fill, reason="")
    bus = MagicMock()
    se = ScalpExecutor(paper=paper, bus=bus)

    cand = EntryCandidate(
        strike=25_000, side="CE",
        ltp=120.0, delta=0.60,
        spread_pct=1.0, reason="delta=0.600",
    )
    sig = Signal(side="CE", confidence=0.7,
                 reasons=("micro_bid_imbalance=0.50",))
    out = se.enter(
        index="NIFTY50", expiry_d=date(2026, 4, 28),
        cand=cand, signal=sig,
        lots=2, target_ltp=135.0, stop_ltp=110.0,
        time_stop_ms=99_999_999, signal_ts_ms=1_234_500,
    )

    assert out.ok is True
    assert out.position is not None
    assert out.position.lots == 2
    assert out.position.lot_size == LOT_SIZES["NIFTY50"]
    assert out.position.entry_ltp == 120.5

    order = paper.execute.call_args.args[0]
    assert order.action == "BUY"
    assert order.qty == 2 * LOT_SIZES["NIFTY50"]
    assert order.instrument == "NSE:NIFTY26APR25000CE"
    # Observability event published
    assert bus.publish.called


def test_trades_log_writes_go_to_isolated_path(tmp_path, monkeypatch):
    """Regression guard: prove the isolation fixture above actually
    redirects the JSONL write. If someone ever reverts this, they'll
    see this test fail rather than silently polluting production data."""
    import json
    fake_fill = MagicMock(fill_price=50.0, ts_ms=1_234_567, fees=0.0)
    paper = MagicMock()
    paper.execute.return_value = MagicMock(ok=True, fill=fake_fill, reason="")
    se = ScalpExecutor(paper=paper, bus=MagicMock())
    cand = EntryCandidate(
        strike=24_500, side="CE", ltp=50.0, delta=0.60,
        spread_pct=1.0, reason="delta=0.600",
    )
    sig = Signal(side="CE", confidence=0.6,
                  reasons=("test_reason",))
    se.enter(
        index="NIFTY50", expiry_d=date(2026, 4, 28), cand=cand,
        signal=sig, lots=1, target_ltp=60.0, stop_ltp=45.0,
        time_stop_ms=99_999_999, signal_ts_ms=1_234_500,
    )
    trades_path = tmp_path / "trades.jsonl"
    assert trades_path.is_file()
    events = [json.loads(l) for l in trades_path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["kind"] == "entry"
    assert events[0]["strike"] == 24_500   # the TEST's strike, not a zombie
