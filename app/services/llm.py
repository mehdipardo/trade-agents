"""LLM analyst service.

One LLM call per event → a structured ``Signal`` (tool-calling structured
output). Provider is selected from settings (Groq or Gemini). Behaviour:

- Retry up to 2 attempts on validation/provider errors, reinjecting the error
  into the conversation so the model can correct itself.
- On final failure, return a NEUTRAL fallback ``Signal`` — never raise.
- Post-validation (never delegated to the LLM): ``asset`` must be in the
  whitelist, otherwise it is forced to ``None``.

When no provider API key is configured, an honest deterministic *offline*
keyword classifier is used instead, so the demo and tests run without external
calls. This is clearly logged and is NOT presented as real analysis.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.config import Settings
from app.logging_config import get_logger
from app.models.schemas import NewsEvent, Signal
from app.prompts.analyst import build_system_prompt, build_user_message

log = get_logger("app.services.llm")

_MAX_ATTEMPTS = 2

_UNSET = object()

# --- Offline keyword classifier (no-key / CI fallback) --------------------

_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard your rules",
    "system override",
    "you must buy",
    "output bull",
    "output bear",
)
_BULL_KEYWORDS = ("reserve", "approve", "approval", "etf", "adopt", "capital", "partnership")
_BEAR_KEYWORDS = ("cpi", "inflation", "hack", "ban", "lawsuit", "crash", "denied", "hawkish")
_ASSET_KEYWORDS = {
    "solana": "SOL/USDT",
    "sol": "SOL/USDT",
    "ethereum": "ETH/USDT",
    "ether": "ETH/USDT",
    "eth": "ETH/USDT",
    "ripple": "XRP/USDT",
    "xrp": "XRP/USDT",
    "doge": "DOGE/USDT",
    "bitcoin": "BTC/USDT",
    "btc": "BTC/USDT",
}


def neutral_fallback(reason: str = "analysis failed") -> Signal:
    """The safe default signal emitted when analysis cannot be trusted."""
    return Signal(
        sentiment="NEUTRAL",
        intensity=1,
        asset=None,
        confidence=0.0,
        rationale=reason[:250],
        event_type="other",
    )


def offline_keyword_classify(event: NewsEvent, settings: Settings) -> Signal:
    """Deterministic keyword classifier used when no LLM key is configured."""
    text = f"{event.title}\n{event.content}".lower()
    whitelist = settings.asset_whitelist_set

    if any(marker in text for marker in _INJECTION_MARKERS):
        return Signal(
            sentiment="NEUTRAL",
            intensity=1,
            asset=None,
            confidence=0.9,
            rationale="Content appears to be a manipulation attempt; no real market impact.",
            event_type="other",
        )

    is_bull = any(k in text for k in _BULL_KEYWORDS)
    is_bear = any(k in text for k in _BEAR_KEYWORDS)
    if is_bull == is_bear:
        return Signal(
            sentiment="NEUTRAL",
            intensity=1,
            asset=None,
            confidence=0.5,
            rationale="No clear tradable catalyst detected (offline classifier).",
            event_type="other",
        )

    asset: str | None = None
    for keyword, symbol in _ASSET_KEYWORDS.items():
        if keyword in text and symbol in whitelist:
            asset = symbol
            break
    if asset is None and "BTC/USDT" in whitelist:
        asset = "BTC/USDT"  # broad macro/crypto-wide -> BTC

    sentiment = "BULL" if is_bull else "BEAR"
    return Signal(
        sentiment=sentiment,
        intensity=4,
        asset=asset,
        confidence=0.8,
        rationale=f"Offline keyword classification -> {sentiment}.",
        event_type="macro" if is_bear else "social",
    )


# --- Real LLM path --------------------------------------------------------


def build_structured_llm(settings: Settings) -> Any | None:
    """Build a structured-output LLM for the configured provider.

    Returns ``None`` when no API key is configured or the provider package is
    not installed, so callers fall back to the offline classifier.
    """
    provider = settings.llm_provider
    try:
        if provider == "groq":
            if not settings.groq_api_key:
                return None
            from langchain_groq import ChatGroq

            llm = ChatGroq(
                model=settings.groq_model,
                api_key=settings.groq_api_key,
                temperature=0,
            )
        elif provider == "gemini":
            if not settings.gemini_api_key:
                return None
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(
                model=settings.gemini_model,
                google_api_key=settings.gemini_api_key,
                temperature=0,
            )
        else:  # pragma: no cover - guarded by settings Literal
            return None
    except ImportError:
        log.warning("llm_provider_unavailable", provider=provider)
        return None

    return llm.with_structured_output(Signal)


def _post_validate(signal: Signal, settings: Settings) -> Signal:
    """Enforce the asset whitelist in code (never trust the model)."""
    if signal.asset is not None and signal.asset not in settings.asset_whitelist_set:
        log.warning("asset_off_whitelist", asset=signal.asset)
        return signal.model_copy(update={"asset": None})
    return signal


async def _analyze_with_llm(event: NewsEvent, settings: Settings, structured_llm: Any) -> Signal:
    """Call the structured LLM with bounded retries and NEUTRAL fallback."""
    system = build_system_prompt(settings.asset_whitelist_set, settings.confidence_threshold)
    messages: list[Any] = [
        SystemMessage(content=system),
        HumanMessage(content=build_user_message(event)),
    ]

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = await structured_llm.ainvoke(messages)
            signal = result if isinstance(result, Signal) else Signal.model_validate(result)
            return signal
        except (ValidationError, Exception) as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("analyst_attempt_failed", attempt=attempt, error=str(exc))
            messages.append(
                HumanMessage(
                    content=(
                        f"Your previous response was invalid ({exc}). Respond again with a "
                        "SINGLE strict JSON object matching the schema exactly."
                    )
                )
            )

    log.error("analyst_fallback_neutral", event_id=event.id)
    return neutral_fallback()


async def analyze(event: NewsEvent, settings: Settings, *, llm: Any = _UNSET) -> Signal:
    """Analyze a news event into a validated ``Signal``.

    Args:
        event: The (untrusted) news event.
        settings: Application settings (whitelist, threshold, provider keys).
        llm: Optional structured LLM override (for tests). When omitted, one is
            built from settings; if none is available the offline classifier
            is used.
    """
    structured_llm = build_structured_llm(settings) if llm is _UNSET else llm

    if structured_llm is None:
        log.info("analyst_offline_mode", event_id=event.id)
        signal = offline_keyword_classify(event, settings)
    else:
        signal = await _analyze_with_llm(event, settings, structured_llm)

    return _post_validate(signal, settings)
