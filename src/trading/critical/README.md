# `trading.critical` — scalper decision layer

A read-only, plug-and-play layer that sits on top of the base data
pipeline. It watches the live chain, makes scalp entry/exit decisions
deterministically in Python, and consults an LLM **only** every 15 min
for market-regime classification.

## Design constraints

1. **Fully additive.** No edits to any file outside
   `src/trading/critical/`, `tests/critical/`, `logs/critical/`.
2. **Base never imports `critical`.** Reverse is fine — `critical`
   reads `LiveStore`, subscribes `EventBus`, and uses `PaperExecutor`.
3. **Deleting the folder must leave the base identical.** See the
   deletion test below.

## Architecture

```
                     ┌─────────────────┐
                     │ base pipeline   │  (ingest → redis → greeks → candles)
                     └────────┬────────┘
                              │ publishes on: ticks.*, greeks.*, candles.*
                              ▼
 ┌──────────────────────────────────────────────────────────┐
 │ trading.critical.engine                                  │
 │   subscribes to all base pub/sub channels (READ-ONLY)    │
 │                                                          │
 │   every event ──► state.update() ──► triggers.evaluate() │
 │                                          │               │
 │                  regime.client (15 min)  │               │
 │                        │ gates ──────────┤               │
 │                        ▼                 ▼               │
 │                  risk.check ──► executor.fire (paper)    │
 └──────────────────────────┬───────────────────────────────┘
                            │ emits on: scalp.{INDEX},
                            │           critical.regime.{INDEX}
                            ▼
                     ┌─────────────────┐
                     │ logs/critical/  │   observability + tuning
                     └─────────────────┘
```

## Runtime configuration (v1)

| Setting | Value |
|---|---|
| Lots | NIFTY 2, SENSEX 2, BANKNIFTY 1 |
| Max loss / trade | ₹1,500 (hard-killed by Python, no LLM consulted) |
| Mode | Paper (`PaperExecutor`) |
| Instrument pick | ITM CE/PE with live Δ ∈ [0.55, 0.65] |
| Direction | BUY only |
| Concurrency | 1 position / index, max 2 indices open |
| Post-loss cooldown | 3 min |
| Daily circuit-breaker | 3 consecutive losses |
| Regime cadence | 15 min per index, Claude Haiku via Anthropic SDK |

## Running

```bash
# v1: standalone process. Requires base pipeline to be up (redis + ingest).
python -m trading.critical
```

## Uninstall / deletion test

The folder must be fully removable without touching anything else:

```bash
rm -rf src/trading/critical tests/critical logs/critical
pytest -q                     # must still pass 133/133
./tpp-up.ps1 -SkipAuth -SkipDocker   # must start cleanly
```

If either fails, a dependency has leaked out of the folder and needs
fixing before anything else ships.
