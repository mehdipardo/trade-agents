#!/usr/bin/env python3
"""Replay the 5 canonical demo scenarios against a running server.

Usage:
    uvicorn app.main:app &          # or: docker compose up
    python scripts/demo.py [--base-url http://localhost:8000]

Self-contained: depends only on the API, no external news source. Injects each
scenario, waits briefly, then prints the pipeline outcomes from /api/signals.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

SCENARIOS = [
    "trump_btc_bull",
    "cpi_hot_bear",
    "sec_etf_approval",
    "neutral_report",
    "prompt_injection",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    with httpx.Client(base_url=base, timeout=10.0) as client:
        try:
            client.get("/api/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"server not reachable at {base}: {exc}", file=sys.stderr)
            return 1

        print(f"Injecting {len(SCENARIOS)} scenarios into {base} ...\n")
        for name in SCENARIOS:
            resp = client.post("/admin/inject", json={"scenario": name})
            resp.raise_for_status()
            print(f"  -> {name:20} queued as {resp.json()['event_id'][:8]}")
            time.sleep(0.8)

        time.sleep(1.5)  # let the pipeline drain

        print("\nOutcomes (most recent first):")
        signals = client.get("/api/signals").json()["signals"]
        for h in signals:
            print(
                f'  {h.get("emoji", "?")} {h.get("status", "?"):16} '
                f'{str(h.get("asset")):10} {str(h.get("sentiment")):8} '
                f'latency={h.get("total_latency_ms")}ms'
            )

        positions = client.get("/api/positions").json()
        print(f"\nOpen positions: {[p.get('asset') for p in positions['positions']]}")
        print(f"Risk state: {positions['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
