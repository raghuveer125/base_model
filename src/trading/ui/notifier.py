"""Alert notification dispatcher — Slack, Email, generic Webhook.

- Pluggable sinks, each enabled by env. Dispatch is all-or-nothing per alert:
  "delivered" iff at least one sink succeeded.
- Per-rule dedup via Redis `tpp:alert:last_fired:{rule}` with cooldown
  `NOTIFY_DEDUP_SECONDS`. A rule re-fires only after its cooldown.
- Resolution notifications: when a previously-fired rule no longer appears in
  active alerts, emit one-shot "resolved" message and clear state.
- Severity filter: only alerts ≥ `NOTIFY_MIN_SEVERITY` dispatch.
- Background thread polls `evaluate_health()` every `NOTIFY_POLL_SECONDS`.
  Tests call `.tick()` directly (no thread, no sleep).
"""

from __future__ import annotations

import json
import smtplib
import threading
import time
from email.message import EmailMessage
from typing import Callable, Protocol

import httpx
import orjson

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.storage import get_redis
from trading.ui.health import evaluate_health

log = get_logger(__name__)

SEVERITY_RANK = {"info": 0, "warn": 1, "crit": 2}
_HISTORY_KEY = "tpp:alert:history"


class Sink(Protocol):
    name: str
    def send(self, subject: str, body: dict) -> bool: ...


