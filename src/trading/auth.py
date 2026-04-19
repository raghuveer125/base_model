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

AUTH_BASE = "https://api-t2.fyers.in"   # TOTP auto-login flow (vagator)
API_BASE = "https://api-t1.fyers.in"    # OAuth + token exchange (APIv3)

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


def manual_auth_url() -> str:
    """Build the Fyers OAuth authorize URL for browser login (APIv3)."""
    s = get_settings()
    from urllib.parse import urlencode
    qs = urlencode({
        "client_id": s.fyers_client_id,
        "redirect_uri": s.fyers_redirect_uri,
        "response_type": "code",
        "state": "sample",
    })
    return f"{API_BASE}/api/v3/generate-authcode?{qs}"


def capture_auth_code_via_loopback(timeout_s: int = 180) -> str:
    """Open the Fyers auth URL in a browser, catch the redirect on
    `FYERS_REDIRECT_URI`'s host:port, return the auth_code.

    Uses the OAuth loopback-interface flow: a one-shot HTTP server binds
    to the redirect host/port, serves whatever GET hits '/' or any path,
    and extracts the `auth_code` query param.
    """
    import threading
    import time
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    s = get_settings()
    uri = urlparse(s.fyers_redirect_uri)
    host = uri.hostname or "127.0.0.1"
    port = uri.port or 8080

    captured: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("auth_code") or qs.get("code") or [None])[0]
            err = qs.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if code:
                captured["auth_code"] = code
                self.wfile.write(
                    b"<html><body style='font-family:sans-serif;background:#0e1116;"
                    b"color:#3fb950;padding:2rem'><h2>auth_code captured</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )
            elif err:
                captured["error"] = err
                self.wfile.write(
                    f"<html><body><h2>Login error</h2><pre>{err}</pre></body></html>"
                    .encode()
                )
            else:
                self.wfile.write(b"<html><body>waiting...</body></html>")

        def log_message(self, *_a, **_kw):  # silence default stdout spam
            return

    server = HTTPServer((host, port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("loopback_listening", host=host, port=port)

    url = manual_auth_url()
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass  # user can open it manually
    log.info("loopback_browser_opened", url=url)

    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            if "auth_code" in captured or "error" in captured:
                break
            time.sleep(0.25)
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001
            pass

    if "error" in captured:
        raise FyersAuthError(f"login error: {captured['error']}")
    if "auth_code" not in captured:
        raise FyersAuthError(
            f"timeout after {timeout_s}s waiting for auth_code — "
            f"if login succeeded, re-run with --auth-code <value>"
        )
    return captured["auth_code"]


def exchange_auth_code(auth_code: str) -> str:
    """Exchange an auth_code (captured from the redirect URL) for an access token
    and cache it in Redis. Use when TOTP auto-login is unavailable."""
    import time
    auth = FyersAuth()
    with httpx.Client(timeout=20.0) as client:
        token = auth._exchange_auth_code(client, auth_code)
    now_ms = int(time.time() * 1000)
    auth.store.set_token({
        "access_token": token,
        "fetched_at": now_ms,
        "expires_at": now_ms + TOKEN_SOFT_TTL_S * 1000,
    })
    return token
