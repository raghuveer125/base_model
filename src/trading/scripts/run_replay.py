"""`tpp-replay` — run strategies over historical data deterministically.

Sources:
  wal       replay WAL jsonl segments (default)
  pg        replay Postgres index_ticks + option_chain_data + index_candles
  merged    merge wal + pg by timestamp

Outputs LOG_DIR/replays/<run_id>/{signals.jsonl, summary.json, manifest.json}.
`run_id` is a deterministic hash of (source, strategies, config).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger
from trading.replay.engine import ReplayEngine
from trading.replay.sources import (
    MergedEventSource,
    PostgresEventSource,
    WALEventSource,
)
from trading.strategies import STRATEGY_REGISTRY


def _day_bounds_ms(day: str) -> tuple[int, int]:
    d = date.fromisoformat(day)
    tz = ZoneInfo("Asia/Kolkata")
    start = datetime.combine(d, dtime(0, 0), tzinfo=tz)
    end = datetime.combine(d, dtime(23, 59, 59, 999_000), tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


@click.command()
@click.option("--source", "source_kind",
              type=click.Choice(["wal", "pg", "merged"]),
              default="wal", show_default=True)
@click.option("--date", "date_str", default=None,
              help="YYYY-MM-DD. Required for pg/merged; optional for wal.")
@click.option("--strategy", "strategies", multiple=True, required=True,
              help="Strategy name (repeat for multiple).")
@click.option("--output-dir", type=click.Path(file_okay=False), default=None)
@click.option("--wal-dir", type=click.Path(file_okay=False), default=None)
@click.option("--timeframe", "timeframes", multiple=True,
              default=("1m", "5m", "15m"), show_default=True)
@click.option("--atm-range", type=int, default=None)
@click.option("--no-cooldown", is_flag=True)
@click.option("--no-risk", is_flag=True)
@click.option("--pg-exclude-candles", is_flag=True,
              help="Skip pre-computed candles from Postgres.")
def main(
    source_kind: str,
    date_str: str | None,
    strategies: tuple[str, ...],
    output_dir: str | None,
    wal_dir: str | None,
    timeframes: tuple[str, ...],
    atm_range: int | None,
    no_cooldown: bool,
    no_risk: bool,
    pg_exclude_candles: bool,
) -> None:
    configure_logging()
    log = get_logger("tpp-replay")

    missing = [s for s in strategies if s not in STRATEGY_REGISTRY]
    if missing:
        click.echo(
            f"unknown strategy names: {missing} "
            f"(registered: {sorted(STRATEGY_REGISTRY)})",
            err=True,
        )
        sys.exit(2)

    try:
        settings = get_settings()
        if source_kind == "wal":
            source = WALEventSource(
                wal_dir=Path(wal_dir) if wal_dir else None, date=date_str,
            )
        elif source_kind == "pg":
            if not date_str:
                click.echo("--date YYYY-MM-DD is required for pg source", err=True)
                sys.exit(2)
            start_ms, end_ms = _day_bounds_ms(date_str)
            source = PostgresEventSource(
                start_ts_ms=start_ms, end_ts_ms=end_ms,
                include_candles=not pg_exclude_candles,
            )
        else:  # merged
            if not date_str:
                click.echo("--date YYYY-MM-DD is required for merged source", err=True)
                sys.exit(2)
            start_ms, end_ms = _day_bounds_ms(date_str)
            source = MergedEventSource([
                WALEventSource(
                    wal_dir=Path(wal_dir) if wal_dir else None, date=date_str,
                ),
                PostgresEventSource(
                    start_ts_ms=start_ms, end_ts_ms=end_ms,
                    include_candles=not pg_exclude_candles,
                ),
            ])

        engine = ReplayEngine(
            source=source,
            strategy_names=list(strategies),
            indices=settings.index_list,
            output_dir=Path(output_dir) if output_dir else None,
            timeframes=list(timeframes),  # type: ignore[arg-type]
            atm_range=atm_range,
            apply_cooldown=not no_cooldown,
            apply_risk=not no_risk,
        )
        summary = engine.run()
        click.echo(json.dumps(summary, indent=2, sort_keys=True))
        click.echo(f"\nrun artifacts: {engine.output_dir}", err=True)
    except Exception as e:  # noqa: BLE001
        log.error("replay_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
