"""Backtest harness tests — WAL replay drives strategies end to end."""

from __future__ import annotations

from pathlib import Path

import orjson

from trading.backtest import BacktestRunner
from trading.strategies import register
from trading.strategies.base import Strategy, StrategyContext
from trading.wal import WALWriter


@register("bt_candle_emit")
class _CandleEmitStrategy(Strategy):
    """Test-only: emits one BUY per candle close."""

    def on_candle_close(self, ctx: StrategyContext, candle) -> None:
        ctx.emit(
            strategy=self.name,
            index=candle.index,
            action="BUY",
            instrument=candle.index,
            reason=f"{candle.timeframe} close at {candle.close}",
            confidence=0.5,
            ts=candle.close_ts,
        )


def _write_wal_ticks(tmp_path: Path, ticks: list[dict]) -> Path:
    wal_dir = tmp_path / "wal"
    w = WALWriter(wal_dir=wal_dir)
    try:
        for t in ticks:
            w.append(t, kind="raw_tick")
    finally:
        w.close()
    return wal_dir


def test_backtest_replay_index_ticks_builds_candles_and_emits_signals(tmp_path):
    base_ms = 1_729_300_000_000
    ticks: list[dict] = []
    for bucket in range(3):
        for sec in range(10):
            ticks.append({
                "symbol": "NSE:NIFTY50-INDEX",
                "ltp": 25_000 + bucket * 10 + sec * 0.1,
                "exch_feed_time": base_ms + bucket * 60_000 + sec * 1_000,
            })
    wal_dir = _write_wal_ticks(tmp_path, ticks)
    out = tmp_path / "signals.jsonl"

    runner = BacktestRunner(
        wal_dir=wal_dir,
        indices=["NIFTY50"],
        strategy_names=["bt_candle_emit"],
        output_path=out,
        timeframes=("1m",),
        apply_cooldown=False,
        apply_risk=False,
    )
    summary = runner.run()

    assert summary["records_read"] == len(ticks)
    assert summary["ticks_index"] == len(ticks)
    assert summary["ticks_option"] == 0
    # First 2 buckets close on rollover ticks; 3rd closes on the final sweep.
    assert summary["candles_closed"]["1m"] == 3
    assert summary["signals_emitted"] == 3
    assert summary["signals_by_strategy"]["bt_candle_emit"] == 3
    assert summary["signals_by_action"]["BUY"] == 3

    lines = [l for l in out.read_bytes().splitlines() if l.strip()]
    assert len(lines) == 3
    for line in lines:
        obj = orjson.loads(line)
        assert obj["strategy"] == "bt_candle_emit"
        assert obj["action"] == "BUY"
        assert obj["index"] == "NIFTY50"


def test_backtest_cooldown_suppresses_duplicates(tmp_path):
    base_ms = 1_729_300_000_000
    ticks: list[dict] = []
    for bucket in range(2):
        for sec in range(5):
            ticks.append({
                "symbol": "NSE:NIFTY50-INDEX",
                "ltp": 25_000 + bucket * 5,
                "exch_feed_time": base_ms + bucket * 60_000 + sec * 1_000,
            })
    wal_dir = _write_wal_ticks(tmp_path, ticks)
    out = tmp_path / "signals_cooldown.jsonl"

    runner = BacktestRunner(
        wal_dir=wal_dir,
        indices=["NIFTY50"],
        strategy_names=["bt_candle_emit"],
        output_path=out,
        timeframes=("1m",),
        apply_cooldown=True,  # default 300s cooldown > 60s bucket
        apply_risk=False,
    )
    summary = runner.run()

    assert summary["candles_closed"]["1m"] == 2
    assert summary["signals_emitted"] == 1
    assert summary["signals_suppressed_cooldown"] == 1
