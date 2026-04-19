"""`tpp-replay-diff` — compare two replay run directories."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from trading.replay.diff import compare_signal_jsonl, compare_summaries


@click.command()
@click.argument("run_a", type=click.Path(exists=True, file_okay=False))
@click.argument("run_b", type=click.Path(exists=True, file_okay=False))
@click.option("--sample", type=int, default=5, show_default=True,
              help="Max sample rows per divergence bucket.")
def main(run_a: str, run_b: str, sample: int) -> None:
    a = Path(run_a)
    b = Path(run_b)

    summary_a = (
        json.loads((a / "summary.json").read_text())
        if (a / "summary.json").exists() else {}
    )
    summary_b = (
        json.loads((b / "summary.json").read_text())
        if (b / "summary.json").exists() else {}
    )

    summary_diff = compare_summaries(summary_a, summary_b)
    signal_diff = compare_signal_jsonl(
        a / "signals.jsonl", b / "signals.jsonl", sample=sample,
    )

    out = {
        "a": str(a),
        "b": str(b),
        "summary_diff": [
            {"key": k, "a": av, "b": bv} for k, av, bv in summary_diff
        ],
        "signals": signal_diff,
        "identical": not summary_diff and signal_diff["identical"],
    }
    click.echo(json.dumps(out, indent=2, sort_keys=True))
    sys.exit(0 if out["identical"] else 1)


if __name__ == "__main__":
    main()
