"""`tpp-backup` — one-shot WAL + Postgres backup + retention prune.

Examples:
  tpp-backup                 # WAL + DB snapshot + prune old
  tpp-backup --no-db         # WAL only (fast, no pg_dump needed)
  tpp-backup --list          # list existing snapshots
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from trading.backup import BackupManager
from trading.logging_setup import configure_logging, get_logger


@click.command()
@click.option("--wal/--no-wal", default=True, help="Snapshot WAL dir.")
@click.option("--db/--no-db", default=True, help="Snapshot Postgres via pg_dump.")
@click.option("--prune/--no-prune", default=True,
              help="Remove backups older than BACKUP_RETENTION_DAYS.")
@click.option("--days", type=int, default=None,
              help="Override BACKUP_RETENTION_DAYS for this run.")
@click.option("--list", "list_only", is_flag=True,
              help="List existing snapshots and exit.")
@click.option("--backup-dir", "backup_dir", type=click.Path(), default=None,
              help="Override BACKUP_DIR for this run.")
def main(
    wal: bool, db: bool, prune: bool,
    days: int | None, list_only: bool, backup_dir: str | None,
) -> None:
    configure_logging()
    log = get_logger("tpp-backup")
    mgr = BackupManager(backup_dir=Path(backup_dir) if backup_dir else None)

    if list_only:
        click.echo(json.dumps(mgr.list_snapshots(), indent=2))
        return

    out: dict = {}
    exit_code = 0
    if wal:
        try:
            out["wal"] = str(mgr.snapshot_wal())
        except Exception as e:  # noqa: BLE001
            log.error("wal_snapshot_failed", error=str(e))
            out["wal_error"] = str(e)
            exit_code = 1
    if db:
        try:
            out["db"] = str(mgr.snapshot_db())
        except Exception as e:  # noqa: BLE001
            log.error("db_snapshot_failed", error=str(e))
            out["db_error"] = str(e)
            exit_code = 1
    if prune:
        try:
            out["pruned"] = mgr.prune_old(days=days)
        except Exception as e:  # noqa: BLE001
            log.error("prune_failed", error=str(e))
            out["prune_error"] = str(e)
            exit_code = 1

    click.echo(json.dumps(out, indent=2, sort_keys=True))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
