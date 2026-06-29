"""Durable multi-agent **"produce an episode"** orchestration.

Coordinates Kinora's six-agent crew (Adapter, Cinematographer, Critic,
Continuity, Showrunner, Director) as a single long-running, crash-resumable
workflow. It exercises every advanced engine feature on a realistic shape:

* **Child workflows** — each scene is rendered by a child
  :func:`~app.platform.workflows.defs.ingest_render.ingest_render_scene_workflow`,
  so the episode composes the per-scene pipeline rather than re-implementing it.
* **Signals** — a human **director approval** gate: the workflow drafts a scene
  plan, then *waits* (durably, for as long as it takes — a 7-day wait costs
  nothing) for a ``director_decision`` signal of ``approve`` / ``revise`` /
  ``skip``. ``revise`` loops the Showrunner; ``skip`` drops the scene.
* **Queries** — a synchronous ``progress`` query returns live status
  (scenes done / in review / remaining) without mutating the run, for a UI.
* **Durable timers** — an approval **reminder/auto-approve timeout** races the
  signal via :func:`~app.platform.workflows.futures.wait_any`.
* **Continue-as-new** — after a batch of scenes the workflow continues-as-new
  with its carried-forward state, keeping the event history bounded across a
  book-length production.

The Showrunner/Adapter/etc. activities are idempotent simulations here (zero
credits); each is the seam where the real :mod:`app.agents` crew call goes. The
orchestration logic — approval gating, revise loops, scene composition,
history-compaction — is the durable, valuable part and is fully tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.context import WorkflowContext

#: Reuse the ingest/render registries so the episode's child workflow + crew
#: activities resolve from one place (a worker for episodes serves both).
from app.platform.workflows.defs.ingest_render import (  # noqa: E402
    INGEST_RENDER_ACTIVITIES,
    INGEST_RENDER_WORKFLOWS,
)
from app.platform.workflows.futures import wait_any
from app.platform.workflows.registry import activity, workflow
from app.platform.workflows.retry import RetryPolicy

EPISODE_WORKFLOWS = INGEST_RENDER_WORKFLOWS
EPISODE_ACTIVITIES = INGEST_RENDER_ACTIVITIES

_AGENT_RETRY = RetryPolicy(initial_interval_s=1.0, backoff_coefficient=3.0, maximum_attempts=4)

#: How many scenes to produce per run before continue-as-new compacts history.
_SCENES_PER_RUN = 3
#: Director auto-approve timeout (workflow seconds). A real deployment sets this
#: from policy; the durable timer makes a multi-day wait free.
_APPROVAL_TIMEOUT_S = 7 * 24 * 3600

# Signal / query names (also the public protocol the API/client uses).
SIGNAL_DIRECTOR_DECISION = "director_decision"
QUERY_PROGRESS = "progress"


# --------------------------------------------------------------------------- #
# Crew activities — idempotent adapters (simulations in this build).          #
# --------------------------------------------------------------------------- #
@activity(name="crew.adapter_outline", retry_policy=_AGENT_RETRY, registry=EPISODE_ACTIVITIES)
async def adapter_outline(actx: ActivityContext, book_id: str, total_scenes: int) -> dict[str, Any]:
    """Adapter: outline the episode into ``total_scenes`` scene stubs."""
    return {"book_id": book_id, "scenes": list(range(total_scenes))}


@activity(name="crew.showrunner_plan", retry_policy=_AGENT_RETRY, registry=EPISODE_ACTIVITIES)
async def showrunner_plan(
    actx: ActivityContext, book_id: str, scene_index: int, revision: int
) -> dict[str, Any]:
    """Showrunner: draft (or revise) a scene plan for director review."""
    return {
        "scene_index": scene_index,
        "revision": revision,
        "synopsis": f"{book_id} scene {scene_index} (rev {revision})",
    }


@activity(name="crew.continuity_check", retry_policy=_AGENT_RETRY, registry=EPISODE_ACTIVITIES)
async def continuity_check(actx: ActivityContext, scene_index: int) -> dict[str, Any]:
    """Continuity: confirm the scene plan doesn't contradict canon."""
    return {"scene_index": scene_index, "ok": True}


@activity(name="crew.assemble_episode", registry=EPISODE_ACTIVITIES)
async def assemble_episode(
    actx: ActivityContext, book_id: str, scene_manifests: list[dict[str, Any]]
) -> dict[str, Any]:
    """Director/editor: assemble the produced scenes into the final episode."""
    total = sum(int(m.get("accepted", 0)) for m in scene_manifests)
    return {
        "book_id": book_id,
        "scenes": len(scene_manifests),
        "accepted_shots": total,
        "uri": f"episode://{book_id}",
    }


# --------------------------------------------------------------------------- #
# State carried across continue-as-new boundaries.                            #
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EpisodeState:
    """The carried-forward state for the episode production loop."""

    book_id: str
    source_uri: str
    total_scenes: int
    next_scene: int = 0
    completed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "source_uri": self.source_uri,
            "total_scenes": self.total_scenes,
            "next_scene": self.next_scene,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EpisodeState:
        return cls(
            book_id=data["book_id"],
            source_uri=data["source_uri"],
            total_scenes=data["total_scenes"],
            next_scene=data.get("next_scene", 0),
            completed=list(data.get("completed", [])),
        )


