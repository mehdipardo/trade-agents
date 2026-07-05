"""FastAPI application entrypoint.

Étape 0 scope: configure structured logging, load and validate settings
(which enforces the paper-trading / sandbox safety guards), expose the
health endpoint, and provide an app ``lifespan`` hook where later steps
will initialise Redis, the exchange and the pipeline worker.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import ValidationError

from app.logging_config import configure_logging, get_logger


def _load_settings():
    """Load settings, converting guard violations into a clean fatal exit.

    ``Settings`` raises on unsafe configuration (e.g. ``PAPER_TRADING=false``).
    We surface a readable message and exit with a non-zero status rather than
    dumping a pydantic traceback.
    """
    from app.config import get_settings

    try:
        return get_settings()
    except ValidationError as exc:
        messages = [err.get("msg", str(err)) for err in exc.errors()]
        sys.stderr.write(
            "FATAL: application refused to start.\n" + "\n".join(messages) + "\n"
        )
        raise SystemExit(1) from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown hooks."""
    import asyncio

    from app.services.store import get_store, init_store
    from app.worker import create_queue, worker_loop

    settings = _load_settings()
    configure_logging(json_logs=True)
    log = get_logger("app.lifespan")
    log.info(
        "startup",
        app_env=settings.app_env,
        paper_trading=settings.paper_trading,
        exchange_sandbox=settings.exchange_sandbox,
        exchange_id=settings.exchange_id,
        llm_provider=settings.llm_provider,
    )

    # State store (Redis with in-memory fallback). Later steps: init the
    # exchange (sandbox) here too.
    await init_store(settings.redis_url)

    # Ingestion queue + single sequential worker.
    app.state.queue = create_queue()
    worker_task = asyncio.create_task(worker_loop(app.state.queue), name="ingestion-worker")

    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await get_store().close()
        log.info("shutdown")


def create_app() -> FastAPI:
    """Application factory."""
    # Validate configuration eagerly so an unsafe config fails at import time
    # (e.g. when a process manager imports ``app.main:app``).
    settings = _load_settings()
    configure_logging(json_logs=True)

    app = FastAPI(
        title="FlashSentiment Trader",
        version="0.0.0",
        summary="Event-driven agentic PAPER-TRADING backend (demo only).",
        lifespan=lifespan,
    )

    from app.api.routes_admin import router as admin_router
    from app.api.routes_dashboard import router as dashboard_router

    app.include_router(dashboard_router)
    app.include_router(admin_router)

    get_logger("app").info("app_created", app_env=settings.app_env)
    return app


app = create_app()
