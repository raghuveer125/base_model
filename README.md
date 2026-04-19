# Trading Plug&Play

Modular real-time options analytics engine for **Nifty50**, **BankNifty**, **Sensex** via Fyers.
Phases 1 (data pipeline), 2 (candles), 3 (Greeks), 4 (strategy framework) and 5 (backtest + replay) are complete; UI is next.

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

Redis pub/sub: ticks.index.* + ticks.option.*
                 │
                 ▼
            greeks.GreeksEngine (separate process)
                 │
                 ├──► on option tick → compute Greeks for that contract
                 ├──► on index tick → throttled chain-wide recompute (spot move ≥ N pts)
                 ├──► ATM ± range filter (per-index strike step)
                 ├──► Black-Scholes with bucketized lru_cache (4K entries)
                 │
                 └──► emits:
                       ├──► tpp:greeks:<IDX>:<EXPIRY>:<STRIKE>:<TYPE>
                       └──► publish greeks.<IDX>

Redis pub/sub: ticks.* + candles.* + greeks.*
                 │
                 ▼
            strategies.StrategyEngine (separate process)
                 │
                 ├──► fanout to each registered Strategy (on_tick / on_candle_close / on_greeks)
                 ├──► Strategy.emit(…) → CooldownManager (per strategy+instrument)
                 │                     → RiskEngine     (hour/day caps, index+action allowlist)
                 │                     → SignalLogger
                 │
                 └──► SignalLogger:
                       ├──► append signals.jsonl (fsync)
                       ├──► INSERT into signals table
                       └──► publish signals.<STRATEGY>
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
  greeks.py              Black-Scholes + GreeksEngine (subscribes ticks.*)
  backtest.py            BacktestRunner (legacy shim → ReplayEngine)
  replay/
    sources.py           EventSource protocol + WAL / Postgres / Merged sources
    engine.py            ReplayEngine + deterministic run-id + per-run artifacts
    diff.py              compare_summaries + compare_signal_jsonl
  strategies/
    base.py              Strategy ABC + StrategyContext + emit
    risk.py              CooldownManager + RiskEngine
    logger.py            SignalLogger (jsonl + PG + pub/sub)
    engine.py            StrategyEngine runner
    heartbeat.py         observer strategy (no-op signals)
  metrics.py             in-process counters + Redis snapshot
  scripts/
    auth_bootstrap.py    `tpp-auth`
    init_db.py           `tpp-init-db`
    run_ingest.py        `tpp-ingest`
    run_retention.py     `tpp-retention`
    replay_wal.py        `tpp-replay-wal`
    run_candles.py       `tpp-candles`
    run_greeks.py        `tpp-greeks`
    run_strategies.py    `tpp-strategies`
    run_backtest.py      `tpp-backtest`
    run_replay.py        `tpp-replay`
    diff_replay.py       `tpp-replay-diff`
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

# 7. Run Greeks engine in a separate shell (also independent)
tpp-greeks

# 8. Run strategy framework (observer-only until strategies are registered)
tpp-strategies --list                    # show registered strategies
tpp-strategies                            # uses STRATEGIES_ENABLED
tpp-strategies --strategy heartbeat       # or explicit override

# 9. Daily maintenance
tpp-retention

# 10. Replay from WAL (after outage / dry rebuild)
tpp-replay-wal --date 2026-04-19

# 11. Offline replay (deterministic; WAL, Postgres, or merged)
tpp-replay --source wal    --strategy heartbeat --date 2026-04-19
tpp-replay --source pg     --strategy my_strat  --date 2026-04-19
tpp-replay --source merged --strategy my_strat  --date 2026-04-19

# 12. Compare two replay runs
tpp-replay-diff ./logs/replays/<run_A> ./logs/replays/<run_B>
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
| `tpp:greeks:{INDEX}:{EXPIRY}:{STRIKE}:{TYPE}`    | string | latest `OptionGreeks` JSON     |

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
| `greeks.<IDX>`              | `OptionGreeks` JSON on recompute      |
| `signals.<STRATEGY>`        | `Signal` JSON on admitted emission    |

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

## Phase 3 — Greeks engine

Independent process (`tpp-greeks`). Subscribes to `ticks.index.*` + `ticks.option.*`.
Zero changes to ingest or candles.

- **Black-Scholes** for Δ, Γ, Θ, ν. Inputs: current spot, strike, σ (from tick `iv`),
  time-to-expiry, `RISK_FREE_RATE`. Output units: Θ per day, ν per 1 % σ.
- **Trigger policy:** per-contract recompute on every option tick; chain-wide recompute
  on index tick only when spot has moved ≥ `GREEKS_SPOT_TRIGGER_POINTS`.
- **ATM filter:** `|strike − spot| ≤ ATM_STRIKE_WINDOW × strike_step` (per-index step).
- **Caching:** BS pure-function `lru_cache` (4 096 entries) keyed on bucketed inputs —
  spot→int, σ→basis points, T→minutes, r→basis points. Stable keys + high hit rate.
- **TTE alignment:** time to expiry is computed against **15:30 IST** on the expiry date,
  not 00:00 UTC — so 0 DTE ends exactly at market close.
- **Degenerate inputs:** T ≤ 0 or σ = 0 → Δ is the intrinsic step, Γ/Θ/ν = 0.
  Never NaN.
- **Emits** `greeks.<IDX>` on each computation.

## Phase 4 — Strategy framework (no execution)

