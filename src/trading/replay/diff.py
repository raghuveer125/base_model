"""Compare two replay runs — summary JSON and signals JSONL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

_SIG_KEY = ("strategy", "ts", "instrument", "action")


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = d
    return out


def compare_summaries(a: dict, b: dict) -> list[tuple[str, Any, Any]]:
    """Sorted list of (keypath, a_val, b_val) for leaves that differ."""
    fa, fb = _flatten(a), _flatten(b)
    keys = sorted(set(fa) | set(fb))
    return [(k, fa.get(k, "<missing>"), fb.get(k, "<missing>"))
            for k in keys if fa.get(k) != fb.get(k)]


def _sig_key(sig: dict) -> tuple:
    return tuple(sig.get(k) for k in _SIG_KEY)


def _load_signals(path: Path) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    if not path.exists():
        return out
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            sig = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        out[_sig_key(sig)] = sig
    return out


def compare_signal_jsonl(
    path_a: Path, path_b: Path, *, sample: int = 5,
) -> dict:
    """Diff two signal jsonl files keyed on (strategy, ts, instrument, action)."""
    a = _load_signals(Path(path_a))
    b = _load_signals(Path(path_b))
    only_a = [a[k] for k in sorted(set(a) - set(b))]
    only_b = [b[k] for k in sorted(set(b) - set(a))]
    shared = sorted(set(a) & set(b))
    differing = []
    for k in shared:
        if a[k] != b[k]:
            differing.append({"key": k, "a": a[k], "b": b[k]})
    return {
        "a_total": len(a),
        "b_total": len(b),
        "only_in_a": len(only_a),
        "only_in_b": len(only_b),
        "differing": len(differing),
        "only_in_a_sample": only_a[:sample],
        "only_in_b_sample": only_b[:sample],
        "differing_sample": differing[:sample],
        "identical": len(only_a) == 0 and len(only_b) == 0 and len(differing) == 0,
    }
