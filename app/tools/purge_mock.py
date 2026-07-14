"""Maintenance CLI: purge false SL/TP rows left by the (fixed) mock-price bug.

The monitor used to fall back to a static mock price (e.g. BTC=60000) when a
live price was momentarily unavailable, which fabricated stop-loss exits at that
mock value. Those false rows pollute the closed-trade ledger AND — now that the
learning loop feeds SL post-mortems back into the analyst — would teach the LLM
from losses that never really happened. This removes any journal row whose
exit_price equals its symbol's mock reference and rolls back the realized-PnL /
win-rate counters accordingly.

Run inside the app container on the VPS:

    docker compose -f docker-compose.prod.yml exec app python -m app.tools.purge_mock
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.graph.nodes.executor import _MOCK_PRICES
from app.services.store import init_store


async def _run() -> dict:
    settings = get_settings()
    store = await init_store(settings.redis_url)
    result = await store.purge_mock_journal(_MOCK_PRICES)
    backend = (await store.snapshot()).get("backend", "?")
    print(f"store backend: {backend}")
    print(
        "purged mock-price rows: "
        f"{result['trades_removed']} closed-trade(s), "
        f"{result['critiques_removed']} critique(s); "
        f"reversed realized PnL {result['realized_reversed']:+.2f}, "
        f"closed_count -{result['closed_reversed']}, wins -{result['wins_reversed']}"
    )
    return result


if __name__ == "__main__":
    asyncio.run(_run())