@workflow(name="produce_episode", registry=EPISODE_WORKFLOWS)
async def produce_episode_workflow(
    ctx: WorkflowContext,
    book_id: str,
    source_uri: str,
    total_scenes: int = 5,
    carried_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a full episode: per-scene director-gated, child-rendered, compacted."""
    state = (
        EpisodeState.from_dict(carried_state)
        if carried_state is not None
        else EpisodeState(book_id=book_id, source_uri=source_uri, total_scenes=total_scenes)
    )

    # --- live-progress query (registered every task; reads current state) ---
    def _progress() -> dict[str, Any]:
        return {
            "book_id": state.book_id,
            "total_scenes": state.total_scenes,
            "completed": len(state.completed),
            "next_scene": state.next_scene,
            "in_review": _current_review[0],
        }

    _current_review: list[int | None] = [None]  # cell for the query (scene under review)
    ctx.register_query(QUERY_PROGRESS, _progress)

    # First run only: outline the whole episode (idempotent across replays).
    if state.next_scene == 0 and not state.completed:
        await ctx.execute_activity("crew.adapter_outline", state.book_id, state.total_scenes)

    produced_this_run = 0
    while state.next_scene < state.total_scenes and produced_this_run < _SCENES_PER_RUN:
        scene_index = state.next_scene

        # --- Showrunner plan → director approval gate (signal vs. timeout) --
        revision = 0
        decision = "approve"
        while True:
            await ctx.execute_activity("crew.showrunner_plan", state.book_id, scene_index, revision)
            await ctx.execute_activity("crew.continuity_check", scene_index)
            _current_review[0] = scene_index

            decision = await _await_director_decision(ctx)
            if decision in ("approve", "skip"):
                break
            # "revise": loop the Showrunner with a higher revision number.
            revision += 1

        _current_review[0] = None
        if decision == "skip":
            state.next_scene += 1
            continue

        # --- Approved: render the scene via a CHILD workflow ----------------
        child_id = f"{ctx.info.workflow_id}:scene:{scene_index}"
        manifest = await ctx.start_child_workflow(
            "ingest_render_scene",
            state.book_id,
            state.source_uri,
            scene_index,
            child_workflow_id=child_id,
        )
        state.completed.append(manifest)
        state.next_scene += 1
        produced_this_run += 1

    # --- More scenes remain? continue-as-new to compact history ------------
    if state.next_scene < state.total_scenes:
        ctx.continue_as_new(state.book_id, state.source_uri, state.total_scenes, state.to_dict())

    # --- All scenes produced: assemble the episode ----------------------------
    episode = await ctx.execute_activity("crew.assemble_episode", state.book_id, state.completed)
    return {
        "book_id": episode["book_id"],
        "scenes_produced": len(state.completed),
        "accepted_shots": episode["accepted_shots"],
        "uri": episode["uri"],
    }


async def _await_director_decision(ctx: WorkflowContext) -> str:
    """Wait for a director decision signal, racing an auto-approve timeout.

    Returns one of ``approve`` / ``revise`` / ``skip``. The durable timer means
    the workflow can wait days for a human with zero resource cost; if the timer
    wins, the scene auto-approves so production never stalls indefinitely.

    Determinism: :meth:`WorkflowContext.wait_for_signal` already returns the next
    *unconsumed* payload (advancing an internal per-name cursor) and suspends if
    none is delivered yet, so this is replay-stable without any extra bookkeeping.
    The timer race is decided by :func:`wait_any`, which selects the lowest
    command seq among resolved futures (never wall-clock arrival).
    """
    # Arm the timer unconditionally *before* resolving the signal, so the
    # ``StartTimer`` command is emitted on the first pass through this point and
    # re-emitted identically on every replay (a conditional timer would make the
    # command stream depend on signal-arrival timing → non-determinism). The
    # signal future is then resolved against history; if it's already delivered
    # it wins the race, otherwise the workflow parks until the signal or the
    # timer fires.
    timeout_future = ctx.start_timer(_APPROVAL_TIMEOUT_S)
    signal_future = ctx.wait_for_signal(SIGNAL_DIRECTOR_DECISION)
    index, value = await wait_any(signal_future, timeout_future)
    if index == 0:
        return _normalise(value)
    return "approve"  # auto-approve on timeout


def _normalise(decision: Any) -> str:
    if isinstance(decision, dict):
        decision = decision.get("decision", "approve")
    text = str(decision).lower()
    return text if text in ("approve", "revise", "skip") else "approve"


__all__ = [
    "EPISODE_ACTIVITIES",
    "EPISODE_WORKFLOWS",
    "QUERY_PROGRESS",
    "SIGNAL_DIRECTOR_DECISION",
    "EpisodeState",
    "produce_episode_workflow",
]
