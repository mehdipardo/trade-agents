"""Slack notifications (async, non-blocking).

The graph never waits on Slack: the notifier schedules ``send_slack`` as a
background task. News-derived text (title, rationale) is escaped before being
placed into the message (guard-rail: untrusted content must not be rendered raw).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.logging_config import get_logger

log = get_logger("app.services.slack")


def _escape(text: str) -> str:
    """Escape the Slack control characters in untrusted text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_slack_message(summary: dict[str, Any]) -> str:
    """Render a one-line Slack message from a pipeline summary."""
    emoji = summary.get("emoji", "⚪")
    status = summary.get("status", "?")
    asset = summary.get("asset") or "-"
    sentiment = summary.get("sentiment") or "-"
    intensity = summary.get("intensity") or 0
    stars = "★" * int(intensity) + "☆" * (5 - int(intensity)) if intensity else "-"
    confidence = summary.get("confidence")
    conf_str = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else "-"
    latency = summary.get("total_latency_ms")
    lat_str = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"

    lines = [
        f"{emoji} *{status.upper()}* — {_escape(str(summary.get('title', '')))[:120]}",
        f"asset: `{asset}`  sentiment: {sentiment}  intensity: {stars}  conf: {conf_str}",
    ]

    if summary.get("side"):
        size = summary.get("position_size_quote")
        price = summary.get("avg_price")
        size_str = f"{size:.2f} USDT" if isinstance(size, (int, float)) else "-"
        price_str = f"@ {price}" if price else ""
        lines.append(f"order: {summary['side']} {size_str} {price_str}".strip())

    if summary.get("reject_reason"):
        lines.append(f"reject: {_escape(str(summary['reject_reason']))}")
    if summary.get("error"):
        lines.append(f"error: {_escape(str(summary['error']))}")

    lines.append(f"latency news→order: {lat_str}")
    if summary.get("url"):
        lines.append(f"<{summary['url']}|source>")
    return "\n".join(lines)


async def send_slack(webhook_url: str | None, text: str) -> bool:
    """POST a message to the Slack incoming webhook. Returns success."""
    if not webhook_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"text": text})
        ok = resp.status_code < 300
        if not ok:
            log.warning("slack_non_2xx", status=resp.status_code)
        return ok
    except Exception as exc:  # noqa: BLE001 - Slack must never break the pipeline
        log.warning("slack_send_failed", error=str(exc))
        return False
