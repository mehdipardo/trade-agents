"""Dedup node.

Two guards before the pipeline spends an LLM call or opens a trade:

1. **Freshness** — an event whose ``published_at`` is older than
   ``max_news_age_s`` is dropped as ``skipped_stale``. A broad aggregator
   routinely re-surfaces old stories (e.g. a weeks-old ceasefire headline
   reappearing), which would otherwise re-trigger trades on already-priced
   news. Events with no ``published_at`` are treated as fresh.
2. **Dedup** — a normalized title hash (lowercase, punctuation stripped,
   whitespace compacted) stored via the store's SETNX-with-TTL primitive
   (30-minute window). Two sources publishing the same headline collapse to a
   single pipeline pass; the second is ``skipped_duplicate``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.graph.state import TradingState
from app.graph.timing import timed_node
from app.services.store import get_store

DEDUP_TTL_S = 1800  # 30 minutes

# The freshness gate only applies to "wild feed" sources that can re-surface old
# stories. Operator-driven events (a manual inject or an integration webhook) are
# deliberate "treat this as happening now" actions and are never gated as stale.
_FRESHNESS_GATED_SOURCES = frozenset({"news", "rss", "social", "economic", "regulatory"})

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def dedup_key(title: str) -> str:
    """SHA-256 of the normalized title."""
    normalized = _WS.sub(" ", _PUNCT.sub(" ", title.lower())).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_stale(published_at: datetime | None, max_age_s: int, *, now: datetime | None = None) -> bool:
    """True when a published timestamp is older than ``max_age_s`` (0 disables)."""
    if max_age_s <= 0 or published_at is None:
        return False
    now = now or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return (now - published_at).total_seconds() > max_age_s


@timed_node("dedup")
async def dedup_node(state: TradingState) -> dict[str, Any]:
    event = state["event"]
    settings = get_settings()
    store = get_store()
    # Ingestion funnel: every event entering the pipeline is "received"; the
    # stale/duplicate drops below are counted too, so the dashboard shows why
    # nothing reached the analyst (the "0 news analyzed" mystery).
    await store.bump_ingest("received")
    # Source latency = how old the news already is when it reaches us (publish ->
    # receipt). This is the real speed metric: a tight freshness gate is only
    # sensible if sources deliver within it. Skip missing/future timestamps.
    if event.published_at is not None:
        pub = event.published_at
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=UTC)
        age_s = (event.received_at - pub).total_seconds()
        if age_s >= 0:
            await store.record_news_age(age_s)

    # Freshness gate first (cheapest, and it must veto stale re-syndications
    # even if their exact title hasn't been seen in the last 30 minutes). Only
    # wild-feed sources are gated; manual injects / webhooks are always live.
    if event.source in _FRESHNESS_GATED_SOURCES and is_stale(
        event.published_at, settings.max_news_age_s
    ):
        await store.bump_ingest("stale")
        return {"is_duplicate": False, "status": "skipped_stale"}

    key = dedup_key(event.title)
    is_duplicate = await store.seen_before(key, ttl_s=DEDUP_TTL_S)
    if is_duplicate:
        await store.bump_ingest("duplicate")
    return {
        "is_duplicate": is_duplicate,
        "status": "skipped_duplicate" if is_duplicate else "received",
    }
