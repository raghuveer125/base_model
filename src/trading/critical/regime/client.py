"""Anthropic SDK wrapper — with prompt caching, JSON-only parsing,
and a graceful degradation path to the deterministic fallback.

No hard dep on the `anthropic` package: import is lazy so the base
suite can run without it installed. If the package is missing, or the
API key is blank, or the call times out, the fallback classifier is
used and the decision is tagged `source: "fallback"`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from trading.critical.config import CriticalConfig, load_config
from trading.critical.regime import cache as cache_mod
from trading.critical.regime import fallback as fb
from trading.critical.regime.prompt import (
    SYSTEM_PROMPT, RegimeInput, build_user_prompt,
)
from trading.critical.regime.schema import RegimeDecision
from trading.logging_setup import get_logger

log = get_logger(__name__)


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _parse_json(text: str) -> dict | None:
    """LLMs occasionally wrap JSON in backticks/prose — extract the first
    top-level object and try to load it. Returns None on failure."""
    m = _JSON_BLOCK.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


class RegimeClient:
    """Classifies regime for one index. Re-used across calls so the SDK
    client / HTTP session is pooled."""

    def __init__(self, cfg: CriticalConfig | None = None) -> None:
        self._cfg = cfg or load_config()
        self._anthropic: Any = None   # lazy
        self._init_failed = False

    # ---- lazy SDK init ----

    def _client(self):
        if self._anthropic is not None or self._init_failed:
            return self._anthropic
        if not self._cfg.anthropic_api_key:
            self._init_failed = True
            return None
        try:
            import anthropic  # type: ignore[import-not-found]
            self._anthropic = anthropic.Anthropic(
                api_key=self._cfg.anthropic_api_key,
            )
            return self._anthropic
        except Exception as e:  # noqa: BLE001 — optional dep
            log.warning("anthropic_sdk_unavailable", error=str(e))
            self._init_failed = True
            return None

    # ---- main entry point ----

    def classify(self, snap: RegimeInput, ts_ms: int) -> RegimeDecision:
        user_prompt = build_user_prompt(snap)
        bucket = cache_mod.minute_bucket(ts_ms, self._cfg.regime_interval_s)

        cached = cache_mod.get(snap.index, bucket, user_prompt)
        if cached:
            return RegimeDecision(**{**cached, "source": "cache"})

        raw = self._call_llm(user_prompt)
        if raw is not None:
            parsed = _parse_json(raw)
            if parsed:
                try:
                    decision = RegimeDecision(**{**parsed, "source": "llm"})
                    cache_mod.put(snap.index, bucket, user_prompt, parsed)
                    return decision
                except Exception as e:  # noqa: BLE001
                    log.warning("regime_parse_failed", error=str(e), raw=raw[:200])

        # Degraded path — still useful, still gates trades.
        payload = fb.classify(snap)
        try:
            # cache fallback too — replay determinism matters even in
            # degraded mode.
            cache_mod.put(snap.index, bucket, user_prompt, payload)
        except Exception:   # noqa: BLE001
            pass
        return RegimeDecision(**payload)

    # ---- private ----

    def _call_llm(self, user_prompt: str) -> str | None:
        client = self._client()
        if client is None:
            return None
        start = time.monotonic()
        try:
            resp = client.messages.create(
                model=self._cfg.anthropic_model,
                max_tokens=120,
                # Prompt caching: the system block stays identical across
                # every call, so subsequent calls within the 5-min cache
                # window get a ~85% input-token discount.
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("regime_llm_call_failed", error=str(e))
            return None
        dt_ms = int((time.monotonic() - start) * 1000)
        try:
            blocks = getattr(resp, "content", None) or []
            text = "".join(
                getattr(b, "text", "") for b in blocks
                if getattr(b, "type", "") == "text"
            )
        except Exception as e:  # noqa: BLE001
            log.warning("regime_llm_parse_failed", error=str(e))
            return None
        log.info("regime_llm_ok", latency_ms=dt_ms, chars=len(text))
        return text
