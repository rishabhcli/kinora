"""Tests for the breaking-change diff gate (app.apispec.diff).

Each break *class* gets a dedicated test that mutates a tiny synthetic spec and
asserts the diff flags it as ``breaking`` — and that forward-compatible
additions are *not* flagged. Plus a real round-trip: the live enriched spec vs.
itself must be fully compatible.
"""

from __future__ import annotations

import copy

from app.apispec.diff import (
    ChangeKind,
    diff_specs,
    load_snapshot,
    snapshot_spec,
)
from app.apispec.enricher import build_enriched_spec
from app.main import create_app


def _base_spec() -> dict:
    """A minimal but realistic two-operation spec for surgical mutation."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "components": {
            "schemas": {
                "Book": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "pages": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    },
                    "required": ["id"],
                },
                "CreateBook": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "author": {"type": "string"},
                    },
                    "required": ["title"],
                },
            }
        },
        "paths": {
            "/books": {
                "get": {
                    "operationId": "booksGetBooks",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Book"}
                                }
                            }
                        }
                    },
                },
                "post": {
                    "operationId": "booksCreateBooks",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateBook"}
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Book"}
                                }
                            }
                        }
                    },
                },
            }
        },
    }


def _kinds(result, category: str) -> list:
    return [c for c in result.changes if c.category == category]


def test_no_change_is_compatible() -> None:
    spec = _base_spec()
    result = diff_specs(spec, copy.deepcopy(spec))
    assert result.is_compatible
    assert result.breaking == []


def test_endpoint_removed_is_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    del new["paths"]["/books"]["post"]
    result = diff_specs(old, new)
    assert not result.is_compatible
    flagged = _kinds(result, "endpoint_removed")
    assert flagged and flagged[0].kind is ChangeKind.BREAKING


def test_response_field_removed_is_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    del new["components"]["schemas"]["Book"]["properties"]["pages"]
    result = diff_specs(old, new)
    assert any(
        c.category == "response_field_removed" and c.kind is ChangeKind.BREAKING
        for c in result.changes
    )


def test_response_type_narrowed_is_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    # Was string|null-ish? id was just string; narrow a number field's union.
    new["components"]["schemas"]["Book"]["properties"]["pages"] = {"type": "integer"}
    result = diff_specs(old, new)
    # old accepted {integer,null}; new only {integer} -> narrowing.
    assert any(
        c.category == "response_type_narrowed" and c.kind is ChangeKind.BREAKING
        for c in result.changes
    )


def test_new_required_request_field_is_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["CreateBook"]["properties"]["isbn"] = {"type": "string"}
    new["components"]["schemas"]["CreateBook"]["required"] = ["title", "isbn"]
    result = diff_specs(old, new)
    assert any(
        c.category == "request_required_field_added" and c.kind is ChangeKind.BREAKING
        for c in result.changes
    )


def test_optional_field_becoming_required_is_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["CreateBook"]["required"] = ["title", "author"]
    result = diff_specs(old, new)
    assert any(
        c.category == "request_field_now_required" and c.kind is ChangeKind.BREAKING
        for c in result.changes
    )


def test_request_type_narrowed_is_breaking() -> None:
    old = _base_spec()
    old["components"]["schemas"]["CreateBook"]["properties"]["title"] = {
        "anyOf": [{"type": "string"}, {"type": "integer"}]
    }
    new = copy.deepcopy(old)
    new["components"]["schemas"]["CreateBook"]["properties"]["title"] = {"type": "string"}
    result = diff_specs(old, new)
    assert any(
        c.category == "request_type_narrowed" and c.kind is ChangeKind.BREAKING
        for c in result.changes
    )


def test_success_status_removed_is_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    new["paths"]["/books"]["post"]["responses"]["200"] = new["paths"]["/books"]["post"][
        "responses"
    ].pop("201")
    result = diff_specs(old, new)
    assert any(
        c.category == "success_status_removed" and c.kind is ChangeKind.BREAKING
        for c in result.changes
    )


def test_new_endpoint_is_additive_not_breaking() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    new["paths"]["/books/{id}"] = {"get": {"operationId": "booksGetById", "responses": {"200": {}}}}
    result = diff_specs(old, new)
    assert result.is_compatible
    assert any(
        c.category == "endpoint_added" and c.kind is ChangeKind.ADDITION for c in result.changes
    )


def test_new_optional_response_field_is_additive() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["Book"]["properties"]["cover_url"] = {"type": "string"}
    result = diff_specs(old, new)
    assert result.is_compatible
    assert any(c.category == "response_field_added" for c in result.changes)


def test_operation_id_change_is_informational() -> None:
    old = _base_spec()
    new = copy.deepcopy(old)
    new["paths"]["/books"]["get"]["operationId"] = "booksListBooks"
    result = diff_specs(old, new)
    assert result.is_compatible  # not breaking for a JSON client
    assert any(
        c.category == "operation_id_changed" and c.kind is ChangeKind.INFO for c in result.changes
    )


def test_snapshot_round_trip_is_stable() -> None:
    spec = _base_spec()
    text = snapshot_spec(spec)
    assert snapshot_spec(load_snapshot(text)) == text  # canonical + deterministic


def test_live_spec_compatible_with_itself() -> None:
    spec = build_enriched_spec(create_app())
    result = diff_specs(spec, copy.deepcopy(spec))
    assert result.is_compatible, result.summary()
    assert result.breaking == []
