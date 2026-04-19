"""Replay engine tests — determinism, source merge, diff utilities."""

from __future__ import annotations

from pathlib import Path

import orjson

from trading.replay.diff import compare_signal_jsonl, compare_summaries
from trading.replay.engine import ReplayEngine
from trading.replay.sources import (
    EventKind,
    MergedEventSource,
    ReplayEvent,
    WALEventSource,
)
from trading.strategies import register
from trading.strategies.base import Strategy, StrategyContext
from trading.wal import WALWriter


@register("rp_candle_emit")
class _CandleEmitStrategy(Strategy):
    def on_candle_close(self, ctx: StrategyContext, candle) -> None:
        ctx.emit(
            strategy=self.name, index=candle.index,
            action="BUY", instrument=candle.index,
            reason=f"{candle.timeframe}@{candle.close}",
            confidence=0.5, ts=candle.close_ts,
        )


def _write_ticks(tmp_path: Path, ticks: list[dict], name: str = "wal") -> Path:
    wal_dir = tmp_path / name
    w = WALWriter(wal_dir=wal_dir)
    try:
        for t in ticks:
            w.append(t, kind="raw_tick")
    finally:
        w.close()
    return wal_dir


def _synthetic_ticks(n_buckets: int = 3, per_bucket: int = 10) -> list[dict]:
    base_ms = 1_729_300_000_000
    out: list[dict] = []
    for bucket in range(n_buckets):
        for sec in range(per_bucket):
            out.append({
                "symbol": "NSE:NIFTY50-INDEX",
                "ltp": 25_000 + bucket * 10 + sec * 0.1,
                "exch_feed_time": base_ms + bucket * 60_000 + sec * 1_000,
            })
    return out


def test_two_runs_on_same_inputs_produce_same_run_id_and_signals(tmp_path):
    wal_dir = _write_ticks(tmp_path, _synthetic_ticks(), "wal_det")
    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"

    eng_a = ReplayEngine(
        source=WALEventSource(wal_dir=wal_dir),
        strategy_names=["rp_candle_emit"], indices=["NIFTY50"],
        output_dir=out_a, timeframes=("1m",),
        apply_cooldown=False, apply_risk=False,
    )
    eng_b = ReplayEngine(
        source=WALEventSource(wal_dir=wal_dir),
        strategy_names=["rp_candle_emit"], indices=["NIFTY50"],
        output_dir=out_b, timeframes=("1m",),
        apply_cooldown=False, apply_risk=False,
    )

    assert eng_a.run_id == eng_b.run_id

    sum_a = eng_a.run()
    sum_b = eng_b.run()

    sum_a.pop("wall_seconds", None)
    sum_b.pop("wall_seconds", None)
    assert sum_a == sum_b

    bytes_a = (out_a / "signals.jsonl").read_bytes()
    bytes_b = (out_b / "signals.jsonl").read_bytes()
    assert bytes_a == bytes_b


def test_different_strategy_set_produces_different_run_id(tmp_path):
    wal_dir = _write_ticks(tmp_path, _synthetic_ticks(), "wal_rid")
    eng1 = ReplayEngine(
        source=WALEventSource(wal_dir=wal_dir),
        strategy_names=["heartbeat"], indices=["NIFTY50"],
        output_dir=tmp_path / "r1", timeframes=("1m",),
    )
    eng2 = ReplayEngine(
        source=WALEventSource(wal_dir=wal_dir),
        strategy_names=["rp_candle_emit"], indices=["NIFTY50"],
        output_dir=tmp_path / "r2", timeframes=("1m",),
    )
    assert eng1.run_id != eng2.run_id


def test_wal_fingerprint_stable_across_instances(tmp_path):
    wal_dir = _write_ticks(tmp_path, _synthetic_ticks(n_buckets=1, per_bucket=3), "wal_fp")
    fp1 = WALEventSource(wal_dir=wal_dir).fingerprint()
    fp2 = WALEventSource(wal_dir=wal_dir).fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 16


