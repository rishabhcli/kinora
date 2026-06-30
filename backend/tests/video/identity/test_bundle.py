"""IdentityBundle / LockedReference / vector helpers — pure, deterministic."""

from __future__ import annotations

import base64

import pytest

from app.video.identity import (
    IdentityBundle,
    LockedReference,
    Pose,
    centroid,
    cosine,
)

from .conftest import ELSA_APPEARANCE, PNG, make_bundle, unit


def test_locked_reference_requires_url_or_bytes() -> None:
    with pytest.raises(ValueError, match="neither url nor bytes"):
        LockedReference(ref_id="x")


def test_locked_reference_quality_bounds() -> None:
    with pytest.raises(ValueError, match="quality"):
        LockedReference(ref_id="x", url="u", quality=1.5)


def test_locked_reference_data_uri_and_base64() -> None:
    ref = LockedReference(ref_id="x", image_bytes=PNG, mime="image/png")
    b64 = ref.as_base64()
    assert base64.b64decode(b64) == PNG
    assert ref.as_data_uri() == f"data:image/png;base64,{b64}"


def test_locked_reference_base64_requires_bytes() -> None:
    ref = LockedReference(ref_id="x", url="https://oss/x.png")
    with pytest.raises(ValueError, match="no bytes"):
        ref.as_base64()


def test_transport_value_url_vs_inline() -> None:
    url_ref = LockedReference(ref_id="u", url="https://oss/u.png")
    byte_ref = LockedReference(ref_id="b", image_bytes=PNG)
    # URL transport (inline=False) returns the URL; inline returns None for url-only.
    assert url_ref.transport_value(inline=False, data_uri=False) == "https://oss/u.png"
    assert url_ref.transport_value(inline=True, data_uri=True) is None
    # Byte ref: URL transport returns None (no url), inline serves data-uri/base64.
    assert byte_ref.transport_value(inline=False, data_uri=False) is None
    inline_data = byte_ref.transport_value(inline=True, data_uri=True)
    assert inline_data is not None and inline_data.startswith("data:")
    assert byte_ref.transport_value(inline=True, data_uri=False) == byte_ref.as_base64()


def test_pose_coerce_aliases() -> None:
    assert Pose.coerce("three_quarter") is Pose.THREE_QUARTER
    assert Pose.coerce("34") is Pose.THREE_QUARTER
    assert Pose.coerce("side") is Pose.PROFILE
    assert Pose.coerce("wide") is Pose.ESTABLISHING
    assert Pose.coerce(None) is Pose.UNKNOWN
    assert Pose.coerce("nonsense") is Pose.UNKNOWN
    assert Pose.coerce("front") is Pose.FRONT


def test_cosine_tolerates_empty_and_mismatch() -> None:
    assert cosine((), (1.0,)) == 0.0
    assert cosine((1.0, 0.0), (1.0,)) == 0.0
    assert cosine((0.0, 0.0), (1.0, 0.0)) == 0.0
    assert cosine(ELSA_APPEARANCE, ELSA_APPEARANCE) == pytest.approx(1.0)


def test_centroid_mean_and_ragged() -> None:
    a = unit(1.0, 0.0)
    b = unit(0.0, 1.0)
    c = centroid([a, b])
    # mean of two orthogonal unit vectors, re-normalized → 45°.
    assert c[0] == pytest.approx(c[1])
    assert cosine(c, unit(1.0, 1.0)) == pytest.approx(1.0)
    # ragged → empty
    assert centroid([(1.0, 0.0), (1.0,)]) == ()
    assert centroid([]) == ()


def test_style_centroid_and_anchor() -> None:
    bundle = make_bundle()
    # anchor prefers the stored appearance embedding.
    assert bundle.anchor_descriptor == ELSA_APPEARANCE
    # centroid is the mean of the locked ref descriptors (close to appearance).
    assert cosine(bundle.style_centroid, ELSA_APPEARANCE) > 0.9


def test_anchor_falls_back_to_centroid_when_no_appearance_embedding() -> None:
    bundle = make_bundle(with_appearance_embedding=False)
    assert bundle.appearance_descriptor == ()
    assert bundle.anchor_descriptor == bundle.style_centroid
    assert bundle.anchor_descriptor != ()


def test_locked_references_excludes_unlocked() -> None:
    base = make_bundle()
    derived = LockedReference(ref_id="derived", image_bytes=PNG, locked=False)
    bundle = IdentityBundle(
        entity_key=base.entity_key,
        entity_type=base.entity_type,
        name=base.name,
        references=(*base.references, derived),
    )
    assert derived not in bundle.locked_references
    assert len(bundle.locked_references) == 3


def test_reference_set_hash_is_stable_and_order_independent() -> None:
    bundle = make_bundle()
    h1 = bundle.reference_set_hash()
    # Same locked ids in a different order → same hash (sorted internally).
    reordered = IdentityBundle(
        entity_key=bundle.entity_key,
        entity_type=bundle.entity_type,
        name=bundle.name,
        version=bundle.version,
        references=tuple(reversed(bundle.references)),
    )
    assert reordered.reference_set_hash() == h1
    assert h1.startswith("sha1:")


def test_has_inline_bytes_and_has_references() -> None:
    assert make_bundle(with_bytes=True).has_inline_bytes is True
    assert make_bundle(with_bytes=False).has_inline_bytes is False
    empty = IdentityBundle(entity_key="e", entity_type="character", name="E")
    assert empty.has_references is False
