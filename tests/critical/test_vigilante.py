"""Vigilante tests — pure checks + forensic CLI helpers.

Does NOT start the daemon thread or touch psutil / Redis. Daemon
lifecycle is tested by hand, not automatically — it's a sidecar with
side effects on an external process table.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trading.critical.vigilante.checks import (
    ProcInfo, SuspectExit, detect_collisions,
    evaluate_heartbeat, find_fast_wall_breaks, heartbeat_threshold_s,
    verify_prompt_shape,
)
from trading.critical.vigilante.forensics import (
    _list_open_positions, render_scan_report, scan_trades,
)


_IST = ZoneInfo("Asia/Kolkata")


# ────────────────────────────────────────────────────────────────────────
# heartbeat_threshold_s — time-of-day damping
# ────────────────────────────────────────────────────────────────────────


def test_heartbeat_threshold_normal_mid_morning():
    now = datetime(2026, 4, 21, 10, 30, tzinfo=_IST)
    assert heartbeat_threshold_s(now) == 5.0

def test_heartbeat_threshold_lunch_lull_widens():
    for h, m in [(11, 30), (12, 0), (12, 30)]:
        now = datetime(2026, 4, 21, h, m, tzinfo=_IST)
        assert heartbeat_threshold_s(now) == 10.0

def test_heartbeat_threshold_opening_spike():
    # 09:15-09:20 IST — opening auction tail
    now = datetime(2026, 4, 21, 9, 17, tzinfo=_IST)
    assert heartbeat_threshold_s(now) == 15.0

def test_heartbeat_threshold_closing_spike():
    now = datetime(2026, 4, 21, 15, 27, tzinfo=_IST)
    assert heartbeat_threshold_s(now) == 15.0

def test_heartbeat_threshold_none_uses_default():
    assert heartbeat_threshold_s(None) == 5.0


# ────────────────────────────────────────────────────────────────────────
# evaluate_heartbeat — spike detection
# ────────────────────────────────────────────────────────────────────────


def test_heartbeat_fresh_returns_zero_streak():
    r = evaluate_heartbeat(
        "NIFTY50", last_seen_ms=1000, now_ms=3000,
        prior_streak=0, threshold_s=5.0,
    )
    assert r.verdict == "fresh"
    assert r.streak == 0
    assert r.age_s == 2.0

def test_heartbeat_single_stale_is_not_sustained():
    # 1st stale sample — log only, don't set reconnect flag yet.
    r = evaluate_heartbeat(
        "NIFTY50", last_seen_ms=0, now_ms=10_000,
        prior_streak=0, threshold_s=5.0,
    )
    assert r.verdict == "stale_once"
    assert r.streak == 1

def test_heartbeat_three_consecutive_stale_trips_sustained():
    r = evaluate_heartbeat(
        "NIFTY50", last_seen_ms=0, now_ms=10_000,
        prior_streak=2, threshold_s=5.0,
    )
    assert r.verdict == "stale_sustained"
    assert r.streak == 3

def test_heartbeat_fresh_after_stale_resets_streak():
    r = evaluate_heartbeat(
        "NIFTY50", last_seen_ms=9_000, now_ms=10_000,
        prior_streak=3, threshold_s=5.0,
    )
    assert r.verdict == "fresh"
    assert r.streak == 0

def test_heartbeat_no_last_seen_counts_as_stale():
    r = evaluate_heartbeat(
        "NIFTY50", last_seen_ms=None, now_ms=10_000,
        prior_streak=0, threshold_s=5.0,
    )
    assert r.verdict in ("stale_once", "stale_sustained")
    assert r.age_s is None


# ────────────────────────────────────────────────────────────────────────
# detect_collisions
# ────────────────────────────────────────────────────────────────────────


def test_collisions_none_when_one_launcher_per_module():
    procs = [
        ProcInfo(pid=100, module="trading.critical",
                 create_time=1000.0, is_launcher=True),
        ProcInfo(pid=101, module="trading.critical",
                 create_time=1000.1, is_launcher=False),  # child
    ]
    reports = detect_collisions(procs)
    assert all(not r.collided for r in reports)

def test_collisions_flags_two_independent_launchers():
    procs = [
        ProcInfo(pid=100, module="trading.critical",
                 create_time=1000.0, is_launcher=True),
        ProcInfo(pid=200, module="trading.critical",
                 create_time=9000.0, is_launcher=True),
    ]
    reports = detect_collisions(procs, min_delta_s=5.0)
    crit = [r for r in reports if r.module == "trading.critical"]
    assert crit and crit[0].collided
    assert 100 in crit[0].launcher_pids and 200 in crit[0].launcher_pids

def test_collisions_ignores_near_simultaneous_pair():
    # Two "launchers" born within 2s of each other are launcher/child
    # race jitter on Windows — not a real collision.
    procs = [
        ProcInfo(pid=100, module="trading.critical",
                 create_time=1000.0, is_launcher=True),
        ProcInfo(pid=101, module="trading.critical",
                 create_time=1001.5, is_launcher=True),
    ]
    reports = detect_collisions(procs, min_delta_s=5.0)
    crit = next(r for r in reports if r.module == "trading.critical")
    assert not crit.collided


# ────────────────────────────────────────────────────────────────────────
# verify_prompt_shape
# ────────────────────────────────────────────────────────────────────────


def test_payload_ok_nifty_with_vix_and_pct_iv():
    prompt = (
        "Index: NIFTY50\nSpot: 24526.00\nIndia VIX: 17.69\n"
        "ATM IV (this index): CE=23.07  PE=19.53  (skew PE-CE: -3.54)\n"
        "Recent LTPs: []\n..."
    )
    r = verify_prompt_shape("NIFTY50", prompt)
    assert r.ok
    assert r.violations == ()

def test_payload_fails_when_banknifty_carries_vix():
    prompt = (
        "Index: BANKNIFTY\nSpot: 57100.00\nIndia VIX: 17.69\n"
        "ATM IV (this index): CE=20.00  PE=22.00  (skew PE-CE: +2.00)\n"
    )
    r = verify_prompt_shape("BANKNIFTY", prompt)
    assert not r.ok
    assert any("cross-index leak" in v for v in r.violations)

def test_payload_fails_on_unnormalised_decimal_iv():
    prompt = (
        "Index: NIFTY50\nSpot: 24526\nIndia VIX: 17.69\n"
        "ATM IV (this index): CE=0.23  PE=0.19  (skew PE-CE: -0.04)\n"
    )
    r = verify_prompt_shape("NIFTY50", prompt)
    assert not r.ok
    assert any("looks like a decimal" in v for v in r.violations)

def test_payload_no_vix_on_banknifty_passes():
    prompt = (
        "Index: BANKNIFTY\nSpot: 57100\n"
        "ATM IV (this index): CE=20.00  PE=22.00  (skew PE-CE: +2.00)\n"
    )
    r = verify_prompt_shape("BANKNIFTY", prompt)
    assert r.ok


# ────────────────────────────────────────────────────────────────────────
# find_fast_wall_breaks + scan_trades
# ────────────────────────────────────────────────────────────────────────


def test_find_fast_wall_breaks_matches_only_short_ones():
    events = [
        {"kind": "exit", "reason": "wall_break: spot=x", "held_ms": 4500,
         "index": "SENSEX", "strike": 79000, "side": "PE", "ts": 1},
        {"kind": "exit", "reason": "wall_break: spot=x", "held_ms": 42_000,
         "index": "SENSEX", "strike": 79000, "side": "PE", "ts": 2},
        {"kind": "exit", "reason": "target", "held_ms": 3000,
         "index": "NIFTY50", "strike": 24500, "side": "CE", "ts": 3},
    ]
    out = find_fast_wall_breaks(events, max_held_ms=10_000)
    assert len(out) == 1
    assert out[0].held_ms == 4500

def test_scan_trades_reports_reconcile_count_and_suspects(tmp_path):
    p = tmp_path / "trades.jsonl"
    lines = [
        {"kind": "entry", "ts": 1, "index": "NIFTY50", "strike": 25000,
         "side": "CE", "entry_ltp": 120.5},
        {"kind": "exit", "ts": 2, "index": "NIFTY50", "strike": 25000,
         "side": "CE", "exit_ltp": 120.5, "pnl": 0,
         "reason": "reconciled_on_startup", "source": "reconcile",
         "held_ms": 1000},
        {"kind": "entry", "ts": 3, "index": "SENSEX", "strike": 79000,
         "side": "PE", "entry_ltp": 666.55},
        {"kind": "exit", "ts": 4, "index": "SENSEX", "strike": 79000,
         "side": "PE", "exit_ltp": 665, "pnl": -100,
         "reason": "wall_break: spot=79057>res=79000", "held_ms": 5000},
    ]
    with open(p, "wb") as f:
        for ev in lines:
            f.write(json.dumps(ev).encode() + b"\n")
    report = scan_trades(p, max_held_ms=10_000)
    assert report["total_events"] == 4
    assert report["reconcile_exits"] == 1
    assert report["fast_wall_break_count"] == 1


def test_render_scan_report_handles_missing_file(tmp_path):
    out = render_scan_report({
        "trades_path": str(tmp_path / "missing.jsonl"),
        "exists": False, "total_events": 0,
        "fast_wall_breaks": [], "reconcile_exits": 0,
    })
    assert "not found" in out


# ────────────────────────────────────────────────────────────────────────
# _list_open_positions — helper for reset guard
# ────────────────────────────────────────────────────────────────────────


def test_list_open_positions_pairs_correctly(tmp_path):
    p = tmp_path / "trades.jsonl"
    lines = [
        {"kind": "entry", "ts": 1, "index": "NIFTY50", "strike": 25000,
         "side": "CE"},
        {"kind": "exit", "ts": 2, "index": "NIFTY50", "strike": 25000,
         "side": "CE"},
        {"kind": "entry", "ts": 3, "index": "SENSEX", "strike": 79000,
         "side": "PE"},
    ]
    with open(p, "wb") as f:
        for ev in lines:
            f.write(json.dumps(ev).encode() + b"\n")
    opens = _list_open_positions(p)
    assert len(opens) == 1
    assert opens[0]["index"] == "SENSEX"


def test_list_open_positions_handles_missing_file(tmp_path):
    assert _list_open_positions(tmp_path / "nope.jsonl") == []


# ────────────────────────────────────────────────────────────────────────
# Engine _session_feedback — must strip reconcile exits
# ────────────────────────────────────────────────────────────────────────


def test_session_feedback_excludes_reconcile_exits(tmp_path, monkeypatch):
    """Reconcile-on-startup exits (pnl=0 synthetic) must not pollute the
    hit-rate Claude sees — they'd dilute real signal."""
    import time as _time
    from trading.critical.engine import CriticalEngine

    # Timestamp events in the last few minutes so they pass the day-start
    # filter regardless of when the test runs.
    now_ms = int(_time.time() * 1000)
    p = tmp_path / "trades.jsonl"
    lines = [
        {"kind": "entry", "ts": now_ms - 600_000, "index": "NIFTY50",
         "strike": 24_500, "side": "CE", "entry_ltp": 100.0},
        {"kind": "exit", "ts": now_ms - 540_000, "index": "NIFTY50",
         "strike": 24_500, "side": "CE", "exit_ltp": 95.0, "pnl": -750,
         "reason": "stop", "held_ms": 60_000},
        {"kind": "entry", "ts": now_ms - 500_000, "index": "NIFTY50",
         "strike": 24_500, "side": "CE", "entry_ltp": 100.0},
        {"kind": "exit", "ts": now_ms - 440_000, "index": "NIFTY50",
         "strike": 24_500, "side": "CE", "exit_ltp": 97.0, "pnl": -450,
         "reason": "stop", "held_ms": 60_000},
        # 4 reconcile exits (deploy churn) — must be ignored
        *[
            {"kind": "exit", "ts": now_ms - 300_000 + i * 1000,
             "index": "NIFTY50", "strike": 25_000, "side": "CE",
             "exit_ltp": 120.5, "pnl": 0,
             "reason": "reconciled_on_startup", "source": "reconcile",
             "held_ms": 60_000}
            for i in range(4)
        ],
    ]
    with open(p, "wb") as f:
        for ev in lines:
            f.write(json.dumps(ev).encode() + b"\n")

    eng = CriticalEngine(indices=["NIFTY50"])
    monkeypatch.setattr(eng, "_TRADES_JSONL_PATH", str(p))
    fb = eng._session_feedback("NIFTY50")
    assert fb.dominant_exit_reason == "stop"   # reconcile must be filtered
    assert fb.hit_rate == 0.0                  # 0/2, not 0/6
    assert fb.trades_today == 2