def test_wal_fingerprint_changes_when_file_grows(tmp_path):
    wal_dir = _write_ticks(tmp_path, _synthetic_ticks(n_buckets=1, per_bucket=3), "wal_fp2")
    fp1 = WALEventSource(wal_dir=wal_dir).fingerprint()
    w = WALWriter(wal_dir=wal_dir)
    w.append({"symbol": "NSE:NIFTY50-INDEX", "ltp": 99_999, "exch_feed_time": 1},
             kind="raw_tick")
    w.close()
    fp2 = WALEventSource(wal_dir=wal_dir).fingerprint()
    assert fp1 != fp2


class _StubSource:
    def __init__(self, events):
        self._events = events
    def iter_events(self):
        return iter(self._events)
    def fingerprint(self):
        return "stub1234"
    def description(self):
        return "stub"


def test_merged_source_preserves_ts_order():
    evs_a = [
        ReplayEvent(EventKind.INDEX_TICK, 100, 1, {"a": 1}),
        ReplayEvent(EventKind.INDEX_TICK, 300, 2, {"a": 3}),
    ]
    evs_b = [
        ReplayEvent(EventKind.INDEX_TICK, 200, 1, {"b": 2}),
        ReplayEvent(EventKind.INDEX_TICK, 400, 2, {"b": 4}),
    ]
    merged = list(MergedEventSource([_StubSource(evs_a), _StubSource(evs_b)]).iter_events())
    tss = [e.ts for e in merged]
    assert tss == sorted(tss)
    assert len(merged) == 4


def test_merged_source_stable_tiebreak_on_same_ts():
    evs_a = [ReplayEvent(EventKind.INDEX_TICK, 100, 1, {"src": "a"})]
    evs_b = [ReplayEvent(EventKind.INDEX_TICK, 100, 2, {"src": "b"})]
    merged = list(MergedEventSource([_StubSource(evs_a), _StubSource(evs_b)]).iter_events())
    assert [e.payload["src"] for e in merged] == ["a", "b"]


def test_compare_summaries_detects_divergent_leaves():
    a = {"ticks": 10, "nested": {"x": 1, "y": 2}}
    b = {"ticks": 10, "nested": {"x": 1, "y": 99}}
    diffs = compare_summaries(a, b)
    assert len(diffs) == 1
    key, av, bv = diffs[0]
    assert "nested.y" in key
    assert (av, bv) == (2, 99)


def test_compare_summaries_returns_empty_for_identical():
    a = {"a": 1, "b": [1, 2, 3]}
    assert compare_summaries(a, dict(a)) == []


def test_compare_signal_jsonl_identical_when_same_bytes(tmp_path):
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    sig = {
        "strategy": "s", "index": "NIFTY50", "action": "BUY",
        "instrument": "NIFTY50", "reason": "", "confidence": 0.5,
        "metadata": {}, "ts": 1,
    }
    body = orjson.dumps(sig) + b"\n"
    p1.write_bytes(body)
    p2.write_bytes(body)
    diff = compare_signal_jsonl(p1, p2)
    assert diff["identical"] is True
    assert diff["only_in_a"] == diff["only_in_b"] == diff["differing"] == 0


def test_compare_signal_jsonl_detects_extra_in_one_side(tmp_path):
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    s1 = {
        "strategy": "s", "index": "NIFTY50", "action": "BUY",
        "instrument": "NIFTY50", "reason": "", "confidence": 0.5,
        "metadata": {}, "ts": 1,
    }
    s2 = dict(s1, ts=2)
    p1.write_bytes(orjson.dumps(s1) + b"\n" + orjson.dumps(s2) + b"\n")
    p2.write_bytes(orjson.dumps(s1) + b"\n")
    diff = compare_signal_jsonl(p1, p2)
    assert diff["identical"] is False
    assert diff["only_in_a"] == 1
    assert diff["only_in_b"] == 0
