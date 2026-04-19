"""`tpp-up` — bring the entire TPP engine up with a single command.

Starts all services as subprocesses in the correct order:
  1. Docker (Redis + Postgres) — if not already healthy
  2. tpp-ingest   (WS → WAL → Redis/PG)
  3. tpp-candles   (Redis pub/sub → candle aggregation)
  4. tpp-greeks    (Redis pub/sub → Black-Scholes)
  5. tpp-strategies (Redis pub/sub → signal framework)
  6. tpp-orders    (signals → paper executor)
  7. tpp-ui        (FastAPI dashboard)

Ctrl-C shuts everything down gracefully in reverse order.

Usage:
  tpp-up                                     # reads EXPIRIES from .env
  tpp-up --expiry NIFTY50=2026-04-30         # override specific expiry
  tpp-up --no-orders                          # skip order engine
  tpp-up --no-docker                          # Docker already running
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger


# Resolve the Python executable from the current venv
_PYTHON = sys.executable

# Docker binary — try common Windows locations if not on PATH
_DOCKER: str | None = None
for candidate in ("docker", r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"):
    try:
        subprocess.run(
            [candidate, "--version"],
            capture_output=True, timeout=10,
        )
        _DOCKER = candidate
        break
    except (FileNotFoundError, OSError):
        continue


def _docker_healthy() -> bool:
    """Return True if tpp-redis and tpp-postgres are both healthy."""
    if not _DOCKER:
        return False
    try:
        r = subprocess.run(
            [_DOCKER, "compose", "ps", "--format", "{{.Name}} {{.Status}}"],
            capture_output=True, text=True, timeout=30,
            cwd=Path(__file__).resolve().parents[3],  # project root
        )
        lines = r.stdout.strip().splitlines()
        found = {"tpp-redis": False, "tpp-postgres": False}
        for line in lines:
            for name in found:
                if name in line and "(healthy)" in line:
                    found[name] = True
        return all(found.values())
    except Exception:  # noqa: BLE001
        return False


def _start_docker(log) -> None:  # noqa: ANN001
    """Bring up Docker containers and wait for healthy status."""
    if not _DOCKER:
        log.error("docker_not_found", hint="Install Docker Desktop or add docker to PATH")
        sys.exit(1)
    root = Path(__file__).resolve().parents[3]
    log.info("docker_compose_up")
    subprocess.run(
        [_DOCKER, "compose", "up", "-d"],
        cwd=root, check=True, timeout=120,
    )
    # Wait for healthy
    for attempt in range(30):
        if _docker_healthy():
            log.info("docker_healthy")
            return
        time.sleep(2)
    log.error("docker_not_healthy", hint="Containers did not become healthy in 60s")
    sys.exit(1)


class ProcessManager:
    """Manages child processes with ordered startup and reverse-order shutdown."""

    def __init__(self, log) -> None:  # noqa: ANN001
        self.log = log
        self._procs: list[tuple[str, subprocess.Popen]] = []  # (name, proc)
        self._shutting_down = False

    def start(self, name: str, args: list[str], delay: float = 1.0) -> None:
        """Start a subprocess and register it for cleanup."""
        self.log.info("starting", service=name)
        env = os.environ.copy()
        proc = subprocess.Popen(
            [_PYTHON, "-m", args[0]] if len(args) == 1 else [_PYTHON] + args,
            env=env,
            cwd=Path(__file__).resolve().parents[3],
        )
        self._procs.append((name, proc))
        time.sleep(delay)
        if proc.poll() is not None:
            self.log.error("service_failed_to_start", service=name, returncode=proc.returncode)
            self.shutdown()
            sys.exit(1)
        self.log.info("started", service=name, pid=proc.pid)

    def shutdown(self) -> None:
        """Send SIGTERM/CTRL_BREAK to all processes in reverse order, then wait."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.log.info("shutdown_start", count=len(self._procs))
        for name, proc in reversed(self._procs):
            if proc.poll() is not None:
                continue
            self.log.info("stopping", service=name, pid=proc.pid)
            try:
                if sys.platform == "win32":
                    # On Windows, send CTRL_BREAK_EVENT for graceful shutdown
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
            except OSError:
                pass
        # Wait for all to exit (timeout per process)
        for name, proc in reversed(self._procs):
            try:
                proc.wait(timeout=10)
                self.log.info("stopped", service=name, returncode=proc.returncode)
            except subprocess.TimeoutExpired:
                self.log.warning("force_killing", service=name)
                proc.kill()
                proc.wait(timeout=5)
        self.log.info("shutdown_complete")

    def wait(self) -> None:
        """Block until any child exits (crash detection) or KeyboardInterrupt."""
        try:
            while True:
                for name, proc in self._procs:
                    ret = proc.poll()
                    if ret is not None:
                        self.log.error("service_exited", service=name, returncode=ret)
                        self.shutdown()
                        sys.exit(1)
                time.sleep(1)
        except KeyboardInterrupt:
            self.log.info("ctrl_c_received")
            self.shutdown()


