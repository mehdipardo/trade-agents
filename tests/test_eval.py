"""Étape 9 tests: eval metrics (pure) + golden-set regression."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.eval.golden import load_golden
from app.eval.metrics import (
    Prediction,
    asset_accuracy,
    confusion_matrix,
    expected_calibration_error,
    per_class_prf,
    sentiment_accuracy,
)
from app.eval.runner import format_report, run_eval


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("EXCHANGE_SANDBOX", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _p(exp: str, pred: str, conf: float, ea=None, pa=None) -> Prediction:
    return Prediction(exp, pred, conf, ea, pa)


# --- pure metrics ---------------------------------------------------------


def test_sentiment_accuracy() -> None:
    preds = [_p("BULL", "BULL", 0.8), _p("BEAR", "NEUTRAL", 0.5)]
    assert sentiment_accuracy(preds) == 0.5
    assert sentiment_accuracy([]) == 0.0


def test_asset_accuracy_only_counts_expected() -> None:
    preds = [
        _p("BULL", "BULL", 0.8, "BTC/USDT", "BTC/USDT"),
        _p("BULL", "BULL", 0.8, "ETH/USDT", "BTC/USDT"),
        _p("NEUTRAL", "NEUTRAL", 0.9, None, None),  # ignored (no expected asset)
    ]
    assert asset_accuracy(preds) == 0.5


def test_confusion_matrix_counts() -> None:
    preds = [_p("BULL", "BEAR", 0.7), _p("BULL", "BULL", 0.9)]
    m = confusion_matrix(preds)
    assert m["BULL"]["BEAR"] == 1
    assert m["BULL"]["BULL"] == 1


def test_per_class_prf_perfect() -> None:
    preds = [_p("BULL", "BULL", 0.9), _p("BEAR", "BEAR", 0.9)]
    prf = per_class_prf(preds)
    assert prf["BULL"]["precision"] == 1.0
    assert prf["BULL"]["recall"] == 1.0
    assert prf["BULL"]["f1"] == 1.0


def test_ece_zero_when_perfectly_calibrated() -> None:
    # All confidence 1.0 and all correct -> ECE 0.
    preds = [_p("BULL", "BULL", 1.0), _p("BEAR", "BEAR", 1.0)]
    assert expected_calibration_error(preds) == pytest.approx(0.0)


def test_ece_detects_overconfidence() -> None:
    # Confidence 1.0 but half wrong -> ECE 0.5.
    preds = [_p("BULL", "BULL", 1.0), _p("BEAR", "BULL", 1.0)]
    assert expected_calibration_error(preds) == pytest.approx(0.5)


# --- golden set + runner --------------------------------------------------


def test_golden_set_loads_50_items() -> None:
    items = load_golden()
    assert len(items) == 50
    assert all(i.expected_sentiment in ("BULL", "BEAR", "NEUTRAL") for i in items)


async def test_run_eval_regression_floor() -> None:
    # Deterministic offline classifier: guard against regressions.
    preds, report = await run_eval()
    assert report["n"] == 50
    assert report["sentiment_accuracy"] >= 0.6
    # The free relevance pre-filter emits confidence 0.0 on skipped items, which
    # loosens calibration slightly; still guard against gross miscalibration.
    assert report["ece"] <= 0.2
    assert "BULL" in format_report(report)


def test_langfuse_callbacks_inert_without_keys() -> None:
    from app.services.llm import langfuse_callbacks

    assert langfuse_callbacks(get_settings()) == []
