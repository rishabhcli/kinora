"""Unit tests for the §8.7 content-hash helper (no services required)."""

from __future__ import annotations

from typing import TypedDict

from app.db.hashing import compute_shot_hash


class _ShotHashArgs(TypedDict):
    """The exact keyword arguments :func:`compute_shot_hash` accepts."""

    book_id: str
    beat_id: str
    canon_version_at_render: int
    render_mode: str
    seed: int
    reference_set_hash: str


_BASE: _ShotHashArgs = {
    "book_id": "book_grimm_snow",
    "beat_id": "beat_0034",
    "canon_version_at_render": 7,
    "render_mode": "reference_to_video",
    "seed": 88123,
    "reference_set_hash": "sha1:af83",
}


def test_shot_hash_is_deterministic() -> None:
    first = compute_shot_hash(**_BASE)
    second = compute_shot_hash(**_BASE)
    assert first == second
    # SHA-1 hex digest.
    assert len(first) == 40
    assert all(c in "0123456789abcdef" for c in first)


def test_shot_hash_changes_with_each_input() -> None:
    base_hash = compute_shot_hash(**_BASE)
    # Changing ANY component must change the hash (e.g. a Director edit changes
    # reference_set_hash and only the dependent shots re-render).
    variants: list[_ShotHashArgs] = [
        {**_BASE, "reference_set_hash": "sha1:beef"},
        {**_BASE, "seed": 99999},
        {**_BASE, "canon_version_at_render": 8},
        {**_BASE, "render_mode": "first_last_frame"},
        {**_BASE, "beat_id": "beat_0035"},
        {**_BASE, "book_id": "book_other"},
    ]
    for variant in variants:
        assert compute_shot_hash(**variant) != base_hash


def test_shot_hash_has_no_boundary_collision() -> None:
    # The unit-separator keeps ("a", "bc") distinct from ("ab", "c").
    left = compute_shot_hash(
        book_id="a",
        beat_id="bc",
        canon_version_at_render=1,
        render_mode="m",
        seed=0,
        reference_set_hash="r",
    )
    right = compute_shot_hash(
        book_id="ab",
        beat_id="c",
        canon_version_at_render=1,
        render_mode="m",
        seed=0,
        reference_set_hash="r",
    )
    assert left != right
