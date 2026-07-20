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
class ConfigField:
    """Metadata for a user-editable source-config field (rendered as an input)."""

    name: str  # setting key, e.g. "truth_social_url" (matches Settings attr name)
    label: str  # human-readable label shown next to the input
    placeholder: str = ""
    secret: bool = False  # renders as <input type="password"> and is masked on GET
    required: bool = True
    help: str = ""
    default: str = ""  # pre-filled value when the operator hasn't set one


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
    # Config fields the operator can fill from the dashboard. Empty tuple means
    # the source works out-of-the-box (no setup required).
    config_fields: tuple[ConfigField, ...] = field(default_factory=tuple)


# The curated catalog. Ordered by recommended prominence for the demo.
CATALOG: tuple[SourceSpec, ...] = (
    SourceSpec(
        id="news_aggregator",
        name="SSE news aggregator (optional, paid)",
        kind="news",
        description=(
            "Single low-latency SSE firehose fanning in 200+ outlets. The former "
            "free endpoint (cryptocurrency.cv) went paywalled (HTTP 402), so this "
            "is OFF by default — paste a working SSE URL below to re-enable it. "
            "Crypto + world coverage is otherwise provided by the RSS source."
        ),
        cost="freemium",
        reactivity="~seconds (SSE stream)",
        default_enabled=False,
        notes="Set an SSE endpoint that emits JSON news items to turn this on.",
        tags=("news", "broad"),
        config_fields=(
            ConfigField(
                name="aggregator_sse_url",
                label="SSE endpoint URL",
                placeholder="https://your-provider.example/api/sse",
                required=True,
                help="Any SSE stream of JSON news items {title, description, link, pubDate}.",
            ),
        ),
    ),
    SourceSpec(
        id="econ_calendar",
        name="Economic calendar (NFP, CPI, FOMC…)",
        kind="economic",
        description=(
            "Scheduled macro releases scored by expected volatility. Armed events "
            "trigger a pre-positioned watcher that fires within seconds of the "
            "free feed publishing the actual value (the feed itself lags the wire "
            "print by minutes — sub-minute entry needs a paid low-latency feed)."
        ),
        cost="free",
        reactivity="scheduled → seconds after the free feed publishes the actual",
        default_enabled=True,
        notes="Schedule + impact rating from a free Forex Factory feed; releases "
        "confirmed against the official source when armed.",
        tags=("macro", "high-impact", "recommended"),
    ),
    SourceSpec(
        id="trump_truthsocial",
        name="Trump — Truth Social (live watchlist)",
        kind="social",
        description="Live posts from a watchlist of prominent public accounts, polled "
        "frequently.",
        cost="free",
        reactivity="~seconds (poll floor — not millisecond/HFT)",
        default_enabled=False,
        notes="Pre-configured for the Trump family, but OFF by default: Truth "
        "Social's API is Cloudflare-gated and blocks datacenter/VPS IPs, so direct "
        "polling from a server usually 403s. Enable it only when you point the "
        "watchlist at a legitimate feed our server CAN reach — a third-party RSS/"
        "JSON mirror, an automation that POSTs to /webhooks/news, or the paid Truth "
        "API. Do not try to defeat the Cloudflare protection.",
        tags=("social", "high-impact", "recommended"),
        config_fields=(
            ConfigField(
                name="truth_social_urls",
                label="Accounts watchlist (handles or URLs)",
                placeholder="@realDonaldTrump, @DonaldJTrumpJr, @dbongino, @kashpatel",
                default="https://truthsocial.com/api/v1/accounts/107780257626128497/"
                "statuses?exclude_replies=true, @DonaldJTrumpJr, @EricTrump, @LaraLeaTrump",
                help="Comma/newline-separated handles (resolved automatically) or full "
                "statuses URLs. Defaults to the active Trump-family accounts; edit to "
                "add the other most-influential accounts.",
            ),
            ConfigField(
                name="truth_social_token",
                label="API bearer token (optional)",
                placeholder="eyJ… (leave blank to try unauthenticated / a mirror)",
                required=False,
                secret=True,
                help="Truth Social's API is Cloudflare/auth-gated; a token makes polling "
                "reliable. Set via env, never commit it.",
            ),
            ConfigField(
                name="truth_social_url",
                label="Statuses feed URL (single, legacy)",
                placeholder="https://truthsocial.com/api/v1/accounts/<id>/statuses",
                required=False,
                help="Single-account fallback if the watchlist above is empty.",
            ),
        ),
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
        config_fields=(
            ConfigField(
                name="congress_api_key",
                label="Congress.gov API key",
                placeholder="paste your free api.congress.gov key",
                secret=True,
            ),
            ConfigField(
                name="congress_tracked_bills",
                label="Tracked bills",
                placeholder="119/hr/1747,119/s/1582",
                help="Comma-separated bill refs, e.g. 119/hr/1747 (CLARITY).",
            ),
        ),
    ),
    SourceSpec(
        id="crypto_news_rss",
        name="Crypto · markets · world news (RSS)",
        kind="news",
        description=(
            "The primary firehose: crypto (CoinTelegraph, Decrypt, CoinDesk) + "
            "business/markets (BBC, CNBC) + world/geopolitics (BBC World). Only "
            "items published after startup flow through; the LLM triages impact."
        ),
        cost="free",
        reactivity="minutes (poll)",
        default_enabled=True,
        notes="Ships with a curated default feed set; override the list below "
        "with any comma-separated public RSS endpoints.",
        tags=("news", "broad", "crypto", "tradfi", "geopolitics", "recommended"),
        config_fields=(
            ConfigField(
                name="rss_feeds",
                label="RSS feed URLs (optional override)",
                placeholder="leave empty to use the built-in world+markets feeds",
                required=False,
                help="Comma-separated public RSS endpoints. Empty = curated defaults.",
            ),
        ),
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


def _mask(value: str) -> str:
    """Never echo a secret back verbatim; keep the last 4 chars for recognition."""
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else ""
    return f"••••{tail}"


def as_dict(
    *,
    current_values: dict[str, dict[str, str]] | None = None,
    running_ids: set[str] | None = None,
) -> list[dict]:
    """Serialize the catalog for the API/UI.

    Args:
        current_values: {source_id: {field_name: current_value}} — used to
            pre-fill inputs on the dashboard. Secrets are masked.
        running_ids: source ids the manager has an active background task for.
    """
    current_values = current_values or {}
    running_ids = running_ids or set()
    out: list[dict] = []
    for s in CATALOG:
        vals = current_values.get(s.id, {})
        fields: list[dict] = []
        for f in s.config_fields:
            # Operator override first, else the shipped default (pre-filled).
            raw = vals.get(f.name, "") or f.default
            fields.append({
                "name": f.name,
                "label": f.label,
                "placeholder": f.placeholder,
                "secret": f.secret,
                "required": f.required,
                "help": f.help,
                "value": _mask(raw) if f.secret else raw,
                "has_value": bool(raw),
            })
        out.append({
            "id": s.id,
            "name": s.name,
            "kind": s.kind,
            "description": s.description,
            "cost": s.cost,
            "reactivity": s.reactivity,
            "notes": s.notes,
            "tags": list(s.tags),
            "enabled": is_enabled(s.id),
            "running": s.id in running_ids,
            "config_fields": fields,
            # Only REQUIRED fields left empty count as "needs setup" — optional
            # fields (a token, a legacy fallback) never block a source.
            "needs_config": any(
                not fld["has_value"] for fld in fields if fld["required"]
            ),
        })
    return out
