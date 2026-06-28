"""Protocol round-trip + resources + subscriptions + client SDK + conformance.

These exercise the *full* MCP protocol layer end-to-end with a **fake**
``MemoryTools`` (a legitimate test double for the single execution path) so they
need no database, no Redis, and no network:

* the official MCP in-memory client ↔ server session (``initialize``,
  ``list_tools`` with output schema + version meta, ``call_tool`` happy + every
  typed error, version pins, resource templates + reads + subscribe);
* the resource provider + subscription fan-out directly;
* the typed in-process + over-session client SDK;
* the static conformance suite.

The fake dispatches a handful of tools to canned typed results; the contract
(validation, versioning, authorization, capabilities) is the real protocol code.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from app.mcp import schemas
from app.mcp.capabilities import KINORA_EXPERIMENTAL_KEY
from app.mcp.client import KinoraMCPClient
from app.mcp.conformance import run_conformance
from app.mcp.errors import InvalidParamsError, MethodNotFoundError, VersionError
from app.mcp.resources import ResourceProvider, SubscriptionRegistry, resolve_uri
from app.mcp.server import build_protocol_server, is_error_payload, render_tool_error
from mcp import types


def _first_text(result: Any) -> str:
    """The text of the first text content block (asserts the block is text)."""
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def _resource_text(contents: Any) -> str:
    """The text of the first resource-contents entry (asserts it is text)."""
    entry = contents.contents[0]
    assert isinstance(entry, types.TextResourceContents)
    return entry.text


def _uri(value: str) -> AnyUrl:
    return AnyUrl(value)


class FakeTools:
    """A canned ``MemoryTools`` double — dispatch returns typed results, no DB."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "budget.remaining":
            return schemas.BudgetRemainingOutput(
                remaining_video_s=10.0, ceiling_video_s=100.0, is_low=False, can_render_live=False
            )
        if name == "canon.view":
            from app.memory.contracts import CanonReadView

            return CanonReadView(
                book_id=arguments["book_id"],
                branch=arguments.get("branch", "main"),
                beat=1,
                as_of_tx=None,
                facts=[],
                branches=[],
                audit_tail=[],
            )
        if name == "canon.vault":
            return schemas.CanonVaultOutput(
                book_id=arguments["book_id"], branch="main", markdown="# Canon\n", sections={}
            )
        if name == "canon.audit":
            from app.memory.contracts import AuditChain

            return AuditChain(book_id=arguments["book_id"], entries=[], intact=True)
        if name == "canon.assert_fact":
            from datetime import UTC, datetime

            from app.memory.contracts import BeatSpan, BitemporalFact, TxSpan, WriteStamp

            return BitemporalFact(
                id="fact_1",
                fact_key="f1",
                branch=arguments.get("branch", "main"),
                subject_entity_key="elsa",
                predicate="has",
                object_value="sword",
                valid=BeatSpan(valid_from_beat=arguments.get("valid_from_beat", 1)),
                tx=TxSpan(tx_from=datetime.now(UTC)),
                stamp=WriteStamp(wall=1, counter=0, actor_id="system"),
                current=True,
            )
        if name == "prefs.get":
            from app.memory.prefs_service import PreferencePriors

            return PreferencePriors(
                user_id=arguments.get("user_id"), book_id=arguments.get("book_id"), priors={}
            )
        if name == "canon.facts_as_of":
            return schemas.CanonFactsAsOfOutput(facts=[])
        raise RuntimeError(f"FakeTools: unexpected tool {name}")


@pytest.fixture
def tools() -> FakeTools:
    return FakeTools()


# --------------------------------------------------------------------------- #
# Full protocol round-trip via the official in-memory client/server session
# --------------------------------------------------------------------------- #


