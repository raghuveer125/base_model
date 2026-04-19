"""Event bus — thin wrapper over Redis pub/sub.

All inter-module communication in Phase 1 flows through these channels.
Publishers do not block on subscribers; if nobody is listening, the message is dropped.
"""

from __future__ import annotations

from typing import Callable, Iterable

import orjson
import redis

from trading.config import get_settings
from trading.logging_setup import get_logger

log = get_logger(__name__)


def ch_index_tick(index: str) -> str:
    return f"ticks.index.{index.upper()}"


def ch_option_tick(index: str) -> str:
    return f"ticks.option.{index.upper()}"


def ch_option_chain(index: str) -> str:
    return f"option_chain.{index.upper()}"


def ch_candle(index: str, timeframe: str) -> str:
    return f"candles.{index.upper()}.{timeframe}"


def ch_greeks(index: str) -> str:
    return f"greeks.{index.upper()}"


ALL_INGEST_CHANNELS = ("ticks.index.*", "ticks.option.*", "option_chain.*")
ALL_CANDLE_CHANNELS = ("candles.*",)
ALL_GREEKS_CHANNELS = ("greeks.*",)


class EventBus:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client = client or redis.Redis.from_url(
            get_settings().redis_url, decode_responses=False
        )

    def publish(self, channel: str, payload: dict | bytes) -> int:
        body = payload if isinstance(payload, (bytes, bytearray)) else orjson.dumps(payload)
        try:
            return int(self._client.publish(channel, body))
        except redis.RedisError as e:
            log.error("event_publish_failed", channel=channel, error=str(e))
            return 0

    def subscribe(
        self,
        patterns: Iterable[str],
        handler: Callable[[str, dict], None],
    ) -> None:
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        pubsub.psubscribe(*patterns)
        log.info("event_subscribed", patterns=list(patterns))
        try:
            for msg in pubsub.listen():
                if msg.get("type") != "pmessage":
                    continue
                raw_ch = msg["channel"]
                ch = raw_ch.decode() if isinstance(raw_ch, bytes) else raw_ch
                try:
                    data = orjson.loads(msg["data"])
                    handler(ch, data)
                except Exception as e:  # noqa: BLE001
                    log.error("event_handler_failed", channel=ch, error=str(e))
        finally:
            pubsub.close()

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
