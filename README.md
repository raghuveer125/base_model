# Trading Plug&Play

Modular real-time options analytics engine for **Nifty50**, **BankNifty**, **Sensex** via Fyers.
Phase 1 (data pipeline) and Phase 2 (candle engine) are complete; later phases add Greeks, strategies, and UI.

## Design principles (non-negotiable)

1. Data correctness > speed.
2. Every module is independently replaceable and independently restartable.
3. Redis = live truth. Postgres = 2-day rolling history. No Kafka.
4. WAL first, always. No tick is processed before it hits disk.
5. All inter-module communication goes through Redis pub/sub — no direct imports between runtime processes.

## Architecture

```
Fyers WS ──► ingest.Orchestrator ──► WAL (jsonl on disk, fsync'd)
                 │
                 ├──► Dedup (symbol, ts_exchange)
                 ├──► GapDetector  (per-symbol gap > threshold alert)
                 ├──► FreezeDetector (no tick for N s during market hours alert)
                 │
                 ├──► adapter.normalize_* ──► Pydantic IndexTick / OptionTick
                 │                               │
                 │                               ├──► LiveStore  (Redis)
                 │                               ├──► EventBus   (Redis pub/sub)
                 │                               └──► BatchBuffer ──► Postgres
                 │
                 └──► reconnect loop (exp backoff, capped)

Redis pub/sub: ticks.index.*
                 │
                 ▼
            candles.CandleEngine (separate process)
                 │
                 ├──► CandleAggregator per (index, timeframe)   {1m, 5m, 15m}
                 ├──► bucket = floor(ts_exchange, step) — exchange-time aligned
                 ├──► in-progress state persisted to Redis every tick
                 ├──► closer thread fires buckets whose end has passed
                 │
                 └──► on bucket close:
                       ├──► INSERT into index_candles (UPSERT on conflict)
                       ├──► publish candles.<IDX>.<TF>
                       └──► cache tpp:candle:last_close:<IDX>:<TF>
```

## Project layout

```
src/trading/
  config.py              pydantic settings loaded from .env
  logging_setup.py       structlog, JSON or console
  schemas.py             IndexTick / OptionTick / OptionChainSnapshot + constants
  events.py              EventBus (Redis pub/sub) + channel names
  storage.py             LiveStore (Redis) + Postgres pool + retention
  storage_schema.sql     DDL for index_ticks / option_chain_data / index_candles
  wal.py                 WALWriter + WALReader (durable, replayable)
  auth.py                Fyers TOTP auto-login + token cache
  adapter.py             normalize raw Fyers payloads into canonical models
  ingest.py              Orchestrator: WS → WAL → processors → Redis + PG + bus
  candles.py             CandleAggregator + CandleEngine (subscribes ticks.index.*)
  metrics.py             in-process counters + Redis snapshot
  scripts/
    auth_bootstrap.py    `tpp-auth`
    init_db.py           `tpp-init-db`
    run_ingest.py        `tpp-ingest`
    run_retention.py     `tpp-retention`
    replay_wal.py        `tpp-replay-wal`
    run_candles.py       `tpp-candles`
tests/
  test_adapter.py
  test_wal_and_ingest_helpers.py
docker-compose.yml       Redis 7 + Postgres 16
pyproject.toml
.env.example
```

## Quickstart

```bash
# 1. Clone & create env
cp .env.example .env
# edit .env with your real Fyers creds

# 2. Infra
docker compose up -d

# 3. Python env
python -m venv .venv
.venv/Scripts/activate            # Windows
# source .venv/bin/activate       # macOS/Linux
pip install -e ".[dev]"

# 4. Initialise DB + auth
tpp-init-db
tpp-auth

# 5. Run ingest (market hours, 09:15–15:30 IST)
tpp-ingest \
  --expiry NIFTY50=2026-04-30 \
  --expiry BANKNIFTY=2026-04-30 \
  --expiry SENSEX=2026-04-25

# 6. Run candle engine in a separate shell (independent process)
tpp-candles

# 7. Daily maintenance
tpp-retention

# 8. Replay from WAL (after outage / dry rebuild)
tpp-replay-wal --date 2026-04-19
```

## Redis key schema

| key                                              | type   | value                          |
|--------------------------------------------------|--------|--------------------------------|
| `tpp:tick:idx:{INDEX}`                           | string | latest `IndexTick` JSON        |
| `tpp:tick:opt:{INDEX}:{EXPIRY}:{STRIKE}:{TYPE}`  | string | latest `OptionTick` JSON       |
| `tpp:chain:{INDEX}:{EXPIRY}`                     | hash   | field `{STRIKE}:{TYPE}` → JSON |
| `tpp:atm:{INDEX}`                                | string | current ATM strike (int)       |
| `tpp:spot:{INDEX}`                               | string | latest spot (float)            |
| `tpp:last_seen:{INDEX}`                          | string | epoch ms of most recent tick   |
| `tpp:token:fyers`                                | string | access-token JSON              |
| `tpp:candle:in_progress:{INDEX}:{TF}`            | string | in-progress aggregator state   |
| `tpp:candle:last_close:{INDEX}:{TF}`             | string | last closed `IndexCandle` JSON |

