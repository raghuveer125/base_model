"""`tpp-candles` — run the candle engine.

Usage:
  tpp-candles                              # all indices, 1m/5m/15m
  tpp-candles --timeframe 1m --timeframe 5m
"""

from __future__ import annotations

import signal
import sys

import click

from trading.candles import CandleEngine
from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger
from trading.schemas import TIMEFRAME_MS
from trading.storage import ensure_schema, get_redis


@click.command()
@click.option(
    "--timeframe",
    "timeframes",
    multiple=True,
    default=("1m", "5m", "15m"),
    help="Timeframe(s) to produce. Repeat flag for multiple.",
)
def main(timeframes: tuple[str, ...]) -> None:
    configure_logging()
    log = get_logger("tpp-candles")
    for tf in timeframes:
        if tf not in TIMEFRAME_MS:
            click.echo(f"unsupported timeframe: {tf} (supported: {list(TIMEFRAME_MS)})", err=True)
            sys.exit(2)
    try:
        get_redis().ping()
        ensure_schema()
        indices = get_settings().index_list
        engine = CandleEngine(indices=indices, timeframes=list(timeframes))

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
        log.error("candles_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
