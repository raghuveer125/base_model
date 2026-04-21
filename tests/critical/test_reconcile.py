"""Startup reconciliation — orphan-entry cleanup for trades.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.critical.reconcile import reconcile_orphan_entries


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for ev in events:
            f.write(json.dumps(ev).encode() + b"\n")


def _read_events(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l]


def test_reconcile_no_file_returns_empty(tmp_path):
    result = reconcile_orphan_entries(tmp_path / "missing.jsonl")
    assert result == []


def test_reconcile_closes_single_orphan_entry(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_events(path, [
        {"kind": "entry", "ts": 1000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 120.0,
         "instrument": "NSE:NIFTY24500CE"},
    ])
    result = reconcile_orphan_entries(path)
    assert len(result) == 1
    ev = result[0]
    assert ev["kind"] == "exit"
    assert ev["reason"] == "reconciled_on_startup"
    assert ev["source"] == "reconcile"
    assert ev["pnl"] == 0.0
    assert ev["exit_ltp"] == 120.0
    assert ev["held_ms"] > 0

    # File should now have the original entry + synthetic exit
    all_events = _read_events(path)
    assert len(all_events) == 2
    assert all_events[1]["reason"] == "reconciled_on_startup"


def test_reconcile_skips_paired_entries(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_events(path, [
        {"kind": "entry", "ts": 1000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 120.0},
        {"kind": "exit",  "ts": 2000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 120.0,
         "exit_ltp": 125.0, "pnl": 750.0, "reason": "target"},
    ])
    result = reconcile_orphan_entries(path)
    assert result == []
    assert len(_read_events(path)) == 2


def test_reconcile_handles_multiple_orphans_across_indices(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_events(path, [
        {"kind": "entry", "ts": 1000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 120.0},
        {"kind": "entry", "ts": 2000, "index": "SENSEX",
         "strike": 79000, "side": "PE", "entry_ltp": 666.55},
        {"kind": "exit",  "ts": 2500, "index": "SENSEX",
         "strike": 79000, "side": "PE",
         "exit_ltp": 660.0, "pnl": -262.0, "reason": "stop"},
        {"kind": "entry", "ts": 3000, "index": "BANKNIFTY",
         "strike": 57000, "side": "CE", "entry_ltp": 800.0},
    ])
    result = reconcile_orphan_entries(path)
    # NIFTY50 + BANKNIFTY are orphans; SENSEX paired
    indices = sorted(ev["index"] for ev in result)
    assert indices == ["BANKNIFTY", "NIFTY50"]


def test_reconcile_overwritten_entry_only_closes_latest(tmp_path):
    # Repeated entry with same (index, strike, side) — the later one
    # overwrites the earlier in the pairing map. Only one synthetic exit.
    path = tmp_path / "trades.jsonl"
    _write_events(path, [
        {"kind": "entry", "ts": 1000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 100.0},
        {"kind": "entry", "ts": 5000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 110.0},
    ])
    result = reconcile_orphan_entries(path)
    assert len(result) == 1
    assert result[0]["entry_ltp"] == 110.0   # latest wins


def test_reconcile_ignores_malformed_lines(tmp_path):
    path = tmp_path / "trades.jsonl"
    path.write_bytes(
        b'{"kind":"entry","ts":1000,"index":"NIFTY50","strike":24500,"side":"CE","entry_ltp":100.0}\n'
        b'this is not json\n'
        b'\n'
        b'{"kind":"entry","ts":2000,"index":"SENSEX","strike":79000,"side":"PE","entry_ltp":666.55}\n'
    )
    result = reconcile_orphan_entries(path)
    assert len(result) == 2


def test_reconcile_is_idempotent(tmp_path):
    # Running reconcile twice in a row should only produce exits once:
    # after the first run, every entry has a matching exit.
    path = tmp_path / "trades.jsonl"
    _write_events(path, [
        {"kind": "entry", "ts": 1000, "index": "NIFTY50",
         "strike": 24500, "side": "CE", "entry_ltp": 100.0},
    ])
    first = reconcile_orphan_entries(path)
    second = reconcile_orphan_entries(path)
    assert len(first) == 1
    assert second == []
