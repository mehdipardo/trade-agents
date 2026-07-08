"""Read-only dashboard API routes.

Étape 0 only wires the health endpoint. The signals/orders/positions
endpoints described in the brief are added in later steps.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.config import get_settings
from app.services.store import get_store
from app.services.strategy import DEFAULT_STRATEGY_ID, list_strategies

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/health")
async def health() -> dict[str, object]:
    """Liveness/readiness probe.

    Returns basic runtime facts, including confirmation that the safety
    guards are active (the app cannot start otherwise).
    """
    settings = get_settings()
    return {
        "status": "ok",
        "app_env": settings.app_env,
        "paper_trading": settings.paper_trading,
        "exchange_sandbox": settings.exchange_sandbox,
        "exchange_id": settings.exchange_id,
        "llm_provider": settings.llm_provider,
        "asset_whitelist": list(settings.asset_whitelist_set),
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/signals")
async def signals(limit: int = 50) -> dict[str, list[dict]]:
    """Recent pipeline results that produced a signal."""
    items = [h for h in await get_store().history(limit) if h.get("sentiment")]
    return {"signals": items}


@router.get("/orders")
async def orders(limit: int = 50) -> dict[str, list[dict]]:
    """Recent executed orders."""
    items = [h for h in await get_store().history(limit) if h.get("order_status")]
    return {"orders": items}


@router.get("/positions")
async def positions() -> dict[str, object]:
    """Currently open positions + risk-state snapshot."""
    store = get_store()
    return {"positions": await store.open_positions(), "state": await store.snapshot()}


@router.get("/strategies")
async def strategies() -> dict[str, object]:
    """List every available strategy plus the id of the active one."""
    store = get_store()
    active_id = await store.get_strategy_id() or DEFAULT_STRATEGY_ID
    return {"strategies": list_strategies(), "active": active_id}
