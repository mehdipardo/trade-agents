"""Curated catalog of live sources.

Instead of the operator pasting arbitrary URLs, the platform ships a vetted
catalog of high-signal, (mostly) free live sources that the UI can browse and
toggle. Each entry is metadata describing a connector; the connectors themselves
live in this package and all normalize into the same ``NewsEvent`` queue.

Enable/disable state is process-local for now (mirrors the in-memory store);
it can be moved to Redis when horizontal scaling is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SourceKind = Literal["social", "economic", "regulatory", "news"]
Cost = Literal["free", "freemium", "paid"]


@dataclass(frozen=True)
class SourceSpec:
    """Metadata describing a catalog source (what the UI renders)."""

    id: str
    name: str
    kind: SourceKind
    description: str
    cost: Cost
    # Honest, human-readable reactivity expectation.
    reactivity: str
    default_enabled: bool = False
    # Free-form notes surfaced in the UI (caveats, config hints).
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# The curated catalog. Ordered by recommended prominence for the demo.
CATALOG: tuple[SourceSpec, ...] = (
    SourceSpec(
        id="econ_calendar",
        name="Economic calendar (NFP, CPI, FOMC…)",
        kind="economic",
        description=(
            "Scheduled macro releases scored by expected volatility. Armed events "
            "trigger a pre-positioned watcher that captures the print within ~1s."
        ),
        cost="free",
        reactivity="scheduled → ~1–2s at release (pre-armed)",
        default_enabled=True,
        notes="Schedule + impact rating from a free Forex Factory feed; releases "
        "confirmed against the official source when armed.",
        tags=("macro", "high-impact", "recommended"),
    ),
    SourceSpec(
        id="trump_truthsocial",
        name="Trump — Truth Social (live)",
        kind="social",
        description="Live posts from a prominent public account, polled frequently.",
        cost="free",
        reactivity="~seconds (fragile)",
        default_enabled=False,
        notes="No official API: direct polling of the public statuses endpoint is "
        "fastest but ToS-gray and can break; a mirror archive (~5 min) is the "
        "robust fallback.",
        tags=("social", "high-impact"),
    ),
    SourceSpec(
        id="sec_press",
        name="SEC — press & litigation (RSS)",
        kind="regulatory",
        description="Official SEC press releases and litigation feeds.",
        cost="free",
        reactivity="minutes (poll)",
        default_enabled=False,
        tags=("regulation",),
    ),
    SourceSpec(
        id="congress_bills",
        name="US Congress — tracked bills (CLARITY, GENIUS…)",
        kind="regulatory",
        description="Status changes on tracked crypto bills via the Congress.gov API.",
        cost="free",
        reactivity="hours (status changes)",
        default_enabled=False,
        notes="Free official API; track specific bill numbers.",
        tags=("regulation", "legislation"),
    ),
    SourceSpec(
        id="crypto_news_rss",
        name="Crypto news (CoinDesk, The Block…)",
        kind="news",
        description="Always-on baseline crypto headlines; dedup filters the noise.",
        cost="free",
        reactivity="minutes (poll)",
        default_enabled=False,
        tags=("baseline",),
    ),
)

_BY_ID = {s.id: s for s in CATALOG}

# Process-local enabled set, seeded from defaults.
_enabled: set[str] = {s.id for s in CATALOG if s.default_enabled}


def list_specs() -> tuple[SourceSpec, ...]:
    return CATALOG


def get_spec(source_id: str) -> SourceSpec | None:
    return _BY_ID.get(source_id)


def is_enabled(source_id: str) -> bool:
    return source_id in _enabled


def set_enabled(source_id: str, enabled: bool) -> bool:
    """Toggle a source. Returns the new state. Unknown ids raise KeyError."""
    if source_id not in _BY_ID:
        raise KeyError(source_id)
    if enabled:
        _enabled.add(source_id)
    else:
        _enabled.discard(source_id)
    return is_enabled(source_id)


def enabled_ids() -> set[str]:
    return set(_enabled)


def reset_state() -> None:
    """Reset to defaults (used by tests)."""
    global _enabled
    _enabled = {s.id for s in CATALOG if s.default_enabled}


def as_dict() -> list[dict]:
    """Serialize the catalog + enabled state for the API/UI."""
    return [
        {
            "id": s.id,
            "name": s.name,
            "kind": s.kind,
            "description": s.description,
            "cost": s.cost,
            "reactivity": s.reactivity,
            "notes": s.notes,
            "tags": list(s.tags),
            "enabled": is_enabled(s.id),
        }
        for s in CATALOG
    ]
