"""Pure, side-effect-free checks the vigilante daemon composes.

Each function takes the minimum inputs it needs and returns a structured
result (never raises on expected failure modes). The daemon is the only
thing that turns a check result into a log line or a Redis flag.

Side effects are confined to `daemon.py` and `forensics.py` so these
functions are trivial to unit-test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# ────────────────────────────────────────────────────────────────────────
# Managed service modules. Vigilante watches these; nothing else.
# ────────────────────────────────────────────────────────────────────────

MANAGED_MODULES: tuple[str, ...] = (
    "trading.scripts.run_ingest",
    "trading.scripts.run_candles",
    "trading.scripts.run_greeks",
    "trading.scripts.run_strategies",
    "trading.scripts.run_orders",
    "trading.critical",
    "trading.critical.vigilante",
    "trading.scripts.run_ui",
)

# ────────────────────────────────────────────────────────────────────────
# PID-collision detection
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcInfo:
    """Minimal psutil projection — keeps checks injectable in tests."""
    pid: int
    module: str
    create_time: float   # epoch seconds (psutil.Process.create_time())
    is_launcher: bool    # True if venv-launcher (no python-process parent)


@dataclass(frozen=True)
class CollisionReport:
    module: str
    launcher_pids: tuple[int, ...]
    collided: bool
    detail: str = ""


def detect_collisions(
    procs: list[ProcInfo], *, min_delta_s: float = 5.0,
) -> list[CollisionReport]:
    """Flag modules running as MULTIPLE independent launchers.

    On Windows a venv service shows up as 2 processes (launcher stub +
    real interpreter child). That's expected — we count only LAUNCHER
    roots (`is_launcher=True`). Two launchers with create_time within
    `min_delta_s` are assumed to be a launcher/child pair seen out-of-
    order and NOT a collision.
    """
    by_mod: dict[str, list[ProcInfo]] = {}
    for p in procs:
        if p.module not in MANAGED_MODULES or not p.is_launcher:
            continue
        by_mod.setdefault(p.module, []).append(p)

    reports: list[CollisionReport] = []
    for mod, group in by_mod.items():
        if len(group) <= 1:
            reports.append(CollisionReport(
                module=mod,
                launcher_pids=tuple(p.pid for p in group),
                collided=False,
            ))
            continue
        # Sort by create_time; if all pairs are within `min_delta_s`
        # it's launcher/child jitter, not a collision.
        group.sort(key=lambda p: p.create_time)
        deltas = [
            group[i + 1].create_time - group[i].create_time
            for i in range(len(group) - 1)
        ]
        if deltas and max(deltas) < min_delta_s:
            reports.append(CollisionReport(
                module=mod,
                launcher_pids=tuple(p.pid for p in group),
                collided=False,
                detail=f"{len(group)} procs within {max(deltas):.2f}s — jitter, not collision",
            ))
            continue
        reports.append(CollisionReport(
            module=mod,
            launcher_pids=tuple(p.pid for p in group),
            collided=True,
            detail=f"{len(group)} independent launchers (max inter-create gap {max(deltas):.1f}s)",
        ))
    return reports


# ────────────────────────────────────────────────────────────────────────
# Heartbeat monitoring — time-of-day damped, spike-detected
# ────────────────────────────────────────────────────────────────────────


HeartbeatVerdict = Literal["fresh", "stale_once", "stale_sustained"]


@dataclass(frozen=True)
class HeartbeatReport:
    index: str
    last_seen_ms: int | None
    age_s: float | None
    threshold_s: float
    verdict: HeartbeatVerdict
    streak: int = 0        # consecutive stale samples (post-threshold)


def heartbeat_threshold_s(
    ist_time: datetime | None = None,
    *,
    default_s: float = 5.0,
) -> float:
    """Tolerated staleness at the current time-of-day.

    Indian market microstructure:
      * 09:15–09:20 IST — opening auction tail; batch settlement can
        stall the feed for ~10–15 s.
      * 11:30–12:30 IST — lunch lull; low-liquidity strikes tick every
        2–5 s, raise tolerance to 10 s.
      * 15:25–15:30 IST — closing cross, similar to open.
      * Everything else — tight 5 s.

    Passing `None` assumes default — useful for tests.
    """
    if ist_time is None:
        return default_s
    h, m = ist_time.hour, ist_time.minute
    minute_of_day = h * 60 + m
    # 09:15 = 555, 09:20 = 560. 15:25 = 925. 15:30 = 930.
    if 555 <= minute_of_day <= 560:
        return 15.0
    if 925 <= minute_of_day <= 930:
        return 15.0
    # 11:30 – 12:30 inclusive
    if 690 <= minute_of_day <= 750:
        return 10.0
    return default_s


def evaluate_heartbeat(
    index: str,
    last_seen_ms: int | None,
    now_ms: int,
    prior_streak: int,
    *,
    threshold_s: float,
    spike_streak_required: int = 3,
) -> HeartbeatReport:
    """Classify feed freshness for one index.

    `prior_streak` is the running count of consecutive stale samples the
    daemon has seen; the daemon supplies this, we return the updated one.
    Only after `spike_streak_required` back-to-back stale samples do we
    return `stale_sustained` — the signal the daemon uses to set the
    Redis reconnect flag. Single stale samples (micro-gap, GC pause) are
    reported as `stale_once` for logging but do NOT flip the flag.
    """
    if last_seen_ms is None:
        age_s = None
        new_streak = prior_streak + 1
        verdict: HeartbeatVerdict = (
            "stale_sustained" if new_streak >= spike_streak_required
            else "stale_once"
        )
        return HeartbeatReport(
            index=index, last_seen_ms=None, age_s=None,
            threshold_s=threshold_s,
            verdict=verdict, streak=new_streak,
        )
    age_s = (now_ms - last_seen_ms) / 1000.0
    if age_s <= threshold_s:
        return HeartbeatReport(
            index=index, last_seen_ms=last_seen_ms, age_s=age_s,
            threshold_s=threshold_s, verdict="fresh", streak=0,
        )
    new_streak = prior_streak + 1
    verdict = (
        "stale_sustained" if new_streak >= spike_streak_required
        else "stale_once"
    )
    return HeartbeatReport(
        index=index, last_seen_ms=last_seen_ms, age_s=age_s,
        threshold_s=threshold_s, verdict=verdict, streak=new_streak,
    )


# ────────────────────────────────────────────────────────────────────────
# Payload verification — assert the shape Claude will see
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PayloadReport:
    index: str
    ok: bool
    violations: tuple[str, ...] = ()


def verify_prompt_shape(index: str, rendered_prompt: str) -> PayloadReport:
    """Static assertions on what would be sent to Claude for this index.

    Enforces:
      * Only NIFTY50 prompts may contain an "India VIX:" line.
      * If "ATM IV" appears, its values must be in percentage range
        (1–80). A 0.23 here would mean the decimal→percent normaliser
        failed.
      * The prompt must NOT carry stale-state breadcrumbs like a raw
        dataclass repr or a python traceback.
    """
    violations: list[str] = []
    has_vix_line = "India VIX:" in rendered_prompt
    if index != "NIFTY50" and has_vix_line:
        violations.append(
            f"cross-index leak: {index} prompt carries 'India VIX:' "
            "(VIX should be NIFTY50-only)"
        )
    # Extract ATM IV numeric values if present.
    if "ATM IV (this index):" in rendered_prompt:
        for token in ("CE=", "PE="):
            idx = rendered_prompt.find(f"ATM IV (this index):")
            segment = rendered_prompt[idx:idx + 160]
            pos = segment.find(token)
            if pos < 0:
                continue
            tail = segment[pos + len(token):pos + len(token) + 8]
            num_str = ""
            for ch in tail:
                if ch.isdigit() or ch == ".":
                    num_str += ch
                elif num_str:
                    break
            if not num_str or num_str == "n/a":
                continue
            try:
                v = float(num_str)
            except ValueError:
                continue
            if v < 1.0:
                violations.append(
                    f"ATM IV {token}{v} looks like a decimal (unnormalised); "
                    "expected percentage (10-50 typical)"
                )
            elif v > 80.0:
                violations.append(
                    f"ATM IV {token}{v} is implausibly high — data issue"
                )
    if "Traceback" in rendered_prompt or "<object at 0x" in rendered_prompt:
        violations.append("prompt contains python debug output")
    return PayloadReport(
        index=index, ok=not violations,
        violations=tuple(violations),
    )


# ────────────────────────────────────────────────────────────────────────
# Forensic scan helpers (used by forensics.py, exposed for tests)
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SuspectExit:
    ts_ms: int
    index: str
    strike: int | None
    side: str | None
    held_ms: int
    reason: str


def find_fast_wall_breaks(
    events: list[dict], *, max_held_ms: int = 10_000,
) -> list[SuspectExit]:
    """Wall-break exits that closed faster than the hysteresis should
    allow — the symptom the user flagged on 2026-04-21."""
    out: list[SuspectExit] = []
    for ev in events:
        if ev.get("kind") != "exit":
            continue
        reason = str(ev.get("reason") or "")
        if not reason.startswith("wall_break"):
            continue
        held = int(ev.get("held_ms") or 0)
        if held >= max_held_ms:
            continue
        out.append(SuspectExit(
            ts_ms=int(ev.get("ts") or 0),
            index=str(ev.get("index") or "?"),
            strike=ev.get("strike"),
            side=ev.get("side"),
            held_ms=held,
            reason=reason,
        ))
    return out
