"""Strategy presets: overlay the risk config, persist active id."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.risk.rules import RiskConfig
from app.services.store import InMemoryStore, set_store
from app.services.strategy import (
    DEFAULT_STRATEGY_ID,
    STRATEGIES,
    get_active_strategy,
    get_strategy,
    list_strategies,
    set_active_strategy,
)


def _settings() -> Settings:
    return Settings(_env_file=None, paper_trading="true", exchange_sandbox="true")  # type: ignore[arg-type]


def test_all_four_presets_present() -> None:
    ids = {s["id"] for s in list_strategies()}
    assert ids == {"conservative", "balanced", "aggressive", "scalp"}


def test_get_strategy_falls_back_to_default() -> None:
    assert get_strategy("nonsense").id == DEFAULT_STRATEGY_ID


def test_conservative_is_tighter_than_aggressive() -> None:
    c, a = STRATEGIES["conservative"], STRATEGIES["aggressive"]
    assert c.stop_loss_pct < a.stop_loss_pct
    assert c.risk_per_trade_pct < a.risk_per_trade_pct
    assert c.confidence_threshold > a.confidence_threshold
    assert c.min_intensity >= a.min_intensity


def test_risk_config_overlaid_by_strategy() -> None:
    settings = _settings()
    scalp = STRATEGIES["scalp"]
    config = RiskConfig.from_settings(settings, scalp)
    assert config.stop_loss_pct == scalp.stop_loss_pct
    assert config.take_profit_pct == scalp.take_profit_pct
    assert config.risk_per_trade_pct == scalp.risk_per_trade_pct
    assert config.confidence_threshold == scalp.confidence_threshold
    # Daily loss limit stays global.
    assert config.daily_loss_limit_pct == settings.daily_loss_limit_pct


def test_risk_config_without_strategy_uses_settings_defaults() -> None:
    config = RiskConfig.from_settings(_settings())
    assert config.stop_loss_pct == _settings().stop_loss_pct


async def test_set_active_strategy_persists_via_store() -> None:
    set_store(InMemoryStore())
    await set_active_strategy("aggressive")
    active = await get_active_strategy()
    assert active.id == "aggressive"


async def test_unknown_strategy_id_raises() -> None:
    set_store(InMemoryStore())
    with pytest.raises(ValueError):
        await set_active_strategy("does_not_exist")


async def test_active_strategy_defaults_when_none_set() -> None:
    set_store(InMemoryStore())
    active = await get_active_strategy()
    assert active.id == DEFAULT_STRATEGY_ID
