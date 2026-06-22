"""The §13 protocol: crew-vs-baseline aggregation, projection math, thresholds.

Uses canned arms (injected per-arm embeddings) to prove the harness aggregates
mean+spread over 3 runs and that the crew beats the single-agent baseline on CCS
and accepted-footage efficiency (the thesis). Also pins the per-run projection
math (CCS → regen → efficiency), the pre-registered/frozen thresholds, and the
crew-arm adapter over a fake render pipeline (no infra).
"""

from __future__ import annotations

import dataclasses
import math
import statistics

import pytest

from app.eval.harness import (
    PRE_REGISTERED,
    CrewArm,
    DemoSequence,
    DemoShot,
    SequenceRun,
    ShotOutcome,
    embed_locked_refs,
    run_protocol,
    score_run,
)
from tests.conftest import FakeEmbedder

_DIM = 8


def one_hot(axis: int, *, dim: int = _DIM) -> list[float]:
    vec = [0.0] * dim
    vec[axis % dim] = 1.0
    return vec


def vec_at_cos(c: float, *, dim: int = _DIM) -> list[float]:
    """A unit vector whose cosine with ``one_hot(0)`` is exactly ``c``."""
    vec = [0.0] * dim
    vec[0] = c
    vec[1] = math.sqrt(max(0.0, 1.0 - c * c))
    return vec


def outcome(
    shot_id: str, crop: list[float], style: list[float], *, scene: str = "scene_1", est: float = 5.0
) -> ShotOutcome:
    return ShotOutcome(
        shot_id=shot_id,
        scene_id=scene,
        est_duration_s=est,
        style_embedding=style,
        character_crops={"char_a": crop},
    )


class CannedArm:
    """An :class:`Arm` that replays injected per-run outcomes (no providers)."""

    def __init__(self, name: str, runs: list[list[ShotOutcome]]) -> None:
        self.name = name
        self._runs = runs
        self.calls: list[int] = []

    async def run_sequence(self, sequence: DemoSequence, run_index: int) -> SequenceRun:
        self.calls.append(run_index)
        return SequenceRun(arm=self.name, outcomes=self._runs[run_index])


def _sequence() -> DemoSequence:
    return DemoSequence(
        book_id="book_demo",
        shots=[DemoShot(shot_id=f"s{i}", scene_id="scene_1", seed=i, prompt=f"b{i}",
                        character_keys=["char_a"]) for i in range(4)],
        locked_refs={"char_a": b"ref-a"},
    )


# --------------------------------------------------------------------------- #
# Per-run scoring / projection math
# --------------------------------------------------------------------------- #


def test_score_run_projects_regen_and_efficiency_from_ccs() -> None:
    run = SequenceRun(
        arm="x",
        outcomes=[
            outcome("s0", one_hot(0), one_hot(0)),  # pass (cos 1)
            outcome("s1", one_hot(0), one_hot(0)),  # pass
            outcome("s2", one_hot(1), one_hot(0)),  # fail (cos 0 < 0.85)
            outcome("s3", one_hot(1), one_hot(0)),  # fail
        ],
    )
    metrics = score_run(run, {"char_a": one_hot(0)})
    assert metrics.ccs == 0.5  # (1 + 1 + 0 + 0) / 4
    assert metrics.regen_rate == 0.5  # 2 of 4 shots fail first-pass QA
    # total = 4 one-pass (20s) + 2 rejected (10s) = 30s; efficiency = (1 - 10/30)*100.
    assert math.isclose(metrics.efficiency, (1 - 10 / 30) * 100, abs_tol=1e-6)
    assert metrics.style_drift == 0.0  # identical style embeddings


def test_score_run_per_character_ccs() -> None:
    run = SequenceRun(
        arm="x",
        outcomes=[
            ShotOutcome("s0", "scene_1", 5.0, one_hot(0),
                        {"char_a": one_hot(0), "char_b": one_hot(2)}),
            ShotOutcome("s1", "scene_1", 5.0, one_hot(0), {"char_a": one_hot(0)}),
        ],
    )
    metrics = score_run(run, {"char_a": one_hot(0), "char_b": one_hot(1)})
    assert metrics.per_character_ccs["char_a"] == 1.0  # matches its ref
    assert metrics.per_character_ccs["char_b"] == 0.0  # orthogonal to its ref


# --------------------------------------------------------------------------- #
# The crew-vs-baseline protocol (the thesis, with canned numbers)
# --------------------------------------------------------------------------- #


