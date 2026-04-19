"""`tpp-backtest` — replay WAL + candles + greeks through the strategy framework.

Examples:
  tpp-backtest --strategy heartbeat --date 2026-04-19
  tpp-backtest --strategy my_strat --output ./backtest_myrun.jsonl --no-risk
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from trading.backtest import BacktestRunner
from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger
from trading.strategies import STRATEGY_REGISTRY


@click.command()
@click.option("--date", "date_str", default=None,
              help="YYYY-MM-DD (UTC). Omit to replay all available WAL segments.")
@click.option("--strategy", "strategies", multiple=True, required=True,
              help="Strategy name. Repeat flag for multiple.")
@click.option("--output", "output", type=click.Path(), default=None,
              help="Output jsonl path (defaults to LOG_DIR/backtest_signals_{date}.jsonl).")
@click.option("--wal-dir", "wal_dir", type=click.Path(exists=True, file_okay=False),
              default=None, help="Override WAL_DIR for this run.")
@click.option("--timeframe", "timeframes", multiple=True, default=("1m", "5m", "15m"),
              help="Candle timeframe(s) to synthesize (repeat for multiple).")
@click.option("--atm-range", type=int, default=None,
              help="ATM window for Greeks. Defaults to settings.")
@click.option("--no-cooldown", is_flag=True, help="Bypass CooldownManager.")
@click.option("--no-risk", is_flag=True, help="Bypass RiskEngine caps/allowlists.")
def main(
    date_str: str | None,
    strategies: tuple[str, ...],
    output: str | None,
    wal_dir: str | None,
    timeframes: tuple[str, ...],
    atm_range: int | None,
    no_cooldown: bool,
    no_risk: bool,
) -> None:
    configure_logging()
    log = get_logger("tpp-backtest")

    missing = [s for s in strategies if s not in STRATEGY_REGISTRY]
    if missing:
        click.echo(
            f"unknown strategy names: {missing} "
            f"(registered: {sorted(STRATEGY_REGISTRY)})",
            err=True,
        )
        sys.exit(2)

    try:
        runner = BacktestRunner(
            wal_dir=Path(wal_dir) if wal_dir else None,
            date=date_str,
            indices=get_settings().index_list,
            strategy_names=list(strategies),
            output_path=Path(output) if output else None,
            timeframes=list(timeframes),  # type: ignore[arg-type]
            atm_range=atm_range,
            apply_cooldown=not no_cooldown,
            apply_risk=not no_risk,
        )
        summary = runner.run()
        click.echo(json.dumps(summary, indent=2))
    except Exception as e:  # noqa: BLE001
        log.error("backtest_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
