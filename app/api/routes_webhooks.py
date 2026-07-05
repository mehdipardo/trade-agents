"""Generic secured news webhook.

``POST /webhooks/news`` accepts an arbitrary JSON payload from any external
source (Zapier, n8n, a bridge, ...). The ``X-Webhook-Secret`` header is checked
against ``WEBHOOK_SECRET`` in constant time; the payload is normalized and
queued. Dedup downstream collapses redeliveries.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import enqueue
from app.config import get_settings
from app.ingestion.normalizer import normalize_payload
from app.logging_config import get_logger

log = get_logger("app.api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/news", status_code=status.HTTP_202_ACCEPTED)
async def receive_news(
    payload: dict[str, Any],
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, str]:
    settings = get_settings()
    if not x_webhook_secret or not hmac.compare_digest(
        x_webhook_secret, settings.webhook_secret
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid secret")

    try:
        event = normalize_payload(payload, source="webhook")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    enqueue(request, event)
    log.info("webhook_received", event_id=event.id, title=event.title)
    return {"event_id": event.id, "status": "queued"}
