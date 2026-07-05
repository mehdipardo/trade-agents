"""Economic calendar connector.

Two roles:
1. **Schedule + volatility scoring** — parse a free calendar feed (Forex Factory
   shape) into ``CalendarEvent`` objects, each scored 1–5 by expected market
   impact so the UI can rank upcoming events (NFP/CPI/FOMC = 5).
2. **Release capture** — an armed event's actual value, once published, is turned
   into a ``NewsEvent`` and pushed into the pipeline (handled by the watcher).

Parsing is pure and unit-tested; fetching is a thin httpx wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from app.logging_config import get_logger

log = get_logger("app.sources.economic_calendar")

# Free weekly Forex Factory feed (impact ratings included).
DEFAULT_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Impact rating -> expected-volatility score (1–5).
_IMPACT_SCORE = {
    "high": 5,
    "red": 5,
    "medium": 3,
    "orange": 3,
    "low": 1,
    "yellow": 1,
    "holiday": 0,
    "": 0,
}


def volatility_from_impact(impact: str | None) -> int:
    """Map an impact/color label to a 1–5 volatility score (0 = none)."""
    return _IMPACT_SCORE.get((impact or "").strip().lower(), 0)


class CalendarEvent(BaseModel):
    id: str
    title: str
    country: str | None = None
    currency: str | None = None
    when: datetime
    impact: str
    volatility: int  # 1–5 derived from impact
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None


def _event_id(title: str, when: datetime, country: str | None) -> str:
    stamp = when.strftime("%Y%m%dT%H%M")
    slug = "".join(c for c in title.lower() if c.isalnum())[:24]
    return f"{country or 'XX'}-{stamp}-{slug}"


def _parse_dt(raw: str) -> datetime:
    # Forex Factory uses ISO 8601, sometimes with a trailing offset.
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_forexfactory(items: list[dict]) -> list[CalendarEvent]:
    """Parse Forex-Factory-shaped calendar items into ``CalendarEvent``."""
    events: list[CalendarEvent] = []
    for it in items:
        title = it.get("title") or it.get("event")
        raw_date = it.get("date") or it.get("datetime")
        if not title or not raw_date:
            continue
        try:
            when = _parse_dt(str(raw_date))
        except ValueError:
            continue
        impact = str(it.get("impact") or it.get("importance") or "")
        country = it.get("country")
        events.append(
            CalendarEvent(
                id=_event_id(title, when, country),
                title=title,
                country=country,
                currency=it.get("currency") or country,
                when=when,
                impact=impact,
                volatility=volatility_from_impact(impact),
                forecast=(it.get("forecast") or None),
                previous=(it.get("previous") or None),
                actual=(it.get("actual") or None),
            )
        )
    return events


def rank_by_volatility(
    events: list[CalendarEvent], *, min_volatility: int = 1, upcoming_only: bool = True
) -> list[CalendarEvent]:
    """Filter and sort events: highest volatility first, then soonest."""
    now = datetime.now(UTC)
    filtered = [
        e
        for e in events
        if e.volatility >= min_volatility and (not upcoming_only or e.when >= now)
    ]
    return sorted(filtered, key=lambda e: (-e.volatility, e.when))


# Small process-local cache of the last fetched events (for arm lookups / UI).
_cache: dict[str, CalendarEvent] = {}


def cache_events(events: list[CalendarEvent]) -> None:
    _cache.clear()
    _cache.update({e.id: e for e in events})


def get_cached(event_id: str) -> CalendarEvent | None:
    return _cache.get(event_id)


async def fetch_calendar(url: str = DEFAULT_CALENDAR_URL) -> list[CalendarEvent]:
    """Fetch and parse the calendar feed (best-effort; returns [] on failure)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": "flashsentiment/0.1"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - never break the app on a feed error
        log.warning("calendar_fetch_failed", url=url, error=str(exc))
        return []
    items = data if isinstance(data, list) else data.get("events", [])
    return parse_forexfactory(items)
