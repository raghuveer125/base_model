"""The "critical" decision layer — scalper engine + LLM regime filter.

## Plug-and-play contract

This package is self-contained. Deleting the folder must not impact the
base model:

  * Base model (ingest, greeks, candles, storage, UI, orders, strategies)
    NEVER imports anything from `trading.critical`.
  * `trading.critical` imports from the base model only as a READ-ONLY
    consumer: `LiveStore` (reads), `EventBus` (subscribe + publish to its
    own channels), `schemas` (data shapes), `orders.PaperExecutor`
    (advisory paper trades).
  * No edits to `pyproject.toml`, `tpp-up.ps1`, or any existing file.
    The layer is launched as its own process:

        python -m trading.critical

  * Logs go to `logs/critical/`. Redis channels are namespaced
    `scalp.{INDEX}` and `critical.regime.{INDEX}` — no collision with
    the base-model `ticks.*` / `candles.*` / `greeks.*` / `signals.*`
    channels.

See README.md for the full architecture and uninstall verification.
"""

__version__ = "0.1.0"
