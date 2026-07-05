"""Dedup node.

Étape 2: mock implementation that treats every event as new. The Redis-backed
SHA-256 dedup (SETNX + TTL) is wired in Étape 7.
"""

from __future__ import annotations

from typing import Any

from app.graph.state import TradingState
from app.graph.timing import timed_node


@timed_node("dedup")
async def dedup_node(state: TradingState) -> dict[str, Any]:
    is_duplicate = False  # mock: never a duplicate
    return {
        "is_duplicate": is_duplicate,
        "status": "skipped_duplicate" if is_duplicate else "received",
    }
