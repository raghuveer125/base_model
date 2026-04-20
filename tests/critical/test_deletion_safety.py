"""Plug-and-play deletion check.

Without actually removing the folder (destructive in a shared test
run), assert the critical layer never leaks imports out of its folder
— i.e. no file outside `src/trading/critical/` references `trading.critical`.
"""

from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[2]
CRITICAL_ROOT = REPO / "src" / "trading" / "critical"

_FORBIDDEN = re.compile(r"\b(?:from\s+trading\.critical|import\s+trading\.critical)\b")


def test_base_model_does_not_import_critical():
    """Grep every Python file outside `critical/` and `tests/critical/`
    for an import of `trading.critical` — finding any means someone
    leaked a dependency and the "plug-and-play" contract is broken."""
    offenders: list[str] = []
    for path in REPO.rglob("*.py"):
        # Skip the package itself and its tests — they're allowed.
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel.startswith("src/trading/critical/"):
            continue
        if rel.startswith("tests/critical/"):
            continue
        # Skip virtual envs and caches
        if "/.venv/" in "/" + rel or "/__pycache__/" in "/" + rel:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _FORBIDDEN.search(text):
            offenders.append(rel)
    assert not offenders, (
        "Base model files import trading.critical — breaks plug-and-play:\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )
