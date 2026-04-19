"""Backup manager tests — WAL tarball, pg_dump mock, retention prune."""

from __future__ import annotations

import gzip
import os
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.backup import BackupManager


def _seed_wal(tmp_path: Path) -> Path:
    wal = tmp_path / "wal"
    wal.mkdir()
    (wal / "2026-04-19.jsonl").write_text('{"seq":1,"kind":"raw_tick","data":{}}\n')
    (wal / "2026-04-20.jsonl").write_text('{"seq":2,"kind":"raw_tick","data":{}}\n')
    (wal / ".seq").write_text("2")
    return wal


def _patch_settings_paths(monkeypatch, tmp_path: Path) -> None:
    wal = _seed_wal(tmp_path)
    bks = tmp_path / "backups"
    monkeypatch.setenv("WAL_DIR", str(wal))
    monkeypatch.setenv("BACKUP_DIR", str(bks))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "7")
    from trading import config as _config
    _config.get_settings.cache_clear()


def test_snapshot_wal_creates_tar_gz_with_all_segments(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    mgr = BackupManager()
    archive = mgr.snapshot_wal()

    assert archive.exists()
    assert archive.suffixes[-2:] == [".tar", ".gz"]

    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
    assert any(n.endswith("2026-04-19.jsonl") for n in names)
    assert any(n.endswith("2026-04-20.jsonl") for n in names)
    assert any(n.endswith(".seq") for n in names)


def test_snapshot_db_runs_pg_dump_and_gzips_output(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    canned_dump = b"-- fake pg_dump\nCREATE TABLE t();\n"

    def _fake_run(argv, capture_output, check):  # noqa: ARG001
        assert argv[0] == "pg_dump"
        return SimpleNamespace(returncode=0, stdout=canned_dump, stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    mgr = BackupManager()
    out = mgr.snapshot_db()
    assert out.exists()
    assert out.name.startswith("pg-") and out.name.endswith(".sql.gz")
    with gzip.open(out, "rb") as gz:
        body = gz.read()
    assert body == canned_dump


def test_snapshot_db_raises_on_nonzero_rc(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom"),
    )
    mgr = BackupManager()
    with pytest.raises(RuntimeError, match="pg_dump failed"):
        mgr.snapshot_db()


def test_prune_old_removes_only_stale_by_mtime(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    mgr = BackupManager()
    now = time.time()
    stale_ts = now - 10 * 86_400
    fresh_ts = now - 1 * 86_400
    for name, ts in [
        ("wal-old1.tar.gz", stale_ts),
        ("wal-old2.tar.gz", stale_ts),
        ("wal-new.tar.gz", fresh_ts),
        ("pg-old.sql.gz", stale_ts),
        ("pg-new.sql.gz", fresh_ts),
        ("unrelated.txt", stale_ts),
    ]:
        p = mgr.backup_dir / name
        p.write_bytes(b"x")
        os.utime(p, (ts, ts))

    removed = mgr.prune_old(days=7)
    assert removed == {"wal": 2, "pg": 1}
    assert (mgr.backup_dir / "unrelated.txt").exists()
    assert (mgr.backup_dir / "wal-new.tar.gz").exists()
    assert (mgr.backup_dir / "pg-new.sql.gz").exists()


def test_list_snapshots_returns_only_wal_and_pg_prefixed(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    mgr = BackupManager()
    for name in ("wal-a.tar.gz", "pg-b.sql.gz", "random.bin"):
        (mgr.backup_dir / name).write_bytes(b"x")
    out = mgr.list_snapshots()
    names = {e["name"] for e in out}
    assert "wal-a.tar.gz" in names
    assert "pg-b.sql.gz" in names
    assert "random.bin" not in names
    kinds = {e["kind"] for e in out}
    assert kinds == {"wal", "pg"}
