"""`tpp-replay-wal` — rebuild Redis/Postgres state from WAL segments.

Usage:
  tpp-replay-wal --date 2026-04-19
  tpp-replay-wal --date 2026-04-19 --dry-run
  tpp-replay-wal                              # replay ALL segments
"""

from __future__ import annotations

import sys

import click

from trading.adapter import normalize_index_tick, normalize_option_tick
from trading.logging_setup import configure_logging, get_logger
from trading.schemas import FYERS_INDEX_SYMBOL
from trading.storage import LiveStore, ensure_schema, insert_index_ticks, insert_option_ticks
from trading.wal import WALReader

log = get_logger(__name__)
BATCH = 500


@click.command()
@click.option("--date", "date_str", default=None, help="YYYY-MM-DD (UTC). Defaults to all segments.")
@click.option("--dry-run", is_flag=True, help="Parse and count; do not write.")
def main(date_str: str | None, dry_run: bool) -> None:
    configure_logging()
    if not dry_run:
        ensure_schema()
    store = LiveStore()
    reader = WALReader()

    idx_batch: list = []
    opt_batch: list = []
    counts = {"read": 0, "index": 0, "option": 0, "skipped": 0}

    for rec in reader.iter_records(date=date_str):
        counts["read"] += 1
        if rec.get("kind") != "raw_tick":
            continue
        payload = rec.get("data") or {}
        sym = payload.get("symbol") or payload.get("sym") or ""
        if sym in FYERS_INDEX_SYMBOL.values():
            tick = normalize_index_tick(payload)
            if tick is None:
                counts["skipped"] += 1
                continue
            if not dry_run:
                store.set_index_tick(tick)
            idx_batch.append(tick)
            counts["index"] += 1
            if len(idx_batch) >= BATCH and not dry_run:
                insert_index_ticks(idx_batch)
                idx_batch.clear()
        else:
            tick = normalize_option_tick(payload)
            if tick is None:
                counts["skipped"] += 1
                continue
            if not dry_run:
                store.set_option_tick(tick)
            opt_batch.append(tick)
            counts["option"] += 1
            if len(opt_batch) >= BATCH and not dry_run:
                insert_option_ticks(opt_batch)
                opt_batch.clear()

    if not dry_run:
        if idx_batch:
            insert_index_ticks(idx_batch)
        if opt_batch:
            insert_option_ticks(opt_batch)

    click.echo(f"replay complete (dry_run={dry_run}) {counts}")
    if counts["read"] == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
