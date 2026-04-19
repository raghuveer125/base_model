"""Event sources for the replay engine.

A source is an iterable of `ReplayEvent` ordered by `(ts, seq, kind)`.
`seq` is a monotonic integer used to break ties deterministically within a ts.

Each source exposes `fingerprint()` — a short stable hash of what it will yield —
used by the engine to derive a deterministic run-id.
"""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from trading.adapter import normalize_index_tick, normalize_option_tick
from trading.logging_setup import get_logger
from trading.schemas import FYERS_INDEX_SYMBOL
from trading.wal import WALReader

log = get_logger(__name__)


class EventKind(StrEnum):
    INDEX_TICK = "index_tick"
    OPTION_TICK = "option_tick"
    CANDLE = "candle"            # pre-computed candle (PG source); engine skips re-aggregation


@dataclass(frozen=True)
class ReplayEvent:
    kind: EventKind
    ts: int          # epoch ms
    seq: int         # tie-breaker within same ts
    payload: dict


class EventSource(Protocol):
    def iter_events(self) -> Iterator[ReplayEvent]: ...
    def fingerprint(self) -> str: ...
    def description(self) -> str: ...


class WALEventSource:
    """Read WAL jsonl segments and emit INDEX_TICK / OPTION_TICK events in seq order."""

    def __init__(self, wal_dir: Path | None = None, date: str | None = None) -> None:
        self.reader = WALReader(wal_dir) if wal_dir is not None else WALReader()
        self.wal_dir = self.reader.wal_dir
        self.date = date

    def iter_events(self) -> Iterator[ReplayEvent]:
        for rec in self.reader.iter_records(date=self.date):
            if rec.get("kind") != "raw_tick":
                continue
            payload = rec.get("data") or {}
            sym = payload.get("symbol") or payload.get("sym") or ""
            seq = int(rec.get("seq") or 0)
            if sym in FYERS_INDEX_SYMBOL.values():
                tick = normalize_index_tick(payload)
                if tick is None:
                    continue
                yield ReplayEvent(
                    EventKind.INDEX_TICK, tick.ts_exchange, seq,
                    tick.model_dump(mode="json"),
                )
            else:
                tick = normalize_option_tick(payload)
                if tick is None:
                    continue
                yield ReplayEvent(
                    EventKind.OPTION_TICK, tick.ts_exchange, seq,
                    tick.model_dump(mode="json"),
                )

    def fingerprint(self) -> str:
        h = hashlib.sha256(b"wal|")
        if self.date:
            h.update(self.date.encode())
            h.update(b"|")
        for seg in self.reader.list_segments(date=self.date):
            try:
                st = seg.stat()
                h.update(seg.name.encode())
                h.update(b":")
                h.update(str(st.st_size).encode())
                h.update(b";")
            except FileNotFoundError:
                continue
        return h.hexdigest()[:16]

    def description(self) -> str:
        return f"wal({self.wal_dir}, date={self.date or 'all'})"


