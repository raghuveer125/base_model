"""`tpp-init-db` — apply Postgres DDL. Idempotent (CREATE TABLE IF NOT EXISTS)."""

from __future__ import annotations

import sys

import click

from trading.logging_setup import configure_logging, get_logger
from trading.storage import ensure_schema, get_redis


@click.command()
def main() -> None:
    configure_logging()
    log = get_logger("tpp-init-db")
    try:
        get_redis().ping()
        ensure_schema()
    except Exception as e:  # noqa: BLE001
        log.error("init_db_failed", error=str(e))
        sys.exit(1)
    click.echo("OK - Redis reachable and Postgres schema applied.")


if __name__ == "__main__":
    main()