`{EXPIRY}` = ISO `YYYY-MM-DD`. `{TYPE}` = `CE` or `PE`.

## Postgres schema

See `src/trading/storage_schema.sql`.
Three tables: `index_ticks`, `option_chain_data`, `index_candles` (populated in Phase 2).
Retention enforced by `tpp-retention` (row purge, bounded to these three tables).

## Metrics

Published to Redis every 5 seconds while `tpp-ingest` is running. Read with:

```bash
docker exec tpp-redis redis-cli hgetall tpp:metrics
docker exec tpp-redis redis-cli get tpp:metrics:ts
```

| field                    | meaning                                       |
|--------------------------|-----------------------------------------------|
| `ticks_total`            | cumulative tick count                         |
| `tick_rate_per_s`        | rolling-window rate (2000-sample window)      |
| `ingest_latency_p50_ms`  | p50 of `ts_received - ts_exchange`            |
| `ingest_latency_p95_ms`  | p95 latency                                   |
| `ingest_latency_max_ms`  | max latency in window                         |
| `gap_count`              | GapDetector firings                           |
| `reconnect_count`        | WS reconnect attempts                         |
| `dedup_drops`            | duplicate ticks dropped                       |
| `wal_appends`            | WAL records written                           |
| `pg_flushes`             | BatchBuffer flushes                           |
| `pg_rows_flushed`        | rows inserted into Postgres                   |

The same snapshot is also logged at INFO every 5s as `metrics_snapshot` events.

## Pub/sub channels

| pattern                     | payload                               |
|-----------------------------|---------------------------------------|
| `ticks.index.<IDX>`         | `IndexTick` JSON                      |
| `ticks.option.<IDX>`        | `OptionTick` JSON                     |
| `option_chain.<IDX>`        | `OptionChainSnapshot` JSON (future)   |
| `candles.<IDX>.<TF>`        | `IndexCandle` JSON on bucket close    |

## WAL format

Newline-delimited JSON, one record per line:

```json
{"seq": 42, "ts_ns": 1745030401234567800, "kind": "raw_tick", "data": { /* raw Fyers payload */ }}
```

Files: `{WAL_DIR}/{YYYY-MM-DD}.jsonl`, rotated to `{YYYY-MM-DD}.N.jsonl` on size cap (`WAL_MAX_FILE_MB`).
`fsync` every `WAL_FSYNC_EVERY_N` records and on rotation/close. Sequence counter persisted to `{WAL_DIR}/.seq`.

## Reliability behaviours

- **Reconnect:** exponential backoff (1 → 2 → 4 → … → `WS_RECONNECT_MAX_BACKOFF_S`), reset on clean connect.
- **Freeze detection:** background thread alerts when no tick for `WS_FREEZE_THRESHOLD_S` during market hours.
- **Gap detection:** per-symbol `ts_exchange` delta — logged if > `WS_GAP_MAX_SECONDS`.
- **Dedup:** LRU of `(symbol, ts_exchange)`; silent drop on repeat.
- **WAL first:** every raw message is written to disk *before* normalization, so a processor crash is fully recoverable via `tpp-replay-wal`.
- **Graceful shutdown:** `SIGINT`/`SIGTERM` flush batch buffers, `fsync` WAL, close pool + bus.

## Testing

```bash
pytest -q
```

Adapter, WAL, Dedup, GapDetector are covered. Postgres/Redis integration tests live in Phase 2.

## Phase 2 — Candle engine

Independent process (`tpp-candles`). Subscribes to Redis pub/sub `ticks.index.*`
and does **not** touch the ingestion pipeline. Core guarantees:

- **Exchange-time aligned buckets.** Bucket start = `floor(ts_exchange_ms / step_ms) * step_ms`.
  We never rely on wall-clock for alignment — only for the closer sweep.
- **Restart-safe.** Every tick flushes the in-progress aggregator state to Redis
  (`tpp:candle:in_progress:{IDX}:{TF}`). On startup the engine rehydrates; if the bucket's
  `close_ts` is already in the past, it's closed and emitted immediately.
- **Stale-close sweep.** A 1-second closer thread emits buckets whose end has passed
  even if no new tick has arrived (low-volume symbols, market close).
- **Out-of-order drop.** Ticks for a past bucket are logged + discarded — closed candles
  are immutable.
- **PG idempotent upsert.** `ON CONFLICT (index, timeframe, open_ts) DO UPDATE` — replays
  or reconnects don't duplicate rows.
- **Emits `candle_close`** as Redis pub/sub on `candles.<IDX>.<TF>` for downstream strategies.

## What is not built yet

- Greeks (Phase 3).
- Strategy framework (Phase 5).
- Web UI (Phase 6).

Each plugs into the existing `EventBus` without changes to earlier phases.