async def test_crew_beats_baseline_over_three_runs() -> None:
    locked = {"char_a": one_hot(0)}
    # Crew: every crop matches the canonical ref (CCS 1, no regen), coherent style.
    crew_run = [outcome(f"s{i}", one_hot(0), one_hot(0)) for i in range(4)]
    # Baseline: drifts off the ref (CCS 0, every shot fails), incoherent style.
    base_run = [outcome(f"s{i}", one_hot(1), one_hot(2 + i % 3)) for i in range(4)]

    crew = CannedArm("crew", [crew_run, crew_run, crew_run])
    baseline = CannedArm("baseline", [base_run, base_run, base_run])
    report = await run_protocol(
        crew=crew, baseline=baseline, sequence=_sequence(),
        locked_ref_embeddings=locked, runs=3,
    )

    assert report.runs == 3
    assert crew.calls == [0, 1, 2] and baseline.calls == [0, 1, 2]

    # The thesis: crew wins on consistency AND budget.
    assert report.crew.ccs.mean > report.baseline.ccs.mean
    assert report.crew.efficiency.mean > report.baseline.efficiency.mean
    # And on the supporting metrics too.
    assert report.crew.regen_rate.mean < report.baseline.regen_rate.mean
    assert report.crew.style_drift.mean < report.baseline.style_drift.mean

    # Concrete: crew CCS 1.0 / 0 regen / 100% efficiency; baseline 0 / 1.0 / 50%.
    assert report.crew.ccs.mean == 1.0
    assert report.baseline.ccs.mean == 0.0
    assert report.crew.efficiency.mean == 100.0
    assert report.baseline.efficiency.mean == 50.0
    assert report.crew.regen_rate.mean == 0.0
    assert report.baseline.regen_rate.mean == 1.0
    print(
        f"\n[CREW vs BASELINE] CCS {report.crew.ccs.mean:.2f} vs {report.baseline.ccs.mean:.2f}; "
        f"efficiency {report.crew.efficiency.mean:.0f}% vs {report.baseline.efficiency.mean:.0f}%; "
        f"regen {report.crew.regen_rate.mean:.2f} vs {report.baseline.regen_rate.mean:.2f}"
    )


async def test_report_contract_shape_and_mean_spread() -> None:
    locked = {"char_a": one_hot(0)}
    # Crew CCS varies across runs -> a non-zero spread the mean is computed over.
    crew_runs = [
        [outcome("s0", vec_at_cos(c), one_hot(0))] for c in (1.0, 0.9, 0.95)
    ]
    base_runs = [[outcome("s0", one_hot(1), one_hot(3))] for _ in range(3)]
    crew = CannedArm("crew", crew_runs)
    baseline = CannedArm("baseline", base_runs)

    report = await run_protocol(
        crew=crew, baseline=baseline, sequence=_sequence(),
        locked_ref_embeddings=locked, runs=3,
    )
    contract = report.to_contract()

    # Exact shared-contract keys + nested {crew, baseline} pairs.
    for key in ("ccs", "efficiency", "regen_rate", "style_drift"):
        assert set(contract[key].keys()) == {"crew", "baseline"}
        assert isinstance(contract[key]["crew"], float)
    assert contract["runs"] == 3
    assert contract["thresholds"]["ccs_min"] == 0.85  # §9.5, pre-registered
    assert set(contract["per_character_ccs"].keys()) == {"crew", "baseline"}

    # Mean + spread (the §13 "mean and spread across 3 runs").
    assert math.isclose(contract["ccs"]["crew"], statistics.fmean([1.0, 0.9, 0.95]), abs_tol=1e-6)
    assert "spread" in contract
    assert contract["spread"]["ccs"]["crew"] > 0.0  # real variance, reported
    assert report.crew.ccs.runs == [1.0, 0.9, 0.95]


def test_thresholds_pre_registered_and_frozen() -> None:
    # The §9.5 gates, fixed before any run and immutable (can't be tuned post-hoc).
    assert PRE_REGISTERED.ccs_min == 0.85
    assert PRE_REGISTERED.style_drift_max == 0.08
    assert PRE_REGISTERED.motion_artifact_max == 0.25
    with pytest.raises(dataclasses.FrozenInstanceError):
        PRE_REGISTERED.ccs_min = 0.5  # type: ignore[misc]


async def test_run_protocol_rejects_zero_runs() -> None:
    with pytest.raises(ValueError, match="runs must be >= 1"):
        await run_protocol(
            crew=CannedArm("crew", []), baseline=CannedArm("baseline", []),
            sequence=_sequence(), locked_ref_embeddings={}, runs=0,
        )


# --------------------------------------------------------------------------- #
# The crew arm adapter over a fake render pipeline (no infra)
# --------------------------------------------------------------------------- #


async def test_crew_arm_scores_canon_reference_as_perfect_ccs() -> None:
    class FakeRenderResult:
        last_frame_key: str | None = None
        video_seconds: float = 0.0

    rendered: list[str] = []

    async def render_shot(book_id: str, shot_id: str) -> FakeRenderResult:
        rendered.append(shot_id)
        return FakeRenderResult()

    embedder = FakeEmbedder()
    sequence = DemoSequence(
        book_id="b",
        shots=[DemoShot("s0", "scene_1", 1, "p", ["char_a"])],
        locked_refs={"char_a": b"ref-a"},
    )
    crew = CrewArm(render_shot=render_shot, embedder=embedder)

    run = await crew.run_sequence(sequence, run_index=0)
    assert rendered == ["s0"]  # the real per-shot pipeline was actually invoked

    locked = await embed_locked_refs(embedder, sequence)
    metrics = score_run(run, locked)
    # The keyframe ladder shows the canonical reference -> zero identity drift.
    assert metrics.ccs == 1.0
    assert metrics.regen_rate == 0.0
