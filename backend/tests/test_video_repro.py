"""Deterministic unit tests for :mod:`app.video.repro` (NO infra, NO network).

Covers the four contractual guarantees of the reproducibility subsystem:

* fingerprint **stability** (same inputs → same id) + **sensitivity** (each keyed
  field, when changed, moves the id);
* seed-tree **determinism** (re-derivation is stable) + **no-collision** (siblings
  spread, the tree axes don't alias) + **bounds** (31-bit non-negative);
* replay **reconstruction** (request re-built losslessly, URL re-resolution) and
  its reproducibility verdict;
* diff **attribution** (the right field is blamed, with the right causality, and
  non-causal noise like a changed task id is never blamed).

Plus the determinism classifier and the canonical hashing primitives that
underpin all of the above.
"""

from __future__ import annotations

import pytest

from app.providers.types import WanMode, WanSpec
from app.video.repro import (
    ByteStability,
    Causality,
    Concession,
    DeterminismClassifier,
    DeterminismProfile,
    PostOp,
    RenderFingerprint,
    RenderReplay,
    ReproLabel,
    ResolvedProvider,
    SeedHonoring,
    SeedLineage,
    canonical_text,
    diff_fingerprints,
    digest,
    root_seed,
)
from app.video.repro.hashing import join_digest
from app.video.repro.seedtree import SEED_MASK

# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def _provider() -> ResolvedProvider:
    return ResolvedProvider(
        provider="dashscope",
        model="wan2.1-i2v-turbo",
        version="2.1-turbo",
        protocol="legacy",
    )


def _spec(**overrides: object) -> WanSpec:
    base: dict[str, object] = {
        "mode": WanMode.REFERENCE_TO_VIDEO,
        "prompt": "Elsa stands at the frost-laced window",
        "negative_prompt": "blurry, extra limbs",
        "seed": 88123,
        "duration_s": 5,
        "resolution": "720P",
        "reference_image_urls": [
            "https://oss/char_elsa_001/ref_front.png?sig=AAA",
            "https://oss/loc_window/ref.png?sig=BBB",
        ],
        "shot_id": "shot_00042",
    }
    base.update(overrides)
    return WanSpec(**base)


def _fingerprint(**spec_overrides: object) -> RenderFingerprint:
    return RenderFingerprint.from_spec(
        _spec(**spec_overrides),
        provider=_provider(),
        book_id="book_42",
        scene_id="scene_005",
        beat_id="beat_0034",
        canon_version_at_render=7,
        reference_image_ids=["char_elsa_001@v3", "loc_window@v1"],
    )


# --------------------------------------------------------------------------- #
# Canonical hashing primitives
# --------------------------------------------------------------------------- #


def test_canonical_text_is_key_order_insensitive() -> None:
    assert canonical_text({"a": 1, "b": 2}) == canonical_text({"b": 2, "a": 1})


def test_canonical_text_is_type_faithful() -> None:
    # int 1, str "1", float 1.0, bool True must all be distinct.
    seen = {
        canonical_text(1),
        canonical_text("1"),
        canonical_text(1.0),
        canonical_text(True),
    }
    assert len(seen) == 4


def test_digest_set_is_order_insensitive_but_list_is_not() -> None:
    assert digest({frozenset({"a", "b"})}) == digest({frozenset({"b", "a"})})
    assert digest(["a", "b"]) != digest(["b", "a"])


def test_digest_is_boundary_unambiguous() -> None:
    # "ab"+"c" must not collide with "a"+"bc".
    assert join_digest("ab", "c") != join_digest("a", "bc")


def test_digest_is_deterministic_across_calls() -> None:
    payload = {"prompt": "x", "refs": ["a@v1", "b@v2"], "seed": 7}
    assert digest(payload) == digest(dict(payload))


def test_digest_distinguishes_tuple_from_list() -> None:
    assert digest((1, 2)) != digest([1, 2])


# --------------------------------------------------------------------------- #
# Seed lineage: determinism, no-collision, bounds
# --------------------------------------------------------------------------- #


def test_root_seed_is_deterministic_and_salted() -> None:
    assert root_seed("book_42") == root_seed("book_42")
    assert root_seed("book_42") != root_seed("book_43")
    assert root_seed("book_42", salt="alt") != root_seed("book_42")


