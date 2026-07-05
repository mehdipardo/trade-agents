"""Shared test fixtures.

Enforces safe defaults (paper-trading + sandbox) and resets the process-wide
singletons (settings cache, store, exchange) before and after every test so
tests never leak state into one another.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _safe_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("APP_ENV", "test")

    from app.config import get_settings
    from app.services.exchange import set_exchange
    from app.services.store import InMemoryStore, set_store
    from app.sources import catalog, watcher

    get_settings.cache_clear()
    set_store(InMemoryStore())
    set_exchange(None)
    catalog.reset_state()
    watcher.reset_state()
    yield
    get_settings.cache_clear()
    set_store(None)
    set_exchange(None)
    catalog.reset_state()
    watcher.reset_state()
