"""Ingestion queue and pipeline worker.

Ingestion endpoints never block: they normalize inbound data into a
``NewsEvent`` and push it onto an ``asyncio.Queue``. A single worker task,
started in the FastAPI lifespan, drains the queue and (from Étape 2 onwards)
invokes the LangGraph pipeline.

Étape 1 scope: the worker only logs each dequeued event. Processing is
sequential — simple to reason about and sufficient for the MVP.
"""

from __future__ import annotations

import asyncio

from app.logging_config import get_logger
from app.models.schemas import NewsEvent

log = get_logger("app.worker")

# Bound the queue so a runaway producer can't grow memory without limit.
QUEUE_MAXSIZE = 1000


def create_queue() -> asyncio.Queue[NewsEvent]:
    """Create the bounded ingestion queue."""
    return asyncio.Queue(maxsize=QUEUE_MAXSIZE)


async def process_event(event: NewsEvent) -> None:
    """Handle a single event.

    Étape 1: log the normalized event. Later steps replace this body with a
    call into the compiled LangGraph pipeline.
    """
    log.info(
        "event_dequeued",
        event_id=event.id,
        source=event.source,
        author=event.author,
        title=event.title,
        received_at=event.received_at.isoformat(),
    )


async def worker_loop(queue: asyncio.Queue[NewsEvent]) -> None:
    """Consume events from the queue until cancelled.

    Any exception while processing an event is logged and swallowed so a
    single bad event never kills the worker.
    """
    log.info("worker_started")
    try:
        while True:
            event = await queue.get()
            try:
                await process_event(event)
            except Exception as exc:  # noqa: BLE001 - worker must never die
                log.error("worker_error", event_id=event.id, error=str(exc))
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        log.info("worker_stopped")
        raise