def test_shot_seed_is_stable_across_lineage_instances() -> None:
    a = SeedLineage.for_book("book_42").shot_seed("scene_005", "shot_00042")
    b = SeedLineage.for_book("book_42").shot_seed("scene_005", "shot_00042")
    assert a == b


def test_shot_seed_within_bounds() -> None:
    lin = SeedLineage.for_book("book_42")
    for i in range(200):
        s = lin.shot_seed("scene_005", f"shot_{i:05d}")
        assert 0 <= s <= SEED_MASK


def test_sibling_shots_do_not_collide() -> None:
    lin = SeedLineage.for_book("book_42")
    seeds = lin.scene_shot_seeds("scene_005", [f"shot_{i:05d}" for i in range(500)])
    # 500 deterministic siblings should be spread with no collisions.
    assert len(set(seeds.values())) == 500


def test_seed_tree_axes_do_not_alias() -> None:
    lin = SeedLineage.for_book("book_42")
    scene = lin.scene_node("scene_005").seed
    shot = lin.shot_seed("scene_005", "shot_00042")
    attempt1 = lin.shot_seed("scene_005", "shot_00042", attempt=1)
    custom = lin.custom_node("scene_005", "shot_00042").seed
    # scene vs shot vs repair-attempt vs custom are distinct nodes.
    assert len({scene, shot, attempt1, custom}) == 4


def test_repair_attempts_are_reproducible_and_distinct() -> None:
    lin = SeedLineage.for_book("book_42")
    a0 = lin.shot_seed("scene_005", "shot_00042", attempt=0)
    a2_first = lin.shot_seed("scene_005", "shot_00042", attempt=2)
    a2_again = SeedLineage.for_book("book_42").shot_seed(
        "scene_005", "shot_00042", attempt=2
    )
    assert a2_first == a2_again  # reproducible
    assert a0 != a2_first  # distinct attempt


def test_negative_attempt_rejected() -> None:
    with pytest.raises(ValueError):
        SeedLineage.for_book("book_42").shot_seed("s", "shot", attempt=-1)


def test_cross_book_shot_seeds_differ() -> None:
    a = SeedLineage.for_book("book_42").shot_seed("scene_005", "shot_00042")
    b = SeedLineage.for_book("book_99").shot_seed("scene_005", "shot_00042")
    assert a != b


def test_seed_node_path_reconstructs_seed() -> None:
    from app.video.repro.seedtree import _derive  # implementation detail under test

    node = SeedLineage.for_book("book_42").shot_node("scene_005", "shot_00042")
    assert _derive(*node.path) == node.seed
    assert node.label == "/".join(node.path)


# --------------------------------------------------------------------------- #
# Fingerprint: stability + sensitivity
# --------------------------------------------------------------------------- #


def test_fingerprint_id_is_stable() -> None:
    assert _fingerprint().fingerprint_id == _fingerprint().fingerprint_id


def test_fingerprint_emits_canonical_shot_hash_matching_db_hashing() -> None:
    from app.db.hashing import compute_shot_hash

    fp = _fingerprint()
    expected = compute_shot_hash(
        book_id="book_42",
        beat_id="beat_0034",
        canon_version_at_render=7,
        render_mode="reference_to_video",
        seed=88123,
        reference_set_hash=fp.reference_identity_digest,
    )
    assert fp.shot_hash() == expected


def test_shot_hash_requires_beat() -> None:
    fp = RenderFingerprint.from_spec(
        _spec(), provider=_provider(), book_id="book_42"
    )
    with pytest.raises(ValueError):
        fp.shot_hash()


@pytest.mark.parametrize(
    "overrides",
    [
        {"seed": 99999},
        {"prompt": "A wholly different prompt"},
        {"negative_prompt": "different negatives"},
        {"duration_s": 8},
        {"resolution": "1080P"},
        {"watermark": True},
        {"prompt_extend": True},
        {"mode": WanMode.FIRST_LAST_FRAME},
    ],
)
def test_fingerprint_request_digest_sensitive_to_keyed_fields(
    overrides: dict[str, object],
) -> None:
    base = _fingerprint()
    changed = _fingerprint(**overrides)
    assert changed.request_digest != base.request_digest
    assert changed.fingerprint_id != base.fingerprint_id


