"""End-to-end GraphQL gateway tests over the ASGI app (infra-bound).

These exercise the mounted ``/graphql`` surface against throwaway
Postgres/Redis/MinIO using the same fixtures as the REST API tests; they skip
cleanly when ``KINORA_TEST_*`` infra is not configured. They cover: API-key
minting via the user JWT, key auth + scope enforcement, a real ``books`` query
with cursor pagination, the ``node`` global lookup, a mutation
(``createReadingSession``), the SDL/SDK/version endpoints, and persisted queries.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from app.composition import Container
from tests.conftest import requires_infra, seed_owned_book

pytestmark = requires_infra


async def _mint_key(
    client: AsyncClient, headers: dict[str, str], *, scopes: list[str] | None = None
) -> str:
    body: dict[str, Any] = {"label": "test"}
    if scopes is not None:
        body["scopes"] = scopes
    resp = await client.post("/graphql/keys", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["apiKey"])


async def _gql(
    client: AsyncClient, api_key: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await client.post(
        "/graphql",
        headers={"x-api-key": api_key},
        json={"query": query, "variables": variables or {}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_schema_sdk_version_endpoints_public(api_client: AsyncClient) -> None:
    sdl = await api_client.get("/graphql/schema")
    assert sdl.status_code == 200
    assert "type Query" in sdl.text
    sdk = await api_client.get("/graphql/sdk")
    assert "KinoraGraphQLClient" in sdk.text
    version = await api_client.get("/graphql/version")
    assert version.status_code == 200
    assert version.json()["version"]


async def test_unauthenticated_request_is_masked_error(api_client: AsyncClient) -> None:
    resp = await api_client.post("/graphql", json={"query": "{ apiVersion { version } }"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"][0]["extensions"]["code"] == "UNAUTHENTICATED"


async def test_mint_key_and_query_books(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="GraphQL Tale")
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])

    data = await _gql(
        api_key=api_key,
        client=api_client,
        query="""
        query {
          viewer { userId scopes }
          books(first: 10) {
            totalCount
            edges { cursor node { id title status legacyId } }
            pageInfo { hasNextPage }
          }
        }
        """,
    )
    assert "errors" not in data, data
    assert data["data"]["viewer"]["scopes"] == ["books:read"]
    titles = [e["node"]["title"] for e in data["data"]["books"]["edges"]]
    assert "GraphQL Tale" in titles
    # The opaque global id is not the raw book id.
    node = next(
        e["node"]
        for e in data["data"]["books"]["edges"]
        if e["node"]["title"] == "GraphQL Tale"
    )
    assert node["id"] != book_id
    assert node["legacyId"] == book_id


async def test_node_global_lookup(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Node Tale")
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])
    # The global id is deterministic from the local book id (Relay opaque encoding).
    from app.graphql.types.node import global_id

    gid = global_id("Book", book_id)
    fetched = await _gql(
        api_client,
        api_key,
        "query($id: ID!) { node(id: $id) { id ... on Book { title } } }",
        {"id": gid},
    )
    assert "errors" not in fetched, fetched
    assert fetched["data"]["node"]["id"] == gid
    assert fetched["data"]["node"]["title"] == "Node Tale"


async def test_scope_enforcement(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Scoped")
    # A key WITHOUT canon:read cannot read the canon field.
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])
    data = await _gql(
        api_client,
        api_key,
        "query($id: ID!) { book(id: $id) { id canon { bookId } } }",
        {"id": book_id},
    )
    assert data["data"]["book"]["canon"] is None
    assert any(e["extensions"]["code"] == "FORBIDDEN" for e in data["errors"])


async def test_create_reading_session_mutation(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Session Tale")
    api_key = await _mint_key(
        api_client, auth_headers, scopes=["books:read", "sessions:read", "sessions:write"]
    )
    created = await _gql(
        api_client,
        api_key,
        """
        mutation($input: CreateReadingSessionInput!) {
          createReadingSession(input: $input) { id bookId mode focusWord }
        }
        """,
        {"input": {"bookId": book_id, "focusWord": 3, "mode": "VIEWER"}},
    )
    assert "errors" not in created, created
    session = created["data"]["createReadingSession"]
    assert session["bookId"] == book_id
    assert session["mode"] == "VIEWER"
    # ``session(id:)`` expects the *local* session id; the mutation returns the
    # opaque global id, so decode it first, then refetch it.
    from app.graphql.types.node import from_global_id

    _type, local = from_global_id(session["id"])
    fetched = await _gql(
        api_client,
        api_key,
        "query($id: ID!) { session(id: $id) { focusWord } }",
        {"id": local},
    )
    assert fetched["data"]["session"]["focusWord"] == 3


async def test_persisted_query_apq_flow(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])
    query = "{ apiVersion { version } }"
    import hashlib

    sha = hashlib.sha256(query.encode()).hexdigest()
    ext = {"persistedQuery": {"version": 1, "sha256Hash": sha}}
    # Probe with only the hash → not found.
    miss = await api_client.post(
        "/graphql", headers={"x-api-key": api_key}, json={"extensions": ext}
    )
    assert miss.json()["errors"][0]["extensions"]["code"] == "PERSISTED_QUERY_NOT_FOUND"
    # Register by sending text + hash.
    reg = await api_client.post(
        "/graphql", headers={"x-api-key": api_key}, json={"query": query, "extensions": ext}
    )
    assert reg.json()["data"]["apiVersion"]["version"]
    # Now the hash alone hits.
    hit = await api_client.post(
        "/graphql", headers={"x-api-key": api_key}, json={"extensions": ext}
    )
    assert hit.json()["data"]["apiVersion"]["version"]


async def test_depth_limit_rejects_pathological_query(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])
    # Deeply nested book→shots→node→book→… chain via shot.book recursion.
    deep = (
        '{ book(id: "x") { shots { edges { node { book { shots { edges '
        "{ node { book { id } } } } } } } } } }"
    )
    resp = await api_client.post(
        "/graphql", headers={"x-api-key": api_key}, json={"query": deep}
    )
    body = resp.json()
    # Either depth or complexity rejects it before any DB hit.
    assert "errors" in body
    codes = {e["extensions"]["code"] for e in body["errors"]}
    assert codes & {"DEPTH_LIMIT_EXCEEDED", "COMPLEXITY_LIMIT_EXCEEDED"}


async def test_directing_style_field(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Style Tale")
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read", "prefs:read"])
    data = await _gql(
        api_client,
        api_key,
        "query($id: ID!) { book(id: $id) { directingStyle { kind label applied } } }",
        {"id": book_id},
    )
    assert "errors" not in data, data
    # A freshly-seeded book has learned nothing yet → an empty list (zero-state).
    assert data["data"]["book"]["directingStyle"] == []


async def test_request_batching(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])
    resp = await api_client.post(
        "/graphql",
        headers={"x-api-key": api_key},
        json=[
            {"query": "{ apiVersion { version } }"},
            {"query": "{ viewer { userId } }"},
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list) and len(body) == 2
    assert body[0]["data"]["apiVersion"]["version"]
    assert body[1]["data"]["viewer"]["userId"]


async def test_revoked_key_is_rejected(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    api_key = await _mint_key(api_client, auth_headers, scopes=["books:read"])
    # Find the key id and revoke it.
    listing = await api_client.get("/graphql/keys", headers=auth_headers)
    key_id = listing.json()["keys"][0]["keyId"]
    revoke = await api_client.delete(f"/graphql/keys/{key_id}", headers=auth_headers)
    assert revoke.status_code == 200
    data = await _gql(api_client, api_key, "{ apiVersion { version } }")
    assert data["errors"][0]["extensions"]["code"] == "UNAUTHENTICATED"
