"""The Kinora service catalog — the ~40 packages named as logical services.

This is where the abstract mesh meets the concrete system: it declares a
:class:`ServiceContract` for each backend capability that *could* become an
independently-deployable service (kinora.md §6: "every agent is an independently
deployable service"). The contracts mirror the real method surfaces — the six
agents, the memory/canon MCP, the scheduler control plane, the budget service,
search, render — so a caller can address any of them through the mesh today,
in-process, with the full resilience stack.

The catalog uses lightweight **dataclass** request/response types declared *here*,
not the packages' heavy pydantic DTOs, on purpose: importing this module must stay
cheap and must not drag in the agent crew / DashScope clients (the layer is
additive and infra-free). When a service is genuinely split out, its contract here
is swapped for one that imports the package's real DTOs — a localized change.

The per-method ``idempotent`` flags encode real Kinora semantics: a shot render
keyed by ``shot_hash`` is idempotent (§12.1 — re-enqueue is a no-op), a canon read
is idempotent, but a canon *write* (a continuity edit) is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.distributed.rpc.contracts import ServiceContract, method

# --------------------------------------------------------------------------- #
# Logical service names (the addressable surface). Use these constants so a
# typo'd name fails at import, not at call time.
# --------------------------------------------------------------------------- #

SHOWRUNNER = "showrunner"
ADAPTER = "adapter"
CONTINUITY = "continuity"
CINEMATOGRAPHER = "cinematographer"
GENERATOR = "generator"
CRITIC = "critic"
MEMORY = "memory"
SCHEDULER = "scheduler"
BUDGET = "budget"
SEARCH = "search"
RENDER = "render"


# --------------------------------------------------------------------------- #
# Shared lightweight DTOs (catalog-local; do not import package internals).
# --------------------------------------------------------------------------- #


@dataclass
class ShotRef:
    """Identifies a shot to design/render (keyed by the §8.7 content hash)."""

    shot_hash: str
    scene_id: str = ""
    beat_id: str = ""


@dataclass
class ShotSpecResult:
    """A designed shot spec (opaque dict body; the real type lives in agents)."""

    spec: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenderResult:
    """The outcome of a shot render (or degradation)."""

    shot_hash: str
    status: str = "queued"
    clip_uri: str | None = None
    mode: str = ""


@dataclass
class CanonQuery:
    """A canon-graph retrieval query (entity + version)."""

    entity: str
    version: int | None = None
    limit: int = 8


@dataclass
class CanonResult:
    """Retrieved canon facts (opaque list body)."""

    facts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CanonWrite:
    """A canon mutation (a continuity edit; NON-idempotent)."""

    entity: str
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass
class BudgetReserveReq:
    """Reserve video-seconds against the budget for a shot."""

    shot_hash: str
    seconds: float


@dataclass
class BudgetReserveResp:
    """The reservation outcome (granted + remaining)."""

    granted: bool
    remaining_seconds: float = 0.0


@dataclass
class SchedulerIntent:
    """A reader intent / seek event the Scheduler reacts to (§4)."""

    session_id: str
    playhead: float
    velocity: float = 0.0
    seek: bool = False


@dataclass
class SchedulerAck:
    """The Scheduler's acknowledgement (what it decided to do)."""

    accepted: bool = True
    promoted: int = 0
    cancelled: int = 0


@dataclass
class SearchQuery:
    """A library / canon search query."""

    text: str
    limit: int = 10


@dataclass
class SearchResults:
    """Search hits (opaque list body)."""

    hits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QaReq:
    """A Critic QA request for a rendered shot."""

    shot_hash: str
    clip_uri: str = ""


@dataclass
class QaResult:
    """The Critic's verdict (§9.5 thresholds)."""

    passed: bool
    ccs: float = 0.0
    style_drift: float = 0.0
    motion: float = 0.0


# --------------------------------------------------------------------------- #
# The contracts.
# --------------------------------------------------------------------------- #


