"""Calibration pass — fit the CriticCalibration bundle from accumulated outcomes.

Combines the dataset seam + the reward fits into one bundle and verifies the
cold-start contract (default bundle = §9.5 defaults) and the end-to-end fit through
an in-memory signal source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.render.qa.calibration import (
    CalibrationPass,
    CriticCalibration,
    calibrate_from_outcomes,
    calibrate_from_samples,
)
from app.render.reward import QASample


@dataclass
class _Outcome:
    status: Any
    qa: dict[str, Any] | None


def _qa(ccs: float, drift: float, motion: float) -> dict[str, Any]:
    return {"ccs": ccs, "style_drift": drift, "timeline_ok": True, "motion_artifact": motion}


def _accept_outcomes(n: int) -> list[_Outcome]:
    return [_Outcome("accepted", _qa(0.93, 0.02, 0.05)) for _ in range(n)]


def _reject_outcomes(n: int) -> list[_Outcome]:
    return [_Outcome("degraded", _qa(0.55, 0.30, 0.60)) for _ in range(n)]


class _Source:
    """In-memory RewardSignalSource."""

    def __init__(self, outcomes: list[_Outcome]) -> None:
        self._outcomes = outcomes

    async def recent_outcomes(self, book_id: str, *, limit: int = 500) -> list[_Outcome]:
        return self._outcomes[:limit]


# --------------------------------------------------------------------------- #
# Cold start
# --------------------------------------------------------------------------- #


def test_default_bundle_is_cold_start() -> None:
    cal = CriticCalibration()
    assert cal.is_cold_start is True
    assert cal.thresholds.pinned is True
    assert cal.n_samples == 0


def test_calibrate_from_few_samples_pins_thresholds() -> None:
    samples = [QASample(0.93, 0.02, True, 0.05, accepted=True) for _ in range(3)]
    cal = calibrate_from_samples(samples)
    assert cal.thresholds.pinned is True  # too little data
    assert cal.thresholds.ccs_min == 0.85


# --------------------------------------------------------------------------- #
# Real fit
# --------------------------------------------------------------------------- #


def test_calibrate_from_outcomes_fits_reward() -> None:
    outcomes = _accept_outcomes(20) + _reject_outcomes(20)
    cal = calibrate_from_outcomes(outcomes, book_id="book_x")
    assert cal.book_id == "book_x"
    assert cal.n_samples == 40
    assert cal.is_cold_start is False
    # The bundle's advisory separates a clean clip from a broken one.
    good = cal.advise_clip(ccs=0.93, style_drift=0.02, timeline_ok=True, motion_artifact=0.05)
    bad = cal.advise_clip(ccs=0.55, style_drift=0.30, timeline_ok=True, motion_artifact=0.60)
    assert good.reward > bad.reward


def test_calibration_pass_runs_over_source() -> None:
    import anyio

    source = _Source(_accept_outcomes(20) + _reject_outcomes(20))
    cal = anyio.run(CalibrationPass(source).run, "book_x")
    assert cal.n_samples == 40
    assert cal.book_id == "book_x"


def test_calibration_pass_no_source_method_cold_starts() -> None:
    import anyio

    cal = anyio.run(CalibrationPass(object()).run, "book_x")
    assert cal.is_cold_start is True
    assert cal.book_id == "book_x"
