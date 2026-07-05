"""Sources catalog + economic-calendar API.

Read endpoints power the UI's Sources panel and Calendar view; admin endpoints
toggle sources and arm/disarm scheduled events.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.config import get_settings
from app.logging_config import get_logger
from app.sources import catalog
from app.sources.economic_calendar import (
    DEFAULT_CALENDAR_URL,
    cache_events,
    fetch_calendar,
    get_cached,
    rank_by_volatility,
)
from app.sources.watcher import arm, disarm, is_armed

log = get_logger("app.api.sources")

router = APIRouter(tags=["sources"])


@router.get("/api/sources")
async def list_sources() -> dict[str, list[dict]]:
    """The curated source catalog with enabled state."""
    return {"sources": catalog.as_dict()}


class ToggleRequest(BaseModel):
    enabled: bool


@router.post("/admin/sources/{source_id}/toggle")
async def toggle_source(source_id: str, body: ToggleRequest) -> dict[str, Any]:
    try:
        enabled = catalog.set_enabled(source_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown source") from exc
    log.info("source_toggled", source_id=source_id, enabled=enabled)
    return {"id": source_id, "enabled": enabled}


@router.get("/api/calendar/upcoming")
async def calendar_upcoming(min_volatility: int = 1) -> dict[str, list[dict]]:
    """Upcoming macro events ranked by expected volatility (highest first)."""
    settings = get_settings()
    url = settings.econ_calendar_url or DEFAULT_CALENDAR_URL
    events = await fetch_calendar(url)
    cache_events(events)
    ranked = rank_by_volatility(events, min_volatility=min_volatility)
    return {
        "events": [{**e.model_dump(mode="json"), "armed": is_armed(e.id)} for e in ranked]
    }


class ArmRequest(BaseModel):
    event_id: str
    armed: bool = True


@router.post("/admin/calendar/arm")
async def arm_event(body: ArmRequest) -> dict[str, Any]:
    """Arm/disarm a scheduled event for pre-positioned release capture."""
    if body.armed:
        event = get_cached(body.event_id)
        if event is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="unknown event id (fetch /api/calendar/upcoming first)",
            )
        arm(event)
    else:
        disarm(body.event_id)
    return {"event_id": body.event_id, "armed": is_armed(body.event_id)}
