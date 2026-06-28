"""Spec catalog integrity + model from_dict behaviour (no I/O)."""

from __future__ import annotations

from kinora import ENDPOINTS, ERROR_TYPES, EVENTS
from kinora.models import BookResponse, ShotResponse, _Model
from kinora.spec import endpoints_by_tag, full_path


def test_spec_has_endpoints_and_unique_ids() -> None:
    assert len(ENDPOINTS) >= 30
    ids = [e["id"] for e in ENDPOINTS]
    assert len(set(ids)) == len(ids)


def test_full_path_prepends_prefix() -> None:
    login = next(e for e in ENDPOINTS if e["id"] == "login")
    assert full_path(login) == "/api/auth/login"
    assert login["auth"] is False


def test_endpoints_by_tag() -> None:
    groups = endpoints_by_tag()
    assert "auth" in groups
    assert any(e["id"] == "createSession" for e in groups["sessions"])


def test_events_and_error_types_documented() -> None:
    names = {e["name"] for e in EVENTS}
    assert {"clip_ready", "buffer_state", "conflict_choice"} <= names
    err_types = {e["type"] for e in ERROR_TYPES}
    assert {"book_not_found", "budget_exceeded"} <= err_types


def test_model_from_dict_drops_unknown_into_extra() -> None:
    book = BookResponse.from_dict({"id": "b1", "title": "A", "status": "ready", "weird": 1})
    assert book.id == "b1"
    assert book.extra == {"weird": 1}


def test_model_defaults_when_field_missing() -> None:
    shot = ShotResponse.from_dict({"shot_id": "s1", "status": "accepted"})
    assert shot.reference_image_ids == []
    assert shot.duration_s is None


def test_model_get_helper() -> None:
    book = BookResponse.from_dict({"id": "b1", "title": "A", "status": "ready", "x": 9})
    assert book.get("title") == "A"
    assert book.get("x") == 9
    assert book.get("missing", "fallback") == "fallback"


def test_base_model_is_frozen() -> None:
    m = _Model()
    try:
        m.extra = {}  # type: ignore[misc]
    except (AttributeError, TypeError):
        return
    raise AssertionError("models should be frozen")
