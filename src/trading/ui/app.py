"""FastAPI app factory.

Mounts:
  /api/*         REST (indices, state, chain, candles, signals, health, metrics,
                 replays, notifications)
  /ws/{index}    WebSocket bridge over Redis pub/sub
  /              static single-page UI

Lifespan: spawns the AlertNotifier background thread on startup; stops on
shutdown. Disabled unless NOTIFY_ENABLED=true.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from trading.logging_setup import configure_logging, get_logger
from trading.ui.notifier import AlertNotifier
from trading.ui.rest import router as rest_router
from trading.ui.ws import router as ws_router

log = get_logger(__name__)

STATIC_DIR = Path(__file__).with_name("static")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    notifier = AlertNotifier()
    app.state.notifier = notifier
    try:
        notifier.start()
        yield
    finally:
        notifier.stop()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Trading_Plug&Play UI",
        version="0.6.0",
        lifespan=_lifespan,
    )
    app.include_router(rest_router, prefix="/api")
    app.include_router(ws_router)  # /ws/{index}
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    log.info("ui_app_created", static=str(STATIC_DIR))
    return app
