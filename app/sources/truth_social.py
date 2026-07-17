"""Trump / Truth Social connector (Tier 2 — high narrative, fragile in free mode).

Truth Social is a Mastodon fork, so a prominent public account's posts are
readable via the Mastodon-shaped ``/api/v1/accounts/{id}/statuses`` endpoint.
This connector polls that endpoint (or a compatible mirror), strips the HTML,
and emits each *new* post as a ``NewsEvent`` (source="social").

Honest caveats (surfaced in the catalog): there is no official API; direct
polling is fastest but ToS-gray and can break, and a mirror archive is the
robust-but-slower fallback. Fetching is a thin httpx wrapper; parsing/dedup
logic is pure and unit-tested.
"""

from __future__ import annotations

import asyncio
import html
import re

import httpx

from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger
from app.models.schemas import NewsEvent

log = get_logger("app.sources.truth_social")

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"</p>|<br\s*/?>", flags=re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")

# Process-local set of post ids already emitted (avoids re-emitting on each poll).
_seen: set[str] = set()


def reset_state() -> None:
    """Clear the seen-post set (used by tests)."""
    _seen.clear()


def strip_html(content: str) -> str:
    """Convert Truth Social/Mastodon HTML content to plain text."""
    text = _BREAK_RE.sub("\n", content or "")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    # Collapse intra-line spaces, keep line breaks, trim.
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def parse_status(status: dict) -> dict:
    """Map a Mastodon-shaped status to a normalizer payload."""
    text = strip_html(status.get("content", ""))
    account = status.get("account") or {}
    author = account.get("display_name") or account.get("username") or "Truth Social"
    title = text.splitlines()[0][:120] if text else "(media post)"
    return {
        "id": str(status.get("id")),
        "title": title,
        "content": text,
        "author": author,
        "url": status.get("url") or status.get("uri"),
        "published_at": status.get("created_at"),
    }


def _is_original_post(status: dict) -> bool:
    # Skip re-truths (reblogs); keep original posts and replies.
    return not status.get("reblog")


def new_statuses(statuses: list[dict], seen: set[str]) -> list[dict]:
    """Return unseen original posts, oldest-first (feed is newest-first)."""
    fresh = [
        s
        for s in statuses
        if _is_original_post(s) and str(s.get("id")) and str(s.get("id")) not in seen
    ]
    return list(reversed(fresh))


async def fetch_statuses(url: str) -> list[dict]:
    """Fetch a statuses feed (best-effort; returns [] on failure)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": "flashsentiment/0.1"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - never break the app on a feed error
        log.warning("truth_fetch_failed", url=url, error=str(exc))
        return []
    return data if isinstance(data, list) else data.get("statuses", data.get("data", []))


async def poll_once(queue: asyncio.Queue[NewsEvent], url: str) -> int:
    """Fetch once and enqueue any new posts. Returns the number emitted."""
    statuses = await fetch_statuses(url)
    emitted = 0
    for status in new_statuses(statuses, _seen):
        _seen.add(str(status["id"]))
        try:
            event = normalize_payload(parse_status(status), source="social")
        except ValueError:
            continue
        try:
            queue.put_nowait(event)
            emitted += 1
            log.info("truth_post_emitted", event_id=event.id, title=event.title)
        except asyncio.QueueFull:
            log.warning("truth_queue_full", event_id=event.id)
    return emitted


def parse_account_urls(raw: str) -> list[str]:
    """Split a comma/newline-separated list of account status feeds."""
    return [u.strip() for u in re.split(r"[,\n]", raw or "") if u.strip()]


async def _prime_seen(url: str) -> None:
    """Seed the seen-set from an account's current feed (no replay on startup)."""
    for s in await fetch_statuses(url):
        if s.get("id"):
            _seen.add(str(s["id"]))


async def poll_accounts_loop(
    queue: asyncio.Queue[NewsEvent], urls: list[str], poll_interval_s: float = 10.0
) -> None:
    """Poll a *watchlist* of account feeds forever, enqueueing new posts.

    One shared seen-set spans all accounts (post ids are globally unique), so a
    boosted/quoted post seen on two feeds still fires once. Each account is
    fetched every tick; a single account failing never stops the others.
    """
    log.info("truth_poller_started", accounts=len(urls), poll_interval_s=poll_interval_s)
    first = True
    try:
        while True:
            for url in urls:
                try:
                    if first:
                        await _prime_seen(url)
                    else:
                        await poll_once(queue, url)
                except Exception as exc:  # noqa: BLE001 - never kill the poller
                    log.error("truth_poller_error", url=url, error=str(exc))
            first = False
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        log.info("truth_poller_stopped")
        raise


async def poll_loop(
    queue: asyncio.Queue[NewsEvent], url: str, poll_interval_s: float = 10.0
) -> None:
    """Backward-compatible single-account wrapper over ``poll_accounts_loop``."""
    await poll_accounts_loop(queue, [url], poll_interval_s)
