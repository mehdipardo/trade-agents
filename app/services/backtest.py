"""Backtest / replay engine.

Replays a curated set of historical news events through the SAME analyst + risk
engine the live pipeline uses, then scores each trade against the asset's actual
move over the trade horizon, net of fees. This produces an honest illustrative
track record: it shows what the tool WOULD have done and how it would have
fared — not reconstructed live trades.

Every produced record is tagged ``mode="backtest"`` so it is always
distinguishable from live paper trades in the store, the API and the dashboard.

Determinism: the PnL math is pure and unit-tested. Direction/asset come from the
real ``analyze`` call (LLM when a key is set, offline classifier otherwise), so
a mis-classification correctly shows up as a losing backtest trade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.models.schemas import NewsEvent
from app.risk.rules import RiskConfig, RiskContext, evaluate
from app.services.llm import analyze
from app.services.store import get_store
from app.services.strategy import get_active_strategy

log = get_logger("app.services.backtest")

DEFAULT_BACKTEST_PATH = Path(__file__).resolve().parents[2] / "data" / "backtest" / "sample.json"

# Last backtest report, cached so the dashboard can read it without re-running
# (which would re-spend LLM calls). Set by ``run_backtest(record=True)``.
_last_report: dict[str, Any] | None = None


def get_last_report() -> dict[str, Any] | None:
    return _last_report


def _capped_pnl_pct(side: str, market_move_pct: float, sl_pct: float, tp_pct: float) -> float:
    """Realized move for the position, capped by the strategy's SL/TP.

    ``market_move_pct`` is the asset's signed price move over the horizon. A long
    captures it directly; a short captures its negation. The result is then
    clamped to [-sl_pct, +tp_pct] to approximate the exit logic.
    """
    directional = market_move_pct if side == "buy" else -market_move_pct
    return max(-abs(sl_pct), min(directional, abs(tp_pct)))


def score_trade(
    side: str,
    notional_quote: float,
    entry_price: float,
    market_move_pct: float,
    sl_pct: float,
    tp_pct: float,
    fee_pct: float,
) -> dict[str, float]:
    """Compute a backtest trade's exit price, gross/fee/net PnL (all in quote)."""
    pnl_pct = _capped_pnl_pct(side, market_move_pct, sl_pct, tp_pct)
    amount = notional_quote / entry_price if entry_price else 0.0
    # Exit price implied by the captured directional move.
    exit_move = pnl_pct if side == "buy" else -pnl_pct
    exit_price = entry_price * (1 + exit_move / 100)
    gross = notional_quote * pnl_pct / 100
    fee = (entry_price * amount + exit_price * amount) * (fee_pct / 100)
    return {
        "exit_price": round(exit_price, 8),
        "amount": round(amount, 8),
        "gross_pnl": round(gross, 4),
        "fee": round(fee, 4),
        "net_pnl": round(gross - fee, 4),
        "pnl_pct": round(pnl_pct, 4),
    }


def load_backtest_events(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the curated backtest event set (empty list if the file is absent)."""
    path = path or DEFAULT_BACKTEST_PATH
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", []) if isinstance(data, dict) else data
    return [e for e in events if isinstance(e, dict)]


async def run_backtest(
    events: list[dict[str, Any]] | None = None,
    *,
    settings: Settings | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """Replay events through analyst + risk; return a net-of-fees report.

    Each event dict carries: title, content, author, date (published_at),
    market_move_pct (the asset's actual signed move over the trade horizon).
    """
    settings = settings or get_settings()
    events = events if events is not None else load_backtest_events()
    strategy = await get_active_strategy()
    config = RiskConfig.from_settings(settings, strategy)
    store = get_store()
    equity = settings.starting_equity_quote

    trades: list[dict[str, Any]] = []
    net_total = 0.0
    wins = 0
    taken = 0

    for ev in events:
        news = NewsEvent(
            id=f"bt-{ev.get('id') or ev.get('title', '')[:24]}",
            source="simulator",
            author=ev.get("author"),
            title=ev.get("title", ""),
            content=ev.get("content", ""),
            published_at=None,
            received_at=datetime.now(UTC),
        )
        signal = await analyze(news, settings)
        # A fresh context: no open position / cooldown (each event scored alone).
        ctx = RiskContext(
            equity_quote=equity,
            trades_last_hour=0,
            daily_pnl_quote=0.0,
            kill_switch_active=False,
            asset_in_cooldown=False,
            open_position_on_asset=False,
        )
        verdict = evaluate(signal, ctx, config)
        record_row: dict[str, Any] = {
            "mode": "backtest",
            "ts": (ev.get("date") or datetime.now(UTC).isoformat()),
            "title": ev.get("title", ""),
            "sentiment": signal.sentiment,
            "asset": signal.asset,
            "confidence": signal.confidence,
            "impact_score": signal.impact_score,
            "market_move_pct": ev.get("market_move_pct"),
            "note": ev.get("note", ""),
        }
        if not verdict.approved or verdict.side is None:
            record_row.update({"status": "skipped", "reject_reason": verdict.reject_reason})
            trades.append(record_row)
            continue

        entry_price = float(ev.get("entry_price") or _mock_entry(signal.asset))
        scored = score_trade(
            verdict.side,
            verdict.position_size_quote or 0.0,
            entry_price,
            float(ev.get("market_move_pct") or 0.0),
            verdict.stop_loss_pct or config.stop_loss_pct,
            verdict.take_profit_pct or config.take_profit_pct,
            settings.taker_fee_pct,
        )
        taken += 1
        net_total += scored["net_pnl"]
        wins += 1 if scored["net_pnl"] > 0 else 0
        record_row.update({
            "status": "executed",
            "side": verdict.side,
            "leverage": verdict.leverage,
            "position_size_quote": verdict.position_size_quote,
            "entry_price": entry_price,
            **scored,
        })
        trades.append(record_row)

    report = {
        "mode": "backtest",
        "events": len(events),
        "trades_taken": taken,
        "wins": wins,
        "win_rate": round(wins / taken, 3) if taken else 0.0,
        "net_pnl_quote": round(net_total, 4),
        "total_fees_quote": round(sum(t.get("fee", 0.0) for t in trades), 4),
        "return_pct": round(net_total / equity * 100, 3) if equity else 0.0,
        "trades": trades,
    }

    if record:
        global _last_report
        _last_report = report
        for t in trades:
            await store.record_history(t)
        log.info(
            "backtest_recorded",
            trades_taken=taken, wins=wins, net_pnl=report["net_pnl_quote"],
        )
    return report


def _mock_entry(asset: str | None) -> float:
    from app.graph.nodes.executor import _MOCK_PRICES

    return _MOCK_PRICES.get(asset or "", 100.0)
