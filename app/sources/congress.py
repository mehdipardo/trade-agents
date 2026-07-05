"""US Congress bill tracker (Tier 3 — regulatory, free official API).

Tracks specific bills (e.g. the CLARITY Act, GENIUS Act) via the free
Congress.gov API and emits a ``NewsEvent`` (source="regulatory") whenever a
tracked bill's latest action changes (new committee action, floor vote, passage).

Requires a free Congress.gov API key. Bills are configured as
``{congress}/{type}/{number}`` (e.g. ``119/hr/1747``). Parsing/change-detection
is pure and unit-tested; fetching is a thin httpx wrapper.
"""

from __future__ import annotations

import asyncio

import httpx

from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger
from app.models.schemas import NewsEvent

log = get_logger("app.sources.congress")

API_BASE = "https://api.congress.gov/v3/bill"

# Process-local map: bill_ref -> last emitted action key (change detection).
_last_action: dict[str, str] = {}


def reset_state() -> None:
    _last_action.clear()


def parse_bill_refs(raw: str) -> list[tuple[str, str, str]]:
    """Parse 'congress/type/number' comma-separated refs into tuples."""
    refs: list[tuple[str, str, str]] = []
    for chunk in raw.split(","):
        parts = [p.strip() for p in chunk.strip().split("/")]
        if len(parts) == 3 and all(parts):
            refs.append((parts[0], parts[1].lower(), parts[2]))
    return refs


def _bill_ref(congress: str, bill_type: str, number: str) -> str:
    return f"{congress}/{bill_type}/{number}"


def action_key(bill: dict) -> str | None:
    """Stable key for a bill's latest action (date + text)."""
    action = bill.get("latestAction") or {}
    date = action.get("actionDate")
    text = action.get("text")
    if not date or not text:
        return None
    return f"{date}::{text}"


def parse_bill(bill: dict, ref: str) -> dict | None:
    """Map a Congress.gov bill object to a normalizer payload (or None)."""
    action = bill.get("latestAction") or {}
    date = action.get("actionDate")
    text = action.get("text")
    if not date or not text:
        return None
    number = bill.get("number", "")
    short = bill.get("title") or f"Bill {number}"
    return {
        "id": f"congress-{ref}-{date}",
        "title": f"{short}: {text}",
        "content": f"Congress {ref} latest action ({date}): {text}",
        "author": "US Congress",
        "url": bill.get("url"),
        "published_at": date,
    }


async def fetch_bill(
    congress: str, bill_type: str, number: str, api_key: str
) -> dict | None:
    """Fetch a single bill (best-effort; returns None on failure)."""
    url = f"{API_BASE}/{congress}/{bill_type}/{number}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"api_key": api_key, "format": "json"})
            resp.raise_for_status()
            return resp.json().get("bill")
    except Exception as exc:  # noqa: BLE001 - never break the app on a feed error
        ref = _bill_ref(congress, bill_type, number)
        log.warning("congress_fetch_failed", ref=ref, error=str(exc))
        return None


async def poll_once(
    queue: asyncio.Queue[NewsEvent], refs: list[tuple[str, str, str]], api_key: str
) -> int:
    """Poll tracked bills once; emit on a changed latest action. Returns count."""
    emitted = 0
    for congress, bill_type, number in refs:
        ref = _bill_ref(congress, bill_type, number)
        bill = await fetch_bill(congress, bill_type, number, api_key)
        if bill is None:
            continue
        key = action_key(bill)
        if key is None or _last_action.get(ref) == key:
            continue
        seen_before = ref in _last_action
        _last_action[ref] = key
        if not seen_before:
            continue  # prime on first sight; don't replay the current action
        payload = parse_bill(bill, ref)
        if payload is None:
            continue
        try:
            event = normalize_payload(payload, source="regulatory")
            queue.put_nowait(event)
            emitted += 1
            log.info("congress_action_emitted", event_id=event.id, title=event.title)
        except (ValueError, asyncio.QueueFull):
            continue
    return emitted


async def poll_loop(
    queue: asyncio.Queue[NewsEvent], refs_raw: str, api_key: str, poll_interval_s: float = 300.0
) -> None:
    """Poll tracked bills forever (default every 5 min)."""
    refs = parse_bill_refs(refs_raw)
    if not refs or not api_key:
        return
    log.info("congress_poller_started", bills=len(refs), poll_interval_s=poll_interval_s)
    try:
        while True:
            try:
                await poll_once(queue, refs, api_key)
            except Exception as exc:  # noqa: BLE001 - never kill the poller
                log.error("congress_poller_error", error=str(exc))
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        log.info("congress_poller_stopped")
        raise