class PostgresEventSource:
    """Read index_ticks + option_chain_data + index_candles from Postgres.

    Historical Greeks are not persisted; the ReplayEngine synthesizes them from
    option ticks exactly as the live engine does.
    """

    def __init__(
        self,
        start_ts_ms: int,
        end_ts_ms: int,
        *,
        include_ticks: bool = True,
        include_candles: bool = True,
    ) -> None:
        if end_ts_ms < start_ts_ms:
            raise ValueError(f"end_ts_ms ({end_ts_ms}) < start_ts_ms ({start_ts_ms})")
        self.start_ts_ms = int(start_ts_ms)
        self.end_ts_ms = int(end_ts_ms)
        self.include_ticks = include_ticks
        self.include_candles = include_candles

    def iter_events(self) -> Iterator[ReplayEvent]:
        from trading.storage import get_pg_pool  # lazy import
        events: list[ReplayEvent] = []
        pool = get_pg_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            if self.include_ticks:
                cur.execute(
                    "SELECT id, index, ltp, ts_exchange, ts_received "
                    "FROM index_ticks "
                    "WHERE ts_exchange BETWEEN %s AND %s "
                    "ORDER BY ts_exchange, id",
                    (self.start_ts_ms, self.end_ts_ms),
                )
                for _id, index, ltp, ts_ex, ts_rx in cur.fetchall():
                    events.append(ReplayEvent(
                        EventKind.INDEX_TICK, int(ts_ex), int(_id),
                        {"index": index, "ltp": float(ltp),
                         "ts_exchange": int(ts_ex), "ts_received": int(ts_rx)},
                    ))
                cur.execute(
                    "SELECT id, index, strike, option_type, expiry, ltp, oi, oi_change, iv, "
                    "ts_exchange, ts_received "
                    "FROM option_chain_data "
                    "WHERE ts_exchange BETWEEN %s AND %s "
                    "ORDER BY ts_exchange, id",
                    (self.start_ts_ms, self.end_ts_ms),
                )
                for (_id, index, strike, ot, expiry, ltp, oi, oich, iv,
                     ts_ex, ts_rx) in cur.fetchall():
                    events.append(ReplayEvent(
                        EventKind.OPTION_TICK, int(ts_ex), int(_id),
                        {
                            "index": index, "strike": int(strike), "option_type": ot,
                            "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry),
                            "ltp": float(ltp), "oi": int(oi or 0),
                            "oi_change": int(oich or 0),
                            "iv": float(iv) if iv is not None else None,
                            "ts_exchange": int(ts_ex), "ts_received": int(ts_rx),
                        },
                    ))
            if self.include_candles:
                cur.execute(
                    "SELECT id, index, timeframe, "
                    "(extract(epoch from open_ts) * 1000)::BIGINT, "
                    "(extract(epoch from close_ts) * 1000)::BIGINT, "
                    "open, high, low, close, volume, tick_count "
                    "FROM index_candles "
                    "WHERE open_ts >= to_timestamp(%s / 1000.0) "
                    "  AND open_ts <  to_timestamp(%s / 1000.0) "
                    "ORDER BY open_ts, id",
                    (self.start_ts_ms, self.end_ts_ms),
                )
                for (_id, index, tf, open_ms, close_ms,
                     o, h, l, c, vol, tc) in cur.fetchall():
                    events.append(ReplayEvent(
                        EventKind.CANDLE, int(close_ms), int(_id),
                        {
                            "index": index, "timeframe": tf,
                            "open_ts": int(open_ms), "close_ts": int(close_ms),
                            "open": float(o), "high": float(h), "low": float(l),
                            "close": float(c),
                            "volume": int(vol or 0), "tick_count": int(tc or 0),
                        },
                    ))
        events.sort(key=lambda e: (e.ts, e.seq, e.kind.value))
        return iter(events)

    def fingerprint(self) -> str:
        h = hashlib.sha256(b"pg|")
        h.update(f"{self.start_ts_ms}:{self.end_ts_ms}".encode())
        h.update(f"|ticks={self.include_ticks}|candles={self.include_candles}".encode())
        return h.hexdigest()[:16]

    def description(self) -> str:
        return (f"pg(range_ms=[{self.start_ts_ms},{self.end_ts_ms}], "
                f"ticks={self.include_ticks}, candles={self.include_candles})")


class MergedEventSource:
    """Merge multiple ordered sources by (ts, seq, kind) into one stream."""

    def __init__(self, sources: Iterable[EventSource]) -> None:
        self.sources = list(sources)
        if not self.sources:
            raise ValueError("MergedEventSource requires at least one source")

    def iter_events(self) -> Iterator[ReplayEvent]:
        def _key(e: ReplayEvent) -> tuple[int, int, str]:
            return (e.ts, e.seq, e.kind.value)
        yield from heapq.merge(*(s.iter_events() for s in self.sources), key=_key)

    def fingerprint(self) -> str:
        h = hashlib.sha256(b"merge|")
        for s in self.sources:
            h.update(s.fingerprint().encode())
            h.update(b"+")
        return h.hexdigest()[:16]

    def description(self) -> str:
        return "merge(" + ",".join(s.description() for s in self.sources) + ")"
