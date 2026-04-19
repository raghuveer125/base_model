"""WAL + Postgres backup manager.

Produces two artifact types under `BACKUP_DIR`:
  wal-YYYYMMDDTHHMMSSZ.tar.gz   full tarball of the WAL jsonl segments
  pg-YYYYMMDDTHHMMSSZ.sql.gz    gzipped pg_dump of the Postgres database

Retention is mtime-based: files older than `BACKUP_RETENTION_DAYS` are removed
by `prune_old()`. Designed to be invoked by cron / Task Scheduler or the
`tpp-backup` one-shot CLI.
"""

from __future__ import annotations

import gzip
import shlex
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from trading.config import get_settings
from trading.logging_setup import get_logger

log = get_logger(__name__)


def _utc_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class BackupManager:
    def __init__(self, backup_dir: Path | None = None) -> None:
        s = get_settings()
        self.backup_dir = Path(backup_dir) if backup_dir is not None else s.backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ---- WAL ----

    def snapshot_wal(self, wal_dir: Path | None = None) -> Path:
        s = get_settings()
        src = Path(wal_dir) if wal_dir is not None else s.wal_dir
        if not src.exists():
            raise FileNotFoundError(f"WAL dir missing: {src}")
        out = self.backup_dir / f"wal-{_utc_tag()}.tar.gz"
        with tarfile.open(out, "w:gz") as tf:
            tf.add(src, arcname=src.name)
        size = out.stat().st_size
        log.info("wal_snapshot_written",
                 path=str(out), source=str(src), size_bytes=size)
        return out

    # ---- Postgres ----

    def snapshot_db(self, dsn: str | None = None) -> Path:
        s = get_settings()
        effective_dsn = dsn or s.postgres_dsn
        cmd_str = s.backup_pg_dump_cmd.format(dsn=effective_dsn)
        argv = shlex.split(cmd_str)
        out = self.backup_dir / f"pg-{_utc_tag()}.sql.gz"
        log.info("db_snapshot_begin", path=str(out), cmd=cmd_str)
        proc = subprocess.run(argv, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed: rc={proc.returncode} "
                f"stderr={proc.stderr[:500]!r}"
            )
        with gzip.open(out, "wb") as gz:
            gz.write(proc.stdout)
        log.info("db_snapshot_written",
                 path=str(out), size_bytes=out.stat().st_size)
        return out

    # ---- retention ----

    def prune_old(self, days: int | None = None) -> dict[str, int]:
        s = get_settings()
        d = int(days if days is not None else s.backup_retention_days)
        cutoff_ts = datetime.now(timezone.utc).timestamp() - d * 86_400
        removed = {"wal": 0, "pg": 0}
        for path in self.backup_dir.iterdir():
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime >= cutoff_ts:
                continue
            if path.name.startswith("wal-"):
                path.unlink()
                removed["wal"] += 1
            elif path.name.startswith("pg-"):
                path.unlink()
                removed["pg"] += 1
        log.info("backup_pruned", days=d, removed=removed,
                 root=str(self.backup_dir))
        return removed

    def list_snapshots(self) -> list[dict]:
        out: list[dict] = []
        for path in sorted(self.backup_dir.iterdir(),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file():
                continue
            if not (path.name.startswith("wal-") or path.name.startswith("pg-")):
                continue
            st = path.stat()
            out.append({
                "name": path.name,
                "kind": "wal" if path.name.startswith("wal-") else "pg",
                "size_bytes": st.st_size,
                "mtime_ms": int(st.st_mtime * 1000),
            })
        return out
