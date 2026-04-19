"""Write-Ahead Log — durable record of every raw tick BEFORE processing.

Format: newline-delimited JSON, one record per line:
    {"seq":int, "ts_ns":int, "kind":"raw_tick"|"meta", "data": <payload>}

Files:
    {WAL_DIR}/{YYYY-MM-DD}.jsonl           — primary file for the UTC date
    {WAL_DIR}/{YYYY-MM-DD}.N.jsonl         — rotated segments when size cap hit
    {WAL_DIR}/.seq                          — monotonic sequence counter (persisted)

Durability: fsync every WAL_FSYNC_EVERY_N appends, on rotation, and on close().
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import orjson

from trading.config import get_settings
from trading.logging_setup import get_logger

log = get_logger(__name__)


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class WALWriter:
    """Thread-safe append-only writer with rotation and fsync policy."""

    def __init__(self, wal_dir: Path | None = None) -> None:
        s = get_settings()
        self.wal_dir = Path(wal_dir) if wal_dir else s.wal_dir
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = s.wal_max_file_mb * 1024 * 1024
        self.fsync_every_n = s.wal_fsync_every_n

        self._lock = threading.Lock()
        self._seq = self._load_seq()
        self._since_sync = 0
        self._file = None  # type: ignore[assignment]
        self._path: Path | None = None
        self._date: str | None = None
        self._rotation_idx = 0
        self._open_current_file()

    def _seq_path(self) -> Path:
        return self.wal_dir / ".seq"

    def _load_seq(self) -> int:
        p = self._seq_path()
        if not p.exists():
            return 0
        try:
            return int(p.read_text().strip() or "0")
        except Exception:  # noqa: BLE001
            log.warning("wal_seq_load_failed", path=str(p))
            return 0

    def _persist_seq(self) -> None:
        p = self._seq_path()
        tmp = p.with_suffix(".seq.tmp")
        tmp.write_text(str(self._seq))
        os.replace(tmp, p)

    def _current_path(self, date: str, idx: int) -> Path:
        return self.wal_dir / (f"{date}.jsonl" if idx == 0 else f"{date}.{idx}.jsonl")

    def _open_current_file(self) -> None:
        date = _utc_date()
        idx = 0
        while True:
            path = self._current_path(date, idx)
            if not path.exists() or path.stat().st_size < self.max_bytes:
                break
            idx += 1
        self._path = path
        self._date = date
        self._rotation_idx = idx
        self._file = open(path, "ab", buffering=0)  # noqa: SIM115
        log.info("wal_open", path=str(path))

    def _maybe_rotate(self) -> None:
        if self._file is None or self._path is None:
            return
        date = _utc_date()
        if date != self._date:
            self._close_file()
            self._open_current_file()
            return
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= self.max_bytes:
            self._close_file()
            self._rotation_idx += 1
            self._path = self._current_path(self._date or date, self._rotation_idx)
            self._file = open(self._path, "ab", buffering=0)  # noqa: SIM115
            log.info("wal_rotated", path=str(self._path))

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            finally:
                self._file.close()
                self._file = None  # type: ignore[assignment]

    def append(self, data: Any, kind: str = "raw_tick") -> int:
        with self._lock:
            self._maybe_rotate()
            self._seq += 1
            record = {"seq": self._seq, "ts_ns": time.time_ns(), "kind": kind, "data": data}
            line = orjson.dumps(record) + b"\n"
            assert self._file is not None
            self._file.write(line)
            self._since_sync += 1
            if self.fsync_every_n == 0 or self._since_sync >= self.fsync_every_n:
                os.fsync(self._file.fileno())
                self._persist_seq()
                self._since_sync = 0
            return self._seq

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._persist_seq()
                self._since_sync = 0

    def close(self) -> None:
        with self._lock:
            self._persist_seq()
            self._close_file()
            log.info("wal_closed", seq=self._seq)


class WALReader:
    """Iterate records in seq order across all segments for one or all dates."""

    def __init__(self, wal_dir: Path | None = None) -> None:
        self.wal_dir = Path(wal_dir) if wal_dir else get_settings().wal_dir

    def list_segments(self, date: str | None = None) -> list[Path]:
        pattern = f"{date}*.jsonl" if date else "*.jsonl"
        segs = list(self.wal_dir.glob(pattern))

        def _key(p: Path) -> tuple[str, int]:
            stem = p.stem
            if "." in stem:
                date_part, idx = stem.rsplit(".", 1)
                return (date_part, int(idx)) if idx.isdigit() else (stem, 0)
            return (stem, 0)

        return sorted(segs, key=_key)

    def iter_records(self, date: str | None = None) -> Iterator[dict]:
        for seg in self.list_segments(date):
            with open(seg, "rb") as f:
                for lineno, raw in enumerate(f, start=1):
                    if not raw.strip():
                        continue
                    try:
                        yield orjson.loads(raw)
                    except orjson.JSONDecodeError as e:
                        log.warning("wal_bad_line", file=str(seg), line=lineno, error=str(e))
                        continue


_writer_lock = threading.Lock()
_writer: WALWriter | None = None


def get_writer() -> WALWriter:
    global _writer
    if _writer is not None:
        return _writer
    with _writer_lock:
        if _writer is None:
            _writer = WALWriter()
    return _writer


def shutdown_writer() -> None:
    global _writer
    with _writer_lock:
        if _writer is not None:
            _writer.close()
            _writer = None
