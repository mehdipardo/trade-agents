"""Admin-token gate on mutating dashboard actions + read-only public view."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_session_reports_read_only_without_token(client: TestClient) -> None:
    s = client.get("/api/session").json()
    assert s == {"admin_required": True, "admin": False}


def test_session_grants_admin_with_valid_token(client: TestClient) -> None:
    s = client.get("/api/session", headers={"X-Admin-Token": "s3cret"}).json()
    assert s == {"admin_required": True, "admin": True}


def test_mutation_rejected_without_token(client: TestClient) -> None:
    for path, body in (
        ("/admin/killswitch", {"active": True}),
        ("/admin/positions/close", {"symbol": "BTC/USDT"}),
        ("/admin/strategy", {"id": "balanced"}),
        ("/admin/sources/crypto_news_rss/toggle", {"enabled": False}),
        ("/admin/bias", {"asset": "BTC/USDT", "bias": "BULL"}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 403, path


def test_mutation_allowed_with_token(client: TestClient) -> None:
    r = client.post(
        "/admin/killswitch", json={"active": True}, headers={"X-Admin-Token": "s3cret"}
    )
    assert r.status_code == 200
    assert r.json()["kill_switch"] is True


def test_reads_stay_open_without_token(client: TestClient) -> None:
    # A shared viewer can still watch everything, just not change it.
    assert client.get("/api/positions").status_code == 200
    assert client.get("/api/performance").status_code == 200
    assert client.get("/api/sources").status_code == 200


def test_wrong_token_rejected(client: TestClient) -> None:
    r = client.post(
        "/admin/killswitch", json={"active": True}, headers={"X-Admin-Token": "nope"}
    )
    assert r.status_code == 403


def test_open_mode_allows_mutations_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # No ADMIN_TOKEN configured (local/dev) -> mutations open, session says admin.
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/session").json() == {"admin_required": False, "admin": True}
        assert c.post("/admin/killswitch", json={"active": False}).status_code == 200
    get_settings.cache_clear()
