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
from app.services.store import get_store
from app.sources import catalog
from app.sources.economic_calendar import (
    DEFAULT_CALENDAR_URL,
    cache_events,
    fetch_calendar,
    rank_by_volatility,
)
from app.sources.manager import get_manager
from app.sources.watcher import is_armed

log = get_logger("app.api.sources")

router = APIRouter(tags=["sources"])


@router.get("/api/sources")
async def list_sources() -> dict[str, list[dict]]:
    """The curated source catalog with per-source config + live running state."""
    current_values = await get_store().all_source_configs()
    running = get_manager().running_ids()
    return {"sources": catalog.as_dict(current_values=current_values, running_ids=running)}


class ToggleRequest(BaseModel):
    enabled: bool


@router.post("/admin/sources/{source_id}/toggle")
async def toggle_source(source_id: str, body: ToggleRequest) -> dict[str, Any]:
    try:
        enabled = catalog.set_enabled(source_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown source") from exc
    # Turning ON should start the task immediately; turning OFF should stop it.
    running = await get_manager().restart(source_id) if enabled else False
    if not enabled:
        # restart() would no-op when disabled but be explicit — stop the task.
        await get_manager()._stop_one(source_id)  # noqa: SLF001 - internal API
    log.info("source_toggled", source_id=source_id, enabled=enabled, running=running)
    return {"id": source_id, "enabled": enabled, "running": running}


class ConfigRequest(BaseModel):
    """Body for ``POST /admin/sources/{id}/config``.

    Only the fields declared on the source's ``config_fields`` are accepted.
    Sending an empty string clears that field.
    """

    values: dict[str, str]


@router.post("/admin/sources/{source_id}/config")
async def set_source_config(source_id: str, body: ConfigRequest) -> dict[str, Any]:
    """Persist config for a source and hot-restart its background task."""
    spec = catalog.get_spec(source_id)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown source"
        )
    allowed = {f.name for f in spec.config_fields}
    unknown = set(body.values) - allowed
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown fields for {source_id}: {sorted(unknown)}",
        )
    # Merge with existing so partial saves preserve other fields.
    store = get_store()
    current = await store.get_source_config(source_id)
    merged = {**current, **{k: v for k, v in body.values.items()}}
    # Empty values remove the entry.
    merged = {k: v for k, v in merged.items() if v}
    await store.set_source_config(source_id, merged)

    # Auto-enable if the source is now fully configured but was disabled.
    fully_configured = all(
        merged.get(f.name)
        for f in spec.config_fields
        if f.required
    )
    if fully_configured and not catalog.is_enabled(source_id):
        catalog.set_enabled(source_id, True)

    running = await get_manager().restart(source_id)
    log.info(
        "source_config_set",
        source_id=source_id,
        fields=sorted(body.values),
        running=running,
    )
    return {"id": source_id, "running": running, "enabled": catalog.is_enabled(source_id)}


@router.get("/api/calendar/upcoming")
async def calendar_upcoming(min_volatility: int = 1) -> dict[str, list[dict]]:
    """Upcoming macro events ranked by expected volatility (highest first).

    All events with volatility >= ``AUTO_ARM_MIN_VOL`` are auto-armed by the
    watcher — no operator action needed. The ``armed`` field reflects live
    watcher state and is informational only.
    """
    settings = get_settings()
    url = settings.econ_calendar_url or DEFAULT_CALENDAR_URL
    events = await fetch_calendar(url)
    cache_events(events)
    ranked = rank_by_volatility(events, min_volatility=min_volatility)
    return {
        "events": [{**e.model_dump(mode="json"), "armed": is_armed(e.id)} for e in ranked]
    }
