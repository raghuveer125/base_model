# Going Live — Migration Notes

Production checklist for switching `ORDERS_MODE=paper` → live Fyers execution.
**Do not flip the switch without walking every item below.** Keep this file
current — each time a paper-only simulation choice is made, append a row.

---

## 1. `FyersExecutor` is a stub

- **File:** `src/trading/orders/fyers.py:17-27`
- **State:** `__init__` raises `NotImplementedError`. `execute()` is a stub.
- **Required before live:**
  1. Wrap `fyersModel.place_order({...})` — use the same cached access token
     the ingest websocket uses (`trading.auth.ensure_access_token`).
  2. Poll order status (or subscribe to order-update WS if available) and map
     to `Fill(order_id, fill_price, fees, ts_ms, ...)`.
  3. Handle partial fills (for large qty) — currently `Fill` assumes fully
     filled; may need aggregation.
  4. Map Fyers rejection codes → `ExecutionResult(ok=False, reason=...)`.
  5. Validate order price is on the ₹0.05 tick grid *before* placing —
     fail fast with a clear error instead of a broker rejection.

---

## 2. Paper fill-price snapping does NOT apply to live

- **File:** `src/trading/orders/paper.py` → `_snap_fill(price, side)`
- **What it does:** after slippage math, rounds BUY up / SELL down to the
  nearest ₹0.05 tick. Purely a simulation choice to mimic worst-tick book-walk.
- **Live equivalent:** none — Fyers returns the actual fill price from the
  exchange, which is inherently tick-aligned.
- **Action:** `FyersExecutor.execute()` must NOT call `_snap_fill`. Use the
  broker's returned price verbatim.
- **Drift expectation:** live usually fills *better* than paper (book has
  mid-price liquidity). Paper is a conservative lower bound on realized PnL.

---

## 3. `build_exit_levels` tick snap DOES carry over

- **File:** `src/trading/critical/exit.py::build_exit_levels`
- **What it does:** floors stop / ceils target to ₹0.05 grid.
- **Why live needs it:** Fyers rejects LIMIT / SL orders whose price is not
  a valid tick. Without this, every exit order would be rejected at the API.
- **Action:** no change needed. Leave as-is.

---

## 4. Tick size is hardcoded

- **File:** `src/trading/schemas.py` → `OPTION_TICK_SIZE = 0.05`
- **Why this is OK today:** NIFTY50, BANKNIFTY, SENSEX index options all use
  ₹0.05. Stable for years.
- **Why it could break:** (a) SEBI revision, (b) adding stock options where
  tick varies by price band, (c) adding other exchanges.
- **Action for live:** replace with a per-instrument lookup sourced from the
  Fyers symbol-master CSVs (`NSE_FO.csv` / `BSE_FO.csv`) which carry a
  `minTick` column. Keep `0.05` as a safe fallback.

---

## 5. Lot sizes are hardcoded

- **File:** `src/trading/critical/executor.py:37-41` → `LOT_SIZES`
- **Current values:** NIFTY50=75, BANKNIFTY=30, SENSEX=20 (per SEBI Nov-2024
  revision noted in code comment).
- **Risk:** if SEBI revises again (they did in 2024), this silently mis-sizes
  every trade.
- **Action for live:** resolve per-instrument from the Fyers symbol-master
  (`lotSize` field). Same place you'd fetch tick size from (#4 above).

---

## 6. PaperExecutor fee model ≠ real brokerage

- **File:** `src/trading/orders/paper.py::PaperExecutor.execute`
- **Paper model:** `fee = flat_fee + gross * (fee_bps / 10_000)`.
- **Reality at Fyers (as of 2026):** brokerage + STT + exchange transaction
  charges + SEBI turnover fee + stamp duty + GST on brokerage & transaction
  charges. STT on options sell side is significant.
- **Action for live:** do NOT reuse `PaperExecutor.fee_bps` for live accounting.
  Use Fyers' returned `tradedValue`/`charges` fields if available, or replicate
  the exact fee schedule in a `trading.orders.fees` module. Paper PnL will
  look better than live by ~0.1–0.2% of notional per round-trip without this.

---

## 7. Slippage model is a single-parameter approximation

- **File:** `src/trading/orders/paper.py` → `slippage_bps` config (default from
  `CRITICAL_*` or `paper_slippage_bps` setting).
