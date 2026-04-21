"""Forensic + recovery CLI — post-mortem scanning and destructive reset.

Exposed via the `python -m trading.critical.vigilante` entrypoint. Kept
separate from the daemon so a manual operator can run `scan` or `reset`
without touching any running monitor.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import orjson

from trading.critical.vigilante.checks import (
    MANAGED_MODULES, SuspectExit, find_fast_wall_breaks,
)
from trading.logging_setup import get_logger

log = get_logger(__name__)

# Order MATTERS on both stop + start:
#   stop  — critical first (no new entries), then producers in reverse
#           dependency, so nothing republishes stale data mid-purge.
#   start — ingest → candles → greeks → strategies → orders → critical → ui
STOP_ORDER: tuple[str, ...] = (
    "trading.critical",
    "trading.scripts.run_orders",
    "trading.scripts.run_strategies",
    "trading.scripts.run_greeks",
    "trading.scripts.run_candles",
    "trading.scripts.run_ingest",
    "trading.scripts.run_ui",
)
START_ORDER: tuple[str, ...] = (
    "trading.scripts.run_ingest",
    "trading.scripts.run_candles",
    "trading.scripts.run_greeks",
    "trading.scripts.run_strategies",
    "trading.scripts.run_orders",
    "trading.critical",
    "trading.scripts.run_ui",
)


# ────────────────────────────────────────────────────────────────────────
# scan — read-only forensic report
# ────────────────────────────────────────────────────────────────────────


def scan_trades(
    path: Path | str = "logs/critical/trades.jsonl",
    *,
    max_held_ms: int = 10_000,
) -> dict[str, Any]:
    """Read the audit log and surface suspicious patterns.

    Currently reports:
      * Fast wall_break exits (held < `max_held_ms`) — hysteresis
        failures, the pattern the user flagged on 2026-04-21.
      * Count of synthetic "reconciled_on_startup" exits (engine
        restarts — noise in the PnL view).

    Returns a JSON-serialisable dict; the CLI prints a human-readable
    digest of it.
    """
    path = Path(path)
    if not path.is_file():
        return {
            "trades_path": str(path), "exists": False,
            "total_events": 0,
            "fast_wall_breaks": [],
            "reconcile_exits": 0,
        }
    events: list[dict] = []
    for line in path.read_bytes().splitlines():
        if not line:
            continue
        try:
            events.append(orjson.loads(line))
        except Exception:   # noqa: BLE001
            continue
    suspects = find_fast_wall_breaks(events, max_held_ms=max_held_ms)
    recon = sum(
        1 for ev in events
        if ev.get("kind") == "exit" and ev.get("source") == "reconcile"
    )
    return {
        "trades_path": str(path),
        "exists": True,
        "total_events": len(events),
        "fast_wall_breaks": [_suspect_to_dict(s) for s in suspects],
        "fast_wall_break_count": len(suspects),
        "reconcile_exits": recon,
    }


def _suspect_to_dict(s: SuspectExit) -> dict:
    return {
        "ts_ms": s.ts_ms, "index": s.index, "strike": s.strike,
        "side": s.side, "held_ms": s.held_ms, "reason": s.reason,
    }


def render_scan_report(report: dict[str, Any]) -> str:
    """Format the scan dict for humans."""
    lines = [
        f"Forensic scan  —  {report['trades_path']}",
    ]
    if not report.get("exists"):
        lines.append("  trades.jsonl not found (no audit log yet)")
        return "\n".join(lines)
    lines.append(f"  total events:        {report['total_events']}")
    lines.append(f"  reconcile exits:     {report['reconcile_exits']}")
    lines.append(f"  fast wall_breaks:    {report['fast_wall_break_count']}")
    if report["fast_wall_breaks"]:
        lines.append("  hysteresis-failure suspects:")
        for s in report["fast_wall_breaks"][-10:]:
            lines.append(
                f"    ts={s['ts_ms']}  {s['index']}  {s['side']} {s['strike']}"
                f"  held_ms={s['held_ms']}  reason={s['reason'][:60]}"
            )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# reset — destructive purge + ordered restart
# ────────────────────────────────────────────────────────────────────────


def reset_stack(
    *,
    confirmed: bool = False,
    force_with_open_positions: bool = False,
    venv_python: str | None = None,
    working_dir: str | None = None,
    redis_key_prefix: str = "tpp:",
) -> dict[str, Any]:
    """Destructive recovery: stop everything, purge tpp:* keys, restart.

    Flow:

      0. sanity-check: refuse unless `confirmed=True` (maps to --yes)
      1. enumerate open positions via trades.jsonl pairing. If any exist,
         refuse unless `force_with_open_positions=True` (maps to --force).
      2. import and call reconcile_orphan_entries() — closes the audit
         log honestly BEFORE Redis disappears.
      3. stop processes in STOP_ORDER (critical first, then producers in
         reverse dependency), waiting 1s between kills.
      4. flush only `tpp:*` Redis keys (NOT FLUSHALL — the Fyers token
         and any other non-TPP data stays intact).
      5. restart services in START_ORDER with gaps to let each warm up.

    Returns a dict describing every step. Errors per step are caught and
    reported; the function does not raise.
    """
    if not confirmed:
        return {"ok": False, "reason": "confirmation (--yes) required"}

    # lazy imports so `scan` doesn't need psutil / redis available
    import psutil
    from trading.critical.reconcile import reconcile_orphan_entries
    from trading.storage import get_redis

    steps: list[dict] = []

    # Step 1: open-position check
    opens = _list_open_positions()
    if opens and not force_with_open_positions:
        return {
            "ok": False,
            "reason": "open positions present; pass --force to purge anyway",
            "open_positions": opens,
        }
    steps.append({"step": "open_positions", "count": len(opens),
                   "detail": opens})

    # Step 2: pre-flush reconcile so the audit log is honest
    try:
        synth = reconcile_orphan_entries()
        steps.append({"step": "reconcile", "synthetic_exits_written": len(synth)})
    except Exception as e:  # noqa: BLE001
        steps.append({"step": "reconcile", "error": str(e)})

    # Step 3: stop in STOP_ORDER
    for mod in STOP_ORDER:
        killed = _kill_module(mod)
        steps.append({"step": "stop", "module": mod, "killed_pids": killed})
        time.sleep(1)

    # Step 4: flush only tpp:* keys (preserve Fyers token etc.)
    try:
        r = get_redis()
        deleted = 0
        for key in r.scan_iter(match=f"{redis_key_prefix}*"):
            r.delete(key)
            deleted += 1
        steps.append({"step": "redis_flush", "prefix": redis_key_prefix,
                       "keys_deleted": deleted})
    except Exception as e:   # noqa: BLE001
        steps.append({"step": "redis_flush", "error": str(e)})

    # Step 5: start in START_ORDER — only if caller supplied venv_python
    if venv_python is None:
        steps.append({
            "step": "start",
            "skipped": True,
            "reason": "no venv_python supplied — caller responsible for restart",
        })
    else:
        for mod in START_ORDER:
            spawned = _spawn_service(mod, venv_python, working_dir)
            steps.append({"step": "start", "module": mod, **spawned})
            time.sleep(3)  # space starts so each service can bind its resources

    return {"ok": True, "steps": steps}


# ────────────────────────────────────────────────────────────────────────
# internals
# ────────────────────────────────────────────────────────────────────────


def _list_open_positions(
    path: str | Path = "logs/critical/trades.jsonl",
) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    open_by_key: dict[tuple, dict] = {}
    for line in path.read_bytes().splitlines():
        if not line:
            continue
        try:
            ev = orjson.loads(line)
        except Exception:   # noqa: BLE001
            continue
        key = (ev.get("index"), ev.get("strike"), ev.get("side"))
        if ev.get("kind") == "entry":
            open_by_key[key] = ev
        elif ev.get("kind") == "exit":
            open_by_key.pop(key, None)
    return list(open_by_key.values())


def _kill_module(module: str) -> list[int]:
    import psutil
    killed: list[int] = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if module in cmd and "python" in (p.info.get("name") or "").lower():
                p.kill()
                killed.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def _spawn_service(
    module: str, venv_python: str, working_dir: str | None,
) -> dict:
    import subprocess
    try:
        proc = subprocess.Popen(
            [venv_python, "-m", module],
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return {"pid": proc.pid, "ok": True}
    except Exception as e:   # noqa: BLE001
        return {"ok": False, "error": str(e)}