Independent process (`tpp-strategies`). Subscribes to `ticks.*`, `candles.*`,
`greeks.*`. Emits **signals only** — no order placement. Zero changes to earlier phases.

### Writing a strategy

```python
from trading.strategies import register
from trading.strategies.base import Strategy, StrategyContext

@register("my_strat")
class MyStrat(Strategy):
    def on_candle_close(self, ctx: StrategyContext, candle):
        if candle.close > candle.open * 1.01:
            ctx.emit(
                strategy=self.name,
                index=candle.index,
                action="BUY",
                instrument=candle.index,
                reason="1% up candle",
                confidence=0.6,
            )
```

Drop the module anywhere under `trading/strategies/`, import it, and set
`STRATEGIES_ENABLED=my_strat` (or pass `--strategy my_strat`). The engine
routes the emit through cooldown + risk + logger.

### Guarantees

- **Cooldown** per `(strategy, instrument)` — default 5 min, configurable.
- **Risk caps** — max signals per hour + per day per strategy, index allowlist, action allowlist.
- **Durable log** — every admitted signal is appended to `{LOG_DIR}/signals.jsonl`
  (fsync) before any network call. The jsonl is authoritative; Postgres and pub/sub
  can be rebuilt from it.
- **Crash isolation** — any callback exception is caught and logged; the stream keeps flowing.
- **Observability** — `signals_emitted`, `signals_suppressed_cooldown`, `signals_suppressed_risk`
  surfaced in the metrics snapshot.

### Built-in strategy

`heartbeat` — observer only. Emits nothing; logs tick/candle/greek counts as a
wiring check. Keep it in `STRATEGIES_ENABLED` in dev to verify the pipe is live.

## Phase 5 — Backtesting + replay engine

`tpp-replay` runs strategies over historical data deterministically. One engine,
three swappable sources:

| `--source` | reads                                            | emits                         |
|------------|--------------------------------------------------|-------------------------------|
| `wal`      | `{WAL_DIR}/*.jsonl`                              | `INDEX_TICK` + `OPTION_TICK`  |
| `pg`       | `index_ticks`, `option_chain_data`, `index_candles` | `INDEX_TICK` + `OPTION_TICK` + `CANDLE` |
| `merged`   | both above (heapq-merged by `(ts, seq, kind)`)   | all of the above              |

The engine:
1. dispatches every source event to every registered strategy via the same
   `on_tick` / `on_candle_close` / `on_greeks` callbacks used in live mode;
2. synthesizes missing events inline — `INDEX_TICK` → `CandleAggregator` →
   `on_candle_close`; `OPTION_TICK` → `compute_greeks` (TTE anchored on tick ts)
   → `on_greeks`. If the source already supplied `CANDLE`, aggregation is skipped
   for that timeframe window;
3. gates signals through `CooldownManager` → `RiskEngine` → `signals.jsonl`
   (fsync), with optional `--no-cooldown` / `--no-risk` to measure raw intent;
4. writes three artifacts per run to `{LOG_DIR}/replays/<run_id>/`:
   `signals.jsonl`, `summary.json`, `manifest.json`.

### Deterministic `run_id`

Each run is identified by a 16-hex SHA-256 of:

- source fingerprint (WAL file list + sizes, or PG ts range + flags, or the merge thereof)
- sorted strategy names
- sorted indices
- sorted timeframes
- atm range, risk-free rate, cooldown seconds, cooldown/risk toggles

**Same inputs → same `run_id` → byte-identical `signals.jsonl` and `summary.json`.**
This is enforced by `test_replay::test_two_runs_on_same_inputs_produce_same_run_id_and_signals`.

### Comparing runs

```bash
tpp-replay-diff ./logs/replays/<A> ./logs/replays/<B>
```

Prints a JSON diff — flat-keyed summary deltas plus signal-level `only_in_a` /
`only_in_b` / `differing` with samples. Exit 0 iff fully identical — use in CI
to guarantee a refactor doesn't change strategy output.

## Backtest / signal replay

`tpp-backtest` drives the strategy framework from historical data **without**
touching Redis or Postgres — it's the same event contract, fed from disk.

```
WAL jsonl → normalize → IndexTick / OptionTick
                     │
                     ├── index ticks → CandleAggregator (inline) → on_candle_close
                     ├── option ticks → compute_greeks(...) (inline) → on_greeks
                     │
                     └── strategy.emit(...) → cooldown → risk → backtest_signals.jsonl
```

Guarantees:
- **Deterministic replay:** bucket alignment and TTE are anchored on each tick's
  `ts_exchange`, never wall clock. Running the same WAL + same strategies twice
  produces byte-identical signal jsonl.
- **Framework parity:** strategies never know whether they're live or backtest;
  `on_tick` / `on_candle_close` / `on_greeks` deliver the same Pydantic models.
- **Isolation:** BacktestSignalLogger writes jsonl only. No Postgres writes, no
  pub/sub publishes, no mutation of live Redis state.
- **Flags:** `--no-cooldown` / `--no-risk` let you measure raw strategy intent
  before guardrails; leaving them on reproduces live gating.
- **Summary:** on completion the CLI prints counts of ticks / candles / Greeks
  / emitted-vs-suppressed signals and the ts range — useful for diffing strategy
  variants.

## What is not built yet

- Web UI (Phase 6).
- Order execution (deliberately out of scope).

Each plugs into the existing `EventBus` without changes to earlier phases.
