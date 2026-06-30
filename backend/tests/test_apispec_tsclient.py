"""Tests for the TypeScript typed-client generator (app.apispec.tsclient).

Verifies the generator emits valid-*shape* TS (balanced braces, parseable type
expressions, no leaked Python), covers every renderer route, and is
deterministic (snapshot-stable). No Node/tsc dependency — we assert structural
properties of the generated string.
"""

from __future__ import annotations

import re

from app.apispec.enricher import build_enriched_spec
from app.apispec.tsclient import (
    RENDERER_ROUTES,
    generate_client,
    renderer_coverage,
    ts_type,
)
from app.main import create_app


def _spec() -> dict:
    return build_enriched_spec(create_app())


def test_ts_type_scalars_and_composites() -> None:
    assert ts_type({"type": "string"}) == "string"
    assert ts_type({"type": "integer"}) == "number"
    assert ts_type({"type": "boolean"}) == "boolean"
    assert ts_type({"type": "array", "items": {"type": "string"}}) == "string[]"
    assert ts_type({"anyOf": [{"type": "string"}, {"type": "null"}]}) == "string | null"
    assert ts_type({"$ref": "#/components/schemas/BookResponse"}) == "BookResponse"
    # Unknown / untyped collapses safely.
    assert ts_type({}) == "unknown"
    assert ts_type({"type": "object"}) == "Record<string, unknown>"


def test_generated_client_has_methods_and_imports() -> None:
    client = generate_client(_spec())
    assert "import { http, ApiError }" in client.source
    assert "export const kinoraClient" in client.source
    assert len(client.method_names) == len(client.paths) > 200


def test_generated_method_names_are_unique_identifiers() -> None:
    client = generate_client(_spec())
    ident = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*$")
    assert all(ident.match(n) for n in client.method_names)
    # delete is a reserved-ish word; generator must escape it.
    assert "delete" not in client.method_names


def test_generated_source_is_brace_balanced() -> None:
    client = generate_client(_spec())
    src = client.source
    assert src.count("{") == src.count("}")
    assert src.count("(") == src.count(")")
    # No Python literals leaked into the generated *code* (doc comments, which may
    # legitimately contain prose like "None of these", are excluded).
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith(("/**", "//", "/*"))
    )
    assert " None" not in code and " True" not in code and " False" not in code


def test_generated_interfaces_present_for_key_schemas() -> None:
    client = generate_client(_spec())
    for name in ("BookResponse", "SessionResponse", "ErrorResponse"):
        assert f"interface {name}" in client.source or f"type {name}" in client.source


def test_renderer_coverage_is_complete() -> None:
    client = generate_client(_spec())
    missing = renderer_coverage(client)
    assert missing == [], f"renderer routes missing from generated client: {missing}"


def test_renderer_routes_constant_matches_real_paths() -> None:
    # Every declared renderer route must actually exist in the live spec, so the
    # coverage check can't pass vacuously against a stale route list.
    spec = _spec()

    def _norm(p: str) -> str:
        return re.sub(r"\{.+?\}", "{}", p)

    live = {
        (m.upper(), _norm(path))
        for path, item in spec["paths"].items()
        for m in item
        if m in ("get", "post", "put", "patch", "delete")
    }
    for method, path in RENDERER_ROUTES:
        assert (method, _norm(path)) in live, f"renderer route absent from spec: {method} {path}"


def test_generation_is_deterministic() -> None:
    a = generate_client(build_enriched_spec(create_app()))
    b = generate_client(build_enriched_spec(create_app()))
    assert a.source == b.source
    assert a.method_names == b.method_names


def test_method_signature_shape_for_a_path_param_route() -> None:
    # A route with a path param + body should produce (param: string, body: T).
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/api/books/{book_id}/pages/{page_number}": {
                "get": {
                    "operationId": "booksGetPage",
                    "summary": "Get a page",
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                    },
                }
            }
        },
        "components": {"schemas": {}},
    }
    src = generate_client(spec).source
    assert "booksGetPage(book_id: string, page_number: string)" in src
    assert "`/api/books/${book_id}/pages/${page_number}`" in src
