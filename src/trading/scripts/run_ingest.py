"""`tpp-ingest` — start the Fyers WS ingest orchestrator.

Usage:
  tpp-ingest                                 # auto-fetch expiries from Fyers
  tpp-ingest --expiry NIFTY50=2026-04-30     # manual override
"""

from __future__ import annotations

import sys
from datetime import date

import click

from trading.auth import ensure_access_token
from trading.config import get_settings
from trading.expiry import get_expiries
from trading.ingest import Orchestrator, install_signal_handlers
from trading.logging_setup import configure_logging, get_logger
from trading.storage import ensure_schema, get_redis


def _parse_expiry(pairs: tuple[str, ...]) -> dict[str, date]:
    out: dict[str, date] = {}
    for p in pairs:
        if "=" not in p:
            raise click.BadParameter(f"expected INDEX=YYYY-MM-DD, got: {p}")
        idx, iso = p.split("=", 1)
        out[idx.strip().upper()] = date.fromisoformat(iso.strip())
    return out


@click.command()
@click.option(
    "--expiry",
    "expiries",
    multiple=True,
    default=(),
    help="INDEX=YYYY-MM-DD (repeat for each index). Auto-fetched from Fyers if omitted.",
)
def main(expiries: tuple[str, ...]) -> None:
    configure_logging()
    log = get_logger("tpp-ingest")
    try:
        get_redis().ping()
        ensure_schema()
        ensure_access_token()
        indices = get_settings().index_list

        # Resolve expiries: CLI flags > auto-fetch from Fyers
        if expiries:
            exp_map = _parse_expiry(expiries)
        else:
            log.info("auto_fetching_expiries")
            exp_map = get_expiries(indices)

        for idx, exp in exp_map.items():
            log.info("expiry_active", index=idx, expiry=exp.isoformat())

        orc = Orchestrator(indices=indices, expiries=exp_map)
        install_signal_handlers(orc)
        orc.run()
    except Exception as e:  # noqa: BLE001
        log.error("ingest_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
