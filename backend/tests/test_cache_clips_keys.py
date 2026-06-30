"""Content-addressed render-key stability + normalisation.

Verifies that the §8.7-complementary content key collides exactly when two render
requests are *semantically* identical (and never otherwise): case/whitespace/
Unicode-normalised prompts, order-independent reference sets, quantised durations,
case-folded provider/model, and book/beat identity deliberately excluded so the
key reuses across books. No infra, fully deterministic.
"""

from __future__ import annotations

from app.cache.clips.keys import (
    RENDER_KEY_SCHEMA,
    RenderInputs,
    RenderKey,
    normalize_camera,
    normalize_text,
    quantize_duration,
    reference_identity_digest,
    render_key,
)


def test_key_is_deterministic() -> None:
    a = RenderInputs(prompt="a boat at dawn", seed=4, reference_image_ids=["x", "y"])
    assert a.key().value == a.key().value
    assert render_key(a).value == a.key().value


def test_key_value_and_short_form() -> None:
    k = RenderInputs(prompt="x").key()
    assert k.value.startswith(f"{RENDER_KEY_SCHEMA}:")
    assert k.value == str(k)
    assert k.short() == k.digest[:16]
    assert len(k.digest) == 64  # full sha-256 hex


def test_prompt_normalisation_collides() -> None:
    # Case, doubled/odd whitespace, and surrounding space all normalise away.
    base = RenderInputs(prompt="Hello   World", seed=1)
    variants = [
        RenderInputs(prompt="hello world", seed=1),
        RenderInputs(prompt="  HELLO\tWORLD  ", seed=1),
        RenderInputs(prompt="hello\nworld", seed=1),
    ]
    for v in variants:
        assert v.key().value == base.key().value


def test_unicode_nfc_normalisation_collides() -> None:
    # Composed vs decomposed "é" normalise to the same NFC form.
    composed = RenderInputs(prompt="café", seed=1)
    decomposed = RenderInputs(prompt="café", seed=1)
    assert composed.key().value == decomposed.key().value


def test_distinct_prompts_do_not_collide() -> None:
    a = RenderInputs(prompt="a boat", seed=1)
    b = RenderInputs(prompt="a train", seed=1)
    assert a.key().value != b.key().value


def test_seed_participates() -> None:
    a = RenderInputs(prompt="x", seed=1)
    b = RenderInputs(prompt="x", seed=2)
    assert a.key().value != b.key().value


def test_render_mode_participates_and_casefolds() -> None:
    a = RenderInputs(prompt="x", render_mode="image_to_video")
    b = RenderInputs(prompt="x", render_mode="IMAGE_TO_VIDEO")
    c = RenderInputs(prompt="x", render_mode="text_to_video")
    assert a.key().value == b.key().value
    assert a.key().value != c.key().value


def test_reference_set_is_order_independent_and_dedups() -> None:
    a = RenderInputs(prompt="x", reference_image_ids=["b", "a", "a"])
    b = RenderInputs(prompt="x", reference_image_ids=["a", "b"])
    c = RenderInputs(prompt="x", reference_image_ids=[" a ", "b"])  # whitespace trimmed
    assert a.key().value == b.key().value == c.key().value


def test_reference_set_changes_key() -> None:
    a = RenderInputs(prompt="x", reference_image_ids=["a", "b"])
    b = RenderInputs(prompt="x", reference_image_ids=["a", "c"])
    empty = RenderInputs(prompt="x", reference_image_ids=[])
    assert a.key().value != b.key().value
    assert a.key().value != empty.key().value


def test_reference_identity_digest_stable_and_prefixed() -> None:
    d1 = reference_identity_digest(["e2", "e1"])
    d2 = reference_identity_digest(["e1", "e2", "e1"])
    assert d1 == d2
    assert d1.startswith("ref:")
    # Empty set is distinct and stable.
    assert reference_identity_digest([]).startswith("ref:")
    assert reference_identity_digest([]) != d1


def test_camera_normalisation_drops_unknown_keys_and_defaults() -> None:
    # Missing keys default; unknown extra keys are ignored.
    a = RenderInputs(prompt="x", camera=None)
    b = RenderInputs(prompt="x", camera={})
    c = RenderInputs(
        prompt="x", camera={"move": "static", "speed": "medium", "shot_size": "medium"}
    )
    d = RenderInputs(prompt="x", camera={"move": "static", "lens": "50mm", "iso": 800})
    assert a.key().value == b.key().value == c.key().value == d.key().value


def test_camera_meaningful_change_splits_key() -> None:
    a = RenderInputs(prompt="x", camera={"move": "pan"})
    b = RenderInputs(prompt="x", camera={"move": "static"})
    assert a.key().value != b.key().value