class SlackSink:
    name = "slack"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, subject: str, body: dict) -> bool:
        text = f"*{subject}*\n```\n{json.dumps(body, indent=2)}\n```"
        try:
            r = httpx.post(self.webhook_url, json={"text": text}, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("slack_send_failed", error=str(e))
            return False


class WebhookSink:
    name = "webhook"

    def __init__(self, url: str, headers: dict | None = None) -> None:
        self.url = url
        self.headers = headers or {}

    def send(self, subject: str, body: dict) -> bool:
        payload = {"subject": subject, **body}
        try:
            r = httpx.post(self.url, json=payload, headers=self.headers, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("webhook_send_failed", error=str(e))
            return False


class EmailSink:
    name = "email"

    def __init__(
        self, host: str, port: int, username: str, password: str,
        from_addr: str, to_addrs: str, *, use_tls: bool = True,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]
        self.use_tls = use_tls

    def send(self, subject: str, body: dict) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.set_content(json.dumps(body, indent=2))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as s:
                if self.use_tls:
                    s.starttls()
                if self.username:
                    s.login(self.username, self.password)
                s.send_message(msg)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("email_send_failed", error=str(e))
            return False


def build_sinks() -> list[Sink]:
    s = get_settings()
    sinks: list[Sink] = []
    if s.slack_webhook_url:
        sinks.append(SlackSink(s.slack_webhook_url))
    if s.webhook_url:
        headers: dict = {}
        if s.webhook_headers_json:
            try:
                headers = json.loads(s.webhook_headers_json)
            except Exception:  # noqa: BLE001
                log.warning("webhook_headers_json_invalid")
        sinks.append(WebhookSink(s.webhook_url, headers))
    if s.smtp_host and s.smtp_from and s.smtp_to:
        sinks.append(EmailSink(
            s.smtp_host, s.smtp_port, s.smtp_username, s.smtp_password,
            s.smtp_from, s.smtp_to,
        ))
    return sinks


class AlertNotifier:
    """Stateful notification dispatcher. Test seams: evaluator, sinks_builder, clock."""

    def __init__(
        self,
        *,
        evaluator: Callable[[], dict] = evaluate_health,
        sinks_builder: Callable[[], list[Sink]] = build_sinks,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.evaluator = evaluator
        self.sinks_builder = sinks_builder
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        s = get_settings()
        if not s.notify_enabled:
            log.info("notifier_disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="alert-notifier",
        )
        self._thread.start()
        log.info("notifier_started",
                 poll_s=s.notify_poll_seconds,
                 min_severity=s.notify_min_severity,
                 dedup_s=s.notify_dedup_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        interval = max(1, get_settings().notify_poll_seconds)
        while not self._stop.wait(interval):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.error("notifier_tick_failed", error=str(e))

    def tick(self) -> dict:
        s = get_settings()
        min_rank = SEVERITY_RANK.get(s.notify_min_severity, 2)

        sinks = self.sinks_builder()
        report = self.evaluator()

        eligible = [
            a for a in (report.get("alerts") or [])
            if SEVERITY_RANK.get(a.get("severity"), 0) >= min_rank
        ]
        eligible_rules = {a["rule"] for a in eligible}

        r = get_redis()
        now_ms = int(self.clock() * 1000)
        cd_ms = s.notify_dedup_seconds * 1000

        dispatched = 0
        failed_delivery = 0
        skipped = 0
        for a in eligible:
            key = _last_fired_key(a["rule"])
            raw = r.get(key)
            last = int(raw) if raw else 0
            if last != 0 and now_ms - last <= cd_ms:
                skipped += 1
                continue
            payload = {
                "rule": a["rule"],
                "severity": a["severity"],
                "detail": a.get("detail", ""),
                "status": report.get("status"),
                "ts_ms": now_ms,
            }
            subject = (f"[{a['severity'].upper()}] {a['rule']} "
                       f"({report.get('status', 'unknown')})")
            delivered = _fanout(sinks, subject, payload) if sinks else False
            # Always mark the fire transition so dedup engages; a failing sink
            # does not retry, but the active-alerts view still surfaces it.
            r.set(key, now_ms, ex=max(s.notify_dedup_seconds * 2, 60))
            r.delete(_resolved_key(a["rule"]))
            _push_history(r, {
                "kind": "fired",
                "subject": subject,
                **payload,
                "delivered": delivered,
                "sink_count": len(sinks),
            }, max_entries=s.notify_history_max)
            if delivered:
                dispatched += 1
            else:
                failed_delivery += 1

        resolved = _emit_resolutions(
            r, eligible_rules, sinks,
            report.get("status"), now_ms,
            max_entries=s.notify_history_max,
        )

        return {
            "dispatched": dispatched, "skipped": skipped,
            "failed_delivery": failed_delivery,
            "resolved": resolved,
            "no_sinks": not sinks, "active": len(eligible),
        }

    def tick_test(self) -> dict:
        """Send a canned test payload to every configured sink."""
        sinks = self.sinks_builder()
        if not sinks:
            return {"sent": 0, "attempted": 0, "sinks": []}
        payload = {
            "rule": "_test_",
            "severity": "info",
            "detail": "manual test from /api/notifications/test",
            "status": "test",
            "ts_ms": int(self.clock() * 1000),
        }
        sent = 0
        names: list[str] = []
        for sink in sinks:
            names.append(sink.name)
            try:
                if sink.send("[TEST] Trading_Plug&Play alert pipeline", payload):
                    sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("sink_raised", name=sink.name, error=str(e))
        return {"sent": sent, "attempted": len(sinks), "sinks": names}


def _last_fired_key(rule: str) -> str:
    return f"tpp:alert:last_fired:{rule}"


def _resolved_key(rule: str) -> str:
    return f"tpp:alert:resolved_sent:{rule}"


def _fanout(sinks: list[Sink], subject: str, payload: dict) -> bool:
    any_ok = False
    for sink in sinks:
        try:
            if sink.send(subject, payload):
                any_ok = True
        except Exception as e:  # noqa: BLE001
            log.warning("sink_raised", name=getattr(sink, "name", "?"), error=str(e))
    return any_ok


def _emit_resolutions(
    r, eligible_rules: set[str], sinks: list[Sink],
    status: str | None, now_ms: int, *, max_entries: int,
) -> int:
    sent = 0
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match="tpp:alert:last_fired:*", count=64)
        for raw in keys or []:
            key_s = raw.decode() if isinstance(raw, bytes) else raw
            rule = key_s.split(":", 3)[-1]
            if rule in eligible_rules:
                continue
            resolved_marker = _resolved_key(rule)
            if r.get(resolved_marker):
                continue
            payload = {
                "rule": rule, "severity": "resolved",
                "detail": f"{rule} cleared",
                "status": status, "ts_ms": now_ms,
            }
            subject = f"[RESOLVED] {rule}"
            delivered = _fanout(sinks, subject, payload) if sinks else False
            # Mark as resolved regardless of delivery so we don't spam the log.
            r.set(resolved_marker, 1, ex=3600)
            r.delete(key_s)
            _push_history(r, {
                "kind": "resolved",
                "subject": subject,
                **payload,
                "delivered": delivered,
                "sink_count": len(sinks),
            }, max_entries=max_entries)
            if delivered:
                sent += 1
        if cursor == 0:
            break
    return sent


def _push_history(r, entry: dict, *, max_entries: int) -> None:
    try:
        r.lpush(_HISTORY_KEY, orjson.dumps(entry))
        if max_entries > 0:
            r.ltrim(_HISTORY_KEY, 0, max_entries - 1)
    except Exception as e:  # noqa: BLE001
        log.warning("history_push_failed", error=str(e))


def read_history(limit: int = 50) -> list[dict]:
    """Return newest-first alert history entries (capped at `limit`)."""
    try:
        r = get_redis()
        raw = r.lrange(_HISTORY_KEY, 0, max(0, limit - 1))
    except Exception as e:  # noqa: BLE001
        log.warning("history_read_failed", error=str(e))
        return []
    out: list[dict] = []
    for b in raw or []:
        try:
            out.append(orjson.loads(b))
        except Exception:  # noqa: BLE001
            continue
    return out
