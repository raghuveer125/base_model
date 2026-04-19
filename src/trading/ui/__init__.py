"""FastAPI UI service — REST + WebSocket bridge over Redis pub/sub."""

from trading.ui.app import create_app

__all__ = ["create_app"]
