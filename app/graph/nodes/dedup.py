"""Dedup node.

Deduplicates on a normalized title hash (lowercase, punctuation stripped,
whitespace compacted), stored via the state store's SETNX-with-TTL primitive
(30-minute window). Two sources publishing the same headline collapse to a
single pipeline pass; the second is marked ``skipped_duplicate``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.services.store import get_store

DEDUP_TTL_S = 1800  # 30 minutes

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def dedup_key(title: str) -> str:
    """SHA-256 of the normalized title."""
    normalized = _WS.sub(" ", _PUNCT.sub(" ", title.lower())).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@timed_node("dedup")
async def dedup_node(state: TradingState) -> dict[str, Any]:
    key = dedup_key(state["event"].title)
    is_duplicate = await get_store().seen_before(key, ttl_s=DEDUP_TTL_S)
    return {
        "is_duplicate": is_duplicate,
        "status": "skipped_duplicate" if is_duplicate else "received",
    }
