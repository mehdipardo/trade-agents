"""Pure evaluation metrics for the analyst.

Given a list of prediction records (expected vs predicted), compute:
- sentiment accuracy and asset-mapping accuracy,
- a confusion matrix,
- per-class precision / recall / F1,
- calibration (reliability bins + Expected Calibration Error).

All functions are pure and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass

SENTIMENTS = ("BULL", "BEAR", "NEUTRAL")


@dataclass(frozen=True)
class Prediction:
    """One analyst prediction against its label."""

    expected_sentiment: str
    predicted_sentiment: str
    confidence: float
    expected_asset: str | None = None
    predicted_asset: str | None = None


def sentiment_accuracy(preds: list[Prediction]) -> float:
    if not preds:
        return 0.0
    correct = sum(p.expected_sentiment == p.predicted_sentiment for p in preds)
    return correct / len(preds)


def asset_accuracy(preds: list[Prediction]) -> float:
    """Accuracy of asset mapping over predictions where an asset is expected."""
    relevant = [p for p in preds if p.expected_asset is not None]
    if not relevant:
        return 0.0
    correct = sum(p.expected_asset == p.predicted_asset for p in relevant)
    return correct / len(relevant)


def confusion_matrix(preds: list[Prediction]) -> dict[str, dict[str, int]]:
    """matrix[expected][predicted] = count."""
    matrix = {e: {p: 0 for p in SENTIMENTS} for e in SENTIMENTS}
    for p in preds:
        if p.expected_sentiment in matrix and p.predicted_sentiment in SENTIMENTS:
            matrix[p.expected_sentiment][p.predicted_sentiment] += 1
    return matrix


def per_class_prf(preds: list[Prediction]) -> dict[str, dict[str, float]]:
    """Precision/recall/F1 per sentiment class."""
    out: dict[str, dict[str, float]] = {}
    for cls in SENTIMENTS:
        tp = sum(p.predicted_sentiment == cls and p.expected_sentiment == cls for p in preds)
        fp = sum(p.predicted_sentiment == cls and p.expected_sentiment != cls for p in preds)
        fn = sum(p.predicted_sentiment != cls and p.expected_sentiment == cls for p in preds)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        out[cls] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    return out


def calibration_bins(preds: list[Prediction], n_bins: int = 10) -> list[dict[str, float]]:
    """Reliability bins: for each confidence bucket, mean confidence vs accuracy."""
    bins: list[dict[str, float]] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        # Include the right edge in the last bin.
        in_bin = [
            p
            for p in preds
            if (lo <= p.confidence < hi) or (i == n_bins - 1 and p.confidence == 1.0)
        ]
        if not in_bin:
            continue
        acc = sum(p.expected_sentiment == p.predicted_sentiment for p in in_bin) / len(in_bin)
        mean_conf = sum(p.confidence for p in in_bin) / len(in_bin)
        bins.append(
            {
                "lo": lo,
                "hi": hi,
                "count": len(in_bin),
                "accuracy": acc,
                "mean_confidence": mean_conf,
            }
        )
    return bins


def expected_calibration_error(preds: list[Prediction], n_bins: int = 10) -> float:
    """ECE: weighted average gap between confidence and accuracy across bins."""
    if not preds:
        return 0.0
    total = len(preds)
    ece = 0.0
    for b in calibration_bins(preds, n_bins):
        ece += (b["count"] / total) * abs(b["accuracy"] - b["mean_confidence"])
    return ece


def summarize(preds: list[Prediction], n_bins: int = 10) -> dict:
    """Full metrics report."""
    return {
        "n": len(preds),
        "sentiment_accuracy": sentiment_accuracy(preds),
        "asset_accuracy": asset_accuracy(preds),
        "ece": expected_calibration_error(preds, n_bins),
        "per_class": per_class_prf(preds),
        "confusion": confusion_matrix(preds),
        "calibration": calibration_bins(preds, n_bins),
    }
