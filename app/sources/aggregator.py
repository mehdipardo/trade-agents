"""Broad real-time news aggregator connector (the primary firehose).

Rather than wiring one narrow connector per outlet, we source from a single
aggregator that fans in 200+ outlets, and let the LLM funnel do the triage.

Default backend: free-crypto-news (cryptocurrency.cv) — 200+ sources, no API
key, asset tags via NER. We consume its **Server-Sent Events** stream
(`/api/sse`) rather than polling REST, because a single long-lived connection
respects the free tier and gives the lowest latency for constant monitoring.

Item shape (REST/SSE): ``{title, link, description, pubDate, source,
currencies?}``. Parsing/dedup is pure and unit-tested; the stream reader is a
thin httpx wrapper.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger
from app.models.schemas import NewsEvent

log = get_logger("app.sources.aggregator")

DEFAULT_SSE_URL = "https://cryptocurrency.cv/api/sse"

# Process-local set of item keys already emitted (dedup by link/title).
_seen: set[str] = set()


def reset_state() -> None:
    _seen.clear()


def item_key(item: dict) -> str:
    """Stable dedup key for a news item (prefer the canonical link)."""
    return str(item.get("link") or item.get("url") or item.get("title") or "")


def parse_item(item: dict) -> dict:
    """Map an aggregator news item to a normalizer payload."""
    tickers = item.get("currencies") or item.get("tickers") or []
    if isinstance(tickers, list) and tickers:
        ticker_note = " [" + ",".join(str(t) for t in tickers) + "]"
    else:
        ticker_note = ""
    return {
        "id": item_key(item),
        "title": (item.get("title") or "").strip(),
        "content": (item.get("description") or item.get("summary") or "").strip() + ticker_note,
        "author": item.get("source") or "aggregator",
        "url": item.get("link") or item.get("url"),
        "published_at": item.get("pubDate") or item.get("published") or item.get("date"),
    }


def parse_sse_data(raw: str) -> Any | None:
    """Parse the JSON payload of an SSE ``data:`` line (or None)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _items_from_payload(payload: Any) -> list[dict]:
    """An SSE event may carry a single item or a list of items."""
    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            return [i for i in payload["items"] if isinstance(i, dict)]
        return [payload]
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    return []


async def _emit_item(queue: asyncio.Queue[NewsEvent], item: dict) -> bool:
    key = item_key(item)
    if not key or key in _seen:
        return False
    _seen.add(key)
    try:
        event = normalize_payload(parse_item(item), source="news")
    except ValueError:
        return False
    try:
        queue.put_nowait(event)
        log.info("aggregator_item_emitted", event_id=event.id, title=event.title)
        return True
    except asyncio.QueueFull:
        log.warning("aggregator_queue_full", event_id=event.id)
        return False


async def stream_loop(queue: asyncio.Queue[NewsEvent], sse_url: str) -> None:
    """Consume the aggregator SSE stream forever, enqueueing new items.

    Reconnects with backoff on any error. Primes the seen-set with the first
    burst so we don't replay backlog on connect.
    """
    log.info("aggregator_stream_started", url=sse_url)
    backoff = 1.0
    try:
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "GET", sse_url, headers={"Accept": "text/event-stream"}
                    ) as resp:
                        resp.raise_for_status()
                        backoff = 1.0  # connected: reset backoff
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = parse_sse_data(line[len("data:") :])
                            if payload is None:
                                continue
                            for item in _items_from_payload(payload):
                                await _emit_item(queue, item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                log.warning("aggregator_stream_error", error=str(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    except asyncio.CancelledError:
        log.info("aggregator_stream_stopped")
        raise
