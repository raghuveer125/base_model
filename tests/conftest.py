"""Shared pytest fixtures. Patches env so Settings() validates in isolation."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _dummy_env(tmp_path, monkeypatch) -> Iterator[None]:
    env = {
        "FYERS_CLIENT_ID": "XX-100",
        "FYERS_SECRET_KEY": "secret",
        "FYERS_REDIRECT_URI": "https://127.0.0.1:8080/",
        "FYERS_APP_ID": "XX",
        "FYERS_APP_TYPE": "100",
        "FYERS_FY_ID": "AB12345",
        "FYERS_PIN": "1234",
        "FYERS_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        "WAL_DIR": str(tmp_path / "wal"),
        "LOG_DIR": str(tmp_path / "logs"),
        "LOG_FORMAT": "console",
        "WAL_FSYNC_EVERY_N": "0",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    from trading import config as _config
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()
