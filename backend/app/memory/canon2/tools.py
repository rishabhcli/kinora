"""canon2 MCP tool surface — additive, routed through the same dispatch contract.

These tools *deepen* the canon memory (versioned revisions + diffs + time-travel,
§7.2 conflict resolution, hybrid retrieval, consistency audit) and are exposed as
MCP-style tools under the ``canon2.*`` namespace. They reuse the exact
:class:`~app.mcp.tools.ToolDef` shape and the same ``dispatch`` contract — validate
``arguments`` into the tool's input model, route to a handler — so the MCP server,
the Qwen-skill dispatcher, and the tool catalog all keep working unchanged.

**Additive by construction.** :class:`Canon2Tools` has its own ``dispatch`` over its
own :data:`CANON2_TOOL_DEFS`; it never touches :data:`app.mcp.tools.TOOL_DEFS`. To
let the *existing* :class:`~app.mcp.tools.MemoryTools.dispatch` also serve the
canon2 tools (the single-execution-path the task asks for) without rewriting it,
:func:`mount_on` returns a thin delegating dispatcher that tries the original tools
first and falls back to canon2 — leaving every existing tool contract intact.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from app.memory.canon2 import schemas
from app.memory.canon2.conflict import Proposal
from app.memory.canon2.store import Canon2Store
from app.memory.canon2.versioning import Provenance
from app.memory.interfaces import Embedder


@dataclass(frozen=True, slots=True)
class Canon2ToolDef:
    """One registered canon2 tool (mirrors :class:`app.mcp.tools.ToolDef`)."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: str


class Canon2Tools:
    """The ``canon2.*`` tool surface over an in-memory :class:`Canon2Store`.

    Construct with an :class:`Embedder` (the real provider in prod, a seeded fake
    in tests). State lives in the store, so one ``Canon2Tools`` is a session-like
    handle over one canon — inject a shared instance to share canon across calls.
    """

    def __init__(self, *, embedder: Embedder, store: Canon2Store | None = None) -> None:
        self._store = store or Canon2Store(embedder)

    @property
    def store(self) -> Canon2Store:
        return self._store

    # --- canon2.* ----------------------------------------------------------- #

    async def upsert_entity(
        self, inp: schemas.Canon2UpsertEntityInput
    ) -> schemas.Revision:
        return await self._store.upsert_entity(
            book_id=inp.book_id,
            entity_key=inp.entity_key,
            kind=inp.type,
            name=inp.name,
            valid_from_beat=inp.valid_from_beat,
            branch=inp.branch,
            description=inp.description,
            aliases=inp.aliases,
            appearance=inp.appearance,
            voice=inp.voice,
            style_tokens=inp.style_tokens,
            provenance=Provenance(
                actor_id=inp.actor_id,
                reason=inp.reason,
                source_span=inp.source_span,
                proposed_by=inp.proposed_by,
            ),
        )

    async def get_entity(
        self, inp: schemas.Canon2GetEntityInput
    ) -> schemas.Canon2GetEntityOutput:
        rev = self._store.get_entity(
            book_id=inp.book_id,
            entity_key=inp.entity_key,
            branch=inp.branch,
            at_beat=inp.at_beat,
            as_of_tx=inp.as_of_tx,
        )
        return schemas.Canon2GetEntityOutput(found=rev is not None, revision=rev)

    async def history(
        self, inp: schemas.Canon2HistoryInput
    ) -> schemas.Canon2HistoryOutput:
        hist = self._store.history(
            book_id=inp.book_id, entity_key=inp.entity_key, branch=inp.branch
        )
        return schemas.Canon2HistoryOutput(found=hist is not None, history=hist)

    async def propose_fact(
        self, inp: schemas.Canon2ProposeFactInput
    ) -> schemas.Resolution:
        proposal = Proposal(
            subject=inp.subject,
            predicate=inp.predicate,
            object_value=inp.object_value,
            actor_id=inp.actor_id,
            wall_ms=inp.wall_ms,
            counter=inp.counter,
            source_span=inp.source_span,
            user_directed=inp.user_directed,
            reason=inp.reason,
        )
        return await self._store.propose_fact(
            book_id=inp.book_id,
            proposal=proposal,
            branch=inp.branch,
            valid_from_beat=inp.valid_from_beat,
            current_beat=inp.current_beat,
        )

    async def conflicts(
        self, inp: schemas.Canon2ConflictsInput
    ) -> schemas.Canon2ConflictsOutput:
        return schemas.Canon2ConflictsOutput(
            conflicts=self._store.list_conflicts(
                book_id=inp.book_id,
                branch=inp.branch,
                include_resolved=inp.include_resolved,
            )
        )

    async def resolve_conflict(
        self, inp: schemas.Canon2ResolveConflictInput
    ) -> schemas.FlaggedConflict:
        return await self._store.resolve_conflict(
            book_id=inp.book_id,
            conflict_id=inp.conflict_id,
            chosen_object=inp.chosen_object,
            branch=inp.branch,
            resolved_by=inp.resolved_by,
            reasoning=inp.reasoning,
            valid_from_beat=inp.valid_from_beat,
        )

    async def retrieve(
        self, inp: schemas.Canon2RetrieveInput
    ) -> schemas.Canon2RetrieveOutput:
        results = await self._store.retrieve(
            book_id=inp.book_id,
            query=inp.query,
            branch=inp.branch,
            k=inp.k,
            lambda_=inp.lambda_,
        )
        return schemas.Canon2RetrieveOutput(results=results)

    async def audit(self, inp: schemas.Canon2AuditInput) -> schemas.AuditReport:
        return self._store.audit(
            book_id=inp.book_id,
            branch=inp.branch,
            mutually_exclusive=list(inp.mutually_exclusive),
        )

    # --- dispatch (same contract as MemoryTools.dispatch) ------------------- #

    async def dispatch(self, name: str, arguments: dict[str, object]) -> BaseModel:
        """Validate ``arguments`` into the tool's input model and run the handler.

        Identical contract to :meth:`app.mcp.tools.MemoryTools.dispatch`, so the
        MCP server / skill dispatcher can route a ``canon2.*`` call here unchanged.
        """
        defn = CANON2_TOOLS_BY_NAME.get(name)
        if defn is None:
            raise ValueError(f"unknown tool: {name}")
        model = defn.input_model.model_validate(arguments)
        handler = getattr(self, defn.handler)
        result: BaseModel = await handler(model)
        return result


