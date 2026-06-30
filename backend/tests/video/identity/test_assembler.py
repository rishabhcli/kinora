"""Assembler — project a canon CanonEntitySlice into an IdentityBundle (pure)."""

from __future__ import annotations

from app.memory.interfaces import CanonEntitySlice, RefImage
from app.video.identity import Pose, bundle_from_canon_slice

from .conftest import ELSA_FRONT, PNG


def _slice(**overrides: object) -> CanonEntitySlice:
    base: dict[str, object] = {
        "entity_key": "char_elsa_001",
        "type": "character",
        "name": "Elsa",
        "version": 3,
        "description": "young woman, platinum braid",
        "appearance": {
            "description": "platinum braid, ice-blue gown",
            "embedding": list(ELSA_FRONT),
            "negative_tokens": ["warped face", "extra fingers"],
        },
        "voice_ref_url": "https://oss/elsa/voice.wav",
        "reference_images": [
            RefImage(key="ref_front", url="https://oss/front.png", pose="front", locked=True),
            RefImage(key="ref_3q", url="https://oss/3q.png", pose="three_quarter", locked=True),
        ],
        "valid_from_beat": 1,
    }
    base.update(overrides)
    return CanonEntitySlice(**base)


def test_bundle_from_canon_slice_maps_core_fields() -> None:
    bundle = bundle_from_canon_slice(_slice())
    assert bundle.entity_key == "char_elsa_001"
    assert bundle.entity_type == "character"
    assert bundle.name == "Elsa"
    assert bundle.version == 3
    assert bundle.appearance_prompt == "platinum braid, ice-blue gown"
    assert bundle.appearance_descriptor == ELSA_FRONT
    assert bundle.negative_tokens == ("warped face", "extra fingers")
    assert bundle.voice_ref_url == "https://oss/elsa/voice.wav"
    assert len(bundle.references) == 2
    assert bundle.references[0].pose is Pose.FRONT
    assert bundle.references[1].pose is Pose.THREE_QUARTER
    # ref id derives from entity@version:key
    assert bundle.references[0].ref_id == "char_elsa_001@v3:ref_front"


def test_assembler_attaches_supplied_bytes_and_descriptors() -> None:
    bundle = bundle_from_canon_slice(
        _slice(),
        ref_bytes={"ref_front": PNG},
        ref_descriptors={"ref_front": ELSA_FRONT},
    )
    front = bundle.references[0]
    assert front.image_bytes == PNG
    assert front.descriptor == ELSA_FRONT
    assert bundle.has_inline_bytes is True


def test_assembler_tolerates_missing_appearance() -> None:
    bundle = bundle_from_canon_slice(_slice(appearance=None))
    assert bundle.appearance_descriptor == ()
    # falls back to the slice description for the phrase
    assert bundle.appearance_prompt == "young woman, platinum braid"
    assert bundle.negative_tokens == ()


def test_assembler_reads_character_id() -> None:
    bundle = bundle_from_canon_slice(
        _slice(appearance={"description": "x", "character_id": "ip_elsa"})
    )
    assert bundle.character_id == "ip_elsa"


def test_assembler_ignores_non_numeric_embedding() -> None:
    bundle = bundle_from_canon_slice(
        _slice(appearance={"description": "x", "embedding": ["a", "b"]})
    )
    assert bundle.appearance_descriptor == ()


def test_assembler_parses_string_negatives() -> None:
    bundle = bundle_from_canon_slice(
        _slice(appearance={"description": "x", "negatives": "blurry, warped face"})
    )
    assert bundle.negative_tokens == ("blurry", "warped face")
