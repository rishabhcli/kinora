"""Critic ↔ learned-reward integration (§9.5/§13).

Verifies the additive learned-reward path: the per-character identity vector, the
deterministic temporal/aesthetic axes on the QARecord, the calibrated-threshold gate
(never looser than the pre-registered floor), and the advisory that flags a
gate-passing-but-suspect clip — all without ever overriding the pre-registered gate
or making a network call.
"""

from __future__ import annotations

import pytest

from app.agents.contracts import RepairAction, Verdict
from app.agents.critic import Critic, decide_qa
from app.memory.interfaces import CanonSlice
from app.providers import Providers
from app.render.qa.calibration import CriticCalibration, calibrate_from_samples
from app.render.qa.identity import CharacterCrops
from app.render.reward import QASample, RewardAdvice
from tests.test_agents_support import (
    JsonSequencer,
    OneHotEmbedder,
    one_hot,
    providers,  # noqa: F401  (pytest fixture)
)


def _empty_slice() -> CanonSlice:
    return CanonSlice(book_id="book_x", beat_id="beat_0001", beat_index=1)


def _strict_calibration() -> CriticCalibration:
    """A director who rejects clips the §9.5 floor would have passed (ccs 0.86)."""
    samples = [
        QASample(0.97, 0.01, True, 0.02, accepted=True) for _ in range(20)
    ] + [
        QASample(0.86, 0.07, True, 0.20, accepted=False) for _ in range(20)
    ]
    return calibrate_from_samples(samples, book_id="book_x")


# --------------------------------------------------------------------------- #
# decide_qa: the advisory never overrides the pre-registered gate
# --------------------------------------------------------------------------- #


def test_decide_qa_advice_never_rescues_a_failing_clip() -> None:
    # A failing clip (CCS 0.7) with a glowing advisory still FAILS — the hard gate
    # decides; the advisory cannot rescue it (§13 pre-registration honesty).
    glowing = RewardAdvice(reward=0.99, anomaly=False, flagged_for_review=False)
    verdict, action, _ = decide_qa(0.70, 0.04, True, 0.10, advice=glowing)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.REGEN_TIGHTEN_REFS


def test_decide_qa_advice_never_blocks_a_passing_clip() -> None:
    # A passing clip with a damning advisory still PASSES — the advisory only informs.
    damning = RewardAdvice(reward=0.01, anomaly=True, flagged_for_review=True)
    verdict, action, _ = decide_qa(0.95, 0.02, True, 0.05, advice=damning)
    assert verdict is Verdict.PASS
    assert action is RepairAction.ACCEPT


def test_decide_qa_none_advice_is_unchanged() -> None:
    a = decide_qa(0.91, 0.04, True, 0.10)
    b = decide_qa(0.91, 0.04, True, 0.10, advice=None)
    assert a == b


# --------------------------------------------------------------------------- #
# Critic.score: cold-start path is unchanged
# --------------------------------------------------------------------------- #


async def test_score_cold_start_matches_legacy(providers: Providers) -> None:  # noqa: F811
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
        locked_ref_image=b"same",
        scene_style_centroid=one_hot(frame),
    )
    assert record.verdict is Verdict.PASS
    # No calibration ⇒ no learned advisory attached.
    assert record.learned_reward is None
    assert record.flagged_for_review is False
    assert record.anomaly_score is None
    # Temporal/aesthetic are still computed (the frame bytes don't decode → neutral).
    assert record.temporal == 1.0
    assert record.aesthetic == 1.0


