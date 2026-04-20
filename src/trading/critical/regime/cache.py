"""Replay-safe cache of LLM regime responses.

Key = `(index, minute_bucket, sha256(user_prompt))`. Stored in Redis
(`tpp:critical:regime_cache:<key>`) so a replay of the same WAL at a
later date hits the cached JSON instead of a fresh LLM call, yielding
deterministic backtests.

The cache is namespaced entirely under `tpp:critical:` — deleting the
`critical` folder leaves this Redis key space orphaned but harmless
(nothing in the base model reads or writes it).
"""

from __future__ import annotations

import hashlib
import orjson

from trading.storage import get_redis


_CACHE_KEY = "tpp:critical:regime_cache:{index}:{bucket}:{digest}"
# TTL long enough for a trading session replay, short enough that stale
# days don't bloat Redis forever.
_CACHE_TTL_S = 14 * 86_400


def _digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def cache_key(index: str, minute_bucket: int, user_prompt: str) -> str:
    return _CACHE_KEY.format(
        index=index, bucket=minute_bucket, digest=_digest(user_prompt),
    )


def get(index: str, minute_bucket: int, user_prompt: str) -> dict | None:
    r = get_redis()
    raw = r.get(cache_key(index, minute_bucket, user_prompt))
    if not raw:
        return None
    try:
        return orjson.loads(raw)
    except (ValueError, TypeError):
        return None


def put(index: str, minute_bucket: int, user_prompt: str, payload: dict) -> None:
    r = get_redis()
    r.set(
        cache_key(index, minute_bucket, user_prompt),
        orjson.dumps(payload),
        ex=_CACHE_TTL_S,
    )


def minute_bucket(ts_ms: int, interval_s: int) -> int:
    """Bucket `ts_ms` into `interval_s`-aligned windows.

    Two calls within the same interval hit the same cache key — which
    is exactly what we want for "only run the LLM every 15 min".
    """
    return (ts_ms // 1000) // max(interval_s, 1)
