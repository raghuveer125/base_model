"""`tpp-retention` — purge rows older than N days from history tables."""

from __future__ import annotations

import sys

import click

from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger
from trading.storage import enforce_retention


@click.command()
@click.option(
    "--days",
    type=int,
    default=None,
    help="Retention window in days. Defaults to settings.retention_days.",
)
def main(days: int | None) -> None:
    configure_logging()
    log = get_logger("tpp-retention")
    try:
        effective = days or get_settings().retention_days
        purged = enforce_retention(effective)
    except Exception as e:  # noqa: BLE001
        log.error("retention_failed", error=str(e))
        sys.exit(1)
    click.echo(f"OK - retention applied ({effective} days): {purged}")


if __name__ == "__main__":
    main()
