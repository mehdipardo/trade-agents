"""Eval CLI — score the analyst on the golden set, compare prompt versions.

Usage:
    python -m app.eval.run              # eval the active prompt version
    python -m app.eval.run --all        # eval every version + comparison table
    python -m app.eval.run --version v1 # eval a specific version

Writes the latest report to ``data/eval/latest.json`` so the app can surface
"last eval: 84% accuracy (v2)" without re-running the (LLM-heavy) suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.eval.runner import format_report, run_eval
from app.prompts.analyst import PROMPT_VERSION, PROMPT_VERSIONS

LATEST_PATH = Path(__file__).resolve().parents[2] / "data" / "eval" / "latest.json"


def _persist(report: dict) -> None:
    LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


async def _one(version: str) -> dict:
    _, report = await run_eval(prompt_version=version)
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description="Analyst evaluation")
    parser.add_argument("--all", action="store_true", help="eval every prompt version")
    parser.add_argument("--version", help="eval a specific prompt version")
    args = parser.parse_args()

    if args.all:
        rows = []
        for version in PROMPT_VERSIONS:
            report = await _one(version)
            rows.append((version, report))
            print(format_report(report))
            print()
        # Comparison table.
        print("=== Prompt version comparison ===")
        print(f"{'version':10} {'sentiment_acc':>14} {'asset_acc':>11} {'ECE':>7}")
        for version, r in rows:
            print(
                f"{version:10} {r['sentiment_accuracy']:>13.1%} "
                f"{r['asset_accuracy']:>10.1%} {r['ece']:>7.3f}"
            )
        _persist(rows[-1][1])
        return

    version = args.version or PROMPT_VERSION
    report = await _one(version)
    print(format_report(report))
    _persist(report)


if __name__ == "__main__":
    asyncio.run(main())
