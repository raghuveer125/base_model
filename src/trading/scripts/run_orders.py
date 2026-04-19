"""`tpp-orders` — run the order engine (paper by default).

Subscribes signals.*, executes via PaperExecutor, maintains position book +
realized PnL. Live (Fyers) mode is not yet implemented.
"""

from __future__ import annotations

import signal
import sys

import click

from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger
from trading.orders.engine import OrderEngine
from trading.orders.paper import PaperExecutor
from trading.storage import ensure_schema, get_redis


@click.command()
@click.option("--mode", type=click.Choice(["paper", "live"]), default=None,
              help="Override ORDERS_MODE.")
@click.option("--slippage-bps", type=float, default=None,
              help="Override PAPER_SLIPPAGE_BPS (paper mode only).")
@click.option("--fee-bps", type=float, default=None,
              help="Override PAPER_FEE_BPS (paper mode only).")
@click.option("--flat-fee", type=float, default=None,
              help="Override PAPER_FLAT_FEE (paper mode only).")
def main(
    mode: str | None, slippage_bps: float | None,
    fee_bps: float | None, flat_fee: float | None,
) -> None:
    configure_logging()
    log = get_logger("tpp-orders")
    s = get_settings()
    effective_mode = mode or s.orders_mode

    if effective_mode == "live":
        click.echo(
            "live mode not yet implemented. Re-run with --mode paper or "
            "set ORDERS_MODE=paper.",
            err=True,
        )
        sys.exit(2)

    try:
        get_redis().ping()
        ensure_schema()
        executor = PaperExecutor(
            slippage_bps=slippage_bps, fee_bps=fee_bps, flat_fee=flat_fee,
        )
        engine = OrderEngine(executor=executor)

        def _handler(signum, _frame):  # noqa: ANN001
            log.info("signal_received", signum=signum)
            engine.shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

        engine.run()
    except Exception as e:  # noqa: BLE001
        log.error("orders_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
