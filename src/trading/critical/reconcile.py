"""Startup reconciliation for the paper-trading audit log.

`logs/critical/trades.jsonl` is append-only. If a critical-layer process
dies with an open position (OS kill, crash, deploy restart), the entry
event is on disk but no matching exit ever gets written. The engine
doesn't re-read the log on startup, so those entries become permanent
"zombie open" rows in the UI even though the engine has no such
position in memory.

Running `reconcile_orphan_entries()` at startup fixes this by:

  1. Reading every event in trades.jsonl.
  2. Pairing each "entry" with its matching "exit" on
     (index, strike, side).
  3. For every entry that never got an exit, appending a synthetic
     exit event with reason="reconciled_on_startup".

Synthetic exits:
  - exit_ltp = entry_ltp (honest "we don't know the real close")
  - pnl = 0.0 (not market-derived)
  - source = "reconcile" (forensic filter)
  - held_ms = reconcile_ts - entry_ts

Zero trading impact — this only writes to the log. The engine's
in-memory state starts empty regardless.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

import orjson

from trading.logging_setup import get_logger
from trading.schemas import now_ms

log = get_logger(__name__)

_DEFAULT_PATH = Path("logs/critical/trades.jsonl").resolve()
_WRITE_LOCK = Lock()


def _pair_entries(events: list[dict]) -> list[dict]:
    """Return entries that have no matching later exit (oldest first)."""
    open_by_key: dict[tuple, dict] = {}
    for ev in events:
        key = (ev.get("index"), ev.get("strike"), ev.get("side"))
        kind = ev.get("kind")
        if kind == "entry":
            # Later entries with the same key (e.g., the NIFTY50 25000
            # repeat) overwrite older ones — mirrors rest.py's pairing.
            open_by_key[key] = ev
        elif kind == "exit":
            open_by_key.pop(key, None)
    return list(open_by_key.values())


def reconcile_orphan_entries(path: Path | None = None) -> list[dict]:
    """Scan the trades log, emit synthetic exits for orphan entries.

    Returns the list of synthetic exit events that were written.
    Silent + safe on any I/O error (this is hygiene, never load-bearing).
    """
    path = path or _DEFAULT_PATH
    if not path.is_file():
        return []

    try:
        raw = path.read_bytes().splitlines()
    except OSError as e:
        log.warning("reconcile_read_failed", path=str(path), error=str(e))
        return []

    events: list[dict] = []
    for line in raw:
        if not line:
            continue
        try:
            events.append(orjson.loads(line))
        except Exception:   # noqa: BLE001
            continue

    orphans = _pair_entries(events)
    if not orphans:
        return []

    ts_ms = now_ms()
    synthetic: list[dict] = []
    for e in orphans:
        entry_ts = int(e.get("ts") or ts_ms)
        entry_ltp = float(e.get("entry_ltp") or 0.0)
        synthetic.append({
            "kind": "exit",
            "ts": ts_ms,
            "index": e.get("index"),
            "strike": e.get("strike"),
            "side": e.get("side"),
            "entry_ltp": entry_ltp,
            "exit_ltp": entry_ltp,
            "pnl": 0.0,
            "reason": "reconciled_on_startup",
            "held_ms": ts_ms - entry_ts,
            "instrument": e.get("instrument"),
            "source": "reconcile",
        })

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"".join(orjson.dumps(ev) + b"\n" for ev in synthetic)
        with _WRITE_LOCK, open(path, "ab") as f:
            f.write(payload)
    except OSError as e:
        log.warning("reconcile_write_failed", path=str(path), error=str(e))
        return []

    log.info("reconcile_orphans_closed",
             count=len(synthetic),
             indices=sorted({str(s.get("index")) for s in synthetic}))
    return synthetic


__all__ = ["reconcile_orphan_entries"]
