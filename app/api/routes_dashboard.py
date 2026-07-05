"""Read-only dashboard API routes.

Étape 0 only wires the health endpoint. The signals/orders/positions
endpoints described in the brief are added in later steps.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.config import get_settings

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
