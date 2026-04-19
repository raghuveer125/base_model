"""Strategy framework (Phase 4).

Public surface:
  Strategy          — ABC a strategy implements.
  StrategyContext   — read-only handle passed to on_* callbacks.
  register          — class decorator that adds a Strategy to the global registry.
  STRATEGY_REGISTRY — name -> Strategy subclass.

Importing a module that defines a strategy triggers self-registration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading.strategies.base import Strategy

STRATEGY_REGISTRY: dict[str, type["Strategy"]] = {}


def register(name: str):
    def _wrap(cls):
        if name in STRATEGY_REGISTRY and STRATEGY_REGISTRY[name] is not cls:
            raise RuntimeError(f"duplicate strategy name: {name!r}")
        STRATEGY_REGISTRY[name] = cls
        cls.name = name
        return cls
    return _wrap


# Bundled strategies self-register on package import.
from trading.strategies import heartbeat  # noqa: E402,F401 — register side effect
