"""Redis + Postgres storage layer. Redis = live truth; Postgres = rolling history."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Sequence

import orjson
import psycopg
import redis
from psycopg_pool import ConnectionPool
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.orders.base import Fill, Order
from trading.schemas import IndexCandle, IndexTick, OptionGreeks, OptionTick, Signal

log = get_logger(__name__)
SCHEMA_FILE = Path(__file__).with_name("storage_schema.sql")

_redis_lock = threading.Lock()
_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is None:
            _redis_client = redis.Redis.from_url(
                get_settings().redis_url, decode_responses=False,
                socket_keepalive=True, socket_connect_timeout=5, socket_timeout=5,
                retry_on_timeout=True, health_check_interval=15,
            )
            _redis_client.ping()
            log.info("redis_connected", url=get_settings().redis_url)
    return _redis_client


class LiveStore:
    """Redis key schema (all prefixed tpp:).

    tick:idx:{INDEX}                                 IndexTick JSON
    tick:opt:{INDEX}:{EXPIRY}:{STRIKE}:{TYPE}        OptionTick JSON
    chain:{INDEX}:{EXPIRY}                           hash, field={STRIKE}:{TYPE}
    atm:{INDEX}                                      int
    spot:{INDEX}                                     float
    last_seen:{INDEX}                                epoch ms
    token:fyers                                      token JSON
    """

    def __init__(self, client: redis.Redis | None = None) -> None:
        self.r = client or get_redis()

    def set_index_tick(self, tick: IndexTick) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.set(f"tpp:tick:idx:{tick.index}", orjson.dumps(tick.model_dump(mode="json")))
        pipe.set(f"tpp:spot:{tick.index}", tick.ltp)
        pipe.set(f"tpp:last_seen:{tick.index}", tick.ts_received)
        pipe.execute()

    def get_spot(self, index: str) -> float | None:
        raw = self.r.get(f"tpp:spot:{index}")
        return float(raw) if raw is not None else None

    def last_seen_ms(self, index: str) -> int | None:
        raw = self.r.get(f"tpp:last_seen:{index}")
        return int(raw) if raw is not None else None

    def set_option_tick(self, tick: OptionTick) -> None:
        expiry = tick.expiry.isoformat()
        key_latest = f"tpp:tick:opt:{tick.index}:{expiry}:{tick.strike}:{tick.option_type}"
        key_chain = f"tpp:chain:{tick.index}:{expiry}"
        field = f"{tick.strike}:{tick.option_type}"
        body = orjson.dumps(tick.model_dump(mode="json"))
        pipe = self.r.pipeline(transaction=False)
        pipe.set(key_latest, body)
        pipe.hset(key_chain, field, body)
        pipe.execute()

    def set_atm(self, index: str, atm: int) -> None:
        self.r.set(f"tpp:atm:{index}", atm)

    def get_chain(self, index: str, expiry: str) -> dict[str, dict]:
        raw = self.r.hgetall(f"tpp:chain:{index}:{expiry}")
        return {k.decode(): orjson.loads(v) for k, v in raw.items()}

    def set_token(self, token: dict) -> None:
        self.r.set("tpp:token:fyers", orjson.dumps(token))

    def get_token(self) -> dict | None:
        raw = self.r.get("tpp:token:fyers")
        return orjson.loads(raw) if raw else None

    # ---- candle helpers ----

    def set_in_progress_candle(self, index: str, timeframe: str, state: dict) -> None:
        """Persist in-progress candle aggregator state for restart recovery."""
        self.r.set(f"tpp:candle:in_progress:{index}:{timeframe}", orjson.dumps(state))

    def get_in_progress_candle(self, index: str, timeframe: str) -> dict | None:
        raw = self.r.get(f"tpp:candle:in_progress:{index}:{timeframe}")
        return orjson.loads(raw) if raw else None

    def clear_in_progress_candle(self, index: str, timeframe: str) -> None:
        self.r.delete(f"tpp:candle:in_progress:{index}:{timeframe}")

    def set_last_close_candle(self, candle: IndexCandle) -> None:
        self.r.set(
            f"tpp:candle:last_close:{candle.index}:{candle.timeframe}",
            orjson.dumps(candle.model_dump(mode="json")),
        )

    def get_last_close_candle(self, index: str, timeframe: str) -> dict | None:
        raw = self.r.get(f"tpp:candle:last_close:{index}:{timeframe}")
        return orjson.loads(raw) if raw else None

    # ---- greeks helpers ----

    def set_greeks(self, g: OptionGreeks) -> None:
        key = (f"tpp:greeks:{g.index}:{g.expiry.isoformat()}:"
               f"{g.strike}:{g.option_type}")
        self.r.set(key, orjson.dumps(g.model_dump(mode="json")))

    def get_greeks(
        self, index: str, expiry: str, strike: int, option_type: str,
    ) -> dict | None:
        key = f"tpp:greeks:{index}:{expiry}:{strike}:{option_type}"
        raw = self.r.get(key)
        return orjson.loads(raw) if raw else None


_pg_lock = threading.Lock()
_pg_pool: ConnectionPool | None = None


def get_pg_pool() -> ConnectionPool:
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_lock:
        if _pg_pool is None:
            _pg_pool = ConnectionPool(
                conninfo=get_settings().postgres_dsn, min_size=1, max_size=8,
                kwargs={"autocommit": False}, open=True,
            )
            log.info("pg_pool_opened", dsn=_sanitized_dsn())
    return _pg_pool


def _sanitized_dsn() -> str:
    import re
    return re.sub(r":(?:[^@/]+)@", ":***@", get_settings().postgres_dsn)


def ensure_schema() -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    log.info("pg_schema_ensured")


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2),
       retry=retry_if_exception_type(psycopg.OperationalError))
def insert_index_ticks(rows: Sequence[IndexTick]) -> int:
    if not rows:
        return 0
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO index_ticks (index, ltp, ts_exchange, ts_received) VALUES (%s,%s,%s,%s)",
            [(t.index, t.ltp, t.ts_exchange, t.ts_received) for t in rows],
        )
        conn.commit()
    return len(rows)


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2),
       retry=retry_if_exception_type(psycopg.OperationalError))
def insert_option_ticks(rows: Sequence[OptionTick]) -> int:
    if not rows:
        return 0
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO option_chain_data "
            "(index, strike, option_type, expiry, ltp, oi, oi_change, iv, ts_exchange, ts_received) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(t.index, t.strike, t.option_type, t.expiry, t.ltp, t.oi, t.oi_change,
              t.iv, t.ts_exchange, t.ts_received) for t in rows],
        )
        conn.commit()
    return len(rows)


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2),
       retry=retry_if_exception_type(psycopg.OperationalError))
def insert_candles(rows: Sequence[IndexCandle]) -> int:
    """Batch-insert candles. Idempotent via UNIQUE (index, timeframe, open_ts)."""
    if not rows:
        return 0
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO index_candles "
            "(index, timeframe, open_ts, close_ts, open, high, low, close, volume, tick_count) "
            "VALUES (%s,%s, to_timestamp(%s / 1000.0), to_timestamp(%s / 1000.0), "
            "%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (index, timeframe, open_ts) DO UPDATE SET "
            "close_ts = EXCLUDED.close_ts, open = EXCLUDED.open, high = EXCLUDED.high, "
            "low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume, "
            "tick_count = EXCLUDED.tick_count",
            [
                (c.index, c.timeframe, c.open_ts, c.close_ts,
                 c.open, c.high, c.low, c.close, c.volume, c.tick_count)
                for c in rows
            ],
        )
        conn.commit()
    return len(rows)


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2),
       retry=retry_if_exception_type(psycopg.OperationalError))
def insert_signals(rows: Sequence[Signal]) -> int:
    if not rows:
        return 0
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO signals "
            "(strategy, index, action, instrument, reason, confidence, metadata, ts_signal) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            [
                (s.strategy, s.index, s.action, s.instrument, s.reason,
                 s.confidence, orjson.dumps(s.metadata).decode(), s.ts)
                for s in rows
            ],
        )
        conn.commit()
    return len(rows)


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2),
       retry=retry_if_exception_type(psycopg.OperationalError))
def insert_order(order: Order, *, status: str, reject_reason: str = "") -> int:
    if status not in ("filled", "rejected"):
        raise ValueError(f"bad status: {status}")
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orders (signal_ts, strategy, index, instrument, action, qty, "
            "ref_price, status, reject_reason, signal_reason, signal_confidence, ordered_ts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (order.signal_ts, order.strategy, order.index, order.instrument,
             order.action, order.qty, order.ref_price, status, reject_reason,
             order.signal_reason, order.signal_confidence, order.ordered_ts),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row[0]) if row else 0


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2),
       retry=retry_if_exception_type(psycopg.OperationalError))
def insert_fill(fill: Fill) -> int:
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fills (order_id, strategy, index, instrument, side, qty, "
            "fill_price, fees, slippage_bps, ts_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (fill.order_id, fill.strategy, fill.index, fill.instrument,
             fill.side, fill.qty, fill.fill_price, fill.fees,
             fill.slippage_bps, fill.ts_ms),
        )
        conn.commit()
    return 1


def enforce_retention(days: int | None = None) -> dict[str, int]:
    """Remove rows older than `days` days. Single-tenant; bounded to known tables."""
    d = days or get_settings().retention_days
    purged: dict[str, int] = {}
    stmt = "DELETE FROM {tbl} WHERE {col} < now() - interval '{n} days'"
    with get_pg_pool().connection() as conn, conn.cursor() as cur:
        for table, ts_col in (
            ("index_ticks", "ts"),
            ("option_chain_data", "ts"),
            ("index_candles", "open_ts"),
            ("signals", "ts_ingest"),
            ("orders", "ordered_at"),
            ("fills", "filled_at"),
        ):
            cur.execute(stmt.format(tbl=table, col=ts_col, n=d))
            purged[table] = cur.rowcount or 0
        conn.commit()
    log.info("retention_enforced", days=d, purged=purged)
    return purged
