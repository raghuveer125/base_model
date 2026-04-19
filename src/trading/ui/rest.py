"""REST endpoints for the UI — read-only views over Redis + Postgres."""

from __future__ import annotations

from datetime import datetime

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


@router.get("/health")
def health() -> dict:
    return {"ok": True}


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
