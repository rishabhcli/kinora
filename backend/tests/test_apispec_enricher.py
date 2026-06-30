"""Tests for the OpenAPI enricher (app.apispec.enricher).

Infra-free + deterministic: builds a fresh ``create_app()`` and introspects the
enriched spec. Never enables KINORA_LIVE_VIDEO, never touches the network.
"""

from __future__ import annotations

import collections
import copy
import json

import pytest

from app.apispec.enricher import (
    build_enriched_spec,
    enrich_openapi,
    install,
    stable_operation_id,
)
from app.apispec.settings import ApiSpecSettings
from app.main import create_app


@pytest.fixture(scope="module")
def enriched() -> dict:
    return build_enriched_spec(create_app())


def test_spec_is_openapi_3(enriched: dict) -> None:
    assert enriched["openapi"].startswith("3.")
    assert "info" in enriched and "paths" in enriched
    assert enriched["info"]["title"] == "Kinora API"


def test_operation_ids_are_unique_and_stable(enriched: dict) -> None:
    ops = [
        op["operationId"]
        for item in enriched["paths"].values()
        for method, op in item.items()
        if method in ("get", "post", "put", "patch", "delete")
    ]
    # Every operation has an id and none collide.
    assert all(ops)
    dups = {k: v for k, v in collections.Counter(ops).items() if v > 1}
    assert dups == {}, f"duplicate operationIds: {dups}"


def test_operation_ids_are_deterministic() -> None:
    # Same routes -> identical ids across two independent builds.
    a = build_enriched_spec(create_app())
    b = build_enriched_spec(create_app())
    ids_a = {
        p: {m: op.get("operationId") for m, op in it.items() if isinstance(op, dict)}
        for p, it in a["paths"].items()
    }
    ids_b = {
        p: {m: op.get("operationId") for m, op in it.items() if isinstance(op, dict)}
        for p, it in b["paths"].items()
    }
    assert ids_a == ids_b


def test_stable_operation_id_examples() -> None:
    assert stable_operation_id("get", "/api/books", "books") == "booksGetBooks"
    # POST maps to the semantic verb "create"; path params become "By<Param>".
    assert (
        stable_operation_id("post", "/api/sessions/{session_id}/intent", "sessions")
        == "sessionsCreateSessionsBySessionIdIntent"
    )
    # Tagless still produces a valid, param-encoding identifier.
    oid = stable_operation_id("get", "/api/books/{book_id}/pages/{page_number}", None)
    assert oid.startswith("get") and "By" in oid


def test_enrichment_is_idempotent(enriched: dict) -> None:
    once = copy.deepcopy(enriched)
    twice = enrich_openapi(copy.deepcopy(once))
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_error_envelope_schema_present(enriched: dict) -> None:
    schemas = enriched["components"]["schemas"]
    assert "ErrorResponse" in schemas and "ErrorBody" in schemas
    body = schemas["ErrorBody"]
    assert set(body["required"]) == {"type", "message"}


def test_mutating_operations_document_typed_errors(enriched: dict) -> None:
    post = enriched["paths"]["/api/sessions"]["post"]
    codes = set(post["responses"])
    # Auth + body + mutating => 401/422/402/409/429/500/502 are all documented.
    assert {"401", "402", "409", "422", "429", "500", "502"}.issubset(codes)
    # And each error response references the shared envelope.
    ref = post["responses"]["409"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ErrorResponse")


def test_success_response_schema_is_untouched(enriched: dict) -> None:
    # The enricher must never rewrite a documented 2xx body the renderer parses.
    bare = build_enriched_spec(create_app())  # already enriched
    raw = create_app().openapi()  # FastAPI default (no enrichment installed)
    # Compare the 200/201 success schema for a representative route.
    for path, method in [("/api/sessions", "post"), ("/api/books", "get")]:
        for code in ("200", "201"):
            r1 = raw["paths"][path][method]["responses"].get(code)
            r2 = bare["paths"][path][method]["responses"].get(code)
            if r1 is None:
                continue
            assert r1.get("content") == r2.get("content"), (path, method, code)


def test_servers_and_security_metadata(enriched: dict) -> None:
    assert enriched["servers"][0]["url"] == "http://localhost:8000"
    bearer = enriched["components"]["securitySchemes"]["HTTPBearer"]
    assert bearer["scheme"] == "bearer"
    assert bearer["bearerFormat"] == "JWT"
    assert "Bearer" in bearer["description"]


def test_tags_carry_descriptions(enriched: dict) -> None:
    by_name = {t["name"]: t for t in enriched["tags"]}
    assert "books" in by_name and by_name["books"].get("description")
    assert "sessions" in by_name and by_name["sessions"].get("description")


def test_custom_server_url_from_settings() -> None:
    settings = ApiSpecSettings(
        public_server_url="https://api.kinora.example", include_local_server=True
    )
    spec = build_enriched_spec(create_app(), settings=settings)
    urls = [s["url"] for s in spec["servers"]]
    assert urls[0] == "https://api.kinora.example"
    assert "http://localhost:8000" in urls  # local kept as a second target


def test_install_overrides_app_openapi() -> None:
    app = create_app()
    install(app, settings=ApiSpecSettings(enabled=True))
    spec = app.openapi()
    # The cached, enriched spec is served (stable ids, servers present).
    assert spec["servers"][0]["url"] == "http://localhost:8000"
    ids = [
        op["operationId"]
        for it in spec["paths"].values()
        for m, op in it.items()
        if m in ("get", "post")
    ]
    assert any(i.startswith("books") for i in ids)
    # Calling again returns the cached object (no churn).
    assert app.openapi() is spec