def test_with_calibration_does_not_reread_settings(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # with_calibration must reuse the already-resolved model, NOT call get_settings()
    # again (which would fail when the environment has no DASHSCOPE_API_KEY). Force
    # get_settings to blow up to prove the clone never touches it.
    import app.agents.critic as critic_mod

    base = Critic(providers)  # resolves the model once, here

    def _boom() -> object:
        raise AssertionError("with_calibration must not re-read Settings")

    monkeypatch.setattr(critic_mod, "get_settings", _boom)
    clone = base.with_calibration(CriticCalibration())
    assert clone.model == base.model
    assert clone.calibration is not None


# --------------------------------------------------------------------------- #
# Critic.score: per-character identity vector
# --------------------------------------------------------------------------- #


async def test_score_per_character_identity(providers: Providers) -> None:  # noqa: F811
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]
    providers.vl.analyze_json = JsonSequencer(  # type: ignore[method-assign]
        {"timeline_ok": True, "motion_artifact": 0.05, "reason": "ok"}
    )
    record = await Critic(providers).score(
        shot_id="shot_multi",
        clip_frames=[b"frame"],
        canon_slice=_empty_slice(),
        character_crops=[
            CharacterCrops("hero", ref_images=[b"hero"], crops=[b"hero"]),
            CharacterCrops("villain", ref_images=[b"villain"], crops=[b"wrong"]),
        ],
    )
    # One wrong face (villain) gates the shot → CCS 0.0 → identity fail.
    assert record.ccs == 0.0
    assert record.verdict is Verdict.FAIL
    assert record.repair_action is RepairAction.REGEN_TIGHTEN_REFS
    assert record.per_character_ccs is not None
    assert set(record.per_character_ccs) == {"hero", "villain"}


# --------------------------------------------------------------------------- #
# Critic.score: calibrated thresholds + advisory
# --------------------------------------------------------------------------- #


def test_calibrated_threshold_tightens_gate_via_decide_qa() -> None:
    # The strict director's calibration tightens every axis above/below the §9.5
    # floor. A clip that the §9.5 DEFAULT gate passes (CCS 0.86, motion 0.20) FAILS
    # the calibrated gate — proving the calibrated thresholds are honored and only
    # ever tighten (the floor is the loosest the gate can be).
    cal = _strict_calibration()
    th = cal.thresholds
    assert th.ccs_min > 0.85  # tightened CCS floor
    assert th.motion_artifact_max < 0.25  # tightened motion ceiling
    from app.agents.critic import QAThresholds

    calibrated = QAThresholds(
        ccs_min=th.ccs_min,
        style_drift_max=th.style_drift_max,
        motion_artifact_max=th.motion_artifact_max,
    )
    # Passes §9.5 default …
    v_default, _, _ = decide_qa(0.86, 0.07, True, 0.20)
    assert v_default is Verdict.PASS
    # … but fails the calibrated (tighter) gate.
    v_cal, action, _ = decide_qa(0.86, 0.07, True, 0.20, thresholds=calibrated)
    assert v_cal is Verdict.FAIL


async def test_score_attaches_advisory_when_calibrated(providers: Providers) -> None:  # noqa: F811
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]
    providers.vl.analyze_json = JsonSequencer(  # type: ignore[method-assign]
        {"timeline_ok": True, "motion_artifact": 0.01, "reason": "ok"}
    )
    cal = _strict_calibration()
    critic = Critic(providers).with_calibration(cal)
    # A clip matching the accepted cloud (CCS 1.0, no drift, tiny motion) passes the
    # tightened gate; the learned advisory is attached for the gate-passing clip.
    record = await critic.score(
        shot_id="shot_clean",
        clip_frames=[b"frame"],
        canon_slice=_empty_slice(),
        character_crop=b"crop",
        locked_ref_image=b"crop",  # CCS 1.0
    )
    assert record.verdict is Verdict.PASS
    assert record.learned_reward is not None


async def test_score_attaches_anomaly_on_passing_clip(providers: Providers) -> None:  # noqa: F811
    providers.embeddings.embed_images = OneHotEmbedder()  # type: ignore[method-assign]
    providers.vl.analyze_json = JsonSequencer(  # type: ignore[method-assign]
        {"timeline_ok": True, "motion_artifact": 0.0, "reason": "ok"}
    )
    # A calibration whose accepted cloud is tight at very-high CCS, low drift/motion.
    samples = [
        QASample(0.99, 0.01, True, 0.0, accepted=True) for _ in range(20)
    ] + [
        QASample(0.40, 0.40, True, 0.70, accepted=False) for _ in range(20)
    ]
    cal = calibrate_from_samples(samples)
    critic = Critic(providers).with_calibration(cal)
    # A clip matching the accepted cloud passes the (tightened) gate; the learned
    # reward + an anomaly score (vs the accepted cloud) are attached to the record.
    record = await critic.score(
        shot_id="shot_clean",
        clip_frames=[b"frame"],
        canon_slice=_empty_slice(),
        character_crop=b"x",
        locked_ref_image=b"x",  # CCS 1.0 → passes the tightened gate
    )
    assert record.verdict is Verdict.PASS
    assert record.learned_reward is not None
    assert record.anomaly_score is not None
