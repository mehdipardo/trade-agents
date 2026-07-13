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


@router.get("/performance")
async def performance() -> dict[str, object]:
    """Equity / return / win-rate hero metrics for live paper trading.

    Equity = starting equity + lifetime realized PnL (net of fees). Unrealized
    PnL is omitted in offline mode (no live mark price). Backtest trades are NOT
    included here — they have their own labeled report at /api/backtest.
    """
    settings = get_settings()
    store = get_store()
    perf = await store.performance()
    snap = await store.snapshot()
    positions_open = await store.open_positions()
    start = settings.starting_equity_quote
    realized = perf["realized_total"]
    exposure = sum(
        float(p.get("entry_price", 0)) * float(p.get("amount", 0)) for p in positions_open
    )
    closed = perf["closed_trades"]
    return {
        "starting_equity": start,
        "equity": round(start + realized, 2),
        "realized_total": realized,
        "return_pct": round(realized / start * 100, 3) if start else 0.0,
        "daily_pnl": snap.get("daily_pnl", 0.0),
        "open_positions": len(positions_open),
        "exposure": round(exposure, 2),
        "closed_trades": closed,
        "wins": perf["wins"],
        "win_rate": round(perf["wins"] / closed, 3) if closed else 0.0,
    }


@router.get("/strategies")
async def strategies() -> dict[str, object]:
    """List every available strategy plus the id of the active one."""
    store = get_store()
    active_id = await store.get_strategy_id() or DEFAULT_STRATEGY_ID
    return {"strategies": list_strategies(), "active": active_id}


@router.get("/critiques")
async def critiques(limit: int = 20) -> dict[str, list[dict]]:
    """LLM post-mortems of the most recent stop-loss hits."""
    return {"critiques": await get_store().critiques(limit)}


@router.get("/backtest")
async def backtest() -> dict[str, object]:
    """The last illustrative backtest report (empty until one is run)."""
    from app.services.backtest import get_last_report

    return {"report": get_last_report()}
