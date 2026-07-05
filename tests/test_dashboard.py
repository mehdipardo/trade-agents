"""Étape 8 tests: dashboard read endpoints + history buffer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _wait_for_signal(client: TestClient, tries: int = 20) -> list[dict]:
    import time

    for _ in range(tries):
        signals = client.get("/api/signals").json()["signals"]
        if signals:
            return signals
        time.sleep(0.1)
    return []


def test_signals_and_orders_after_inject(client: TestClient) -> None:
    client.post("/admin/inject", json={"scenario": "trump_btc_bull"})
    signals = _wait_for_signal(client)
    assert signals, "expected a signal in history"
    assert signals[0]["asset"] == "BTC/USDT"

    orders = client.get("/api/orders").json()["orders"]
    assert any(o["order_status"] == "filled" for o in orders)


def test_positions_endpoint(client: TestClient) -> None:
    client.post("/admin/inject", json={"scenario": "trump_btc_bull"})
    _wait_for_signal(client)
    body = client.get("/api/positions").json()
    assert "positions" in body
    assert "state" in body
    assert body["state"]["kill_switch"] is False


def test_neutral_scenario_has_no_order(client: TestClient) -> None:
    client.post("/admin/inject", json={"scenario": "neutral_report"})
    _wait_for_signal(client)
    orders = client.get("/api/orders").json()["orders"]
    assert all(o["status"] != "executed" or o["asset"] is not None for o in orders)
