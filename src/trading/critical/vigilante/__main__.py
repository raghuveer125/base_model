"""`python -m trading.critical.vigilante [daemon|scan|reset] ...`

Three entrypoints, all optional. Engine runs fine without any of them.
"""

from __future__ import annotations

import json
import sys
import time

import click

from trading.config import get_settings
from trading.critical.vigilante.daemon import Vigilante
from trading.critical.vigilante.forensics import (
    render_scan_report, reset_stack, scan_trades,
)
from trading.logging_setup import configure_logging, get_logger


@click.group()
def main() -> None:
    configure_logging()


# ────────────────────────────────────────────────────────────────────────
# daemon
# ────────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--interval", "interval_s", type=float, default=2.0,
               help="Seconds between check cycles.")
@click.option("--alert-cooldown", "alert_cooldown_s", type=float, default=30.0,
               help="Min seconds between duplicate alerts for the same key.")
@click.option("--once", is_flag=True,
               help="Run a single cycle and print the summary, then exit.")
def daemon(interval_s: float, alert_cooldown_s: float, once: bool) -> None:
    """Run the background monitor. Ctrl+C to stop."""
    log = get_logger("vigilante-cli")
    v = Vigilante(
        indices=get_settings().index_list,
        tick_interval_s=interval_s,
        alert_cooldown_s=alert_cooldown_s,
    )
    if once:
        summary = v.tick()
        click.echo(json.dumps(summary, indent=2, default=str))
        return
    t = v.start()
    log.info("vigilante_cli_running", interval_s=interval_s)
    try:
        while t.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("vigilante_cli_ctrl_c")
        v.stop()
        t.join(timeout=5)


# ────────────────────────────────────────────────────────────────────────
# scan
# ────────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--path", default="logs/critical/trades.jsonl",
               help="Path to trades.jsonl.")
@click.option("--max-held-ms", type=int, default=10_000,
               help="Wall_break exits shorter than this are flagged.")
@click.option("--json", "as_json", is_flag=True,
               help="Emit raw JSON instead of a human digest.")
def scan(path: str, max_held_ms: int, as_json: bool) -> None:
    """Read-only forensic scan of the paper-trade audit log."""
    report = scan_trades(path, max_held_ms=max_held_ms)
    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
    else:
        click.echo(render_scan_report(report))
    sys.exit(1 if report.get("fast_wall_break_count", 0) > 0 else 0)


# ────────────────────────────────────────────────────────────────────────
# reset — destructive
# ────────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--yes", is_flag=True,
               help="Required to confirm the destructive purge.")
@click.option("--force", is_flag=True,
               help="Proceed even if open positions are found.")
@click.option("--restart", is_flag=True,
               help="After purge, auto-restart services.")
@click.option("--venv-python", default=None,
               help="Python executable to spawn services with (required "
                    "with --restart).")
@click.option("--working-dir", default=None,
               help="Working directory for spawned services.")
def reset(
    yes: bool, force: bool, restart: bool,
    venv_python: str | None, working_dir: str | None,
) -> None:
    """Kill all managed services, purge tpp:* Redis keys, (optionally)
    restart. Preserves the Fyers token (outside the tpp:* prefix)."""
    log = get_logger("vigilante-cli")
    if not yes:
        click.echo("refusing to reset without --yes", err=True)
        sys.exit(2)
    if restart and not venv_python:
        click.echo("--restart requires --venv-python", err=True)
        sys.exit(2)
    result = reset_stack(
        confirmed=yes,
        force_with_open_positions=force,
        venv_python=venv_python if restart else None,
        working_dir=working_dir,
    )
    click.echo(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
