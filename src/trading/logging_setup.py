"""Structlog-based structured logging — call configure_logging() once at process start."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

from trading.config import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    s = get_settings()
    level = getattr(logging, s.log_level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if s.log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s", force=True)

    s.log_dir.mkdir(parents=True, exist_ok=True)
    logfile = s.log_dir / f"{datetime.now(timezone.utc).date().isoformat()}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(fh)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


def log_file_path() -> Path:
    s = get_settings()
    return s.log_dir / f"{datetime.now(timezone.utc).date().isoformat()}.log"