def _resolve_expiries(cli_expiries: tuple[str, ...], log) -> list[str]:  # noqa: ANN001
    """Resolve expiry pairs from CLI flags or EXPIRIES env var."""
    if cli_expiries:
        return list(cli_expiries)
    # Fall back to EXPIRIES setting (csv of INDEX=YYYY-MM-DD)
    raw = get_settings().expiries.strip()
    if not raw:
        log.error(
            "no_expiries",
            hint="Set EXPIRIES in .env (e.g. EXPIRIES=NIFTY50=2026-04-30,BANKNIFTY=2026-04-30) "
                 "or pass --expiry flags",
        )
        sys.exit(2)
    pairs = [p.strip() for p in raw.split(",") if p.strip()]
    log.info("expiries_from_env", expiries=pairs)
    return pairs


@click.command()
@click.option(
    "--expiry", "expiries", multiple=True, default=(),
    help="INDEX=YYYY-MM-DD (repeat for each index). Falls back to EXPIRIES in .env.",
)
@click.option("--no-docker", is_flag=True, help="Skip Docker health check / startup.")
@click.option("--no-orders", is_flag=True, help="Skip the order engine.")
@click.option("--no-ui", is_flag=True, help="Skip the web UI.")
@click.option(
    "--strategy", "strategies", multiple=True, default=(),
    help="Override STRATEGIES_ENABLED (repeat for multiple).",
)
def main(
    expiries: tuple[str, ...],
    no_docker: bool,
    no_orders: bool,
    no_ui: bool,
    strategies: tuple[str, ...],
) -> None:
    configure_logging()
    log = get_logger("tpp-up")

    # ── 1. Docker ──────────────────────────────────────────────
    if not no_docker:
        if _docker_healthy():
            log.info("docker_already_healthy")
        else:
            _start_docker(log)
    else:
        log.info("docker_skipped")

    # ── 2. Resolve expiries ────────────────────────────────────
    resolved = _resolve_expiries(expiries, log)

    # ── 3. Build subprocess commands ───────────────────────────
    expiry_args: list[str] = []
    for e in resolved:
        expiry_args += ["--expiry", e]

    strat_args: list[str] = []
    for s in strategies:
        strat_args += ["--strategy", s]

    mgr = ProcessManager(log)

    # On Windows, create processes in a new process group so we can
    # send CTRL_BREAK_EVENT for graceful shutdown
    if sys.platform == "win32":
        _old_popen_init = subprocess.Popen.__init__

        def _patched_init(self_popen, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
            _old_popen_init(self_popen, *args, **kwargs)

        subprocess.Popen.__init__ = _patched_init  # type: ignore[assignment]

    # ── 3. Start services in order ─────────────────────────────
    entry = "src/trading/scripts"

    mgr.start("ingest", [f"{entry}/run_ingest.py"] + expiry_args, delay=3)
    mgr.start("candles", [f"{entry}/run_candles.py"], delay=2)
    mgr.start("greeks", [f"{entry}/run_greeks.py"], delay=2)
    mgr.start("strategies", [f"{entry}/run_strategies.py"] + strat_args, delay=2)

    if not no_orders:
        mgr.start("orders", [f"{entry}/run_orders.py"], delay=2)

    if not no_ui:
        mgr.start("ui", [f"{entry}/run_ui.py"], delay=1)

    # ── 4. Summary ─────────────────────────────────────────────
    services = [name for name, _ in mgr._procs]
    log.info(
        "all_services_running",
        services=services,
        ui="http://127.0.0.1:8088" if not no_ui else "disabled",
    )
    click.echo()
    click.echo("=" * 60)
    click.echo("  TPP engine is UP")
    click.echo(f"  Services: {', '.join(services)}")
    if not no_ui:
        click.echo("  Dashboard: http://127.0.0.1:8088")
    click.echo("  Press Ctrl+C to shut down all services")
    click.echo("=" * 60)
    click.echo()

    # ── 5. Wait / crash detect ─────────────────────────────────
    mgr.wait()


if __name__ == "__main__":
    main()
