"""`tpp-ui` — serve the FastAPI UI via uvicorn.

Default: 127.0.0.1:8088. Override via UI_HOST / UI_PORT env or flags.
"""

from __future__ import annotations

import sys

import click
import uvicorn

from trading.config import get_settings
from trading.logging_setup import configure_logging, get_logger


@click.command()
@click.option("--host", default=None, help="Override UI_HOST.")
@click.option("--port", type=int, default=None, help="Override UI_PORT.")
@click.option("--reload", is_flag=True, help="Auto-reload on code change (dev only).")
def main(host: str | None, port: int | None, reload: bool) -> None:
    configure_logging()
    log = get_logger("tpp-ui")
    s = get_settings()
    h = host or s.ui_host
    p = port or s.ui_port
    log.info("tpp_ui_start", host=h, port=p, reload=reload)
    try:
        uvicorn.run(
            "trading.ui:create_app",
            host=h, port=p, reload=reload, factory=True,
            log_level=s.log_level.lower(),
            access_log=False,
        )
    except Exception as e:  # noqa: BLE001
        log.error("tpp_ui_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
