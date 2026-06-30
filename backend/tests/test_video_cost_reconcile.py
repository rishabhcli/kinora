"""Unit tests for estimated-vs-actual drift reconciliation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.video.cost.money import Currency, CurrencyMismatch, Money
from app.video.cost.reconcile import DriftRecorder, DriftSample


def test_drift_sample_signed_and_relative() -> None:
    s = DriftSample(
        provider="minimax", model="m", estimated=Money.usd("0.19"), actual=Money.usd("0.21")
    )
    assert s.drift == Money.usd("0.02")  # under-estimated
    assert s.relative_drift == Decimal(2) / Decimal(19)


def test_drift_sample_zero_estimate_relative_zero() -> None:
    s = DriftSample(provider="x", model="m", estimated=Money.usd("0"), actual=Money.usd("0.05"))
    assert s.relative_drift == Decimal(0)


def test_drift_sample_currency_guard() -> None:
    with pytest.raises(CurrencyMismatch):
        DriftSample(
            provider="x", model="m",
            estimated=Money.usd("1"), actual=Money.from_decimal("1", Currency.EUR),
        )


def test_recorder_per_provider_and_overall() -> None:
    rec = DriftRecorder()
    rec.record_estimate_actual("minimax", "m", Money.usd("0.19"), Money.usd("0.21"))
    rec.record_estimate_actual("minimax", "m", Money.usd("0.19"), Money.usd("0.19"))
    rec.record_estimate_actual("dashscope", "w", Money.usd("0.60"), Money.usd("0.50"))

    by = rec.by_provider()
    assert by["minimax"].samples == 2
    assert by["minimax"].total_drift == Money.usd("0.02")
    assert by["minimax"].under_estimating is True
    assert by["dashscope"].total_drift == Money.usd("-0.10")
    assert by["dashscope"].under_estimating is False

    overall = rec.overall()
    assert overall.samples == 3
    # est 0.19+0.19+0.60 = 0.98 ; actual 0.21+0.19+0.50 = 0.90 ; drift -0.08
    assert overall.estimated_total == Money.usd("0.98")
    assert overall.actual_total == Money.usd("0.90")
    assert overall.total_drift == Money.usd("-0.08")


def test_recorder_mean_relative_drift() -> None:
    rec = DriftRecorder()
    rec.record_estimate_actual("p", "m", Money.usd("1.00"), Money.usd("1.50"))
    d = rec.by_provider()["p"]
    assert d.mean_relative_drift == Decimal("0.5")


def test_recorder_metrics_shape() -> None:
    rec = DriftRecorder()
    rec.record_estimate_actual("minimax", "m", Money.usd("0.19"), Money.usd("0.21"))
    metrics = rec.as_metrics()
    assert metrics["samples"] == 1
    assert metrics["currency"] == "USD"
    assert metrics["total_drift"] == "0.02"
    per = metrics["by_provider"]
    assert isinstance(per, dict)
    assert per["minimax"]["under_estimating"] is True


def test_recorder_currency_guard() -> None:
    rec = DriftRecorder(Currency.USD)
    with pytest.raises(CurrencyMismatch):
        rec.record(
            DriftSample(
                provider="x", model="m",
                estimated=Money.from_decimal("1", Currency.EUR),
                actual=Money.from_decimal("1", Currency.EUR),
            )
        )
