"""Exhaustive tests for the canon-edit aggregate (§5.4 canon editor, §8): the
monotonic canon version, the lost-update guard, dependent-shot capture, and the
register/edit/swap/evolve decisions. Pure."""

from __future__ import annotations

import pytest

from app.db.models.enums import EntityType
from app.eventsourcing.domain.canon import (
    CanonEntityAggregate,
    CanonEntityRegistered,
    CanonEvolvedFromConflict,
    CanonFieldEdited,
    CanonReferenceImageSwapped,
)
from app.eventsourcing.domain.errors import CommandRejected, InvariantViolation, ValidationError


def _registered() -> CanonEntityAggregate:
    agg = CanonEntityAggregate("e1")
    agg.register(book_id="b1", entity_type=EntityType.CHARACTER, name="Ada")
    agg.mark_committed()
    return agg


def test_register_starts_at_version_one() -> None:
    agg = CanonEntityAggregate("e1")
    agg.register(book_id="b1", entity_type=EntityType.CHARACTER, name="Ada")
    (event,) = agg.uncommitted
    assert isinstance(event, CanonEntityRegistered)
    assert agg.registered is True
    assert agg.canon_version == 1
    assert agg.name == "Ada"


def test_cannot_register_twice() -> None:
    agg = _registered()
    with pytest.raises(CommandRejected, match="already registered"):
        agg.register(book_id="b1", entity_type=EntityType.CHARACTER, name="x")


def test_register_requires_name() -> None:
    with pytest.raises(ValidationError):
        CanonEntityAggregate("e1").register(
            book_id="b1", entity_type=EntityType.CHARACTER, name="  "
        )


def test_edit_field_bumps_version_and_captures_dependents() -> None:
    agg = _registered()
    emitted = agg.edit_field(
        field_name="coat_color",
        new_value="red",
        dependent_shot_ids=["shot1", "shot2"],
    )
    assert emitted is True
    (event,) = agg.uncommitted
    assert isinstance(event, CanonFieldEdited)
    assert event.old_value == ""
    assert event.new_value == "red"
    assert event.canon_version == 2
    assert event.dependent_shot_ids == ("shot1", "shot2")
    assert agg.canon_version == 2
    assert agg.fields["coat_color"] == "red"


def test_edit_field_dedupes_unchanged() -> None:
    agg = _registered()
    agg.edit_field(field_name="coat_color", new_value="red")
    agg.mark_committed()
    assert agg.edit_field(field_name="coat_color", new_value="red") is False
    assert agg.uncommitted == ()
    assert agg.canon_version == 2  # unchanged


def test_edit_field_requires_registration() -> None:
    agg = CanonEntityAggregate("e1")
    with pytest.raises(CommandRejected, match="not registered"):
        agg.edit_field(field_name="x", new_value="y")


def test_stale_canon_version_is_rejected() -> None:
    agg = _registered()  # version 1
    agg.edit_field(field_name="hair", new_value="black")
    agg.mark_committed()  # now version 2
    # A second editor still believes it is editing version 1 -> lost-update guard.
    with pytest.raises(InvariantViolation, match="stale canon edit"):
        agg.edit_field(field_name="hair", new_value="brown", expected_canon_version=1)


def test_matching_expected_version_allows_edit() -> None:
    agg = _registered()
    assert agg.edit_field(field_name="hair", new_value="black", expected_canon_version=1) is True


def test_swap_reference_image() -> None:
    agg = _registered()
    emitted = agg.swap_reference_image(new_reference_id="ref-2", dependent_shot_ids=["shot7"])
    assert emitted is True
    (event,) = agg.uncommitted
    assert isinstance(event, CanonReferenceImageSwapped)
    assert event.old_reference_id == ""
    assert event.new_reference_id == "ref-2"
    assert event.canon_version == 2
    assert agg.reference_id == "ref-2"


def test_swap_reference_dedupes_same_id() -> None:
    agg = _registered()
    agg.swap_reference_image(new_reference_id="ref-2")
    agg.mark_committed()
    assert agg.swap_reference_image(new_reference_id="ref-2") is False


def test_evolve_from_conflict() -> None:
    agg = _registered()
    emitted = agg.evolve_from_conflict(
        field_name="location",
        new_value="the orchard",
        conflict_id="cf-1",
        dependent_shot_ids=["shot3"],
    )
    assert emitted is True
    (event,) = agg.uncommitted
    assert isinstance(event, CanonEvolvedFromConflict)
    assert event.conflict_id == "cf-1"
    assert event.canon_version == 2
    assert agg.fields["location"] == "the orchard"


def test_evolve_requires_conflict_id() -> None:
    agg = _registered()
    with pytest.raises(ValidationError):
        agg.evolve_from_conflict(field_name="x", new_value="y", conflict_id="")


def test_version_increments_strictly_monotonically() -> None:
    agg = _registered()
    agg.edit_field(field_name="a", new_value="1")
    agg.swap_reference_image(new_reference_id="r")
    agg.evolve_from_conflict(field_name="b", new_value="2", conflict_id="cf")
    assert agg.canon_version == 4  # 1 (register) + 3 edits
    versions = [
        e.canon_version
        for e in agg.uncommitted
        if isinstance(e, (CanonFieldEdited, CanonReferenceImageSwapped, CanonEvolvedFromConflict))
    ]
    assert versions == [2, 3, 4]


def test_replay_reconstructs_canon_state() -> None:
    agg = CanonEntityAggregate("e1")
    agg.replay(
        [
            CanonEntityRegistered(
                entity_id="e1", book_id="b1", entity_type=EntityType.LOCATION.value, name="Manor"
            ),
            CanonFieldEdited(
                entity_id="e1", field_name="mood", new_value="gloomy", canon_version=2
            ),
            CanonReferenceImageSwapped(entity_id="e1", new_reference_id="ref-9", canon_version=3),
        ]
    )
    assert agg.entity_type is EntityType.LOCATION
    assert agg.fields["mood"] == "gloomy"
    assert agg.reference_id == "ref-9"
    assert agg.canon_version == 3
