"""The ingest → canon-build → identity-lock saga.

Kinora's Phase-A ingest is a genuine multi-step, cross-service transaction: a PDF
lands in object storage, pages are extracted, the canon graph is built from those
pages, character identity reference sets are locked, and the book is finally
flipped to ``ready``. Any step can fail (a corrupt PDF, an image-gen ``429``, a DB
hiccup) and a half-built book is worse than no book — so the whole flow is a saga
with a compensation per step (forward action → compensation):

1. ``register_source`` — stage the uploaded PDF → delete the staged object.
2. ``extract_pages`` — parse pages + spans → drop the extracted page rows.
3. ``build_canon`` — build the canon graph (entities) → delete the canon version.
4. ``lock_identity`` — lock character reference sets → unlock + release them.
5. ``mark_ready`` — flip the book to ``ready`` → revert the book to ``failed``.

Every side effect runs through ``ctx.effects.once(...)`` so an at-least-once
re-drive (retry / crash-resume) applies it exactly once, and the forward step
stashes an *undo token* (the staged object key, the canon version, the locked
reference ids) in the saga state so its compensation knows precisely what to
reverse.

The flow depends only on the :class:`IngestPorts` protocol — wired to the real
ingest/canon/storage services in production and to in-memory fakes in tests
(:mod:`~app.distributed.sagas.flows.fakes`). It spends **zero** credits: page
extraction and canon-build are deterministic, and identity-lock here records the
*intent* to lock references (the real image-gen happens in the production adapter,
which is the seam where a ``429`` would surface as a retryable step failure).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.definition import SagaDefinition, saga, step
from app.distributed.sagas.types import SagaContext, StepFailed, StepResult


@runtime_checkable
class IngestPorts(Protocol):
    """The cross-service operations the ingest saga orchestrates.

    Each method is a coarse, idempotent-where-possible unit of work; the saga
    wraps the non-idempotent ones in the effect ledger and pairs each with an undo.
    Implementations: the production adapter over the real ingest/canon/storage
    services, and :class:`~app.distributed.sagas.flows.fakes.FakeIngestServices`.
    """

    async def stage_source(self, book_id: str, source_uri: str) -> str:
        """Stage the uploaded PDF; return the durable object key."""
        ...

    async def delete_source(self, object_key: str) -> None:
        """Delete a staged source object (compensation for :meth:`stage_source`)."""
        ...

    async def extract_pages(self, book_id: str, object_key: str) -> int:
        """Extract pages + source spans; return the page count."""
        ...

    async def drop_pages(self, book_id: str) -> None:
        """Drop extracted page rows (compensation for :meth:`extract_pages`)."""
        ...

    async def build_canon(self, book_id: str, page_count: int) -> int:
        """Build the canon graph from the pages; return the canon version."""
        ...

    async def delete_canon(self, book_id: str, version: int) -> None:
        """Delete a canon graph version (compensation for :meth:`build_canon`)."""
        ...

    async def lock_identity(self, book_id: str, canon_version: int) -> list[str]:
        """Lock character reference sets; return the locked reference ids."""
        ...

    async def unlock_identity(self, book_id: str, reference_ids: list[str]) -> None:
        """Unlock reference sets (compensation for :meth:`lock_identity`)."""
        ...

    async def mark_ready(self, book_id: str) -> None:
        """Flip the book to ``ready``."""
        ...

    async def mark_failed(self, book_id: str) -> None:
        """Revert the book to ``failed`` (compensation for :meth:`mark_ready`)."""
        ...


def _ports(ctx: SagaContext) -> IngestPorts:
    ports = ctx.resource("ingest_ports")
    if ports is None:
        raise StepFailed("ingest_ports resource not wired", retryable=False)
    return ports


# --------------------------------------------------------------------------- #
# Step 1 — register / stage the source PDF
# --------------------------------------------------------------------------- #
async def _register_source(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    book_id = ctx.state["book_id"]
    source_uri = ctx.state["source_uri"]
    object_key = await ctx.effects.once(
        ctx.effect_key("stage"),
        lambda: ports.stage_source(book_id, source_uri),
    )
    return StepResult.ok(object_key=object_key)


async def _unregister_source(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    object_key = ctx.state.get("object_key")
    if object_key:
        await ctx.effects.once(
            ctx.effect_key("stage:undo"),
            lambda: ports.delete_source(object_key),
        )
    return StepResult.ok()


# --------------------------------------------------------------------------- #
# Step 2 — extract pages
# --------------------------------------------------------------------------- #
async def _extract_pages(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    book_id = ctx.state["book_id"]
    object_key = ctx.state["object_key"]
    page_count = await ctx.effects.once(
        ctx.effect_key("extract"),
        lambda: ports.extract_pages(book_id, object_key),
    )
    if page_count <= 0:
        # A genuinely empty/corrupt PDF — terminal, not worth retrying.
        raise StepFailed("no pages extracted from source", retryable=False)
    return StepResult.ok(page_count=page_count)


async def _drop_pages(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    await ctx.effects.once(
        ctx.effect_key("extract:undo"),
        lambda: ports.drop_pages(ctx.state["book_id"]),
    )
    return StepResult.ok()


# --------------------------------------------------------------------------- #
# Step 3 — build the canon graph
# --------------------------------------------------------------------------- #
async def _build_canon(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    version = await ctx.effects.once(
        ctx.effect_key("canon"),
        lambda: ports.build_canon(ctx.state["book_id"], ctx.state["page_count"]),
    )
    return StepResult.ok(canon_version=version)


async def _delete_canon(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    version = ctx.state.get("canon_version")
    if version is not None:
        await ctx.effects.once(
            ctx.effect_key("canon:undo"),
            lambda: ports.delete_canon(ctx.state["book_id"], version),
        )
    return StepResult.ok()


# --------------------------------------------------------------------------- #
# Step 4 — lock character identity reference sets
# --------------------------------------------------------------------------- #
async def _lock_identity(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    reference_ids = await ctx.effects.once(
        ctx.effect_key("lock"),
        lambda: ports.lock_identity(ctx.state["book_id"], ctx.state["canon_version"]),
    )
    return StepResult.ok(reference_ids=reference_ids)


async def _unlock_identity(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    reference_ids = ctx.state.get("reference_ids") or []
    if reference_ids:
        await ctx.effects.once(
            ctx.effect_key("lock:undo"),
            lambda: ports.unlock_identity(ctx.state["book_id"], reference_ids),
        )
    return StepResult.ok()


# --------------------------------------------------------------------------- #
# Step 5 — mark the book ready
# --------------------------------------------------------------------------- #
async def _mark_ready(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    await ctx.effects.once(
        ctx.effect_key("ready"),
        lambda: ports.mark_ready(ctx.state["book_id"]),
    )
    return StepResult.ok(ready=True)


async def _mark_failed(ctx: SagaContext) -> StepResult:
    ports = _ports(ctx)
    await ctx.effects.once(
        ctx.effect_key("ready:undo"),
        lambda: ports.mark_failed(ctx.state["book_id"]),
    )
    return StepResult.ok()


#: Default per-step retry for ingest: transient DB/storage/image-gen hiccups
#: (e.g. a 429 on the identity image-gen) get a few backed-off tries.
_INGEST_RETRY = BackoffPolicy(max_attempts=4, base_delay_s=2.0, factor=3.0, max_delay_s=60.0)


def build_ingest_saga(
    name: str = "ingest_canon_identity",
    *,
    retry: BackoffPolicy | None = None,
    deadline_s: float | None = 1800.0,
) -> SagaDefinition:
    """Build the ingest→canon-build→identity-lock saga definition.

    The returned definition is stateless and reusable across all books; the
    per-book inputs (``book_id``, ``source_uri``) are passed as the saga's
    ``initial_state`` at start time. ``deadline_s`` defaults to 30 minutes — a slow
    ingest that blows that budget rolls back rather than wedging.
    """
    policy = retry or _INGEST_RETRY
    return saga(
        name,
        step("register_source", _register_source, compensation=_unregister_source, retry=policy),
        step("extract_pages", _extract_pages, compensation=_drop_pages, retry=policy),
        step("build_canon", _build_canon, compensation=_delete_canon, retry=policy),
        step("lock_identity", _lock_identity, compensation=_unlock_identity, retry=policy),
        step("mark_ready", _mark_ready, compensation=_mark_failed, retry=policy),
        deadline_s=deadline_s,
        description="Ingest a book: stage source → extract pages → build canon → "
        "lock identity → mark ready (compensatable).",
    )


def initial_ingest_state(book_id: str, source_uri: str, **extra: Any) -> dict[str, Any]:
    """Build the ``initial_state`` bag a caller passes to start the ingest saga."""
    return {"book_id": book_id, "source_uri": source_uri, **extra}


__all__ = ["IngestPorts", "build_ingest_saga", "initial_ingest_state"]
