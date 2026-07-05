#!/usr/bin/env python3
"""Evaluate the analyst against the golden set and print a report.

Usage:
    python scripts/eval.py            # uses the active analyst (LLM if a key is
                                      # set, else the deterministic offline one)

Exit code is non-zero if sentiment accuracy falls below --min-accuracy, so this
doubles as a CI regression gate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.eval.runner import format_report, run_eval


async def _main(min_accuracy: float) -> int:
    from app.logging_config import configure_logging

    configure_logging(level="WARNING")  # keep the report clean
    _preds, report = await run_eval()
    print(format_report(report))
    if report["sentiment_accuracy"] < min_accuracy:
        print(
            f"\nFAIL: sentiment accuracy {report['sentiment_accuracy']:.1%} "
            f"< min {min_accuracy:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-accuracy", type=float, default=0.0)
    args = parser.parse_args()
    return asyncio.run(_main(args.min_accuracy))


if __name__ == "__main__":
    raise SystemExit(main())
