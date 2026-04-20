"""WebSocket bridge — forwards Redis pub/sub messages to the browser.

One pubsub connection per WS client for isolation. Patterns are scoped to the
requested index so a Nifty tab doesn't receive BankNifty traffic.
"""

from __future__ import annotations

from typing import Any

import orjson
import redis.asyncio as aredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.schemas import Index

log = get_logger(__name__)
router = APIRouter()


def _patterns_for(index: str) -> list[str]:
    # `scalp.*` and `critical.regime.*` are published by trading.critical
    # when it's running. Base UI subscribes anyway — if the critical layer
    # isn't up, Redis simply has nothing on those channels and the UI
    # shows empty widgets. No hard dep on the critical package.
    return [
        f"ticks.index.{index}",
        f"ticks.option.{index}",
        f"candles.{index}.*",
        f"greeks.{index}",
        "signals.*",
        f"scalp.{index}",
        f"critical.regime.{index}",
    ]


@router.websocket("/ws/{index}")
async def ws_index(ws: WebSocket, index: str) -> None:
    if index not in Index._value2member_map_:
        await ws.close(code=1008, reason=f"unknown index: {index}")
        return

    await ws.accept()
    settings = get_settings()
    client: aredis.Redis = aredis.from_url(
        settings.redis_url, decode_responses=False,
    )
    pubsub = client.pubsub()
    patterns = _patterns_for(index)
    try:
        await pubsub.psubscribe(*patterns)
        log.info("ws_open", index=index, patterns=patterns)
        async for msg in pubsub.listen():
            if msg.get("type") != "pmessage":
                continue
            channel = _decode(msg.get("channel"))
            try:
                data: Any = orjson.loads(msg["data"])
            except Exception:  # noqa: BLE001
                continue
            try:
                await ws.send_json({"channel": channel, "data": data})
            except WebSocketDisconnect:
                break
            except Exception as e:  # noqa: BLE001
                log.warning("ws_send_failed", error=str(e))
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("ws_fatal", index=index, error=str(e))
    finally:
        try:
            await pubsub.punsubscribe(*patterns)
        except Exception:  # noqa: BLE001
            pass
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
        log.info("ws_closed", index=index)


def _decode(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v) if v is not None else ""
