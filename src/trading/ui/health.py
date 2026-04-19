"""Health + alerting evaluator for the UI.

Runs a fixed set of checks against Redis, Postgres, and the filesystem, and
returns a HealthReport dict:

  status  healthy | degraded | down
  checks  [{name, status, detail, latency_ms?}]
  indices [{index, last_seen_ms, staleness_s, status}]
  alerts  [{rule, severity, detail, since_ms}]
  metrics raw tpp:metrics hash + updated_ms

All probes are bounded; failures become `crit` checks with the error text.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.storage import LiveStore, get_pg_pool, get_redis

log = get_logger(__name__)

_SEV_RANK = {"ok": 0, "info": 0, "warn": 1, "crit": 2}


def _is_market_hours_ist() -> bool:
    s = get_settings()
    try:
        now = datetime.now(tz=ZoneInfo(s.market_tz))
    except Exception:  # noqa: BLE001
        return False
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (t.hour, t.minute) >= (9, 15) and (t.hour, t.minute) <= (15, 30)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _check_redis() -> dict:
    start = time.perf_counter()
    try:
        ok = bool(get_redis().ping())
        lat = (time.perf_counter() - start) * 1000
        return {"name": "redis",
                "status": "ok" if ok else "crit",
                "detail": "ping ok" if ok else "ping returned false",
                "latency_ms": round(lat, 2)}
    except Exception as e:  # noqa: BLE001
        return {"name": "redis", "status": "crit",
                "detail": f"redis unreachable: {type(e).__name__}: {e}",
                "latency_ms": None}


def _check_postgres() -> dict:
    start = time.perf_counter()
    try:
        with get_pg_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        lat = (time.perf_counter() - start) * 1000
        return {"name": "postgres", "status": "ok",
                "detail": "SELECT 1 ok",
                "latency_ms": round(lat, 2)}
    except Exception as e:  # noqa: BLE001
        return {"name": "postgres", "status": "crit",
                "detail": f"postgres unreachable: {type(e).__name__}: {e}",
                "latency_ms": None}


def _check_wal_dir() -> dict:
    s = get_settings()
    try:
        s.wal_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(s.wal_dir), prefix=".hc_", suffix=".tmp", delete=False,
        ) as fh:
            path = fh.name
            fh.write(b"ok")
        os.remove(path)
        return {"name": "wal_dir", "status": "ok",
                "detail": f"writable: {s.wal_dir}", "latency_ms": None}
    except Exception as e:  # noqa: BLE001
        return {"name": "wal_dir", "status": "crit",
                "detail": f"WAL_DIR not writable: {e}", "latency_ms": None}


def _check_metrics_freshness(metrics: dict, now_ms: int, stale_after_s: int) -> dict:
    updated = metrics.get("updated_ms")
    if updated is None:
        return {"name": "metrics", "status": "warn",
                "detail": "no tpp:metrics:ts (is tpp-ingest running?)",
                "latency_ms": None}
    age_s = (now_ms - int(updated)) / 1000.0
    if age_s > stale_after_s:
        return {"name": "metrics", "status": "warn",
                "detail": f"stale: {age_s:.1f}s since last publish (>{stale_after_s}s)",
                "latency_ms": None}
    return {"name": "metrics", "status": "ok",
            "detail": f"fresh: {age_s:.1f}s old", "latency_ms": None}


def _check_latency(metrics: dict, warn_ms: int, crit_ms: int) -> dict:
    p95 = _to_int(metrics.get("ingest_latency_p95_ms"))
    if p95 is None:
        return {"name": "latency_p95", "status": "info",
                "detail": "no samples yet", "latency_ms": None}
    if p95 >= crit_ms:
        status = "crit"
    elif p95 >= warn_ms:
        status = "warn"
    else:
        status = "ok"
    return {"name": "latency_p95", "status": status,
            "detail": f"p95={p95}ms (warn≥{warn_ms}, crit≥{crit_ms})",
            "latency_ms": float(p95)}


def _threshold_check(name: str, metrics: dict, key: str, warn: int, crit: int) -> dict:
    n = _to_int(metrics.get(key))
    if n is None:
        return {"name": name, "status": "info",
                "detail": "no data", "latency_ms": None}
    if n >= crit:
        status = "crit"
    elif n >= warn:
        status = "warn"
    elif n > 0:
        status = "info"
    else:
        status = "ok"
    return {"name": name, "status": status,
            "detail": f"{n} this session (warn≥{warn}, crit≥{crit})",
            "latency_ms": None}


def _index_staleness(
    now_ms: int, market_open: bool, warn_s: int, crit_s: int,
) -> list[dict]:
    s = get_settings()
    out: list[dict] = []
    try:
        store = LiveStore()
    except Exception as e:  # noqa: BLE001
        for idx in s.index_list:
            out.append({"index": idx, "last_seen_ms": None,
                        "staleness_s": None, "status": "idle",
                        "detail": f"redis down: {e}"})
        return out
    for idx in s.index_list:
        try:
            last = store.last_seen_ms(idx)
        except Exception:  # noqa: BLE001
            last = None
        if last is None:
            out.append({"index": idx, "last_seen_ms": None,
                        "staleness_s": None, "status": "idle"})
            continue
        age_s = (now_ms - int(last)) / 1000.0
        if market_open and age_s >= crit_s:
            status = "crit"
        elif market_open and age_s >= warn_s:
            status = "warn"
        else:
            status = "ok"
        out.append({"index": idx, "last_seen_ms": int(last),
                    "staleness_s": round(age_s, 1), "status": status})
    return out


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _read_metrics() -> dict:
    try:
        r = get_redis()
        raw = r.hgetall("tpp:metrics") or {}
        ts = r.get("tpp:metrics:ts")
    except Exception as e:  # noqa: BLE001
        log.warning("health_metrics_read_failed", error=str(e))
        return {"updated_ms": None}
    out: dict = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        val = v.decode() if isinstance(v, bytes) else str(v)
        try:
            out[key] = float(val) if "." in val else int(val)
        except ValueError:
            out[key] = val
    out["updated_ms"] = int(ts) if ts else None
    return out


def evaluate_health() -> dict:
    s = get_settings()
    now_ms = _now_ms()
    market_open = _is_market_hours_ist()

    metrics = _read_metrics()

    checks = [
        _check_redis(),
        _check_postgres(),
        _check_wal_dir(),
        _check_metrics_freshness(metrics, now_ms, s.health_metrics_stale_after_s),
        _check_latency(metrics, s.health_warn_latency_p95_ms, s.health_crit_latency_p95_ms),
        _threshold_check("gaps", metrics, "gap_count",
                         s.health_warn_gaps, s.health_crit_gaps),
        _threshold_check("reconnects", metrics, "reconnect_count",
                         s.health_warn_reconnects, s.health_crit_reconnects),
    ]

    indices = _index_staleness(
        now_ms, market_open,
        s.health_warn_staleness_s, s.health_crit_staleness_s,
    )

    alerts: list[dict] = []
    for c in checks:
        if c["status"] in ("warn", "crit"):
            alerts.append({"rule": c["name"], "severity": c["status"],
                           "detail": c["detail"], "since_ms": now_ms})
    for idx in indices:
        if idx["status"] in ("warn", "crit"):
            alerts.append({
                "rule": f"index_stale_{idx['index']}",
                "severity": idx["status"],
                "detail": (f"{idx['index']} last tick {idx['staleness_s']}s ago "
                           "(market open)"),
                "since_ms": now_ms,
            })

    max_sev = 0
    for c in checks:
        max_sev = max(max_sev, _SEV_RANK.get(c["status"], 0))
    for idx in indices:
        max_sev = max(max_sev, _SEV_RANK.get(idx["status"], 0))
    overall = "healthy" if max_sev == 0 else ("degraded" if max_sev == 1 else "down")

    return {
        "status": overall,
        "generated_ms": now_ms,
        "market_open": market_open,
        "metrics_updated_ms": metrics.get("updated_ms"),
        "checks": checks,
        "indices": indices,
        "alerts": alerts,
        "metrics": metrics,
    }