def test_reference_identity_change_moves_digest() -> None:
    base = _fingerprint()
    other = RenderFingerprint.from_spec(
        _spec(),
        provider=_provider(),
        book_id="book_42",
        scene_id="scene_005",
        beat_id="beat_0034",
        canon_version_at_render=7,
        reference_image_ids=["char_elsa_001@v4", "loc_window@v1"],  # v3 -> v4
    )
    assert other.reference_identity_digest != base.reference_identity_digest
    assert other.request_digest != base.request_digest


def test_rotating_signed_urls_do_not_change_keyed_identity() -> None:
    # Same stable ids, different signed URLs → identical request digest.
    base = _fingerprint()
    rotated = RenderFingerprint.from_spec(
        _spec(
            reference_image_urls=[
                "https://oss/char_elsa_001/ref_front.png?sig=ZZZ",  # rotated sig
                "https://oss/loc_window/ref.png?sig=YYY",
            ]
        ),
        provider=_provider(),
        book_id="book_42",
        scene_id="scene_005",
        beat_id="beat_0034",
        canon_version_at_render=7,
        reference_image_ids=["char_elsa_001@v3", "loc_window@v1"],
    )
    assert rotated.request_digest == base.request_digest
    assert rotated.fingerprint_id == base.fingerprint_id


def test_model_version_change_moves_request_digest() -> None:
    base = _fingerprint()
    bumped = base.evolve(
        provider=ResolvedProvider(
            provider="dashscope",
            model="wan2.1-i2v-turbo",
            version="2.2-plus",  # version bump
            protocol="legacy",
        )
    )
    assert bumped.request_digest != base.request_digest


def test_canon_version_changes_provenance_id_not_request() -> None:
    base = _fingerprint()
    other = base.evolve(canon_version_at_render=8)
    # The provider never sees the canon version → request digest unchanged.
    assert other.request_digest == base.request_digest
    # But the provenance identity differs.
    assert other.fingerprint_id != base.fingerprint_id


def test_concessions_and_post_ops_change_provenance_id_not_request() -> None:
    base = _fingerprint()
    degraded = base.evolve(
        concessions=(Concession(kind="budget_degrade", detail="ken_burns"),),
        post_ops=(PostOp(name="ken_burns", params={"zoom": 1.1}),),
    )
    assert degraded.request_digest == base.request_digest
    assert degraded.fingerprint_id != base.fingerprint_id


def test_post_op_order_is_significant() -> None:
    a = _fingerprint().evolve(
        post_ops=(PostOp(name="color_match"), PostOp(name="caption"))
    )
    b = _fingerprint().evolve(
        post_ops=(PostOp(name="caption"), PostOp(name="color_match"))
    )
    assert a.fingerprint_id != b.fingerprint_id


def test_manifest_roundtrip_is_self_describing() -> None:
    fp = _fingerprint()
    manifest = fp.as_manifest()
    assert manifest["fingerprint_id"] == fp.fingerprint_id
    assert manifest["request_digest"] == fp.request_digest
    # Reconstruct the model from the JSON manifest and confirm the id is stable.
    rebuilt = RenderFingerprint.model_validate(manifest)
    assert rebuilt.fingerprint_id == fp.fingerprint_id


# --------------------------------------------------------------------------- #
# Determinism classifier
# --------------------------------------------------------------------------- #


def test_classifier_labels_hosted_wan_best_effort() -> None:
    c = DeterminismClassifier().classify(
        provider="dashscope", model="wan2.1-i2v-turbo"
    )
    assert c.label is ReproLabel.BEST_EFFORT
    assert c.reproducible_plan() and not c.reproducible_bytes()


def test_classifier_labels_local_kenburns_guaranteed() -> None:
    c = DeterminismClassifier().classify(provider="local", model="kenburns")
    assert c.label is ReproLabel.GUARANTEED
    assert c.reproducible_bytes()


def test_classifier_unknown_model_defaults_to_best_effort() -> None:
    c = DeterminismClassifier().classify(provider="who", model="mystery-1")
    assert c.label is ReproLabel.BEST_EFFORT
    assert c.is_default


