"""Replay subpackage — run strategies against historical data deterministically."""

from trading.replay.diff import compare_signal_jsonl, compare_summaries
from trading.replay.engine import ReplayEngine, ReplaySummary
from trading.replay.sources import (
    EventKind,
    EventSource,
    MergedEventSource,
    PostgresEventSource,
    ReplayEvent,
    WALEventSource,
)

__all__ = [
    "EventKind",
    "EventSource",
    "MergedEventSource",
    "PostgresEventSource",
    "ReplayEngine",
    "ReplayEvent",
    "ReplaySummary",
    "WALEventSource",
    "compare_signal_jsonl",
    "compare_summaries",
]
