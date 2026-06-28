"""Unit tests for the GraphQL gateway plumbing (no infra required).

Covers pagination cursors, the dataloader, persisted queries / APQ, API-key
hashing + scopes, the SDL printer, the introspection object graph, the SDK
emitter, and the end-to-end request orchestrator (``run_graphql``) against the
real schema with a fake container/context.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.graphql.auth import (
    ApiKeyRecord,
    Scope,
    generate_key,
    parse_key,
    require_scope,
)
from app.graphql.dataloader import DataLoader
from app.graphql.errors import ErrorCode, GraphQLError
from app.graphql.introspection import build_introspection, print_schema
from app.graphql.pagination import (
    MAX_PAGE_SIZE,
    connection_from_list,
    decode_cursor,
    effective_page_size,
    encode_cursor,
)
from app.graphql.persisted import (
    PersistedQueryError,
    compute_hash,
    extract_apq_hash,
    resolve_query_text,
)
from app.graphql.root import build_schema
from app.graphql.sdk import generate_typescript_sdk
from app.graphql.types.node import from_global_id, global_id

SCHEMA = build_schema()


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


def test_cursor_round_trip() -> None:
    for offset in (0, 1, 42, 9999):
        assert decode_cursor(encode_cursor(offset)) == offset


def test_cursor_rejects_garbage() -> None:
    with pytest.raises(GraphQLError):
        decode_cursor("not-a-cursor")


def test_connection_forward_paging() -> None:
    items = list(range(10))
    conn = connection_from_list(items, first=3)
    assert [e.node for e in conn.edges] == [0, 1, 2]
    assert conn.total_count == 10
    assert conn.page_info.has_next_page is True
    assert conn.page_info.has_previous_page is False
    # Page 2 via after-cursor.
    conn2 = connection_from_list(items, first=3, after=conn.page_info.end_cursor)
    assert [e.node for e in conn2.edges] == [3, 4, 5]
    assert conn2.page_info.has_previous_page is True


def test_connection_backward_paging() -> None:
    items = list(range(10))
    conn = connection_from_list(items, last=2)
    assert [e.node for e in conn.edges] == [8, 9]
    assert conn.page_info.has_next_page is False


def test_page_size_capped() -> None:
    items = list(range(500))
    conn = connection_from_list(items, first=10_000)
    assert len(conn.edges) == MAX_PAGE_SIZE
    assert effective_page_size(10_000) == MAX_PAGE_SIZE


def test_negative_first_rejected() -> None:
    with pytest.raises(GraphQLError):
        connection_from_list([1, 2, 3], first=-1)


# --------------------------------------------------------------------------- #
# DataLoader
# --------------------------------------------------------------------------- #


async def test_dataloader_batches_calls() -> None:
    calls: list[list[int]] = []

    async def batch(keys: list[int]) -> list[int | None]:
        calls.append(list(keys))
        return [k * 10 for k in keys]

    loader: DataLoader[int, int] = DataLoader(batch)
    results = await asyncio.gather(loader.load(1), loader.load(2), loader.load(3), loader.load(1))
    assert results == [10, 20, 30, 10]
    # All four loads dispatched in ONE batch with de-duplicated keys.
    assert calls == [[1, 2, 3]]


async def test_dataloader_caches_and_primes() -> None:
    async def batch(keys: list[int]) -> list[int | None]:
        return list(keys)

    loader: DataLoader[int, int] = DataLoader(batch)
    loader.prime(5, 500)
    assert await loader.load(5) == 500


# --------------------------------------------------------------------------- #
# Persisted queries + APQ
# --------------------------------------------------------------------------- #


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}

    async def get_json(self, key: str) -> Any:
        return self.kv.get(key)

    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
        self.kv[key] = value


async def test_persisted_query_explicit_miss() -> None:
    from app.graphql.persisted import PersistedQueryStore

    store = PersistedQueryStore(_FakeRedis())  # type: ignore[arg-type]
    with pytest.raises(PersistedQueryError):
        await resolve_query_text(store, query=None, persisted_id="deadbeef", apq_hash=None)


async def test_apq_register_then_hit() -> None:
    from app.graphql.persisted import PersistedQueryStore

    store = PersistedQueryStore(_FakeRedis())  # type: ignore[arg-type]
    query = "{ apiVersion { version } }"
    sha = compute_hash(query)
    # First probe with only the hash → miss.
    with pytest.raises(PersistedQueryError):
        await resolve_query_text(store, query=None, persisted_id=None, apq_hash=sha)
    # Client resends with text + hash → registers + returns.
    assert await resolve_query_text(store, query=query, persisted_id=None, apq_hash=sha) == query
    # Next time the hash alone hits.
    assert await resolve_query_text(store, query=None, persisted_id=None, apq_hash=sha) == query


def test_extract_apq_hash() -> None:
    ext = {"persistedQuery": {"version": 1, "sha256Hash": "abc"}}
    assert extract_apq_hash(ext) == "abc"
    assert extract_apq_hash(None) is None
    assert extract_apq_hash({}) is None


# --------------------------------------------------------------------------- #
# API keys + scopes
# --------------------------------------------------------------------------- #


def test_generate_and_parse_key() -> None:
    full, key_id, secret = generate_key()
    assert full.startswith("kinora_pk_")
    parsed = parse_key(full)
    assert parsed == (key_id, secret)
    assert parse_key("garbage") is None
    assert parse_key("kinora_pk_noseparator") is None


def test_scope_check() -> None:
    record = ApiKeyRecord(
        key_id="k", user_id="u", secret_hash="h", scopes=(Scope.BOOKS_READ,), label="t"
    )
    require_scope(record, Scope.BOOKS_READ)  # no raise
    with pytest.raises(GraphQLError) as exc:
        require_scope(record, Scope.CANON_WRITE)
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_global_id_round_trip() -> None:
    gid = global_id("Book", "b123")
    assert from_global_id(gid) == ("Book", "b123")
    with pytest.raises(GraphQLError):
        from_global_id("@@@")


# --------------------------------------------------------------------------- #
# Introspection + SDL + SDK
# --------------------------------------------------------------------------- #


def test_print_schema_contains_core_types() -> None:
    sdl = print_schema(SCHEMA)
    for needle in (
        "type Query",
        "type Mutation",
        "type Book implements Node",
        "type BookConnection",
        "enum ShotStatus",
        "input CreateReadingSessionInput",
        "scalar JSON",
        "interface Node",
    ):
        assert needle in sdl, needle
    # The deprecated field is annotated.
    assert "@deprecated" in sdl


def test_introspection_payload_shape() -> None:
    intro = build_introspection(SCHEMA)
    assert intro["queryType"] == {"name": "Query"}
    assert intro["mutationType"] == {"name": "Mutation"}
    type_names = {t["name"] for t in intro["types"]}
    assert {"Book", "Shot", "Query", "ShotStatus"} <= type_names
    directive_names = {d["name"] for d in intro["directives"]}
    assert {"skip", "include", "deprecated"} <= directive_names


def test_generate_sdk() -> None:
    sdk = generate_typescript_sdk(SCHEMA)
    assert "export interface Book" in sdk
    assert "export type ShotStatus" in sdk
    assert "class KinoraGraphQLClient" in sdk
    assert "x-api-key" in sdk
    # Scalars map to TS primitives.
    assert "string" in sdk


# --------------------------------------------------------------------------- #
# End-to-end orchestrator with a fake context
# --------------------------------------------------------------------------- #


class _FakeContext:
    """A minimal context for resolver-free queries (apiVersion / viewer)."""

    def __init__(self) -> None:
        self.api_key = ApiKeyRecord(
            key_id="k",
            user_id="user-1",
            secret_hash="h",
            scopes=Scope.ALL,
            label="test key",
        )

    def require(self, scope: str) -> None:
        require_scope(self.api_key, scope)


async def test_run_graphql_api_version() -> None:
    from app.graphql.gateway import GraphQLRequest, run_graphql
    from app.graphql.persisted import PersistedQueryStore

    store = PersistedQueryStore(_FakeRedis())  # type: ignore[arg-type]
    req = GraphQLRequest(query="{ apiVersion { version stability } viewer { userId scopes } }")
    resp = await run_graphql(
        schema=SCHEMA, request=req, context=_FakeContext(), persisted=store
    )
    assert "errors" not in resp, resp
    assert resp["data"]["apiVersion"]["version"]
    assert resp["data"]["viewer"]["userId"] == "user-1"
    assert "books:read" in resp["data"]["viewer"]["scopes"]


async def test_run_graphql_validation_error() -> None:
    from app.graphql.gateway import GraphQLRequest, run_graphql
    from app.graphql.persisted import PersistedQueryStore

    store = PersistedQueryStore(_FakeRedis())  # type: ignore[arg-type]
    req = GraphQLRequest(query="{ apiVersion { nope } }")
    resp = await run_graphql(
        schema=SCHEMA, request=req, context=_FakeContext(), persisted=store
    )
    # A request (pre-execution) error: `errors` present, `data` absent per the spec.
    assert resp.get("data") is None
    assert resp["errors"][0]["extensions"]["code"] == ErrorCode.GRAPHQL_VALIDATION_FAILED


async def test_run_graphql_parse_error() -> None:
    from app.graphql.gateway import GraphQLRequest, run_graphql
    from app.graphql.persisted import PersistedQueryStore

    store = PersistedQueryStore(_FakeRedis())  # type: ignore[arg-type]
    req = GraphQLRequest(query="{ apiVersion {")
    resp = await run_graphql(
        schema=SCHEMA, request=req, context=_FakeContext(), persisted=store
    )
    assert resp["errors"][0]["extensions"]["code"] == ErrorCode.GRAPHQL_PARSE_FAILED


async def test_run_graphql_introspection() -> None:
    from app.graphql.gateway import GraphQLRequest, run_graphql
    from app.graphql.persisted import PersistedQueryStore

    store = PersistedQueryStore(_FakeRedis())  # type: ignore[arg-type]
    req = GraphQLRequest(
        query='{ __schema { queryType { name } } __type(name: "Book") { name } }'
    )
    resp = await run_graphql(
        schema=SCHEMA, request=req, context=_FakeContext(), persisted=store
    )
    assert resp["data"]["__schema"]["queryType"]["name"] == "Query"
    assert resp["data"]["__type"]["name"] == "Book"
