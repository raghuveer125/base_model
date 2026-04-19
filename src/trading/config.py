"""Central configuration loaded from environment (.env).

Single source of truth for all runtime settings. Every module imports `get_settings()`
from here — do not re-read env vars elsewhere.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Fyers
    fyers_client_id: str
    fyers_secret_key: str
    fyers_redirect_uri: str
    fyers_app_id: str
    fyers_app_type: str = "100"
    fyers_fy_id: str
    fyers_pin: str
    fyers_totp_secret: str

    # Storage
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://tpp:tpp@localhost:5432/tpp"

    # WAL
    wal_dir: Path = Path("./wal")
    wal_fsync_every_n: int = 50
    wal_max_file_mb: int = 512

    # Logging
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_dir: Path = Path("./logs")

    # Market
    market_tz: str = "Asia/Kolkata"
    indices: str = "NIFTY50,BANKNIFTY,SENSEX"
    atm_strike_window: int = 10
    retention_days: int = 2

    # WS reliability
    ws_reconnect_max_backoff_s: int = 60
    ws_freeze_threshold_s: int = 5
    ws_gap_max_seconds: int = 10

    # Pipeline
    ingest_queue_maxsize: int = 50_000
    dedup_cache_size: int = 200_000

    # Greeks (Phase 3)
    risk_free_rate: float = 0.065                   # India 10Y G-sec approx; per annum
    greeks_spot_trigger_points: float = 2.0         # chain-wide recompute on spot move ≥ this
    greeks_cache_maxsize: int = 4096

    # Strategies (Phase 4) — framework only
    strategies_enabled: str = "heartbeat"           # csv list of registered names
    strategy_cooldown_seconds: int = 300            # per-(strategy, instrument) throttle
    strategy_max_signals_per_hour: int = 20
    strategy_max_signals_per_day: int = 100
    strategy_signal_log_file: str = "signals.jsonl"

    # UI (Phase 6)
    ui_host: str = "127.0.0.1"
    ui_port: int = 8088

    # Health + alerting
    health_metrics_stale_after_s: int = 30
    health_warn_latency_p95_ms: int = 500
    health_crit_latency_p95_ms: int = 2000
    health_warn_gaps: int = 10
    health_crit_gaps: int = 100
    health_warn_reconnects: int = 3
    health_crit_reconnects: int = 10
    health_warn_staleness_s: int = 15
    health_crit_staleness_s: int = 60

    @property
    def strategy_list(self) -> list[str]:
        return [s.strip() for s in self.strategies_enabled.split(",") if s.strip()]

    @field_validator("wal_dir", "log_dir", mode="before")
    @classmethod
    def _coerce_path(cls, v: object) -> Path:
        return Path(str(v))

    @property
    def index_list(self) -> list[str]:
        return [s.strip().upper() for s in self.indices.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.wal_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    return s
