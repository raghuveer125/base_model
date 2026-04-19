"""Fyers auto-login via TOTP + access-token cache in Redis.

Flow (Fyers v2):
  1. POST /vagator/v2/send_login_otp_v2
  2. POST /vagator/v2/verify_otp
  3. POST /vagator/v2/verify_pin_v2
  4. POST /api/v2/token  -> redirect URL containing auth_code
  5. POST /api/v3/validate-authcode -> access_token

Tokens are cached in Redis under tpp:token:fyers (see LiveStore).
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
from tenacity import retry, stop_after_attempt, wait_exponential

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.storage import LiveStore

log = get_logger(__name__)

AUTH_BASE = "https://api-t2.fyers.in"
API_BASE = "https://api.fyers.in"

TOKEN_SOFT_TTL_S = 6 * 3600


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _app_id_hash(client_id: str, secret_key: str) -> str:
    return hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()


class FyersAuthError(RuntimeError):
    pass


class FyersAuth:
    def __init__(self, store: LiveStore | None = None) -> None:
        self.store = store or LiveStore()
        self.s = get_settings()

    def get_access_token(self, force_refresh: bool = False) -> str:
        cached = self.store.get_token()
        now_ms = int(time.time() * 1000)
        if not force_refresh and cached:
            age_s = (now_ms - int(cached.get("fetched_at", 0))) / 1000.0
            if age_s < TOKEN_SOFT_TTL_S:
                log.debug("token_cache_hit", age_s=age_s)
                return cached["access_token"]
        log.info("token_refresh_begin", forced=force_refresh)
        token = self._login_flow()
        self.store.set_token({
            "access_token": token,
            "fetched_at": now_ms,
            "expires_at": now_ms + TOKEN_SOFT_TTL_S * 1000,
        })
        log.info("token_refresh_ok", token_masked=f"{token[:6]}...{token[-4:]}")
        return token

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=3))
    def _login_flow(self) -> str:
        with httpx.Client(timeout=20.0) as client:
            request_key = self._send_otp(client)
            request_key = self._verify_otp(client, request_key)
            self._verify_pin(client, request_key)
            auth_code = self._get_auth_code(client, request_key)
            return self._exchange_auth_code(client, auth_code)

    def _send_otp(self, client: httpx.Client) -> str:
        r = client.post(
            f"{AUTH_BASE}/vagator/v2/send_login_otp_v2",
            json={"fy_id": _b64(self.s.fyers_fy_id), "app_id": "2"},
        )
        self._raise_if_error(r, "send_login_otp")
        return r.json()["request_key"]

    def _verify_otp(self, client: httpx.Client, request_key: str) -> str:
        otp = pyotp.TOTP(self.s.fyers_totp_secret).now()
        r = client.post(
            f"{AUTH_BASE}/vagator/v2/verify_otp",
            json={"request_key": request_key, "otp": otp},
        )
        self._raise_if_error(r, "verify_otp")
        return r.json()["request_key"]

    def _verify_pin(self, client: httpx.Client, request_key: str) -> None:
        r = client.post(
            f"{AUTH_BASE}/vagator/v2/verify_pin_v2",
            json={
                "request_key": request_key,
                "identity_type": "pin",
                "identifier": _b64(self.s.fyers_pin),
            },
        )
        self._raise_if_error(r, "verify_pin")

    def _get_auth_code(self, client: httpx.Client, request_key: str) -> str:
        r = client.post(
            f"{API_BASE}/api/v2/token",
            headers={
                "Authorization": f"Bearer {request_key}",
                "Content-Type": "application/json",
            },
            json={
                "fyers_id": self.s.fyers_fy_id,
                "app_id": self.s.fyers_app_id,
                "redirect_uri": self.s.fyers_redirect_uri,
                "appType": self.s.fyers_app_type,
                "code_challenge": "",
                "state": "sample",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            },
        )
        self._raise_if_error(r, "get_auth_code")
        redirect_url = r.json()["Url"]
        qs = parse_qs(urlparse(redirect_url).query)
        codes = qs.get("auth_code") or qs.get("code")
        if not codes:
            raise FyersAuthError(f"no auth_code in redirect: {redirect_url}")
        return codes[0]

    def _exchange_auth_code(self, client: httpx.Client, auth_code: str) -> str:
        r = client.post(
            f"{API_BASE}/api/v3/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash": _app_id_hash(self.s.fyers_client_id, self.s.fyers_secret_key),
                "code": auth_code,
            },
        )
        self._raise_if_error(r, "exchange_auth_code")
        body = r.json()
        token = body.get("access_token")
        if not token:
            raise FyersAuthError(f"no access_token in response: {body}")
        return token

    @staticmethod
    def _raise_if_error(r: httpx.Response, step: str) -> None:
        if r.status_code >= 400:
            raise FyersAuthError(f"{step} HTTP {r.status_code}: {r.text[:200]}")
        try:
            body: Any = r.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict) and body.get("s") == "error":
            raise FyersAuthError(f"{step} API error: {body}")


def ensure_access_token(force_refresh: bool = False) -> str:
    return FyersAuth().get_access_token(force_refresh=force_refresh)
