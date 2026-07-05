"""Étape 0 tests: safety guards and the health endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings


def _base_kwargs() -> dict[str, str]:
    # Direct constructor kwargs must use field names, not env-var aliases.
    return {
        "paper_trading": "true",
        "exchange_sandbox": "true",
        "app_env": "test",
    }


def test_settings_ok_when_paper_and_sandbox() -> None:
    settings = Settings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]
    assert settings.paper_trading is True
    assert settings.exchange_sandbox is True
    assert settings.asset_whitelist_set[0] == "BTC/USDT"


@pytest.mark.parametrize(
    "override",
    [
        {"paper_trading": "false"},
        {"exchange_sandbox": "false"},
        {"paper_trading": "false", "exchange_sandbox": "false"},
    ],
)
def test_settings_refuse_unsafe_config(override: dict[str, str]) -> None:
    kwargs = {**_base_kwargs(), **override}
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]
    assert "unsafe configuration" in str(exc.value)


def test_asset_whitelist_parsing_normalizes_and_dedups() -> None:
    settings = Settings(
        _env_file=None,
        asset_whitelist=" btc/usdt , ETH/USDT,btc/usdt ,",
        **_base_kwargs(),
    )  # type: ignore[arg-type]
    assert settings.asset_whitelist_set == ("BTC/USDT", "ETH/USDT")


def test_health_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    monkeypatch.setenv("APP_ENV", "test")

    # Import lazily so the monkeypatched env is picked up by get_settings().
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    client = TestClient(create_app())
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["paper_trading"] is True
    assert body["exchange_sandbox"] is True
    get_settings.cache_clear()
