"""Tests for the contract harness (app.apispec.contract).

Asserts the validator (a) passes a conformant payload, (b) flags every mismatch
class against a deliberately-mismatched doc, and (c) validates *live* TestClient
responses for the infra-free meta endpoints against their documented schemas.
Deterministic + infra-free; never enables live video.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.apispec.contract import (
    check_recorded,
    check_response,
    documented_schema,
)
from app.apispec.enricher import build_enriched_spec, install
from app.apispec.settings import ApiSpecSettings
from app.main import create_app


def _spec() -> dict:
    return build_enriched_spec(create_app())


# --------------------------------------------------------------------------- #
# Synthetic spec for surgical mismatch tests
# --------------------------------------------------------------------------- #

_SYNTH = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/widget": {
            "get": {
                "operationId": "getWidget",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "count": {"type": "integer"},
                                        "ratio": {"type": "number"},
                                        "tags": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "note": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "null"},
                                            ]
                                        },
                                    },
                                    "required": ["id", "count"],
                                }
                            }
                        }
                    }
                },
            }
        }
    },
    "components": {"schemas": {}},
}


def test_conformant_payload_passes() -> None:
    payload = {"id": "w1", "count": 3, "ratio": 0.5, "tags": ["a", "b"], "note": None}
    report = check_response(_SYNTH, "get", "/widget", 200, payload)
    assert report.ok, report.violations
    assert report.checked == 1


def test_missing_required_field_flagged() -> None:
    report = check_response(_SYNTH, "get", "/widget", 200, {"id": "w1"})
    assert not report.ok
    assert any("count" in v.location and "required" in v.reason for v in report.violations)


def test_wrong_scalar_type_flagged() -> None:
    # count documented as integer; live returns a string.
    report = check_response(_SYNTH, "get", "/widget", 200, {"id": "w1", "count": "three"})
    assert not report.ok
    assert any("count" in v.location for v in report.violations)


def test_integer_accepted_where_number_documented() -> None:
    # ratio is number; an int payload must NOT be a violation (JSON has one numeric type).
    report = check_response(_SYNTH, "get", "/widget", 200, {"id": "w1", "count": 1, "ratio": 2})
    assert report.ok, report.violations


def test_array_element_type_flagged() -> None:
    payload = {"id": "w1", "count": 1, "tags": ["ok", 5]}
    report = check_response(_SYNTH, "get", "/widget", 200, payload)
    assert not report.ok
    assert any("tags[1]" in v.location for v in report.violations)


def test_nullable_union_accepts_null_and_string() -> None:
    for note in (None, "hello"):
        report = check_response(
            _SYNTH, "get", "/widget", 200, {"id": "x", "count": 1, "note": note}
        )
        assert report.ok, (note, report.violations)
    # but not an integer
    bad = check_response(_SYNTH, "get", "/widget", 200, {"id": "x", "count": 1, "note": 7})
    assert not bad.ok


def test_deliberately_mismatched_doc_is_caught() -> None:
    # The doc claims count:integer but the (buggy) handler returns a float string;
    # the harness must catch the doc/impl divergence.
    buggy_payload = {"id": "w1", "count": "12", "ratio": "high"}
    report = check_response(_SYNTH, "get", "/widget", 200, buggy_payload)
    assert len(report.violations) >= 2  # count + ratio both wrong


def test_check_recorded_matches_templated_paths() -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/api/books/{book_id}": {
                "get": {
                    "operationId": "booksGet",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"id": {"type": "string"}},
                                        "required": ["id"],
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {"schemas": {}},
    }
    samples = [("GET", "/api/books/abc123", 200, {"id": "abc123"})]
    report = check_recorded(spec, samples)
    assert report.ok and report.checked == 1
    # A concrete path with a bad shape is still caught after template matching.
    bad = check_recorded(spec, [("GET", "/api/books/zzz", 200, {})])
    assert not bad.ok


def test_no_documented_schema_is_treated_conformant() -> None:
    # 204 / streaming routes have no JSON schema => nothing to contradict.
    report = check_response(_SYNTH, "get", "/widget", 204, {"anything": True})
    assert report.ok and report.checked == 0


# --------------------------------------------------------------------------- #
# Live contract check against infra-free meta endpoints
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def live_client_and_spec() -> tuple[TestClient, dict]:
    app = create_app()
    app.state.run_idle_sweeper = False
    app.state.run_realtime_sweeper = False
    app.state.run_notification_bridge = False
    install(app, settings=ApiSpecSettings(enabled=True))
    spec = app.openapi()
    return TestClient(app), spec


def test_health_response_matches_its_documented_schema(live_client_and_spec) -> None:
    client, spec = live_client_and_spec
    resp = client.get("/health")
    assert resp.status_code == 200
    report = check_response(spec, "get", "/health", 200, resp.json())
    assert report.ok, report.violations


def test_root_index_response_matches_schema(live_client_and_spec) -> None:
    client, spec = live_client_and_spec
    resp = client.get("/")
    report = check_response(spec, "get", "/", resp.status_code, resp.json())
    assert report.ok, report.violations


def test_openapi_json_endpoint_serves_enriched_spec(live_client_and_spec) -> None:
    client, _ = live_client_and_spec
    served = client.get("/openapi.json").json()
    # The enriched markers are present on the served document.
    assert served["servers"][0]["url"] == "http://localhost:8000"
    assert "ErrorResponse" in served["components"]["schemas"]


def test_documented_schema_lookup(live_client_and_spec) -> None:
    _, spec = live_client_and_spec
    schema = documented_schema(spec, "get", "/api/books", 200)
    assert schema is not None  # the shelf endpoint documents a 200 body