- **Paper:** flat bps applied symmetrically.
- **Reality:** slippage varies with (a) spread at the moment of order,
  (b) order size vs. displayed depth, (c) volatility, (d) time-of-day.
- **Action for live:** not a blocker — live doesn't use the model. But once a
  week of live data exists, backfill comparable paper runs against the real
  fills to calibrate `slippage_bps` so future paper forecasts are tighter.

---

## 8. Dollar-cap stop fires on paper but may be late in live

- **File:** `src/trading/critical/exit.py:61-69` → `if rupee_loss >= max_loss_rupees`
- **Paper:** checked on every evaluated tick, executes instantly at current
  LTP.
- **Live caveat:** exit is an API call (network round-trip, ~100–300 ms) and
  the market can move during that window. A fast adverse candle could realize
  a larger loss than `max_loss_rupees`.
- **Action for live:** consider placing a pre-armed SL-M order at `pos.stop_ltp`
  alongside each entry, so the exchange triggers it server-side without a
  round-trip. Keep the client-side dollar-cap as a belt-and-braces fallback.

---

## 9. Critical layer launch — unchanged

- **File:** `tpp-up.ps1` (launches `python -m trading.critical` after orders).
- **Action for live:** none. Critical layer is paper-vs-live-agnostic — it
  emits entry/exit *intents* via the `ScalpExecutor` adapter, which delegates
  to whichever `Executor` the orders engine is configured with.

---

## 10. Orphan cleanup — unchanged

- **File:** `tpp-up.ps1::Stop-OrphanServices`
- **Action for live:** none. Safety net, works identically for paper and live.

---

## 11. Regime gating via Claude API — cost & latency

- **File:** `src/trading/critical/regime/client.py`
- **Paper cost:** ~3–4 API calls per index per 15-min window. Negligible.
- **Live cost:** same. Anthropic pricing makes this a rounding error against
  brokerage.
- **Live latency risk:** a slow/failed API call must not block the trade
  decision. Current `RegimeClient` degrades to the deterministic fallback on
  timeout (`client.py:93-101`). **Verify the timeout is tight (<500 ms) for
  live** so a hung API doesn't stall a fast-moving tape.
- **Action:** add a hard timeout on `client.messages.create` (currently
  implicit). Log every fallback so you can see when the LLM is unavailable.

---

## 12. Audit trail completeness

- **Current:** `logs/critical/trades.jsonl` captures entry/exit events.
- **Live gap:** no capture of (a) order-placement timestamps vs. fill
  timestamps (latency analysis), (b) rejection events from the broker, (c)
  broker-assigned order IDs, (d) actual fee breakdown.
- **Action:** extend the JSONL schema with `order_placed_ts_ms`,
  `order_filled_ts_ms`, `broker_order_id`, `broker_fee_breakdown` before
  live goes on. Regulators and you both want this trail.

---

## 13. Kill switch / circuit breaker behavior in live

- **File:** `src/trading/critical/*` (dollar-cap stop, regime-flip exit, etc.)
- **Paper:** failure just logs.
- **Live:** a runaway kill-switch that fires wrongly can flatten real positions
  in seconds. Verify:
  - Circuit-breaker threshold for the day (max drawdown before halting).
  - No auto-retry on a failed exit that keeps sending orders.
  - A manual kill toggle (redis key / config flag) that `orders.engine` checks
    before every place-order call.

---

## 14. Environment separation

- **Today:** one `.env` for everything.
- **Live:** keep live credentials in a separate env file (e.g., `.env.live`)
  that is NOT loaded by default. `tpp-up.ps1` should accept an explicit
  `-Live` flag that loads it, and print a giant warning banner on startup.
  Never let live mode be the accidental default.

---

## 15. Testing gate before live

- **Minimum before flipping:**
  - Week of paper runs with the specific `slippage_bps` / `fee_bps` configured
    for live.
  - At least one end-to-end dry run with FyersExecutor in **sandbox / UAT
    account** if Fyers offers one.
  - Compliance / risk sign-off on the dollar-cap, circuit breaker, kill-switch.
  - A "1-lot for 1 day" shakeout — smallest possible position, real money,
    one trading day, manual review before scaling.

---

## Change log

| Date | Commit | Item added / updated |
|---|---|---|
| 2026-04-21 | `df1ec4e` | #2, #3, #4 — tick-snap simulation flagged paper-only; target/stop snap flagged as required for live |
| 2026-04-21 | initial | #1, #5–#15 — baseline migration notes |
