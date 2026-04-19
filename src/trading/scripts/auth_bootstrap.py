"""`tpp-auth` — one-shot Fyers login via TOTP. Idempotent: uses cached token if fresh."""

from __future__ import annotations

import sys

import click

from trading.auth import FyersAuth
from trading.logging_setup import configure_logging, get_logger


@click.command()
@click.option("--force", is_flag=True, help="Refresh even if cached token is fresh.")
def main(force: bool) -> None:
    configure_logging()
    log = get_logger("tpp-auth")
    try:
        token = FyersAuth().get_access_token(force_refresh=force)
    except Exception as e:  # noqa: BLE001
        log.error("auth_failed", error=str(e))
        sys.exit(1)
    click.echo(f"OK — token cached in Redis (masked: {token[:6]}...{token[-4:]})")


if __name__ == "__main__":
    main()
