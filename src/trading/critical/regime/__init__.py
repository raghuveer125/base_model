"""LLM regime filter — every 15 min, classify the market state for each index.

Output shape is always JSON:
    {"regime": "trending|ranging|volatile",
     "bias":   "long|short|neutral",
     "confidence": 0..100}

The Python Sniper in `trading.critical.engine` uses this as a gate — it
only fires deterministic entry triggers when the regime agrees with the
trigger direction. On timeout / API error / missing key, a deterministic
fallback (realized-vol + candle-flip count) keeps the system alive.

Every LLM response is cached by `(ts_minute_bucket, sha256(input))` so
replays reproduce the same decisions — no nondeterminism in backtests.
"""
