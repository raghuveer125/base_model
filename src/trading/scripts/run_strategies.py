"""`tpp-strategies` — run the strategy framework."""

from __future__ import annotations

import signal
import sys

import click

from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger
from trading.storage import ensure_schema, get_redis
from trading.strategies import STRATEGY_REGISTRY
from trading.strategies.engine import StrategyEngine


@click.command()
@click.option(
    "--strategy", "strategies",
    multiple=True, default=(),
    help="Override STRATEGIES_ENABLED (repeat flag for multiple).",
)
@click.option(
    "--list", "list_only",
    is_flag=True,
    help="Print the registered strategies and exit.",
)
def main(strategies: tuple[str, ...], list_only: bool) -> None:
    configure_logging()
    log = get_logger("tpp-strategies")

    if list_only:
        for name in sorted(STRATEGY_REGISTRY):
            click.echo(name)
        return

    try:
        s = get_settings()
        names = list(strategies) if strategies else s.strategy_list
        if not names:
            click.echo(
                "no strategies enabled (set STRATEGIES_ENABLED or use --strategy)",
                err=True,
            )
            sys.exit(2)
        missing = [n for n in names if n not in STRATEGY_REGISTRY]
        if missing:
            click.echo(
                f"unknown strategy names: {missing} "
                f"(registered: {sorted(STRATEGY_REGISTRY)})",
                err=True,
            )
            sys.exit(2)

        get_redis().ping()
        ensure_schema()
        engine = StrategyEngine(indices=s.index_list, strategy_names=names)

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
        log.error("strategies_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
