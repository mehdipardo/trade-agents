"""Shared API dependencies."""

from __future__ import annotations

import asyncio

from fastapi import Header, HTTPException, Request, status

from app.models.schemas import NewsEvent


def admin_ok(token: str | None) -> bool:
    """True when ``token`` authorizes admin actions.

    Open (returns True for anyone) when no ``admin_token`` is configured — the
    local/dev default. On a shared deployment set ``ADMIN_TOKEN`` and only a
    matching token unlocks mutations.
    """
    from app.config import get_settings

    configured = get_settings().admin_token
    return not configured or token == configured


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 403 unless the request carries a valid admin token."""
    if not admin_ok(x_admin_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin token required for this action",
        )


def get_queue(request: Request) -> asyncio.Queue[NewsEvent]:
    """Return the ingestion queue from app state, or 503 if not ready."""
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ingestion worker not ready",
        )
    return queue


def enqueue(request: Request, event: NewsEvent) -> None:
    """Non-blocking enqueue; 503 if the queue is full."""
    queue = get_queue(request)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ingestion queue is full",
        ) from exc