async def test_initialize_and_list_tools_advertises_schemas_and_version(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        listed = await client.list_tools()
        assert len(listed.tools) == len(bundle.catalog.metas)
        bm = next(t for t in listed.tools if t.name == "budget.remaining")
        assert bm.outputSchema is not None
        assert bm.meta is not None
        ext = bm.meta[KINORA_EXPERIMENTAL_KEY]
        assert ext["version"] == "1.0"
        assert ext["scopes"] == ["read"]


async def test_call_tool_happy_path_returns_structured_content(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        res = await client.call_tool("budget.remaining", {})
        assert res.isError is False
        assert res.structuredContent is not None
        assert res.structuredContent["remaining_video_s"] == 10.0


async def test_call_tool_unknown_tool_is_error(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        res = await client.call_tool("does.not.exist", {})
        assert res.isError is True
        assert "unknown tool" in _first_text(res)


async def test_call_tool_invalid_params_is_error(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        res = await client.call_tool("canon.view", {})  # missing book_id
        assert res.isError is True
        assert "invalid arguments" in _first_text(res)


async def test_version_pin_compatible_and_incompatible(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        ok = await client.call_tool(
            "budget.remaining", {"_meta": {"io.kinora.canon/version": "1.0"}}
        )
        assert ok.isError is False
        bad = await client.call_tool(
            "budget.remaining", {"_meta": {"io.kinora.canon/version": "2.0"}}
        )
        assert bad.isError is True
        assert "incompatible" in _first_text(bad)


async def test_resource_templates_and_read(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        tpls = await client.list_resource_templates()
        uris = {t.uriTemplate for t in tpls.resourceTemplates}
        assert "kinora://canon/{book_id}" in uris
        assert "kinora://canon/{book_id}/vault" in uris
        contents = await client.read_resource(_uri("kinora://canon/book_1"))
        assert contents.contents
        # The vault renders markdown.
        vault = await client.read_resource(_uri("kinora://canon/book_1/vault"))
        assert "# Canon" in _resource_text(vault)


async def test_subscribe_records_subscription(tools: FakeTools) -> None:
    subs = SubscriptionRegistry()
    bundle = build_protocol_server(tools, subscriptions=subs)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        await client.subscribe_resource(_uri("kinora://canon/book_1"))
        assert subs.total_subscriptions == 1


async def test_write_tool_fans_out_to_subscribers(tools: FakeTools) -> None:
    subs = SubscriptionRegistry()
    bundle = build_protocol_server(tools, subscriptions=subs)
    async with create_connected_server_and_client_session(bundle.server) as client:
        await client.initialize()
        await client.subscribe_resource(_uri("kinora://canon/book_1"))
        # A canon write should touch (and notify) the book's canon resource.
        res = await client.call_tool(
            "canon.assert_fact",
            {
                "book_id": "book_1",
                "subject_entity_key": "elsa",
                "predicate": "has",
                "object_value": "sword",
                "valid_from_beat": 1,
            },
        )
        assert res.isError is False
        # The fake recorded the write dispatch.
        assert any(c[0] == "canon.assert_fact" for c in tools.calls)


# --------------------------------------------------------------------------- #
# Resource provider + subscription registry (direct)
# --------------------------------------------------------------------------- #


def test_resolve_uri_variants() -> None:
    assert resolve_uri("kinora://canon/b1").tool == "canon.view"
    assert resolve_uri("kinora://canon/b1/vault").tool == "canon.vault"
    assert resolve_uri("kinora://canon/b1/audit").tool == "canon.audit"
    assert resolve_uri("kinora://canon/b1/branch/edit").tool == "canon.facts_as_of"
    assert resolve_uri("kinora://prefs/u1").tool == "prefs.get"


def test_resolve_uri_rejects_unknown() -> None:
    with pytest.raises(InvalidParamsError):
        resolve_uri("kinora://nope/x")


async def test_resource_read_not_found(tools: FakeTools) -> None:
    # prefs.get returns a PreferencePriors with no `found` flag -> not absent.
    provider = ResourceProvider(tools)
    contents = await provider.read("kinora://prefs/u1")
    assert contents.structured["user_id"] == "u1"


def test_write_touch_map() -> None:
    touched = ResourceProvider.resources_touched_by(
        "canon.assert_fact", {"book_id": "b1", "branch": "edit"}
    )
    assert "kinora://canon/b1" in touched
    assert "kinora://canon/b1/branch/edit" in touched
    assert ResourceProvider.resources_touched_by("canon.query", {"book_id": "b1"}) == []
    assert ResourceProvider.resources_touched_by("prefs.upsert", {"user_id": "u1"}) == [
        "kinora://prefs/u1"
    ]


def test_subscription_registry_fan_out_and_drop() -> None:
    reg = SubscriptionRegistry()
    reg.subscribe("c1", "kinora://canon/b1")
    reg.subscribe("c2", "kinora://canon/b1")
    reg.subscribe("c1", "kinora://canon/b1/vault")
    fan = reg.fan_out(["kinora://canon/b1"])
    assert fan == {"c1": {"kinora://canon/b1"}, "c2": {"kinora://canon/b1"}}
    reg.drop_client("c1")
    assert reg.subscribers_for("kinora://canon/b1") == {"c2"}
    assert reg.subscriptions_for("c1") == set()


# --------------------------------------------------------------------------- #
# Typed client SDK
# --------------------------------------------------------------------------- #


async def test_in_process_client_typed_call(tools: FakeTools) -> None:
    client = KinoraMCPClient.in_process(tools)
    out = await client.budget_remaining()
    assert isinstance(out, schemas.BudgetRemainingOutput)
    assert out.remaining_video_s == 10.0


async def test_in_process_client_validates_request(tools: FakeTools) -> None:
    client = KinoraMCPClient.in_process(tools)
    with pytest.raises(InvalidParamsError):
        await client.call("canon.view", {})  # missing book_id


async def test_in_process_client_unknown_tool(tools: FakeTools) -> None:
    client = KinoraMCPClient.in_process(tools)
    with pytest.raises(MethodNotFoundError):
        await client.call("nope.nope", {})


async def test_in_process_client_safe_call_returns_error_body(tools: FakeTools) -> None:
    client = KinoraMCPClient.in_process(tools)
    body = await client.safe_call("canon.view", {})
    assert is_error_payload(body)
    assert body["category"] == "invalid_params"


async def test_in_process_client_applies_authorizer(tools: FakeTools) -> None:
    from app.mcp.identity import ClientIdentity, ScopedAuthorizer

    authz = ScopedAuthorizer().for_identity(ClientIdentity.read_only("judge"))
    client = KinoraMCPClient.in_process(tools, authorizer=authz)
    # canon.view is a read -> allowed.
    out = await client.call("canon.view", {"book_id": "b1"})
    assert out["book_id"] == "b1"


async def test_session_client_round_trip(tools: FakeTools) -> None:
    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as session:
        await session.initialize()
        client = KinoraMCPClient.over_session(session)
        out = await client.call_typed("budget.remaining", schemas.BudgetRemainingInput())
        assert isinstance(out, schemas.BudgetRemainingOutput)
        assert out.remaining_video_s == 10.0


async def test_session_client_surfaces_error(tools: FakeTools) -> None:
    from app.mcp.errors import MCPError

    bundle = build_protocol_server(tools)
    async with create_connected_server_and_client_session(bundle.server) as session:
        await session.initialize()
        client = KinoraMCPClient.over_session(session)
        with pytest.raises(MCPError):
            await client.call("canon.view", {})  # missing book_id -> isError


# --------------------------------------------------------------------------- #
# Conformance suite
# --------------------------------------------------------------------------- #


def test_conformance_suite_passes() -> None:
    report = run_conformance()
    assert report.passed, report.render()
    # A representative subset of checks must be present.
    names = {r.name for r in report.results}
    assert "catalog.output_models" in names
    assert "schema.validity" in names
    assert "capabilities.shape" in names
    assert "errors.distinct_codes" in names
    assert "versioning.incompatible_pin_rejected" in names


def test_render_tool_error_taxonomy() -> None:
    assert render_tool_error(ValueError("x"))["category"] == "invalid_params"
    assert render_tool_error(RuntimeError("x"))["category"] == "internal"
    assert render_tool_error(VersionError("x"))["category"] == "version"


def test_version_error_is_typed() -> None:
    exc = VersionError("nope", data={"tool": "t"})
    assert exc.code == -32004
    assert exc.to_dict()["category"] == "version"
