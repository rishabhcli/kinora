"""Durable **book-ingest → render-whole-scene** workflow (kinora.md §9.7 / §12).

This is the long-running pipeline expressed as deterministic, crash-resumable
workflow code. The orchestration — *not* the model calls — is the valuable,
hard-to-get-right part, and the engine makes it durable for free: every stage
transition is an event in history, so a crash mid-render resumes at the exact
shot it was on, never re-spends the video budget on an already-accepted shot
(idempotency key = ``shot_hash``, §12.1), and never loses ingest progress.

Shape:

1. **Phase-A ingest** (`ingest_book`): extract → analyze → canon-build →
   identity-lock, each a retried activity. The result is a shot list for the
   target scene.
2. **Per-shot state machine** (§9.7) for every shot in the scene, run with bounded
   concurrency: ``cache-check`` → (hit ⇒ accept, 0 video-s) | (miss ⇒
   budget-gate → render → QA). A failed QA drives ``repair`` (regen, retry ≤ 2);
   exhausted retries **degrade** to a Ken-Burns fallback and log a defect — the
   film never hard-stops (§12.4). Promotion to live render is gated on the budget
   activity, which returns 0 remaining when ``KINORA_LIVE_VIDEO`` is off, so the
   workflow rides the degradation ladder end-to-end without spending credits.
3. **Stitch** the accepted/degraded shots into the scene render and return a
   manifest.

The whole body is deterministic: all randomness, time, and I/O go through the
context, and the per-shot loop is driven by the (deterministic) shot list, so a
replay reconstructs the identical command stream. The activities here are
idempotent simulations; each is the seam where the real
:mod:`app.ingest`/:mod:`app.render` service call goes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.errors import ActivityFailure
from app.platform.workflows.registry import ActivityRegistry, WorkflowRegistry, activity, workflow
from app.platform.workflows.retry import RetryPolicy

#: Dedicated registries so importing the concrete workflows doesn't collide with
#: the process-wide defaults other code/tests register into.
INGEST_RENDER_WORKFLOWS = WorkflowRegistry()
INGEST_RENDER_ACTIVITIES = ActivityRegistry()

# Retry shapes mirroring §12.1's (2s, 8s, 30s) ladder for flaky provider calls.
_INGEST_RETRY = RetryPolicy(initial_interval_s=2.0, backoff_coefficient=4.0, maximum_attempts=4)
_RENDER_RETRY = RetryPolicy(initial_interval_s=2.0, backoff_coefficient=4.0, maximum_attempts=3)


# --------------------------------------------------------------------------- #
# Activities — idempotent adapters (self-contained simulations in this build). #
# --------------------------------------------------------------------------- #
@activity(name="ingest.extract", retry_policy=_INGEST_RETRY, registry=INGEST_RENDER_ACTIVITIES)
async def extract_pages(actx: ActivityContext, book_id: str, source_uri: str) -> dict[str, Any]:
    """Extract page text/illustrations from the source PDF (Phase-A step 1)."""
    await actx.heartbeat({"stage": "extract", "book_id": book_id})
    page_count = _deterministic_page_count(book_id)
    return {"book_id": book_id, "page_count": page_count, "source_uri": source_uri}


@activity(name="ingest.analyze", retry_policy=_INGEST_RETRY, registry=INGEST_RENDER_ACTIVITIES)
async def analyze_pages(actx: ActivityContext, book_id: str, page_count: int) -> dict[str, Any]:
    """Adapter pass: pages → beats → shots (the §9.x adapter contract)."""
    await actx.heartbeat({"stage": "analyze"})
    # ~3 beats/page, ~2 shots/beat — deterministic from the book id for replay.
    beats = max(1, page_count) * 3
    return {"beats": beats, "shots": beats * 2}


@activity(name="ingest.build_canon", retry_policy=_INGEST_RETRY, registry=INGEST_RENDER_ACTIVITIES)
async def build_canon(actx: ActivityContext, book_id: str, beats: int) -> dict[str, Any]:
    """Build the versioned canon (entities, locations, style) from the beats."""
    return {"canon_version": 1, "entities": min(beats, 12)}


@activity(
    name="ingest.lock_identity", retry_policy=_INGEST_RETRY, registry=INGEST_RENDER_ACTIVITIES
)
async def lock_identity(actx: ActivityContext, book_id: str, entities: int) -> dict[str, Any]:
    """Lock reference images for each resolvable character (identity-lock)."""
    await actx.heartbeat({"stage": "identity", "entities": entities})
    return {"locked_refs": entities}


@activity(name="render.plan_scene", registry=INGEST_RENDER_ACTIVITIES)
async def plan_scene(actx: ActivityContext, book_id: str, scene_index: int) -> dict[str, Any]:
    """Resolve the shot list for one scene (the unit this workflow renders)."""
    shot_count = _deterministic_scene_shot_count(book_id, scene_index)
    shots = [f"{book_id}:s{scene_index}:shot{i:03d}" for i in range(shot_count)]
    return {"scene_index": scene_index, "shots": shots}


@activity(name="render.budget_gate", registry=INGEST_RENDER_ACTIVITIES)
async def budget_gate(actx: ActivityContext, shot_id: str) -> dict[str, Any]:
    """Reserve video-seconds for a shot. Returns ``can_render`` per the budget.

    With ``KINORA_LIVE_VIDEO`` off the budget yields 0 remaining, so the workflow
    takes the degradation lane — exactly the §12 off-gate behaviour, no credits.
    """
    import os

    live = os.environ.get("KINORA_LIVE_VIDEO", "").lower() in {"1", "true", "yes"}
    return {"can_render": live, "reserved_s": 5 if live else 0}


@activity(name="render.cache_check", registry=INGEST_RENDER_ACTIVITIES)
async def cache_check(actx: ActivityContext, shot_id: str, shot_hash: str) -> dict[str, Any]:
    """Shot-cache lookup keyed on ``shot_hash`` (§12.3). Hit ⇒ 0 video-seconds."""
    # Deterministic: a fixed fraction of shots are cache hits (re-reads).
    hit = (hash(shot_hash) % 5) == 0
    return {"hit": hit, "uri": f"cache://{shot_hash}" if hit else None}


@activity(name="render.render_shot", retry_policy=_RENDER_RETRY, registry=INGEST_RENDER_ACTIVITIES)
async def render_shot(actx: ActivityContext, shot_id: str, mode: str) -> dict[str, Any]:
    """Render one shot (Wan clip / Ken-Burns). Heartbeats during the long task."""
    await actx.heartbeat({"shot_id": shot_id, "mode": mode, "progress": 0.5})
    return {"shot_id": shot_id, "mode": mode, "uri": f"render://{shot_id}/{mode}"}


@activity(name="render.qa", registry=INGEST_RENDER_ACTIVITIES)
async def qa_shot(actx: ActivityContext, shot_id: str, attempt: int) -> dict[str, Any]:
    """Critic QA against canon (§9.5). Deterministic verdict for replay stability."""
    # First attempt fails for a deterministic subset (drives the repair lane);
    # any retry passes — proving the repair → regen → accept path durably.
    passes = attempt >= 2 or (hash(shot_id) % 4) != 0
    return {"pass": passes, "score": 0.9 if passes else 0.4}


@activity(name="render.degrade", registry=INGEST_RENDER_ACTIVITIES)
async def degrade_shot(actx: ActivityContext, shot_id: str) -> dict[str, Any]:
    """Ken-Burns degradation fallback + defect log (§12.4) — never hard-stop."""
    return {"shot_id": shot_id, "mode": "ken_burns", "degraded": True}


@activity(name="render.stitch", registry=INGEST_RENDER_ACTIVITIES)
async def stitch_scene(
    actx: ActivityContext, scene_index: int, shot_uris: list[str]
) -> dict[str, Any]:
    """Stitch accepted/degraded shots into the scene render manifest."""
    return {
        "scene_index": scene_index,
        "shot_count": len(shot_uris),
        "uri": f"scene://{scene_index}",
    }


# --------------------------------------------------------------------------- #
# Workflows.                                                                   #
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SceneRenderResult:
    """The structured result of rendering one scene."""

    scene_index: int
    accepted: int
    degraded: int
    cache_hits: int
    manifest_uri: str


@workflow(name="ingest_render_scene", registry=INGEST_RENDER_WORKFLOWS)
async def ingest_render_scene_workflow(
    ctx: WorkflowContext, book_id: str, source_uri: str, scene_index: int = 0
) -> dict[str, Any]:
    """Ingest a book (Phase A) then durably render one whole scene (§9.7)."""
    # ---- Phase A: ingest (each step retried; survives crashes) -------------
    extracted = await ctx.execute_activity("ingest.extract", book_id, source_uri)
    analyzed = await ctx.execute_activity("ingest.analyze", book_id, extracted["page_count"])
    canon = await ctx.execute_activity("ingest.build_canon", book_id, analyzed["beats"])
    await ctx.execute_activity("ingest.lock_identity", book_id, canon["entities"])

    # ---- Resolve the scene's shot list -------------------------------------
    plan = await ctx.execute_activity("render.plan_scene", book_id, scene_index)
    shots: list[str] = plan["shots"]

    # ---- Per-shot state machine (§9.7) for each shot in the scene ----------
    # Each shot is driven through cache-check → budget-gate → render → QA →
    # repair/degrade. The *worker* provides the §12.2 render-lane concurrency
    # (multiple activity workers drain in parallel); the workflow body stays a
    # deterministic, replay-stable sequence of activity calls per shot.
    accepted_uris: list[str] = []
    accepted = degraded = cache_hits = 0
    for shot_id in shots:
        outcome = await _render_one_shot(ctx, shot_id)
        accepted_uris.append(outcome["uri"])
        if outcome["status"] == "cache_hit":
            cache_hits += 1
            accepted += 1
        elif outcome["status"] == "accepted":
            accepted += 1
        else:
            degraded += 1

    # ---- Stitch the scene --------------------------------------------------
    manifest = await ctx.execute_activity("render.stitch", scene_index, accepted_uris)
    result = SceneRenderResult(
        scene_index=scene_index,
        accepted=accepted,
        degraded=degraded,
        cache_hits=cache_hits,
        manifest_uri=manifest["uri"],
    )
    return {
        "scene_index": result.scene_index,
        "accepted": result.accepted,
        "degraded": result.degraded,
        "cache_hits": result.cache_hits,
        "manifest_uri": result.manifest_uri,
        "shots_total": len(shots),
    }


async def _render_one_shot(ctx: WorkflowContext, shot_id: str) -> dict[str, Any]:
    """The §9.7 per-shot state machine as a durable sub-routine.

    Returns ``{"status": "cache_hit"|"accepted"|"degraded", "uri": ...}``. This is
    a plain async helper (not its own workflow): it runs inside the parent's
    deterministic context, so its activity calls draw seqs from the same counter
    and replay deterministically alongside the rest of the body.
    """
    shot_hash = f"{shot_id}@v1"
    cache = await ctx.execute_activity("render.cache_check", shot_id, shot_hash)
    if cache["hit"]:
        return {"status": "cache_hit", "uri": cache["uri"]}

    gate = await ctx.execute_activity("render.budget_gate", shot_id)
    mode = "wan" if gate["can_render"] else "ken_burns"

    # Render + QA with the repair loop (retry ≤ 2), then degrade.
    for attempt in range(1, 3):
        try:
            render = await ctx.execute_activity("render.render_shot", shot_id, mode)
        except ActivityFailure:
            break  # render itself exhausted retries → degrade
        qa = await ctx.execute_activity("render.qa", shot_id, attempt)
        if qa["pass"]:
            return {"status": "accepted", "uri": render["uri"]}
        # QA failed → repair (regen) on the next attempt.
    degraded = await ctx.execute_activity("render.degrade", shot_id)
    return {"status": "degraded", "uri": f"render://{degraded['shot_id']}/ken_burns"}


def _deterministic_page_count(book_id: str) -> int:
    return 4 + (abs(hash(book_id)) % 5)  # 4–8 pages (small, for fast tests)


def _deterministic_scene_shot_count(book_id: str, scene_index: int) -> int:
    return 3 + (abs(hash(f"{book_id}:{scene_index}")) % 4)  # 3–6 shots


__all__ = [
    "INGEST_RENDER_ACTIVITIES",
    "INGEST_RENDER_WORKFLOWS",
    "SceneRenderResult",
    "ingest_render_scene_workflow",
]
