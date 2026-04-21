"""Vigilante — sidecar monitoring layer for the critical trading stack.

Three jobs (all optional, toggleable, zero engine-side hooks required):

  * PID-collision detection   — identify duplicate launcher processes for
                                our managed service modules.
  * Heartbeat monitoring      — compare Redis last-seen timestamps to
                                wall-clock and set a reconnect flag on
                                sustained staleness (spike-detected, time-
                                of-day damped).
  * Payload verification      — render what the regime classifier would
                                send right now and assert per-index
                                isolation + unit-normalised IV.

Plus a forensic CLI (`scan`, `reset`) for post-mortem and emergency
recovery.

Entrypoint: `python -m trading.critical.vigilante <daemon|scan|reset>`.
"""

from __future__ import annotations

__all__ = ["checks", "daemon", "forensics"]
