"""Source manager — owns the running background tasks per catalog source.

Bridges the catalog (metadata + enabled flag) and the actual asyncio tasks.
Handles:

- ``start_all(queue, settings)`` at boot: starts every enabled + configured source.
- ``restart(source_id)``: cancels the current task (if any) and restarts it with
  the freshly-persisted config — so a config change from the dashboard takes
  effect in seconds without an app restart.
- ``stop_all()`` at shutdown.

Config resolution order for each source is: Store (dashboard-set) → env var
(from Settings). Missing required config = the source stays stopped and the
dashboard renders it as ``needs_config``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.models.schemas import NewsEvent
from app.services.store import get_store
from app.sources import catalog

log = get_logger("app.sources.manager")


class SourceManager:
    """Runs and hot-reloads catalog sources.

    Sources register a ``factory`` (async function that returns a coroutine to
    schedule as a task) via ``_FACTORIES``. The factory receives the merged
    settings (env + Store overrides) and returns None if the source is not
    configured / not enabled — in which case no task is started.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._queue: asyncio.Queue[NewsEvent] | None = None

    def running(self, source_id: str) -> bool:
        task = self._tasks.get(source_id)
        return task is not None and not task.done()

    def running_ids(self) -> set[str]:
        return {sid for sid, t in self._tasks.items() if not t.done()}

    async def start_all(self, queue: asyncio.Queue[NewsEvent]) -> None:
        """Start every enabled + configured source."""
        self._queue = queue
        for spec in catalog.list_specs():
            if catalog.is_enabled(spec.id):
                await self._start_one(spec.id)

    async def restart(self, source_id: str) -> bool:
        """Cancel and restart a source with fresh config. Returns True if running."""
        await self._stop_one(source_id)
        # If the source isn't enabled we leave it stopped.
        if not catalog.is_enabled(source_id):
            return False
        return await self._start_one(source_id)

    async def stop_all(self) -> None:
        for source_id in list(self._tasks):
            await self._stop_one(source_id)

    async def _stop_one(self, source_id: str) -> None:
        task = self._tasks.pop(source_id, None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown noise
            pass
        log.info("source_stopped", source_id=source_id)

    async def _start_one(self, source_id: str) -> bool:
        if self._queue is None:
            return False
        factory = _FACTORIES.get(source_id)
        if factory is None:
            return False
        merged_settings = await _merged_settings(source_id)
        coro = factory(self._queue, merged_settings)
        if coro is None:
            log.info("source_not_configured", source_id=source_id)
            return False
        self._tasks[source_id] = asyncio.create_task(coro, name=f"source:{source_id}")
        log.info("source_started", source_id=source_id)
        return True


# --- Factories per source id ---------------------------------------------
#
# Each factory returns a coroutine to schedule as a task, or None when the
# source can't run (missing config / disabled dependency). The factory should
# read config via the passed-in settings-like object (env + Store overrides).

_Factory = Callable[
    [asyncio.Queue[NewsEvent], "MergedSettings"], Coroutine[Any, Any, None] | None
]


class MergedSettings:
    """Settings-like object where Store overrides take precedence over env."""

    def __init__(self, settings: Settings, overrides: dict[str, str]) -> None:
        self._settings = settings
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides and self._overrides[name] != "":
            return self._overrides[name]
        return getattr(self._settings, name)


async def _merged_settings(source_id: str) -> MergedSettings:
    """Merge env-loaded Settings with Store-persisted overrides for this source."""
    overrides = await get_store().get_source_config(source_id)
    return MergedSettings(get_settings(), overrides)


def _news_aggregator(
    queue: asyncio.Queue[NewsEvent], settings: MergedSettings
) -> Coroutine[Any, Any, None] | None:
    from app.sources.aggregator import DEFAULT_SSE_URL, stream_loop

    # Free, no-key, default-enabled source: fall back to the built-in URL so it
    # always runs out of the box even when the env var is unset.
    url = getattr(settings, "aggregator_sse_url", "") or DEFAULT_SSE_URL
    return stream_loop(queue, url)


def _econ_calendar(
    queue: asyncio.Queue[NewsEvent], settings: MergedSettings
) -> Coroutine[Any, Any, None] | None:
    from app.sources.economic_calendar import DEFAULT_CALENDAR_URL
    from app.sources.watcher import watcher_loop

    # Free, no-key, default-enabled source: fall back to the built-in feed.
    url = getattr(settings, "econ_calendar_url", "") or DEFAULT_CALENDAR_URL
    return watcher_loop(queue, url)


def _trump_truthsocial(
    queue: asyncio.Queue[NewsEvent], settings: MergedSettings
) -> Coroutine[Any, Any, None] | None:
    from app.sources.truth_social import parse_account_urls, poll_accounts_loop

    # Prefer the multi-account watchlist; fall back to the single legacy URL.
    urls = parse_account_urls(getattr(settings, "truth_social_urls", ""))
    if not urls:
        single = getattr(settings, "truth_social_url", "")
        urls = [single] if single else []
    if not urls:
        return None
    return poll_accounts_loop(
        queue, urls, int(getattr(settings, "truth_social_poll_interval_s", 10))
    )


def _congress_bills(
    queue: asyncio.Queue[NewsEvent], settings: MergedSettings
) -> Coroutine[Any, Any, None] | None:
    api_key = getattr(settings, "congress_api_key", "")
    tracked = getattr(settings, "congress_tracked_bills", "")
    if not (api_key and tracked):
        return None
    from app.sources.congress import poll_loop

    return poll_loop(
        queue, tracked, api_key,
        int(getattr(settings, "congress_poll_interval_s", 300)),
    )


def _crypto_news_rss(
    queue: asyncio.Queue[NewsEvent], settings: MergedSettings
) -> Coroutine[Any, Any, None] | None:
    from app.ingestion.rss_poller import (
        DEFAULT_RSS_FEEDS,
        parse_feeds_setting,
        rss_poller_loop,
    )

    # Default-enabled, key-less source: fall back to the curated world+markets
    # feed set when the operator hasn't provided an override.
    feeds_raw = getattr(settings, "rss_feeds", "")
    feeds = parse_feeds_setting(feeds_raw) if feeds_raw else list(DEFAULT_RSS_FEEDS)
    if not feeds:
        return None
    return rss_poller_loop(
        queue, feeds, int(getattr(settings, "rss_poll_interval_s", 30))
    )


_FACTORIES: dict[str, _Factory] = {
    "news_aggregator": _news_aggregator,
    "econ_calendar": _econ_calendar,
    "trump_truthsocial": _trump_truthsocial,
    "congress_bills": _congress_bills,
    "crypto_news_rss": _crypto_news_rss,
}


# --- Singleton -----------------------------------------------------------

_manager: SourceManager | None = None


def get_manager() -> SourceManager:
    global _manager
    if _manager is None:
        _manager = SourceManager()
    return _manager


def reset_manager() -> None:
    """Reset the module-global manager (used by tests)."""
    global _manager
    _manager = None
