"""Per-character identity verification at scale (§9.5/§13) — weakest-face gate.

Uses the network-free one-hot embedder from ``test_agents_support`` so a "crop" that
shares bytes with its reference embeds identically (CCS 1.0) and a different crop
embeds orthogonally (CCS 0.0) — deterministic, no DashScope.
"""

from __future__ import annotations

from app.render.qa.identity import CharacterCrops, verify_identities
from tests.test_agents_support import OneHotEmbedder


class _Embedder:
    """Wrap the one-hot stand-in into the ``embed_images`` attribute the API needs."""

    def __init__(self) -> None:
        self.embed_images = OneHotEmbedder()


async def test_single_character_matches() -> None:
    report = await verify_identities(
        [CharacterCrops("hero", ref_images=[b"face"], crops=[b"face"])],
        embedder=_Embedder(),
    )
    assert report.ok is True
    assert report.aggregate_ccs == 1.0
    assert report.weakest_character == "hero"
    assert report.ccs_map() == {"hero": 1.0}


async def test_weakest_face_gates_the_shot() -> None:
    # Hero matches (CCS 1.0); the villain crop is different bytes (CCS 0.0) → gate.
    report = await verify_identities(
        [
            CharacterCrops("hero", ref_images=[b"hero"], crops=[b"hero"]),
            CharacterCrops("villain", ref_images=[b"villain-ref"], crops=[b"wrong-face"]),
        ],
        embedder=_Embedder(),
    )
    assert report.ok is False  # one wrong face fails the whole shot
    assert report.aggregate_ccs == 0.0
    assert report.weakest_character == "villain"
    # both present characters appear in the per-character map
    assert set(report.ccs_map()) == {"hero", "villain"}


async def test_absent_character_does_not_gate() -> None:
    # A character with no crop is simply off-screen; it must not gate the shot.
    report = await verify_identities(
        [
            CharacterCrops("hero", ref_images=[b"hero"], crops=[b"hero"]),
            CharacterCrops("ghost", ref_images=[b"ghost"], crops=[]),
        ],
        embedder=_Embedder(),
    )
    assert report.ok is True
    assert report.aggregate_ccs == 1.0
    assert "ghost" not in report.ccs_map()  # absent → not in the present-CCS map


async def test_best_crop_wins_over_a_bad_frame() -> None:
    # The character is verified by ANY clean crop even if one crop is wrong.
    report = await verify_identities(
        [CharacterCrops("hero", ref_images=[b"hero"], crops=[b"wrong", b"hero"])],
        embedder=_Embedder(),
    )
    assert report.aggregate_ccs == 1.0  # the matching crop carries the verification
    assert report.ok is True


async def test_present_but_no_reference_is_na() -> None:
    report = await verify_identities(
        [CharacterCrops("extra", ref_images=[], crops=[b"someone"])],
        embedder=_Embedder(),
    )
    # Present but unlockable → N/A (CCS 1.0), like the single-crop "no ref" branch.
    assert report.aggregate_ccs == 1.0
    assert report.ok is True


async def test_no_embedder_is_na() -> None:
    report = await verify_identities(
        [CharacterCrops("hero", ref_images=[b"hero"], crops=[b"hero"])],
        embedder=object(),  # no embed_images attribute
    )
    assert report.aggregate_ccs == 1.0
    assert report.ok is True


async def test_empty_characters() -> None:
    report = await verify_identities([], embedder=_Embedder())
    assert report.ok is True
    assert report.aggregate_ccs == 1.0
    assert report.per_character == []
