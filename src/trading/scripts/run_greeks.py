"""`tpp-greeks` — run the Greeks engine."""

from __future__ import annotations

import signal
import sys

import click

from trading.config import get_settings
from trading.greeks import GreeksEngine
from trading.logging_setup import configure_logging, get_logger
from trading.storage import get_redis


@click.command()
@click.option(
    "--atm-range",
    type=int,
    default=None,
    help="Override ATM window (defaults to ATM_STRIKE_WINDOW).",
)
@click.option(
    "--spot-trigger",
    type=float,
    default=None,
    help="Override spot-move trigger in points (defaults to GREEKS_SPOT_TRIGGER_POINTS).",
)
def main(atm_range: int | None, spot_trigger: float | None) -> None:
    configure_logging()
    log = get_logger("tpp-greeks")
    try:
        get_redis().ping()
        indices = get_settings().index_list
        engine = GreeksEngine(
            indices=indices,
            atm_range=atm_range,
            spot_trigger_points=spot_trigger,
        )

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
        log.error("greeks_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