def test_normalize_camera_casefolds_and_trims() -> None:
    assert normalize_camera({"move": " PAN ", "speed": "Fast"}) == ("pan", "fast", "medium")
    assert normalize_camera(None) == ("static", "medium", "medium")


def test_duration_quantisation_collides_within_grid() -> None:
    a = RenderInputs(prompt="x", duration_s=5.0)
    b = RenderInputs(prompt="x", duration_s=5.0001)
    c = RenderInputs(prompt="x", duration_s=5.24)  # snaps to 5.0 on a 0.5 grid
    far = RenderInputs(prompt="x", duration_s=6.0)
    assert a.key().value == b.key().value == c.key().value
    assert a.key().value != far.key().value


def test_quantize_duration_grid() -> None:
    assert quantize_duration(5.0) == 5.0
    assert quantize_duration(5.24) == 5.0
    assert quantize_duration(5.3) == 5.5  # nearest grid point, unambiguously up
    assert quantize_duration(0.0) == 0.0
    assert quantize_duration(-3.0) == 0.0


def test_provider_and_model_participate_casefolded() -> None:
    a = RenderInputs(prompt="x", provider="DashScope", model="Wan2.1-I2V-Turbo")
    b = RenderInputs(prompt="x", provider="dashscope", model="wan2.1-i2v-turbo")
    other = RenderInputs(prompt="x", provider="minimax", model="MiniMax-Hailuo-2.3-Fast")
    assert a.key().value == b.key().value
    assert a.key().value != other.key().value


def test_negative_prompt_participates() -> None:
    a = RenderInputs(prompt="x", negative_prompt="blurry")
    b = RenderInputs(prompt="x", negative_prompt="BLURRY")  # casefolds
    c = RenderInputs(prompt="x", negative_prompt=None)
    assert a.key().value == b.key().value
    assert a.key().value != c.key().value


def test_normalize_text_edge_cases() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""
    assert normalize_text("  A  B  ") == "a b"


def test_render_key_equality_and_hash() -> None:
    a = RenderInputs(prompt="x", seed=9).key()
    b = RenderInputs(prompt="x", seed=9).key()
    assert a == b
    assert hash(a) == hash(b)
    assert a in {b}


def test_from_spec_duck_types_a_shot_spec() -> None:
    from app.memory.interfaces import ShotSpec

    spec = ShotSpec(
        book_id="book-1",
        beat_id="beat-7",
        render_mode="image_to_video",
        prompt="A lighthouse",
        seed=11,
        reference_image_ids=["e1", "e2"],
        camera={"move": "pan", "extra": "ignored"},
        target_duration_s=5.0,
    )
    inputs = RenderInputs.from_spec(spec, provider="dashscope", model="wan2.1-i2v-turbo")
    assert inputs.prompt == "A lighthouse"
    assert inputs.render_mode == "image_to_video"
    assert inputs.seed == 11
    assert inputs.duration_s == 5.0
    # Key is stable and book/beat identity did NOT leak into it (cross-book reuse).
    other_book = ShotSpec(
        book_id="book-99",
        beat_id="beat-3",
        render_mode="image_to_video",
        prompt="A lighthouse",
        seed=11,
        reference_image_ids=["e2", "e1"],  # different order, same set
        camera={"move": "pan"},
        target_duration_s=5.0,
    )
    other_inputs = RenderInputs.from_spec(
        other_book, provider="dashscope", model="wan2.1-i2v-turbo"
    )
    assert inputs.key().value == other_inputs.key().value


def test_from_spec_handles_strenum_render_mode() -> None:
    from app.agents.contracts import Camera, RenderMode
    from app.agents.contracts import ShotSpec as AgentShotSpec

    spec = AgentShotSpec(
        shot_id="shot-1",
        render_mode=RenderMode.FIRST_LAST_FRAME,
        prompt="x",
        seed=2,
        camera=Camera(move="dolly"),
    )
    inputs = RenderInputs.from_spec(spec, provider="dashscope")
    assert inputs.render_mode == "first_last_frame"
    assert inputs.camera == {"move": "dolly", "speed": "medium", "shot_size": "medium"}


def test_canonical_is_exposed_for_diagnostics() -> None:
    inputs = RenderInputs(prompt="A  Cat", seed=1)
    canon = inputs.canonical()
    assert canon["prompt"] == "a cat"
    assert canon["schema"] == RENDER_KEY_SCHEMA
    assert canon["ref_digest"].startswith("ref:")


def test_render_key_round_trips_through_json() -> None:
    k = RenderInputs(prompt="x").key()
    again = RenderKey.model_validate_json(k.model_dump_json())
    assert again == k
