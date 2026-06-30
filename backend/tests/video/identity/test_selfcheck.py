"""Identity-consistency self-check — drift scoring + verdict routing."""

from __future__ import annotations

import pytest

from app.video.identity import (
    DriftThresholds,
    DriftVerdict,
    IdentitySelfCheck,
    score_descriptor,
)

from .conftest import (
    ELSA_APPEARANCE,
    ELSA_PROFILE,
    ORTHOGONAL,
    FakeEmbedder,
    make_bundle,
    unit,
)


def test_thresholds_validation() -> None:
    with pytest.raises(ValueError):
        DriftThresholds(fail_below=0.9, warn_below=0.8)  # fail > warn


def test_perfect_match_is_ok_zero_drift() -> None:
    bundle = make_bundle()
    report = score_descriptor(bundle, ELSA_APPEARANCE)
    assert report.verdict is DriftVerdict.OK
    assert report.similarity == pytest.approx(1.0)
    assert report.drift == pytest.approx(0.0)
    assert report.measured is True
    assert report.passed is True


def test_orthogonal_crop_is_fail_max_drift() -> None:
    report = score_descriptor(make_bundle(), ORTHOGONAL)
    assert report.verdict is DriftVerdict.FAIL
    assert report.similarity == pytest.approx(0.0)
    assert report.drift == pytest.approx(1.0)
    assert report.passed is False


def test_off_angle_crop_matches_best_locked_ref() -> None:
    # A crop close to the PROFILE ref but far from the appearance anchor: the
    # best-of-refs logic should rescue it via the profile ref.
    bundle = make_bundle()
    report = score_descriptor(bundle, ELSA_PROFILE)
    assert report.best_ref_id == "char_elsa@v3:profile"
    assert report.similarity == pytest.approx(1.0)
    assert report.verdict is DriftVerdict.OK


def test_warn_band() -> None:
    bundle = make_bundle(with_descriptors=False)  # only the appearance anchor
    # Construct a crop ~0.80 cosine with the anchor (between fail .75 and warn .85).
    crop = unit(0.80, 0.60, 0.0)
    sim = crop[0]  # dot with (1,0,0)
    assert 0.75 <= sim < 0.85
    report = score_descriptor(bundle, crop)
    assert report.verdict is DriftVerdict.WARN
    assert report.passed is True


def test_unknown_when_no_anchor_or_refs() -> None:
    bundle = make_bundle(with_descriptors=False, with_appearance_embedding=False)
    report = score_descriptor(bundle, ELSA_APPEARANCE)
    assert report.verdict is DriftVerdict.UNKNOWN
    assert report.measured is False
    assert report.drift == 1.0


def test_unknown_when_empty_crop() -> None:
    report = score_descriptor(make_bundle(), ())
    assert report.verdict is DriftVerdict.UNKNOWN
    assert report.measured is False


def test_custom_thresholds_route_differently() -> None:
    bundle = make_bundle(with_descriptors=False)
    crop = unit(0.80, 0.60, 0.0)  # sim ~0.80
    strict = DriftThresholds(fail_below=0.85, warn_below=0.95)
    report = score_descriptor(bundle, crop, thresholds=strict)
    assert report.verdict is DriftVerdict.FAIL


async def test_selfcheck_embeds_and_scores() -> None:
    crop_bytes = b"\x89PNG-crop"
    embedder = FakeEmbedder({crop_bytes: ELSA_APPEARANCE})
    check = IdentitySelfCheck(embedder)
    report = await check.check(make_bundle(), crop_bytes)
    assert report.verdict is DriftVerdict.OK
    assert report.measured is True
    assert embedder.calls == [crop_bytes]


async def test_selfcheck_embed_failure_is_unknown() -> None:
    check = IdentitySelfCheck(FakeEmbedder(fail=True))
    report = await check.check(make_bundle(), b"crop")
    assert report.verdict is DriftVerdict.UNKNOWN
    assert report.measured is False


async def test_selfcheck_empty_embedding_is_unknown() -> None:
    crop = b"crop"
    check = IdentitySelfCheck(FakeEmbedder({crop: ()}))
    report = await check.check(make_bundle(), crop)
    assert report.verdict is DriftVerdict.UNKNOWN
