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
import time
from datetime import UTC, datetime, timedelta

from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger
from app.models.schemas import NewsEvent
from app.sources.economic_calendar import CalendarEvent, cache_events, fetch_calendar

log = get_logger("app.sources.watcher")

LEAD_S = 30  # start watching this long before the scheduled time
# Keep watching well past the scheduled time. The free Forex Factory feed does
# not populate an event's `actual` value the instant the print hits the wire —
# it can lag by many minutes. A short trailing window (10 min) let a
# late-populating actual escape the fast pre-armed path and only reach us later
# via the slow general RSS poll (a late, chased entry). 30 min covers the feed's
# realistic latency so the fast path wins the race.
TRAIL_S = 1800  # keep watching this long after the scheduled time
REFRESH_S = 900  # re-fetch and re-arm every 15 minutes
# The feed never updates sub-second, so refetching the calendar every 2s over a
# 30-min window is wasteful and rude to a free feed. Throttle the release
# refetch to at most once per this interval regardless of the loop tick.
RELEASE_FETCH_MIN_INTERVAL_S = 8.0
AUTO_ARM_MIN_VOL = 3  # arm every event with Medium+ volatility (3 = Medium, 5 = High)

# Process-local armed state: event_id -> CalendarEvent, plus already-emitted ids.
_armed: dict[str, CalendarEvent] = {}
_emitted: set[str] = set()
_last_release_fetch: float = 0.0


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
    global _last_release_fetch
    _armed.clear()
    _emitted.clear()
    _last_release_fetch = 0.0


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
    global _last_release_fetch
    now = datetime.now(UTC)
    active = [e for e in _armed.values() if in_watch_window(e.when, now)]
    if not active:
        return
    # Throttle: the feed never updates sub-second, so cap the refetch rate even
    # though the loop ticks faster (keeps the long watch window cheap/polite).
    now_mono = time.monotonic()
    if now_mono - _last_release_fetch < RELEASE_FETCH_MIN_INTERVAL_S:
        return
    _last_release_fetch = now_mono
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


async def _refresh_and_auto_arm(calendar_url: str) -> None:
    """Fetch the calendar and arm every future event with volatility >= threshold.

    Past events are dropped from ``_armed`` so it stays bounded. Any already-armed
    events are refreshed with the newer version (in case the schedule shifted).
    """
    fresh = await fetch_calendar(calendar_url)
    if not fresh:
        return
    cache_events(fresh)
    now = datetime.now(UTC)
    # Drop past-window armed events (idempotent cleanup).
    for eid in list(_armed):
        ev = _armed[eid]
        if ev.when + timedelta(seconds=TRAIL_S) < now:
            _armed.pop(eid, None)
    armed_count = 0
    for ev in fresh:
        if ev.volatility < AUTO_ARM_MIN_VOL:
            continue
        if ev.when + timedelta(seconds=TRAIL_S) < now:
            continue
        arm(ev)
        armed_count += 1
    log.info("econ_calendar_refreshed", total=len(fresh), armed=armed_count)


async def watcher_loop(
    queue: asyncio.Queue[NewsEvent], calendar_url: str, poll_interval_s: float = 2.0
) -> None:
    """Auto-arm every high-impact event and fire the print at release.

    A refresh runs on startup and every ``REFRESH_S`` (15 min) to pick up
    freshly-published or shifted events; the release poll runs every
    ``poll_interval_s`` (2 s) to capture the actual within a second of publish.
    """
    log.info(
        "econ_watcher_started",
        poll_interval_s=poll_interval_s,
        refresh_s=REFRESH_S,
        auto_arm_min_volatility=AUTO_ARM_MIN_VOL,
    )
    last_refresh: float = 0.0
    try:
        while True:
            now_mono = time.monotonic()
            if now_mono - last_refresh >= REFRESH_S:
                try:
                    await _refresh_and_auto_arm(calendar_url)
                    last_refresh = now_mono
                except Exception as exc:  # noqa: BLE001 - never kill the watcher
                    log.error("econ_refresh_error", error=str(exc))
            try:
                await _poll_once(queue, calendar_url)
            except Exception as exc:  # noqa: BLE001 - never kill the watcher
                log.error("econ_watcher_error", error=str(exc))
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        log.info("econ_watcher_stopped")
        raise
