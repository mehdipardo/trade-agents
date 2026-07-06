"""Normalize arbitrary inbound payloads into a canonical ``NewsEvent``.

Sources (webhook, RSS, simulator) hand us loosely-shaped dicts. This module
maps common field aliases to the ``NewsEvent`` schema, generates a stable id
when the source did not provide one, and stamps ``received_at`` (UTC).

The news text itself is treated as UNTRUSTED downstream (see the analyst
prompt); this module only shapes structure, it never interprets content.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from app.models.schemas import NewsEvent

Source = Literal[
    "webhook", "rss", "simulator", "social", "economic", "regulatory", "news"
]

# Field aliases accepted from arbitrary payloads, in priority order.
_TITLE_KEYS = ("title", "headline", "subject")
_CONTENT_KEYS = ("content", "text", "body", "summary", "description")
_AUTHOR_KEYS = ("author", "source_name", "username", "user")
_URL_KEYS = ("url", "link", "permalink")
_PUBLISHED_KEYS = ("published_at", "published", "date", "timestamp")
_ID_KEYS = ("id", "guid", "uuid")


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_payload(payload: dict[str, Any], *, source: Source) -> NewsEvent:
    """Build a ``NewsEvent`` from a raw payload.

    Args:
        payload: Arbitrary source dict.
        source: Which ingestion channel produced the payload.

    Returns:
        A validated ``NewsEvent`` with ``received_at`` set to now (UTC).

    Raises:
        ValueError: If neither a title nor any content could be extracted.
    """
    received_at = datetime.now(UTC)

    title = _first(payload, _TITLE_KEYS)
    content = _first(payload, _CONTENT_KEYS) or ""

    # A usable event needs at least a title; fall back to the first line of
    # content so a bare-text webhook still produces something meaningful.
    if not title:
        if content:
            title = content.strip().splitlines()[0][:200]
        else:
            raise ValueError("payload has neither a title nor content")

    raw_id = _first(payload, _ID_KEYS)
    event_id = str(raw_id) if raw_id is not None else str(uuid.uuid4())

    return NewsEvent(
        id=event_id,
        source=source,
        author=_first(payload, _AUTHOR_KEYS),
        title=str(title),
        content=str(content),
        url=_first(payload, _URL_KEYS),
        published_at=_first(payload, _PUBLISHED_KEYS),
        received_at=received_at,
    )