def test_classifier_longest_prefix_wins() -> None:
    sharp = DeterminismProfile(
        provider="dashscope",
        model_prefix="wan2.7-",
        seed_honoring=SeedHonoring.DETERMINISTIC,
        byte_stability=ByteStability.BYTE_STABLE,
    )
    c = DeterminismClassifier().with_profiles((sharp,)).classify(
        provider="dashscope", model="wan2.7-i2v"
    )
    assert c.label is ReproLabel.GUARANTEED
    # A non-2.7 wan id still falls to the family default.
    c2 = DeterminismClassifier().with_profiles((sharp,)).classify(
        provider="dashscope", model="wan2.1-i2v-turbo"
    )
    assert c2.label is ReproLabel.BEST_EFFORT


def test_label_none_only_when_seed_ignored_and_nondeterministic() -> None:
    prof = DeterminismProfile(
        provider="x",
        seed_honoring=SeedHonoring.IGNORED,
        byte_stability=ByteStability.NON_DETERMINISTIC,
    )
    assert prof.label() is ReproLabel.NONE


# --------------------------------------------------------------------------- #
# Replay reconstruction
# --------------------------------------------------------------------------- #


class _StubResolver:
    """Re-resolves stable ids to *fresh* signed URLs deterministically."""

    def resolve_reference_url(self, reference_id: str) -> str:
        return f"https://oss/{reference_id}?sig=FRESH"


def test_replay_reconstructs_faithful_request_with_resolver() -> None:
    fp = _fingerprint()
    plan = RenderReplay(resolver=_StubResolver()).reconstruct(fp)
    assert plan.faithful
    # Re-resolving stable ids preserves the keyed request digest.
    assert plan.reconstructed_request_digest == fp.request_digest
    # The reconstructed spec carries the recorded prompt/seed/mode verbatim.
    assert plan.spec.prompt == fp.prompt
    assert plan.spec.seed == fp.seed
    assert plan.spec.mode is WanMode.REFERENCE_TO_VIDEO
    # Fresh URLs were substituted for the expired ones.
    assert all("sig=FRESH" in u for u in plan.spec.reference_image_urls)


def test_replay_reconstructs_a_recomputable_fingerprint() -> None:
    fp = _fingerprint()
    plan = RenderReplay(resolver=_StubResolver()).reconstruct(fp)
    # Rebuild a fingerprint from the reconstructed spec + same stable ids and
    # confirm the request digest round-trips (the whole point of "replay").
    rebuilt = RenderFingerprint.from_spec(
        plan.spec,
        provider=fp.provider,
        book_id=fp.book_id,
        scene_id=fp.scene_id,
        beat_id=fp.beat_id,
        canon_version_at_render=fp.canon_version_at_render,
        reference_image_ids=list(fp.reference_image_ids),
    )
    assert rebuilt.request_digest == fp.request_digest
    assert rebuilt.fingerprint_id == fp.fingerprint_id


def test_replay_verdict_best_effort_for_hosted_wan() -> None:
    plan = RenderReplay(resolver=_StubResolver()).reconstruct(_fingerprint())
    assert plan.label is ReproLabel.BEST_EFFORT
    assert plan.will_reproduce_plan()
    assert not plan.will_reproduce_bytes()


def test_replay_verdict_guaranteed_for_byte_stable_model() -> None:
    fp = RenderFingerprint.from_spec(
        _spec(mode=WanMode.IMAGE_TO_VIDEO),
        provider=ResolvedProvider(provider="local", model="kenburns"),
        book_id="book_42",
        beat_id="beat_0034",
        reference_image_ids=["char_elsa_001@v3"],
    )
    plan = RenderReplay(resolver=_StubResolver()).reconstruct(fp)
    assert plan.label is ReproLabel.GUARANTEED
    assert plan.will_reproduce_bytes()


def test_replay_without_resolver_uses_recorded_urls() -> None:
    fp = _fingerprint()
    plan = RenderReplay().reconstruct(fp)
    assert plan.faithful
    assert list(plan.spec.reference_image_urls) == list(fp.reference_image_urls)


def test_replay_unresolvable_reference_is_unfaithful() -> None:
    class _Failing:
        def resolve_reference_url(self, reference_id: str) -> str:
            return reference_id  # cannot resolve → echoes the id back

    fp = _fingerprint()
    plan = RenderReplay(resolver=_Failing()).reconstruct(fp)
    # faithful=False is the load-bearing signal: a reference identity could not
    # be re-resolved to a fresh URL, so the request is only approximate.
    assert not plan.faithful
    assert any("could not be re-resolved" in n for n in plan.notes)
    assert not plan.will_reproduce_bytes()


