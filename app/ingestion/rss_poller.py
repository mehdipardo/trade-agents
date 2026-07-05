"""RSS ingestion poller.

An asyncio loop that polls configured feeds at a fixed interval, honouring
``ETag`` / ``Last-Modified`` to avoid re-processing unchanged feeds. Each entry
is normalized into a ``NewsEvent`` (source = ``rss``) and queued; dedup
downstream collapses items already seen from another source.

``feedparser`` is imported lazily so the app runs even when it is not installed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger
from app.models.schemas import NewsEvent

log = get_logger("app.ingestion.rss")


def parse_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Map a feedparser entry to a normalizer payload."""
    return {
        "id": entry.get("id") or entry.get("guid") or entry.get("link"),
        "title": entry.get("title"),
        "content": entry.get("summary") or entry.get("description") or "",
        "url": entry.get("link"),
        "author": entry.get("author"),
        "published_at": entry.get("published") or entry.get("updated"),
    }


def _poll_feed_sync(url: str, cache: dict[str, tuple[str | None, str | None]]) -> list[NewsEvent]:
    """Blocking single-feed poll (run in a thread). Updates the etag cache."""
    import feedparser  # lazy import

    etag, modified = cache.get(url, (None, None))
    parsed = feedparser.parse(url, etag=etag, modified=modified)

    status = getattr(parsed, "status", None)
    if status == 304:  # not modified
        return []
    cache[url] = (getattr(parsed, "etag", None), getattr(parsed, "modified", None))

    events: list[NewsEvent] = []
    for entry in parsed.entries:
        try:
            events.append(normalize_payload(parse_entry(entry), source="rss"))
        except ValueError:
            continue  # skip entries without title/content
    return events


async def rss_poller_loop(
    queue: asyncio.Queue[NewsEvent], feeds: list[str], interval_s: int
) -> None:
    """Poll all feeds forever, queueing new entries."""
    if not feeds:
        return
    log.info("rss_poller_started", feeds=len(feeds), interval_s=interval_s)
    cache: dict[str, tuple[str | None, str | None]] = {}
    try:
        while True:
            for url in feeds:
                try:
                    events = await asyncio.to_thread(_poll_feed_sync, url, cache)
                except Exception as exc:  # noqa: BLE001 - one bad feed never stops the loop
                    log.warning("rss_feed_error", url=url, error=str(exc))
                    continue
                for event in events:
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        log.warning("rss_queue_full", dropped=event.id)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        log.info("rss_poller_stopped")
        raise


def parse_feeds_setting(raw: str) -> list[str]:
    """Parse the comma-separated RSS_FEEDS setting into a list of URLs."""
    return [u.strip() for u in raw.split(",") if u.strip()]
