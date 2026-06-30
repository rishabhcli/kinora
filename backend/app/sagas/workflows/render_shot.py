"""The per-shot render pipeline as a durable saga (kinora.md §9.2–§9.7, §12.4).

The render of one shot is a chain of expensive, budget-spending, OSS-writing
steps that must never double-spend or leave orphaned objects on a crash:

    reserve_budget → design → generate → normalize → persist → qa

Durability + idempotency mean a worker that crashes mid-``generate`` resumes
*without* re-reserving video-seconds or re-writing the clip. Compensation means a
failure past ``generate`` releases the budget reservation and deletes the OSS
object, so a failed shot costs nothing and leaves nothing behind.

Branching wires the §9.5 self-correcting loop's terminal decision and the §12.4
degradation ladder: ``qa`` either accepts the shot (``END``) or routes to a
``degrade`` step (the Ken-Burns ladder rung) — so a shot that can't pass QA still
produces a playable segment rather than failing the saga. (The full repair
*retry* loop lives in the render pipeline; here QA failure escalates straight to
the ladder, the conservative durable choice.)

All effects go through an injected :class:`RenderPort`; this module imports no
provider, budget service, ffmpeg, or storage SDK.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.sagas.context import StepContext
from app.sagas.definition import END, Step, Workflow
from app.sagas.errors import PermanentStepError
from app.sagas.policy import RetryPolicy, TimeoutPolicy

#: A live provider call (Wan) is slow + flaky → generous transient retries with
#: a long total deadline; the per-attempt deadline guards a hung task.
_GENERATE_RETRY = RetryPolicy(max_attempts=4, base_backoff_s=3.0, factor=2.0, max_backoff_s=45.0)
_GENERATE_TIMEOUT = TimeoutPolicy(per_attempt_s=300.0, total_s=1200.0)
_CHEAP_RETRY = RetryPolicy(max_attempts=3, base_backoff_s=1.0)
#: Reserving budget is a fast DB op but must not retry forever — a hard "no
#: budget" is permanent and should compensate, not loop.
_RESERVE_RETRY = RetryPolicy(max_attempts=2, base_backoff_s=0.5)


class RenderPort(Protocol):
    """The side-effecting operations the render saga drives (injected)."""

    async def reserve_budget(self, shot_id: str, seconds: float, idempotency_key: str) -> str:
        """Reserve ``seconds`` of video budget; return a reservation id.

        Raises a :class:`~app.sagas.errors.PermanentStepError` when the budget
        is exhausted (so the saga compensates rather than retrying forever).
        """
        ...

    async def release_budget(self, shot_id: str, reservation_id: str) -> None:
        """Compensation: release a budget reservation."""
        ...

    async def design(self, shot_id: str, idempotency_key: str) -> dict[str, Any]:
        """Design the shot (prompt + refs + seed); return the spec."""
        ...

    async def generate(self, shot_id: str, spec: dict[str, Any], idempotency_key: str) -> str:
        """Run the provider; return a temporary clip handle/url."""
        ...

    async def normalize(self, shot_id: str, clip: str, idempotency_key: str) -> str:
        """Normalize/transcode the clip; return the normalized local handle."""
        ...

    async def persist(self, shot_id: str, clip: str, idempotency_key: str) -> str:
        """Persist the clip to object storage; return its OSS key."""
        ...

    async def delete_object(self, shot_id: str, oss_key: str) -> None:
        """Compensation: delete a persisted clip object."""
        ...

    async def qa(self, shot_id: str, oss_key: str, idempotency_key: str) -> bool:
        """Score the clip; return True iff it passes (§9.5)."""
        ...

    async def degrade(self, shot_id: str, idempotency_key: str) -> str:
        """Produce a §12.4 ladder rung (Ken-Burns) instead; return its OSS key."""
        ...


def _shot_id(ctx: StepContext) -> str:
    shot_id = ctx.input.get("shot_id") if isinstance(ctx.input, dict) else None
    if not shot_id:
        raise PermanentStepError("render workflow input must include a shot_id")
    return str(shot_id)


def _seconds(ctx: StepContext) -> float:
    if isinstance(ctx.input, dict):
        return float(ctx.input.get("video_seconds", 5.0))
    return 5.0


def build_render_shot_workflow(port: RenderPort) -> Workflow:
    """Wire the render :class:`RenderPort` into a durable :class:`Workflow`."""

    async def reserve(ctx: StepContext) -> str:
        res = await port.reserve_budget(_shot_id(ctx), _seconds(ctx), ctx.idempotency_key)
        ctx.set("reservation_id", res)
        return res

    async def undo_reserve(ctx: StepContext) -> None:
        res = ctx.result_of("reserve_budget") or ctx.get("reservation_id")
        if res:
            await port.release_budget(_shot_id(ctx), str(res))

    async def design(ctx: StepContext) -> dict[str, Any]:
        return await port.design(_shot_id(ctx), ctx.idempotency_key)

    async def generate(ctx: StepContext) -> str:
        spec = ctx.result_of("design") or {}
        return await port.generate(_shot_id(ctx), dict(spec), ctx.idempotency_key)

    async def normalize(ctx: StepContext) -> str:
        clip = ctx.result_of("generate") or ""
        return await port.normalize(_shot_id(ctx), str(clip), ctx.idempotency_key)

    async def persist(ctx: StepContext) -> str:
        clip = ctx.result_of("normalize") or ""
        oss_key = await port.persist(_shot_id(ctx), str(clip), ctx.idempotency_key)
        ctx.set("oss_key", oss_key)
        return oss_key

    async def undo_persist(ctx: StepContext) -> None:
        oss_key = ctx.result_of("persist") or ctx.get("oss_key")
        if oss_key:
            await port.delete_object(_shot_id(ctx), str(oss_key))

    async def qa(ctx: StepContext) -> bool:
        oss_key = ctx.result_of("persist") or ""
        passed = await port.qa(_shot_id(ctx), str(oss_key), ctx.idempotency_key)
        ctx.set("qa_passed", bool(passed))
        return bool(passed)

    def qa_branch(ctx: StepContext) -> str | None:
        # Pass → finish; fail → drop to the §12.4 degradation rung.
        return END if ctx.get("qa_passed") else "degrade"

    async def degrade(ctx: StepContext) -> str:
        # A genuine product rung, not a failure: replace the (failed) clip with
        # a Ken-Burns segment. The persisted full clip is left to QA's caller.
        return await port.degrade(_shot_id(ctx), ctx.idempotency_key)

    return Workflow(
        name="render_shot",
        description="Per-shot render: reserve→design→generate→normalize→persist→qa (§9.7)",
        steps=(
            Step("reserve_budget", reserve, compensation=undo_reserve, retry=_RESERVE_RETRY),
            Step("design", design, retry=_CHEAP_RETRY),
            Step(
                "generate",
                generate,
                retry=_GENERATE_RETRY,
                timeout=_GENERATE_TIMEOUT,
            ),
            Step("normalize", normalize, retry=_CHEAP_RETRY),
            Step("persist", persist, compensation=undo_persist, retry=_CHEAP_RETRY),
            Step("qa", qa, retry=_CHEAP_RETRY, branch=qa_branch),
            Step("degrade", degrade, retry=_CHEAP_RETRY),
        ),
    )


__all__ = ["RenderPort", "build_render_shot_workflow"]