def test_replay_first_last_frame_routes_two_urls() -> None:
    fp = RenderFingerprint.from_spec(
        _spec(
            mode=WanMode.FIRST_LAST_FRAME,
            reference_image_urls=["https://a?sig=1", "https://b?sig=2"],
        ),
        provider=_provider(),
        book_id="book_42",
        beat_id="beat_0034",
        reference_image_ids=["kf_start@v1", "kf_end@v1"],
    )
    plan = RenderReplay(resolver=_StubResolver()).reconstruct(fp)
    assert plan.spec.first_frame_url == "https://oss/kf_start@v1?sig=FRESH"
    assert plan.spec.last_frame_url == "https://oss/kf_end@v1?sig=FRESH"


# --------------------------------------------------------------------------- #
# Diff attribution
# --------------------------------------------------------------------------- #


def test_diff_identical_fingerprints() -> None:
    d = diff_fingerprints(_fingerprint(), _fingerprint())
    assert d.identical
    assert d.same_request
    assert d.changes == ()
    assert "identical" in d.summary()


def test_diff_blames_seed_change() -> None:
    d = diff_fingerprints(_fingerprint(), _fingerprint(seed=42))
    assert not d.identical and not d.same_request
    assert d.primary_cause is not None
    assert d.primary_cause.field == "seed"
    assert d.primary_cause.causality is Causality.REQUEST


def test_diff_blames_model_over_lower_impact_changes() -> None:
    base = _fingerprint()
    # Change BOTH a model field and a concession; model must win as primary.
    other = base.evolve(
        provider=ResolvedProvider(
            provider="minimax", model="MiniMax-Hailuo-2.3-Fast"
        ),
        concessions=(Concession(kind="budget_degrade"),),
    )
    d = diff_fingerprints(base, other)
    assert d.primary_cause is not None
    assert d.primary_cause.causality is Causality.MODEL


def test_diff_reference_identity_explains_canon_edit() -> None:
    base = _fingerprint()
    edited = base.evolve(reference_identity_digest=digest({"refs": ["char_elsa_001@v4"]}))
    d = diff_fingerprints(base, edited)
    assert d.primary_cause is not None
    assert d.primary_cause.field == "reference_identity_digest"
    assert "canon edit" in d.primary_cause.explanation


def test_diff_task_id_is_never_causal() -> None:
    base = _fingerprint()
    rerun = base.evolve(provider_task_id="new-task-xyz")
    d = diff_fingerprints(base, rerun)
    # A changed task id is non-keyed, so the provenance *identity* is unchanged
    # and the provider request is unchanged...
    assert d.identical
    assert d.same_request
    # ...but the diff still *surfaces* the task-id change for the audit trail,
    # strictly as NON_CAUSAL so it can never be blamed for a visual difference.
    task_changes = [c for c in d.changes if c.field == "provider_task_id"]
    assert len(task_changes) == 1
    assert task_changes[0].causality is Causality.NON_CAUSAL
    assert all(c.causality is Causality.NON_CAUSAL for c in d.changes)


def test_diff_same_request_when_only_provenance_changes() -> None:
    base = _fingerprint()
    degraded = base.evolve(
        canon_version_at_render=9,
        concessions=(Concession(kind="budget_degrade", detail="ken_burns"),),
    )
    d = diff_fingerprints(base, degraded)
    assert not d.identical
    assert d.same_request  # provider request unchanged
    # The headline notes that the difference is provenance/context.
    assert "provider request unchanged" in d.summary()
    assert d.primary_cause is not None
    assert d.primary_cause.causality <= Causality.CONTEXTUAL


def test_diff_changes_are_sorted_by_impact() -> None:
    base = _fingerprint()
    other = base.evolve(
        seed=base.seed + 1,
        canon_version_at_render=base.canon_version_at_render + 1,
    )
    d = diff_fingerprints(base, other)
    causalities = [int(c.causality) for c in d.changes]
    assert causalities == sorted(causalities, reverse=True)
    assert d.primary_cause is not None
    assert d.primary_cause.field == "seed"  # REQUEST beats CONTEXTUAL
