"""Reward-dataset seam — map accumulated episodic QA outcomes to labeled samples.

A tiny structural double stands in for an episodic shot row (it just needs ``status``
+ ``qa``), so the pure adapter is tested without the ORM or a DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.render.qa.dataset import (
    build_reward_dataset,
    label_for_status,
    sample_from_qa,
)


@dataclass
class _Outcome:
    status: Any
    qa: dict[str, Any] | None


def _qa(ccs: float, drift: float, motion: float, ok: bool = True) -> dict[str, Any]:
    return {"ccs": ccs, "style_drift": drift, "timeline_ok": ok, "motion_artifact": motion}


# --------------------------------------------------------------------------- #
# label_for_status
# --------------------------------------------------------------------------- #


def test_label_accepted() -> None:
    assert label_for_status("accepted") is True


def test_label_degraded_is_reject() -> None:
    assert label_for_status("degraded") is False


def test_label_nonterminal_is_none() -> None:
    assert label_for_status("rendering") is None
    assert label_for_status("qa") is None


def test_label_handles_enum_like() -> None:
    class _E:
        value = "accepted"

    assert label_for_status(_E()) is True


# --------------------------------------------------------------------------- #
# sample_from_qa
# --------------------------------------------------------------------------- #


def test_sample_from_complete_qa() -> None:
    sample = sample_from_qa(_qa(0.9, 0.03, 0.1), accepted=True)
    assert sample is not None
    assert sample.ccs == 0.9
    assert sample.accepted is True


def test_sample_from_incomplete_qa_is_none() -> None:
    assert sample_from_qa({"ccs": 0.9}, accepted=True) is None  # missing fields
    assert sample_from_qa(None, accepted=True) is None
    assert sample_from_qa({}, accepted=True) is None


def test_sample_carries_extra_axes() -> None:
    qa = _qa(0.9, 0.03, 0.1)
    qa.update({"aesthetic": 0.7, "temporal": 0.8})
    sample = sample_from_qa(qa, accepted=True)
    assert sample is not None
    assert sample.aesthetic == 0.7
    assert sample.temporal == 0.8


# --------------------------------------------------------------------------- #
# build_reward_dataset
# --------------------------------------------------------------------------- #


def test_build_dataset_labels_by_status() -> None:
    outcomes = [
        _Outcome("accepted", _qa(0.92, 0.03, 0.08)),
        _Outcome("degraded", _qa(0.50, 0.30, 0.60)),
        _Outcome("rendering", _qa(0.90, 0.04, 0.10)),  # non-terminal → skipped
        _Outcome("accepted", None),  # no qa → skipped
    ]
    samples = build_reward_dataset(outcomes)
    assert len(samples) == 2
    assert samples[0].accepted is True
    assert samples[1].accepted is False


def test_build_dataset_empty() -> None:
    assert build_reward_dataset([]) == []