#: The canon2 tool surface (read order: write → read/time-travel → conflict → recall → audit).
CANON2_TOOL_DEFS: list[Canon2ToolDef] = [
    Canon2ToolDef(
        "canon2.upsert_entity",
        "Append a new immutable revision of a canon entity (character/location/"
        "prop/style) carrying who/what/when provenance and the field-level diff "
        "against the prior version. Append-only: prior revisions survive for "
        "time-travel and audit.",
        schemas.Canon2UpsertEntityInput,
        "upsert_entity",
    ),
    Canon2ToolDef(
        "canon2.get_entity",
        "Time-travel read: resolve a canon entity as of a story beat (the canon as "
        "of page N) or as the canon believed it at a transaction instant.",
        schemas.Canon2GetEntityInput,
        "get_entity",
    ),
    Canon2ToolDef(
        "canon2.history",
        "The full append-only revision log of one entity — every change, its diff, "
        "and who/what/when changed it (the §8.1 canon editor's change history).",
        schemas.Canon2HistoryInput,
        "history",
    ),
    Canon2ToolDef(
        "canon2.propose_fact",
        "Propose a canon fact; if it contradicts the current belief, resolve it "
        "deterministically under the §7.2 policy (grounded-wins → evolve, "
        "user-facing/ambiguous → flag for arbitration, else last-writer-wins).",
        schemas.Canon2ProposeFactInput,
        "propose_fact",
    ),
    Canon2ToolDef(
        "canon2.conflicts",
        "The flagged-conflict queue: canon disputes the auto-policy could not "
        "resolve, awaiting Showrunner/director arbitration (§7.2).",
        schemas.Canon2ConflictsInput,
        "conflicts",
    ),
    Canon2ToolDef(
        "canon2.resolve_conflict",
        "Close a queued conflict with an arbitration choice and apply the chosen "
        "object as the active canon fact (the §7.2 decision record).",
        schemas.Canon2ResolveConflictInput,
        "resolve_conflict",
    ),
    Canon2ToolDef(
        "canon2.retrieve",
        "Hybrid keyword+vector recall over the book's canon facts (pluggable "
        "embedder), MMR-reranked for relevance + diversity and deduped — the "
        "scalable §8.4 retrieval slice, never the whole book.",
        schemas.Canon2RetrieveInput,
        "retrieve",
    ),
    Canon2ToolDef(
        "canon2.audit",
        "Consistency sweep over the accumulated canon: contradictions (§9.5), "
        "unexplained appearance/style drift, dangling references, and unresolved "
        "conflicts — the canon's self-consistency report.",
        schemas.Canon2AuditInput,
        "audit",
    ),
]

#: Name -> definition for O(1) dispatch.
CANON2_TOOLS_BY_NAME: dict[str, Canon2ToolDef] = {d.name: d for d in CANON2_TOOL_DEFS}


# --------------------------------------------------------------------------- #
# Additive integration with the existing MemoryTools.dispatch
# --------------------------------------------------------------------------- #

DispatchFn = Callable[[str, dict[str, object]], Awaitable[BaseModel]]


class MergedDispatcher:
    """A dispatcher that serves the existing tools first, then the canon2 tools.

    Wraps an existing ``MemoryTools``-shaped dispatcher and a :class:`Canon2Tools`
    so a single ``dispatch(name, arguments)`` call routes ``canon2.*`` to canon2
    and everything else to the original — without modifying either tool table.
    The MCP server only needs an object with a ``dispatch`` coroutine (see
    :class:`app.mcp.server.ToolDispatcher`), so this drops in unchanged.
    """

    def __init__(self, base: object, canon2: Canon2Tools) -> None:
        base_dispatch = getattr(base, "dispatch", None)
        if base_dispatch is None or not callable(base_dispatch):
            raise TypeError("base must expose an async dispatch(name, arguments)")
        self._base: DispatchFn = base_dispatch
        self._canon2 = canon2

    async def dispatch(self, name: str, arguments: dict[str, object]) -> BaseModel:
        if name in CANON2_TOOLS_BY_NAME:
            return await self._canon2.dispatch(name, arguments)
        return await self._base(name, arguments)


def mount_on(
    base: object, *, embedder: Embedder, store: Canon2Store | None = None
) -> MergedDispatcher:
    """Return a :class:`MergedDispatcher` that adds the canon2 tools to ``base``.

    ``base`` is any object exposing the ``dispatch(name, arguments)`` coroutine
    (e.g. an :class:`app.mcp.tools.MemoryTools`). Existing tools are untouched;
    ``canon2.*`` names route to a fresh :class:`Canon2Tools` over an in-memory
    store (or the shared ``store`` you pass).
    """
    return MergedDispatcher(base, Canon2Tools(embedder=embedder, store=store))


__all__ = [
    "CANON2_TOOLS_BY_NAME",
    "CANON2_TOOL_DEFS",
    "Canon2ToolDef",
    "Canon2Tools",
    "MergedDispatcher",
    "mount_on",
]
