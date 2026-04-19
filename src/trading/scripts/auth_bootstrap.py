"""`tpp-auth` — one-shot Fyers login. Default: TOTP auto-login. Fallback: --manual."""

from __future__ import annotations

import sys

import click

from trading.auth import (
    FyersAuth,
    capture_auth_code_via_loopback,
    exchange_auth_code,
    manual_auth_url,
)
from trading.logging_setup import configure_logging, get_logger


@click.command()
@click.option("--force", is_flag=True, help="Refresh even if cached token is fresh.")
@click.option("--manual", is_flag=True,
              help="Browser-based login with automatic auth_code capture "
                   "(opens the Fyers login URL, catches redirect on the "
                   "FYERS_REDIRECT_URI host:port, exchanges for token).")
@click.option("--print-url", is_flag=True,
              help="Just print the login URL for copy-paste (no browser, no capture).")
@click.option("--auth-code", default=None,
              help="Exchange a pre-captured auth_code (from the redirect URL) "
                   "for a token.")
@click.option("--timeout", type=int, default=180,
              help="Seconds to wait for the browser redirect (manual flow).")
def main(
    force: bool, manual: bool, print_url: bool,
    auth_code: str | None, timeout: int,
) -> None:
    configure_logging()
    log = get_logger("tpp-auth")

    if print_url:
        click.echo(manual_auth_url())
        return

    if auth_code:
        try:
            token = exchange_auth_code(auth_code)
        except Exception as e:  # noqa: BLE001
            log.error("auth_exchange_failed", error=str(e))
            sys.exit(1)
        click.echo(f"OK — token cached in Redis (masked: {token[:6]}...{token[-4:]})")
        return

    if manual:
        click.echo("Opening browser for Fyers login...")
        click.echo("Log in normally (password + TOTP); the browser will redirect "
                   "back to this process and the auth_code will be captured "
                   "automatically.")
        try:
            code = capture_auth_code_via_loopback(timeout_s=timeout)
            token = exchange_auth_code(code)
        except Exception as e:  # noqa: BLE001
            log.error("manual_auth_failed", error=str(e))
            click.echo(f"\nIf the browser didn't open, copy this URL yourself:\n  "
                       f"{manual_auth_url()}", err=True)
            sys.exit(1)
        click.echo(f"OK — token cached in Redis (masked: {token[:6]}...{token[-4:]})")
        return

    try:
        token = FyersAuth().get_access_token(force_refresh=force)
    except Exception as e:  # noqa: BLE001
        log.error("auth_failed", error=str(e))
        click.echo(
            "\nHint: TOTP auto-login can break when Fyers changes the endpoint. "
            "Use the automated browser flow instead:\n"
            "  tpp-auth --manual",
            err=True,
        )
        sys.exit(1)
    click.echo(f"OK — token cached in Redis (masked: {token[:6]}...{token[-4:]})")


if __name__ == "__main__":
    main()
