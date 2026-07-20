"""Admin API routes.

Étape 1 wires ``POST /admin/inject``. The killswitch/state endpoints described
in the brief are added with the risk engine (Étape 4).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, model_validator

from app.api.deps import enqueue, require_admin
from app.ingestion.normalizer import normalize_payload
from app.ingestion.simulator import list_scenarios, load_scenario
from app.logging_config import get_logger
from app.services.store import get_store
from app.services.strategy import set_active_strategy

log = get_logger("app.api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


class InjectRequest(BaseModel):
    """Body for ``POST /admin/inject``.

    Provide exactly one of:
    - ``scenario``: name of a canonical scenario in ``data/scenarios``.
    - ``event``: a raw payload (loose fields) to normalize as a simulator event.
    """

    scenario: str | None = None
    event: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> InjectRequest:
        if bool(self.scenario) == bool(self.event):
            raise ValueError("provide exactly one of 'scenario' or 'event'")
        return self


class InjectResponse(BaseModel):
    event_id: str
    status: str = "queued"


@router.post(
    "/inject",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InjectResponse,
    dependencies=[Depends(require_admin)],
)
async def inject(body: InjectRequest, request: Request) -> InjectResponse:
    """Inject a scenario or a raw event into the pipeline (non-blocking)."""
    if body.scenario is not None:
        try:
            event = load_scenario(body.scenario)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
    else:
        try:
            event = normalize_payload(body.event or {}, source="simulator")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    enqueue(request, event)
    log.info("event_injected", event_id=event.id, source=event.source, title=event.title)
    return InjectResponse(event_id=event.id)


@router.get("/scenarios")
async def scenarios() -> dict[str, list[str]]:
    """List the available demo scenarios."""
    return {"scenarios": list_scenarios()}


class KillSwitchRequest(BaseModel):
    """Body for ``POST /admin/killswitch``."""

    active: bool = True
    reason: str | None = None


@router.post("/killswitch", dependencies=[Depends(require_admin)])
async def killswitch(body: KillSwitchRequest) -> dict[str, Any]:
    """Activate or reset the manual kill switch.

    While active, the risk engine rejects every new trade until it is reset.
    """
    store = get_store()
    await store.set_kill_switch(body.active, reason=body.reason or "manual")
    log.info("killswitch_set", active=body.active, reason=body.reason)
    return {"kill_switch": await store.get_kill_switch()}


@router.get("/state")
async def state() -> dict[str, Any]:
    """Return a snapshot of the risk state (counters, positions, kill switch)."""
    return await get_store().snapshot()


class StrategyRequest(BaseModel):
    """Body for ``POST /admin/strategy``."""

    id: str


class ClosePositionRequest(BaseModel):
    """Body for ``POST /admin/positions/close``."""

    symbol: str


@router.post("/positions/close", dependencies=[Depends(require_admin)])
async def close_position(body: ClosePositionRequest) -> dict[str, Any]:
    """Force-close an open position at market (main + runner if present)."""
    from app.services.position_monitor import close_position_manually

    result = await close_position_manually(body.symbol)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no open position on {body.symbol}",
        )
    log.info("manual_close", **result)
    return result


class BiasRequest(BaseModel):
    """Body for ``POST /admin/bias``: the operator's directional view on an asset."""

    asset: str
    bias: str  # "BULL" | "BEAR" | "NEUTRAL" (neutral clears the bias)


@router.post("/bias", dependencies=[Depends(require_admin)])
async def set_bias(body: BiasRequest) -> dict[str, Any]:
    """Set (or clear) the operator bias on a whitelisted asset.

    The risk engine halves the risk budget of trades taken AGAINST the bias.
    This is how external conviction (your own TA, a trusted analyst's call)
    enters the system explicitly instead of via scraping.
    """
    from app.config import get_settings

    if body.bias not in ("BULL", "BEAR", "NEUTRAL"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="bias must be BULL/BEAR/NEUTRAL"
        )
    if body.asset not in get_settings().asset_whitelist_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"{body.asset} not in whitelist"
        )
    store = get_store()
    await store.set_bias(body.asset, None if body.bias == "NEUTRAL" else body.bias)
    log.info("bias_set", asset=body.asset, bias=body.bias)
    return {"biases": await store.all_biases()}


@router.post("/strategy", dependencies=[Depends(require_admin)])
async def set_strategy(body: StrategyRequest) -> dict[str, Any]:
    """Switch the active strategy (SL/TP, sizing, gates applied from next event)."""
    try:
        strategy = await set_active_strategy(body.id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    log.info("strategy_set", strategy_id=strategy.id, strategy_name=strategy.name)
    return {"active": strategy.id, "name": strategy.name}
