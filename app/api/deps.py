"""Shared API dependencies."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request, status

from app.models.schemas import NewsEvent


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
