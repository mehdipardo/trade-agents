"""Pre-armed release watcher for scheduled economic events.

The reactivity trick for *scheduled* events: we know the exact release time, so
an "armed" event gets a watcher that activates in a tight window around that
time, detects the published value, and emits a ``NewsEvent`` into the pipeline —
turning a known-in-advance macro print into an instant trading signal.

Window logic is pure and unit-tested. The loop is opt-in (started only when a
calendar URL is configured).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger
from app.models.schemas import NewsEvent
from app.sources.economic_calendar import CalendarEvent, fetch_calendar

log = get_logger("app.sources.watcher")

LEAD_S = 30  # start watching this long before the scheduled time
TRAIL_S = 600  # keep watching this long after (in case of delay)

# Process-local armed state: event_id -> CalendarEvent, plus already-emitted ids.
_armed: dict[str, CalendarEvent] = {}
_emitted: set[str] = set()


def arm(event: CalendarEvent) -> None:
    _armed[event.id] = event


def disarm(event_id: str) -> None:
    _armed.pop(event_id, None)


def armed() -> list[CalendarEvent]:
    return list(_armed.values())


def is_armed(event_id: str) -> bool:
    return event_id in _armed


def reset_state() -> None:
    """Reset armed/emitted state (used by tests)."""
    _armed.clear()
    _emitted.clear()


def in_watch_window(
    when: datetime, now: datetime, *, lead_s: int = LEAD_S, trail_s: int = TRAIL_S
) -> bool:
    """True when ``now`` is within [when-lead, when+trail]."""
    return (when - timedelta(seconds=lead_s)) <= now <= (when + timedelta(seconds=trail_s))


def is_released(event: CalendarEvent) -> bool:
    """True once the event carries an actual (published) value."""
    return bool(event.actual and str(event.actual).strip())


def build_release_event(event: CalendarEvent) -> NewsEvent:
    """Turn a released calendar print into a normalized ``NewsEvent``."""
    title = (
        f"{event.title}: actual {event.actual} "
        f"(forecast {event.forecast or 'n/a'}, previous {event.previous or 'n/a'})"
    )
    content = (
        f"{event.currency or event.country or ''} economic release. "
        f"Actual={event.actual}, forecast={event.forecast}, previous={event.previous}, "
        f"impact={event.impact}."
    )
    return normalize_payload(
        {
            "id": f"econ-{event.id}",
            "title": title,
            "content": content,
            "author": event.country or "economic-calendar",
            "published_at": event.when.isoformat(),
        },
        source="economic",
    )


async def _poll_once(queue: asyncio.Queue[NewsEvent], calendar_url: str) -> None:
    now = datetime.now(UTC)
    active = [e for e in _armed.values() if in_watch_window(e.when, now)]
    if not active:
        return
    # Re-fetch to pick up freshly-published actuals for the armed events.
    fresh = {e.id: e for e in await fetch_calendar(calendar_url)}
    for armed_event in active:
        latest = fresh.get(armed_event.id, armed_event)
        if is_released(latest) and armed_event.id not in _emitted:
            _emitted.add(armed_event.id)
            event = build_release_event(latest)
            try:
                queue.put_nowait(event)
                log.info("econ_release_emitted", event_id=event.id, title=event.title)
            except asyncio.QueueFull:
                log.warning("econ_queue_full", event_id=event.id)
            disarm(armed_event.id)


async def watcher_loop(
    queue: asyncio.Queue[NewsEvent], calendar_url: str, poll_interval_s: float = 2.0
) -> None:
    """Poll armed events near their release time and emit the print."""
    log.info("econ_watcher_started", poll_interval_s=poll_interval_s)
    try:
        while True:
            try:
                await _poll_once(queue, calendar_url)
            except Exception as exc:  # noqa: BLE001 - never kill the watcher
                log.error("econ_watcher_error", error=str(exc))
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        log.info("econ_watcher_stopped")
        raise