def cinematographer_contract() -> ServiceContract:
    """The Cinematographer: shot design + keyframe planning (§9.2)."""
    return ServiceContract.define(
        CINEMATOGRAPHER,
        version=1,
        description="Shot design + keyframes (Qwen-VL).",
        methods=[
            method(
                "plan_shot",
                ShotRef,
                ShotSpecResult,
                idempotent=True,
                description="Design a shot spec for a beat (cacheable by shot_hash).",
            ),
        ],
    )


def generator_contract() -> ServiceContract:
    """The Generator: enqueue/produce a shot's footage (§9.6, §12.1)."""
    return ServiceContract.define(
        GENERATOR,
        version=1,
        description="Wan/CosyVoice generation + degradation ladder.",
        methods=[
            method(
                "render_shot",
                ShotRef,
                RenderResult,
                idempotent=True,
                description="Render a shot; idempotent on shot_hash (re-enqueue = no-op).",
            ),
        ],
    )


def critic_contract() -> ServiceContract:
    """The Critic: QA a rendered shot against the §9.5 thresholds."""
    return ServiceContract.define(
        CRITIC,
        version=1,
        description="QA / self-correcting loop (Qwen3-VL).",
        methods=[
            method("qa_shot", QaReq, QaResult, idempotent=True),
        ],
    )


def memory_contract() -> ServiceContract:
    """The canon MCP memory server: read (idempotent) + write (not) (§8)."""
    return ServiceContract.define(
        MEMORY,
        version=1,
        description="Canon graph + episodic store (the MCP canon server).",
        methods=[
            method("query_canon", CanonQuery, CanonResult, idempotent=True),
            method(
                "write_canon",
                CanonWrite,
                CanonResult,
                idempotent=False,
                description="A continuity edit — NOT safe to retry/hedge blindly.",
            ),
        ],
    )


def budget_contract() -> ServiceContract:
    """The budget service: reserve / release video-seconds (§11.1)."""
    return ServiceContract.define(
        BUDGET,
        version=1,
        description="Video-seconds budget (reserve / remaining / guardrails).",
        methods=[
            method(
                "reserve",
                BudgetReserveReq,
                BudgetReserveResp,
                idempotent=True,
                description="Reserve seconds for a shot (idempotent on shot_hash).",
            ),
        ],
    )


def scheduler_contract() -> ServiceContract:
    """The Scheduler control plane: react to reader intent / seeks (§4.9)."""
    return ServiceContract.define(
        SCHEDULER,
        version=1,
        description="Prefetch controller: watermark buffer, promotion, cancel.",
        methods=[
            method(
                "on_intent",
                SchedulerIntent,
                SchedulerAck,
                idempotent=False,
                description="A debounced reader intent / seek (drives promotion+cancel).",
            ),
        ],
    )


def search_contract() -> ServiceContract:
    """The search service: library + canon search (read-only)."""
    return ServiceContract.define(
        SEARCH,
        version=1,
        description="Library / canon search.",
        methods=[
            method("search", SearchQuery, SearchResults, idempotent=True),
        ],
    )


def all_contracts() -> dict[str, ServiceContract]:
    """Every catalog contract by service name (for bulk registration / docs)."""
    return {
        c.name: c
        for c in (
            cinematographer_contract(),
            generator_contract(),
            critic_contract(),
            memory_contract(),
            budget_contract(),
            scheduler_contract(),
            search_contract(),
        )
    }


__all__ = [
    "ADAPTER",
    "BUDGET",
    "CINEMATOGRAPHER",
    "CONTINUITY",
    "CRITIC",
    "GENERATOR",
    "MEMORY",
    "RENDER",
    "SCHEDULER",
    "SEARCH",
    "SHOWRUNNER",
    "BudgetReserveReq",
    "BudgetReserveResp",
    "CanonQuery",
    "CanonResult",
    "CanonWrite",
    "QaReq",
    "QaResult",
    "RenderResult",
    "SchedulerAck",
    "SchedulerIntent",
    "SearchQuery",
    "SearchResults",
    "ShotRef",
    "ShotSpecResult",
    "all_contracts",
    "budget_contract",
    "cinematographer_contract",
    "critic_contract",
    "generator_contract",
    "memory_contract",
    "scheduler_contract",
    "search_contract",
]
