"""REST endpoints for the UI — read-only views over Redis + Postgres + replay artifacts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException, Query

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.schemas import Index
from trading.storage import LiveStore, get_pg_pool

log = get_logger(__name__)
router = APIRouter()


def _check_index(index: str) -> None:
    if index not in Index._value2member_map_:
        raise HTTPException(status_code=404, detail=f"unknown index: {index}")


@router.get("/healthz")
def healthz() -> dict:
    """Lightweight liveness probe for load balancers — does not touch deps."""
    return {"ok": True}


@router.get("/health")
def health() -> dict:
    """Full health report with checks, indices, and active alerts.

    See trading.ui.health.evaluate_health for the schema.
    """
    from trading.ui.health import evaluate_health
    return evaluate_health()


@router.post("/notifications/test")
def notifications_test() -> dict:
    """Dispatch a canned test payload to every configured notification sink."""
    from trading.ui.notifier import AlertNotifier
    return AlertNotifier().tick_test()


@router.get("/indices")
def indices() -> list[str]:
    return get_settings().index_list


@router.get("/state/{index}")
def state(index: str) -> dict:
    _check_index(index)
    store = LiveStore()
    tick_raw = store.r.get(f"tpp:tick:idx:{index}")
    atm_raw = store.r.get(f"tpp:atm:{index}")
    return {
        "index": index,
        "spot": store.get_spot(index),
        "atm": int(atm_raw) if atm_raw else None,
        "last_seen_ms": store.last_seen_ms(index),
        "latest_tick": orjson.loads(tick_raw) if tick_raw else None,
    }


@router.get("/chain/{index}")
def chain(
    index: str,
    expiry: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    _check_index(index)
    store = LiveStore()
    raw = store.get_chain(index, expiry)
    result: dict[int, dict] = {}
    for field, tick in raw.items():
        try:
            strike_s, ot = field.split(":")
            strike = int(strike_s)
        except ValueError:
            continue
        if ot not in ("CE", "PE"):
            continue
        greeks = store.get_greeks(index, expiry, strike, ot)
        row = result.setdefault(strike, {})
        row[ot] = {"tick": tick, "greeks": greeks}
    return {"index": index, "expiry": expiry, "strikes": result}


@router.get("/candles/{index}")
def candles(
    index: str,
    timeframe: str = Query("1m", pattern=r"^(1m|5m|15m)$"),
    limit: int = Query(100, ge=1, le=2000),
) -> list[dict]:
    _check_index(index)
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT open_ts, close_ts, open, high, low, close, volume, tick_count "
            "FROM index_candles "
            "WHERE index = %s AND timeframe = %s "
            "ORDER BY open_ts DESC "
            "LIMIT %s",
            (index, timeframe, limit),
        )
        rows = cur.fetchall()
    out = [
        {
            "open_ts": _iso(r[0]),
            "close_ts": _iso(r[1]),
            "open": float(r[2]),
            "high": float(r[3]),
            "low": float(r[4]),
            "close": float(r[5]),
            "volume": int(r[6] or 0),
            "tick_count": int(r[7] or 0),
        }
        for r in rows
    ]
    out.reverse()
    return out


@router.get("/signals")
def signals(
    strategy: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
) -> list[dict]:
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        if strategy:
            cur.execute(
                "SELECT strategy, index, action, instrument, reason, confidence, "
                "metadata, ts_signal, ts_ingest "
                "FROM signals WHERE strategy = %s "
                "ORDER BY ts_ingest DESC LIMIT %s",
                (strategy, limit),
            )
        else:
            cur.execute(
                "SELECT strategy, index, action, instrument, reason, confidence, "
                "metadata, ts_signal, ts_ingest "
                "FROM signals "
                "ORDER BY ts_ingest DESC LIMIT %s",
                (limit,),
            )
        rows = cur.fetchall()
    return [
        {
            "strategy": r[0], "index": r[1], "action": r[2], "instrument": r[3],
            "reason": r[4], "confidence": float(r[5] or 0),
            "metadata": r[6], "ts_signal": int(r[7]),
            "ts_ingest": _iso(r[8]),
        }
        for r in rows
    ]


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@router.get("/metrics")
def metrics() -> dict:
    """Snapshot of the live in-process metrics hash published by MetricsPublisher.

    Keys match the ReplaySummary + live counters: tick_rate_per_s,
    ingest_latency_p50_ms/p95_ms/max_ms, gap_count, reconnect_count, dedup_drops,
    wal_appends, pg_flushes, pg_rows_flushed, candles_closed, greeks_computed,
    greeks_skipped, signals_emitted, signals_suppressed_cooldown,
    signals_suppressed_risk, ticks_total.
    """
    store = LiveStore()
    hash_data = store.r.hgetall("tpp:metrics") or {}
    ts_raw = store.r.get("tpp:metrics:ts")
    out: dict = {}
    for k, v in hash_data.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        val = v.decode() if isinstance(v, bytes) else str(v)
        out[key] = _coerce_number(val)
    out["updated_ms"] = int(ts_raw) if ts_raw else None
    return out


def _coerce_number(v: str) -> int | float | str:
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


# ---------------------------------------------------------------------------
# Replay artifacts
# ---------------------------------------------------------------------------

_SAFE_RUN_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _is_safe_run_id(run_id: str) -> bool:
    return bool(run_id) and len(run_id) <= 64 and all(c in _SAFE_RUN_ID_CHARS for c in run_id)


def _replay_root() -> Path:
    return get_settings().log_dir / "replays"


def _replay_dir(run_id: str) -> Path:
    if not _is_safe_run_id(run_id):
        raise HTTPException(status_code=400, detail="invalid run_id")
    d = _replay_root() / run_id
    if not d.exists() or not d.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return d


def _load_json(path: Path) -> dict:
    try:
        return orjson.loads(path.read_bytes())
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/replays")
def list_replays() -> list[dict]:
    root = _replay_root()
    if not root.exists():
        return []
    runs: list[dict] = []
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        manifest_p = d / "manifest.json"
        summary_p = d / "summary.json"
        if not manifest_p.exists():
            continue
        try:
            manifest = orjson.loads(manifest_p.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        summary: dict = {}
        if summary_p.exists():
            try:
                summary = orjson.loads(summary_p.read_bytes())
            except Exception:  # noqa: BLE001
                pass
        runs.append({
            "run_id": d.name,
            "mtime_ms": int(d.stat().st_mtime * 1000),
            "manifest": manifest,
            "headline": {
                "signals_emitted": summary.get("signals_emitted"),
                "signals_suppressed_cooldown": summary.get("signals_suppressed_cooldown"),
                "signals_suppressed_risk": summary.get("signals_suppressed_risk"),
                "records_read": summary.get("records_read"),
                "ts_range_ms": summary.get("ts_range_ms"),
                "wall_seconds": summary.get("wall_seconds"),
            },
        })
    return runs


@router.get("/replays/{run_id}/summary")
def replay_summary(run_id: str) -> dict:
    return _load_json(_replay_dir(run_id) / "summary.json")


@router.get("/replays/{run_id}/manifest")
def replay_manifest(run_id: str) -> dict:
    return _load_json(_replay_dir(run_id) / "manifest.json")


@router.get("/replays/{run_id}/signals")
def replay_signals(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> list[dict]:
    path = _replay_dir(run_id) / "signals.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, "rb") as f:
        for i, raw in enumerate(f):
            if i < offset:
                continue
            if i >= offset + limit:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(orjson.loads(raw))
            except orjson.JSONDecodeError:
                continue
    return out
