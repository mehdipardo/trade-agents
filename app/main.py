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

    from app.services.exchange import get_exchange, init_exchange
    from app.services.position_monitor import position_monitor_loop
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

    # State store (Redis with in-memory fallback) + exchange (sandbox forced,
    # None in offline mode).
    await init_store(settings.redis_url)
    await init_exchange(settings)

    # Ingestion queue + worker + SL/TP position monitor + optional RSS poller.
    app.state.queue = create_queue()
    tasks = [
        asyncio.create_task(worker_loop(app.state.queue), name="ingestion-worker"),
        asyncio.create_task(position_monitor_loop(), name="position-monitor"),
    ]

    from app.ingestion.rss_poller import parse_feeds_setting, rss_poller_loop

    feeds = parse_feeds_setting(settings.rss_feeds)
    if feeds:
        tasks.append(
            asyncio.create_task(
                rss_poller_loop(app.state.queue, feeds, settings.rss_poll_interval_s),
                name="rss-poller",
            )
        )

    # Broad news aggregator firehose (opt-in: only when an SSE URL is set).
    if settings.aggregator_sse_url:
        from app.sources.aggregator import stream_loop as aggregator_stream

        tasks.append(
            asyncio.create_task(
                aggregator_stream(app.state.queue, settings.aggregator_sse_url),
                name="aggregator-stream",
            )
        )

    # Economic-calendar release watcher (opt-in: only when a feed URL is set).
    if settings.econ_calendar_url:
        from app.sources.watcher import watcher_loop

        tasks.append(
            asyncio.create_task(
                watcher_loop(app.state.queue, settings.econ_calendar_url),
                name="econ-watcher",
            )
        )

    # Trump / Truth Social poller (opt-in: only when a feed URL is set).
    if settings.truth_social_url:
        from app.sources.truth_social import poll_loop as truth_poll_loop

        tasks.append(
            asyncio.create_task(
                truth_poll_loop(
                    app.state.queue,
                    settings.truth_social_url,
                    settings.truth_social_poll_interval_s,
                ),
                name="truth-poller",
            )
        )

    # Congress.gov bill tracker (opt-in: needs API key + tracked bills).
    if settings.congress_api_key and settings.congress_tracked_bills:
        from app.sources.congress import poll_loop as congress_poll_loop

        tasks.append(
            asyncio.create_task(
                congress_poll_loop(
                    app.state.queue,
                    settings.congress_tracked_bills,
                    settings.congress_api_key,
                    settings.congress_poll_interval_s,
                ),
                name="congress-poller",
            )
        )

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        exchange = get_exchange()
        if exchange is not None:
            await exchange.close()
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
    from app.api.routes_sources import router as sources_router
    from app.api.routes_webhooks import router as webhooks_router
    from app.api.ws import router as ws_router

    app.include_router(dashboard_router)
    app.include_router(admin_router)
    app.include_router(webhooks_router)
    app.include_router(sources_router)
    app.include_router(ws_router)

    # Serve the dashboard SPA (deploy/www) at "/" when present. Mounted last so
    # API/WS routes always take precedence. In prod Caddy also serves it, but
    # this lets the app self-serve the single shareable URL.
    _mount_dashboard(app)

    get_logger("app").info("app_created", app_env=settings.app_env)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    from pathlib import Path

    www = Path(__file__).resolve().parents[1] / "deploy" / "www"
    if (www / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(www), html=True), name="dashboard")


app = create_app()
