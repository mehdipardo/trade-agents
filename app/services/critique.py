"""LLM post-mortem on stop-loss hits.

When a stop-loss (main or runner-breakeven) triggers, this asks the analyst
LLM to critique its own signal against the realized outcome and produce a
short, actionable lesson. The result is persisted (bounded ring) so the
operator can browse it from the dashboard.

Never raises: on any provider/parsing failure it stores a minimal fallback
critique so the record of the stop is preserved either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.logging_config import get_logger
from app.services.llm import build_structured_llm as _build_structured_signal_llm
from app.services.store import get_store

log = get_logger("app.services.critique")

_SYSTEM_PROMPT = """\
You are a trading post-mortem analyst. A prior signal produced a losing
paper-trade whose stop-loss just hit. Given the original signal and the
realized outcome, produce a SHORT critique (max 3 short sentences) that
identifies the most likely reason the trade failed and one actionable
lesson for future signals. Be concrete. Avoid platitudes. English only.
Reply with plain text — no JSON, no bullets, no markdown.
"""


def _build_user_message(
    symbol: str, reason: str, position: dict, exit_price: float, pnl: float
) -> str:
    sig = position.get("original_signal") or {}
    return (
        f"Symbol: {symbol}\n"
        f"Side: {position.get('side')}\n"
        f"Entry price: {position.get('entry_price')}\n"
        f"Exit price: {exit_price}\n"
        f"Stop reason: {reason} (main SL or runner-breakeven)\n"
        f"Realized PnL (quote): {pnl:.4f}\n"
        f"Leverage applied: {position.get('leverage', 1)}\n"
        f"\nORIGINAL SIGNAL\n"
        f"  sentiment: {sig.get('sentiment')}\n"
        f"  intensity: {sig.get('intensity')}\n"
        f"  actionability: {sig.get('actionability')}\n"
        f"  impact_score: {sig.get('impact_score')}\n"
        f"  confidence: {sig.get('confidence')}\n"
        f"  event_type: {sig.get('event_type')}\n"
        f"  rationale: {sig.get('rationale')}\n"
    )


async def _call_llm(user_message: str) -> str | None:
    """Call the same provider as the analyst but for plain-text output."""
    settings = get_settings()
    try:
        if settings.llm_provider == "groq" and settings.groq_api_key:
            from langchain_groq import ChatGroq

            llm = ChatGroq(
                model=settings.groq_model,
                api_key=settings.groq_api_key,
                temperature=0.2,
            )
        elif settings.llm_provider == "gemini" and settings.gemini_api_key:
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(
                model=settings.gemini_model,
                google_api_key=settings.gemini_api_key,
                temperature=0.2,
            )
        else:
            # Silence the unused-import warning: build_structured is exported
            # for tests that stub the LLM via the same module.
            _ = _build_structured_signal_llm
            return None
    except ImportError:
        return None
    try:
        from app.services.llm import langfuse_callbacks

        config = {
            "callbacks": langfuse_callbacks(settings),
            "run_name": "critique",
            "metadata": {"langfuse_tags": ["critique", "post-mortem"]},
        }
        msg = await llm.ainvoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user_message)],
            config=config,
        )
        text = getattr(msg, "content", None)
        if isinstance(text, str) and text.strip():
            return text.strip()[:600]
    except Exception as exc:  # noqa: BLE001 - best effort
        log.warning("critique_llm_failed", error=str(exc))
    return None


def _offline_critique(
    symbol: str, reason: str, position: dict, exit_price: float, pnl: float
) -> str:
    """Deterministic fallback critique used when no LLM key is configured."""
    sig = position.get("original_signal") or {}
    conf = sig.get("confidence")
    impact = sig.get("impact_score")
    parts = [
        f"{reason} hit on {symbol} at {exit_price} (PnL {pnl:+.2f}).",
    ]
    if impact is not None and impact >= 8:
        parts.append(
            f"High-impact score ({impact}/10) unlocked leverage but the move "
            "faded — surprise element may have been over-estimated."
        )
    elif conf is not None and conf < 0.7:
        parts.append(
            f"Signal confidence was moderate ({conf:.2f}); tighten the gate "
            "or wait for confirmation before opening on similar setups."
        )
    else:
        parts.append(
            "Market rejected the thesis before TP; consider a wider SL or "
            "a smaller size on this event type."
        )
    return " ".join(parts)


async def generate_and_store_critique(
    symbol: str, reason: str, position: dict, exit_price: float, pnl: float
) -> dict[str, Any]:
    """Produce the critique (LLM or offline) and persist it. Returns the record."""
    user_msg = _build_user_message(symbol, reason, position, exit_price, pnl)
    text = await _call_llm(user_msg)
    if text is None:
        text = _offline_critique(symbol, reason, position, exit_price, pnl)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "reason": reason,
        "entry_price": position.get("entry_price"),
        "exit_price": exit_price,
        "pnl_quote": round(pnl, 4),
        "leverage": position.get("leverage", 1),
        "impact_score": (position.get("original_signal") or {}).get("impact_score"),
        "critique": text,
    }
    await get_store().record_critique(record)
    log.info("critique_stored", symbol=symbol, reason=reason, pnl=round(pnl, 4))
    return record
