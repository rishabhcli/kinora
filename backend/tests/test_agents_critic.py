"""Unit tests for the Critic: the §9.5 thresholds + repair routing (pure, per
failure mode) and the full score path with injected embedding/VL numbers."""

from __future__ import annotations

from app.agents.contracts import RepairAction, Verdict
from app.agents.critic import Critic, decide_qa
from app.memory.interfaces import CanonSlice
from app.providers import Providers, ProviderTimeout
from tests.test_agents_support import (
    JsonSequencer,
    OneHotEmbedder,
    one_hot,
    providers,  # noqa: F401  (pytest fixture)
)


def test_decide_qa_all_pass() -> None:
    verdict, action, score = decide_qa(0.91, 0.04, True, 0.10)
    assert verdict is Verdict.PASS
    assert action is RepairAction.ACCEPT
    assert 0.0 <= score <= 1.0


def test_decide_qa_timeline_raises_conflict() -> None:
    verdict, action, _ = decide_qa(0.91, 0.04, False, 0.10)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.RAISE_CONFLICT


def test_decide_qa_timeline_with_textual_support_evolves_canon() -> None:
    _, action, _ = decide_qa(0.91, 0.04, False, 0.10, textual_evolution_supported=True)
    assert action is RepairAction.EVOLVE_CANON


def test_decide_qa_identity_drift_tightens_refs() -> None:
    # CCS fails, style is fine -> identity drift.
    _, action, _ = decide_qa(0.70, 0.04, True, 0.10)
    assert action is RepairAction.REGEN_TIGHTEN_REFS


def test_decide_qa_style_drift_reprompts_style() -> None:
    _, action, _ = decide_qa(0.91, 0.20, True, 0.10)
    assert action is RepairAction.REPROMPT_STYLE


def test_decide_qa_motion_artifact_new_seed() -> None:
    _, action, _ = decide_qa(0.91, 0.04, True, 0.60)
    assert action is RepairAction.REGEN_NEW_SEED


def test_decide_qa_retries_exhausted_degrades() -> None:
    verdict, action, _ = decide_qa(0.10, 0.50, False, 0.90, retries_exhausted=True)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.DEGRADE


def _empty_slice() -> CanonSlice:
    return CanonSlice(book_id="book_x", beat_id="beat_0001", beat_index=1)


async def test_score_pass_path(providers: Providers) -> None:  # noqa: F811
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]
    providers.vl.analyze_json = JsonSequencer(  # type: ignore[method-assign]
        {"timeline_ok": True, "motion_artifact": 0.10, "reason": "clean"}
    )
    frame = b"\x89PNG-frame"
    record = await Critic(providers).score(
        shot_id="shot_1",
        clip_frames=[frame],
        canon_slice=_empty_slice(),
        character_crop=b"same",
        locked_ref_image=b"same",  # identical bytes -> CCS 1.0
        scene_style_centroid=one_hot(frame),  # matches the clip's style -> drift 0
    )
    assert record.verdict is Verdict.PASS
    assert record.repair_action is RepairAction.ACCEPT
    assert record.ccs == 1.0
    assert record.style_drift == 0.0
    assert record.timeline_ok is True


async def test_score_identity_drift_path(providers: Providers) -> None:  # noqa: F811
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]
    providers.vl.analyze_json = JsonSequencer(  # type: ignore[method-assign]
        {"timeline_ok": True, "motion_artifact": 0.10, "reason": "face drift"}
    )
    frame = b"\x89PNG-frame"
    record = await Critic(providers).score(
        shot_id="shot_2",
        clip_frames=[frame],
        canon_slice=_empty_slice(),
        character_crop=b"crop-A",
        locked_ref_image=b"ref-B",  # different bytes -> CCS 0.0 (fail)
        scene_style_centroid=one_hot(frame),
    )
    assert record.verdict is Verdict.FAIL
    assert record.ccs == 0.0
    assert record.repair_action is RepairAction.REGEN_TIGHTEN_REFS


async def test_score_style_drift_fails_gate(providers: Providers) -> None:  # noqa: F811
    """Fix 4: a clip whose style diverges from the scene centroid fails the §9.5
    style gate (style_drift > 0.08 → FAIL → reprompt_style). Identity is held at
    CCS 1.0 so the failure is unambiguously the *style* check."""
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]
    providers.vl.analyze_json = JsonSequencer(  # type: ignore[method-assign]
        {"timeline_ok": True, "motion_artifact": 0.05, "reason": "palette drift"}
    )
    frame = b"\x89PNG-frame"
    # A centroid on a *different* one-hot axis than the clip's style → cosine 0 →
    # style_drift 1.0 (deterministic; never a hash collision with the frame).
    frame_axis = one_hot(frame).index(1.0)
    centroid = [0.0] * 1152
    centroid[(frame_axis + 7) % 1152] = 1.0

    record = await Critic(providers).score(
        shot_id="shot_style",
        clip_frames=[frame],
        canon_slice=_empty_slice(),
        character_crop=b"same",
        locked_ref_image=b"same",  # CCS 1.0 — isolates the style gate
        scene_style_centroid=centroid,
    )
    assert record.ccs == 1.0
    assert record.style_drift > 0.08
    assert record.verdict is Verdict.FAIL
    assert record.repair_action is RepairAction.REPROMPT_STYLE


async def test_score_uses_deterministic_qa_when_vision_times_out(
    providers: Providers,  # noqa: F811
) -> None:
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]

    async def _timeout(*args: object, **kwargs: object) -> object:
        raise ProviderTimeout("vision timed out")

    providers.vl.analyze_json = _timeout  # type: ignore[method-assign]
    frame = b"\x89PNG-frame"

    record = await Critic(providers).score(
        shot_id="shot_timeout",
        clip_frames=[frame],
        canon_slice=_empty_slice(),
        character_crop=b"same",
        locked_ref_image=b"same",
        scene_style_centroid=one_hot(frame),
    )

    assert record.verdict is Verdict.PASS
    assert record.repair_action is RepairAction.ACCEPT
    assert record.timeline_ok is True
    assert record.flagged_for_review is True
    assert record.reason == "vision unavailable: ProviderTimeout; deterministic QA used"
