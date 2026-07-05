"""Étape 1 tests: POST /admin/inject and the ingestion queue/worker."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("APP_ENV", "test")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:  # enters lifespan -> queue + worker
        yield c
    get_settings.cache_clear()


def test_inject_scenario_returns_202_with_event_id(client: TestClient) -> None:
    resp = client.post("/admin/inject", json={"scenario": "trump_btc_bull"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["event_id"]
    assert body["status"] == "queued"


def test_inject_raw_event_returns_202(client: TestClient) -> None:
    resp = client.post(
        "/admin/inject",
        json={"event": {"title": "Custom", "content": "custom body"}},
    )
    assert resp.status_code == 202
    assert resp.json()["event_id"]


def test_inject_unknown_scenario_404(client: TestClient) -> None:
    resp = client.post("/admin/inject", json={"scenario": "nope"})
    assert resp.status_code == 404


def test_inject_requires_exactly_one_field(client: TestClient) -> None:
    # Neither field.
    assert client.post("/admin/inject", json={}).status_code == 422
    # Both fields.
    resp = client.post(
        "/admin/inject",
        json={"scenario": "trump_btc_bull", "event": {"title": "x"}},
    )
    assert resp.status_code == 422


def test_list_scenarios_endpoint(client: TestClient) -> None:
    resp = client.get("/admin/scenarios")
    assert resp.status_code == 200
    assert "trump_btc_bull" in resp.json()["scenarios"]
