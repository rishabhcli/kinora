"""The render → QA → conflict → degrade saga (kinora.md §9.7, §7.2, §12).

The per-shot render is the other genuinely multi-step, failure-prone flow. The
§9.7 state machine — ``CacheCheck → Rendering → QA → (Accepted | Repair → {regen,
Conflict, Degraded})`` — maps onto a saga whose steps reserve scarce video-budget,
render the clip, QA it against the canon, and, on a canon contradiction, run the
§7.2 Showrunner arbitration before either regenerating, evolving the canon, or
degrading to the Ken-Burns ladder. Modelling it as a saga gives the loop the same
durable crash-resume + compensation guarantees as ingest: a worker that dies after
reserving budget but before rendering must release that reservation, never
double-spend it.

The five steps (forward action → compensation):

1. ``cache_check`` — hit short-circuits to accepted (0 video-s) → none (read-only).
2. ``reserve`` — ``budget.reserve(seconds)`` (skip on hit) → ``budget.release``.
3. ``render`` — produce the clip (or the degrade ladder) → discard the artifact.
4. ``qa`` — Critic scores vs canon; a conflict routes to §7.2 → none (read-only).
5. ``accept`` — log episodic + cache + last-frame → canon → evict + revert frame.

The QA step is where the policy lives. When the Critic raises a **structured canon
conflict**, the step applies the §7.2 resolution policy as a pure function
(:func:`arbitrate`): *evolve_canon* if the source span supports it, else
*surface_to_user* if a director is present, else *honor_canon*. ``honor_canon`` and
``evolve_canon`` both demand a re-render — surfaced here as a *retryable* step
failure so the engine regenerates within the shot's retry budget (§9.7 "regen,
retry ≤ 2"); exhausting that budget drops the shot to the **degrade** ladder rather
than failing the saga (the film never hard-stops, §12.4). ``surface_to_user`` parks
the shot awaiting the reader's pick.

``KINORA_LIVE_VIDEO`` is irrelevant here: the fake render port produces a
Ken-Burns-style placeholder clip id and spends zero credits, exactly as the
off-gate production path does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.definition import SagaDefinition, saga, step
from app.distributed.sagas.types import SagaContext, StepFailed, StepResult


class ArbitrationDecision(StrEnum):
    """The §7.2 Showrunner resolution options for a structured canon conflict."""

    HONOR_CANON = "honor_canon"  # respect established truth → regenerate
    EVOLVE_CANON = "evolve_canon"  # the story changed (textual support) → update + regen
    SURFACE_TO_USER = "surface_to_user"  # ask the director to choose


@dataclass(frozen=True, slots=True)
class Conflict:
    """A structured canon conflict raised by the Critic (kinora.md §7.2)."""

    conflict_id: str
    shot_id: str
    claim: str
    canon_fact: str
    has_textual_support: bool
    user_facing: bool


def arbitrate(conflict: Conflict, *, director_present: bool) -> ArbitrationDecision:
    """Apply the §7.2 resolution policy as a pure function.

    ``evolve_canon`` only with textual support in the source span; else surface to
    the director if a director is present and the conflict is user-facing; else the
    safe default — honor the established canon. This is the deterministic policy the
    spec pins (the Showrunner's reasoning is logged separately).
    """
    if conflict.has_textual_support:
        return ArbitrationDecision.EVOLVE_CANON
    if director_present and conflict.user_facing:
        return ArbitrationDecision.SURFACE_TO_USER
    return ArbitrationDecision.HONOR_CANON


@runtime_checkable
class RenderPorts(Protocol):
    """The per-shot operations the render saga orchestrates.

    Implementations: the production adapter over the budget service / render
    pipeline / Critic / canon + cache, and
    :class:`~app.distributed.sagas.flows.fakes.FakeRenderServices`.
    """

    async def cache_lookup(self, shot_hash: str) -> str | None:
        """Return the cached clip id for ``shot_hash`` (``None`` on miss)."""
        ...

    async def reserve_budget(self, shot_id: str, seconds: float) -> str:
        """Reserve ``seconds`` of video budget; return the reservation id."""
        ...

    async def release_budget(self, reservation_id: str) -> None:
        """Release a budget reservation (compensation for :meth:`reserve_budget`)."""
        ...

    async def render_clip(self, shot_id: str, *, degraded: bool) -> str:
        """Render the clip (full or degraded ladder); return the clip id."""
        ...

    async def discard_clip(self, clip_id: str) -> None:
        """Discard a rendered artifact (compensation for :meth:`render_clip`)."""
        ...

    async def qa_clip(self, shot_id: str, clip_id: str) -> Conflict | None:
        """Critic QA vs canon; return a :class:`Conflict` on failure, else ``None``."""
        ...

    async def is_director_present(self, shot_id: str) -> bool:
        """Whether a director is in the session (gates ``surface_to_user``)."""
        ...

    async def evolve_canon(self, shot_id: str, conflict: Conflict) -> None:
        """Evolve the canon to match the story (the §7.2 ``evolve_canon`` path)."""
        ...

    async def accept_shot(self, shot_id: str, clip_id: str) -> None:
        """Log episodic + cache + push last frame to canon (the §9.7 accept)."""
        ...

    async def unaccept_shot(self, shot_id: str, clip_id: str) -> None:
        """Evict cache + revert the canon last-frame (compensation for accept)."""
        ...


def _ports(ctx: SagaContext) -> RenderPorts:
    ports = ctx.resource("render_ports")
    if ports is None:
        raise StepFailed("render_ports resource not wired", retryable=False)
    return ports


# --------------------------------------------------------------------------- #
# Step 1 — cache check (read-only; no compensation)
# --------------------------------------------------------------------------- #
async def _cache_check(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    clip_id = await ports.cache_lookup(ctx.state["shot_hash"])
    if clip_id is not None:
        return StepResult.ok(cache_hit=True, clip_id=clip_id)
    return StepResult.ok(cache_hit=False)


# --------------------------------------------------------------------------- #
# Step 2 — reserve budget (skipped on a cache hit; releasable)
# --------------------------------------------------------------------------- #
async def _reserve(ctx: SagaContext) -> StepResult:
    if ctx.state.get("cache_hit"):
        return StepResult.ok(reservation_id=None)
    ports = _ports(ctx)
    reservation_id = await ctx.effects.once(
        ctx.effect_key("reserve"),
        lambda: ports.reserve_budget(ctx.state["shot_id"], ctx.state.get("seconds", 5.0)),
    )
    return StepResult.ok(reservation_id=reservation_id)


async def _release(ctx: SagaContext) -> StepResult:
    reservation_id = ctx.state.get("reservation_id")
    if reservation_id:
        ports = _ports(ctx)
        await ctx.effects.once(
            ctx.effect_key("reserve:undo"),
            lambda: ports.release_budget(reservation_id),
        )
    return StepResult.ok()


# --------------------------------------------------------------------------- #
# Step 3 — render the clip (skipped on a cache hit; artifact discardable)
# The render is keyed by a regeneration counter so a QA-driven regen produces a
# fresh artifact while a same-generation crash-resume reuses the prior one.
# --------------------------------------------------------------------------- #
async def _render(ctx: SagaContext) -> StepResult:
    if ctx.state.get("cache_hit"):
        return StepResult.ok()  # clip_id already set by the cache check
    ports = _ports(ctx)
    degraded = bool(ctx.state.get("degraded"))
    generation = int(ctx.state.get("render_generation", 0))
    suffix = f"render:{'degraded' if degraded else 'full'}:{generation}"
    clip_id = await ctx.effects.once(
        ctx.effect_key(suffix),
        lambda: ports.render_clip(ctx.state["shot_id"], degraded=degraded),
    )
    return StepResult.ok(clip_id=clip_id)


async def _discard(ctx: SagaContext) -> StepResult:
    if ctx.state.get("cache_hit"):
        return StepResult.ok()  # never discard a cached artifact
    clip_id = ctx.state.get("clip_id")
    if clip_id:
        ports = _ports(ctx)
        await ctx.effects.once(
            ctx.effect_key("render:undo"),
            lambda: ports.discard_clip(clip_id),
        )
    return StepResult.ok()


#: §9.7: "regen, retry ≤ 2". A QA failure regenerates up to twice; on the final
#: regeneration we render degraded (Ken-Burns) so the film never hard-stops (§12.4).
_QA_RETRY = BackoffPolicy(max_attempts=3, base_delay_s=1.0, factor=2.0, max_delay_s=30.0)
#: Render transient errors (provider hiccups) get their own backed-off retries.
_RENDER_RETRY = BackoffPolicy(max_attempts=3, base_delay_s=2.0, factor=4.0, max_delay_s=120.0)


# --------------------------------------------------------------------------- #
# Step 4 — QA + §7.2 arbitration + §12.4 degrade ladder (read-only; no comp)
# --------------------------------------------------------------------------- #
async def _qa(ctx: SagaContext) -> StepResult:
    """Critic QA vs canon, then apply §7.2 arbitration / the §12.4 degrade ladder.

    The QA step owns the §9.7 ``QA → Repair → Rendering`` regeneration loop. A
    passing clip is accepted. A structured canon conflict is resolved by the §7.2
    policy: ``surface_to_user`` parks the shot for the reader's pick;
    ``evolve_canon`` updates the canon then **regenerates the clip in place**;
    ``honor_canon`` regenerates empty-handed. Each regeneration re-renders the clip
    and then re-checks via a *retryable* failure, so the engine re-drives QA within
    the §9.7 "retry ≤ 2" budget against the freshly rendered clip. On the **final**
    allowed attempt a still-failing shot is dropped to the Ken-Burns ladder and
    accepted degraded — the film never hard-stops.
    """
    if ctx.state.get("cache_hit"):
        # A cache hit is already an accepted shot — QA passed when it was cached.
        return StepResult.ok(qa="cached")
    is_final_attempt = ctx.attempt >= _QA_RETRY.max_attempts
    ports = _ports(ctx)
    conflict = await ports.qa_clip(ctx.state["shot_id"], ctx.state["clip_id"])
    if conflict is None:
        return StepResult.ok(qa="pass")

    director = await ports.is_director_present(ctx.state["shot_id"])
    decision = arbitrate(conflict, director_present=director)

    if decision is ArbitrationDecision.SURFACE_TO_USER:
        # Park for the reader's pick — terminal-for-now, not a saga rollback.
        return StepResult.ok(qa="awaiting_user", conflict_id=conflict.conflict_id)

    if is_final_attempt:
        # §12.4: retries exhausted → degrade to the Ken-Burns ladder and accept.
        ctx.state["degraded"] = True
        generation = int(ctx.state.get("render_generation", 0)) + 1
        ctx.state["render_generation"] = generation
        ctx.state["clip_id"] = await ctx.effects.once(
            ctx.effect_key(f"render:degraded:{generation}"),
            lambda: ports.render_clip(ctx.state["shot_id"], degraded=True),
        )
        return StepResult.ok(qa="degraded", degraded=True)

    if decision is ArbitrationDecision.EVOLVE_CANON:
        # The story genuinely changed: update canon, then regenerate.
        await ctx.effects.once(
            ctx.effect_key(f"evolve:{conflict.conflict_id}"),
            lambda: ports.evolve_canon(ctx.state["shot_id"], conflict),
        )
    # honor_canon or evolve_canon both regenerate: re-render the clip in place
    # (a distinct, generation-keyed effect so it is a genuinely fresh artifact),
    # then raise retryable so the next QA attempt scores the new clip (§9.7
    # QA → Repair → Rendering loop, bounded by the QA retry budget).
    generation = int(ctx.state.get("render_generation", 0)) + 1
    ctx.state["render_generation"] = generation
    ctx.state["clip_id"] = await ctx.effects.once(
        ctx.effect_key(f"render:full:{generation}"),
        lambda: ports.render_clip(ctx.state["shot_id"], degraded=False),
    )
    raise StepFailed(f"{decision.value} → regenerate ({conflict.conflict_id})", retryable=True)


# --------------------------------------------------------------------------- #
# Step 5 — accept (log + cache + last-frame → canon; reversible)
# --------------------------------------------------------------------------- #
async def _accept(ctx: SagaContext) -> StepResult:
    if ctx.state.get("qa") == "awaiting_user":
        # Director hasn't chosen yet; nothing to accept on this drive.
        return StepResult.ok(accepted=False)
    ports = _ports(ctx)
    await ctx.effects.once(
        ctx.effect_key("accept"),
        lambda: ports.accept_shot(ctx.state["shot_id"], ctx.state["clip_id"]),
    )
    return StepResult.ok(accepted=True)


async def _unaccept(ctx: SagaContext) -> StepResult:
    if not ctx.state.get("accepted"):
        return StepResult.ok()
    ports = _ports(ctx)
    await ctx.effects.once(
        ctx.effect_key("accept:undo"),
        lambda: ports.unaccept_shot(ctx.state["shot_id"], ctx.state["clip_id"]),
    )
    return StepResult.ok()


def build_render_saga(
    name: str = "render_qa_conflict_degrade",
    *,
    qa_retry: BackoffPolicy | None = None,
    render_retry: BackoffPolicy | None = None,
    deadline_s: float | None = 300.0,
) -> SagaDefinition:
    """Build the render→QA→conflict→degrade saga definition (§9.7 + §7.2 + §12.4).

    Stateless + reusable; per-shot inputs (``shot_id``, ``shot_hash``, ``seconds``)
    are passed as ``initial_state``. The QA step regenerates on a canon conflict up
    to ``qa_retry.max_attempts`` and, on the final attempt, degrades to the
    Ken-Burns ladder rather than failing — so the saga only rolls back on a true
    infrastructure failure (e.g. the render provider is unreachable past its retry
    budget), releasing any budget reservation on the way out.
    """
    qa_policy = qa_retry or _QA_RETRY
    render_policy = render_retry or _RENDER_RETRY
    return saga(
        name,
        step("cache_check", _cache_check, retry=BackoffPolicy(max_attempts=2, jitter=False)),
        step("reserve", _reserve, compensation=_release, retry=render_policy),
        step("render", _render, compensation=_discard, retry=render_policy),
        step("qa", _qa, retry=qa_policy),
        step("accept", _accept, compensation=_unaccept, retry=render_policy),
        deadline_s=deadline_s,
        description="Render a shot: cache → reserve → render → QA/arbitrate → accept, "
        "degrading to the Ken-Burns ladder on exhausted QA retries (compensatable).",
    )


def initial_render_state(
    shot_id: str, shot_hash: str, *, seconds: float = 5.0, **extra: Any
) -> dict[str, Any]:
    """Build the ``initial_state`` bag a caller passes to start the render saga."""
    return {"shot_id": shot_id, "shot_hash": shot_hash, "seconds": seconds, **extra}


__all__ = [
    "ArbitrationDecision",
    "Conflict",
    "RenderPorts",
    "arbitrate",
    "build_render_saga",
    "initial_render_state",
]
