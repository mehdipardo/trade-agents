"""Run the analyst over the golden set and compute metrics.

Uses ``app.services.llm.analyze`` so it evaluates whatever analyst is active:
the real LLM when a provider key is configured, otherwise the deterministic
offline classifier (which makes CI results stable and regression-safe).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import get_settings
from app.eval.golden import GoldenItem, load_golden
from app.eval.metrics import Prediction, summarize
from app.models.schemas import NewsEvent
from app.services.llm import analyze


def _to_event(item: GoldenItem) -> NewsEvent:
    return NewsEvent(
        id=item.id,
        source="simulator",
        author=item.author,
        title=item.title,
        content=item.content,
        received_at=datetime.now(UTC),
    )


async def run_eval(items: list[GoldenItem] | None = None) -> tuple[list[Prediction], dict]:
    """Run the analyst over the golden set; return predictions + metrics report."""
    settings = get_settings()
    items = items if items is not None else load_golden()

    preds: list[Prediction] = []
    for item in items:
        signal = await analyze(_to_event(item), settings)
        preds.append(
            Prediction(
                expected_sentiment=item.expected_sentiment,
                predicted_sentiment=signal.sentiment,
                confidence=signal.confidence,
                expected_asset=item.expected_asset,
                predicted_asset=signal.asset,
            )
        )
    return preds, summarize(preds)


def format_report(report: dict) -> str:
    """Render a human-readable evaluation report."""
    lines = [
        "=== Analyst evaluation ===",
        f"items:              {report['n']}",
        f"sentiment accuracy: {report['sentiment_accuracy']:.1%}",
        f"asset accuracy:     {report['asset_accuracy']:.1%}",
        f"ECE (calibration):  {report['ece']:.3f}",
        "",
        "per-class (P / R / F1 / support):",
    ]
    for cls, m in report["per_class"].items():
        lines.append(
            f"  {cls:8} {m['precision']:.2f} / {m['recall']:.2f} / "
            f"{m['f1']:.2f} / {int(m['support'])}"
        )
    lines.append("")
    lines.append("confusion (rows=expected, cols=predicted):")
    header = "           " + "  ".join(f"{c:>7}" for c in ("BULL", "BEAR", "NEUTRAL"))
    lines.append(header)
    for exp, row in report["confusion"].items():
        cells = "  ".join(f"{row[c]:>7}" for c in ("BULL", "BEAR", "NEUTRAL"))
        lines.append(f"  {exp:8} {cells}")
    lines.append("")
    lines.append("calibration bins (conf -> accuracy):")
    for b in report["calibration"]:
        lines.append(
            f"  [{b['lo']:.1f},{b['hi']:.1f})  n={int(b['count']):3}  "
            f"conf={b['mean_confidence']:.2f}  acc={b['accuracy']:.2f}"
        )
    return "\n".join(lines)
