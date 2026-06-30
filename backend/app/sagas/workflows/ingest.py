"""The Phase-A ingest pipeline as a durable saga.

Ingest (kinora.md §4.x) turns an uploaded PDF into a versioned canon through a
fixed sequence of expensive, side-effecting steps:

    parse → keyframes → identity_lock → canon_build → mark_ready

Each step is *idempotent* (keyed by the book id + the step's idempotency key) and
*compensable* (a failed import shouldn't leave half-written keyframes or a stray
identity lock in object storage / the canon). If ``canon_build`` fails past its
retries, the saga unwinds in reverse: drop the canon draft, release the identity
lock, delete the keyframes — leaving the book back at ``import_failed`` with no
orphaned artefacts.

The pipeline talks to an injected :class:`IngestPort`, so this module imports no
DashScope client, no DB, and no storage SDK. The production composition root
implements :class:`IngestPort` over the real ingest services; tests implement it
in-memory and assert ordering, resume, and compensation.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.sagas.context import StepContext
from app.sagas.definition import Step, Workflow
from app.sagas.policy import RetryPolicy, TimeoutPolicy

#: A book's keyframe generation can hit the §gotchas image-model 429 — give it
#: a few transient retries with backoff before giving up.
_KEYFRAME_RETRY = RetryPolicy(max_attempts=4, base_backoff_s=2.0, factor=2.0, max_backoff_s=30.0)
_PARSE_RETRY = RetryPolicy(max_attempts=3, base_backoff_s=1.0)
_CANON_TIMEOUT = TimeoutPolicy(total_s=600.0)


class IngestPort(Protocol):
    """The side-effecting operations the ingest saga drives (injected).

    Each method is keyed by ``book_id`` (and an ``idempotency_key`` for the
    write operations) so a resume that re-invokes it dedupes the side effect.
    """

    async def parse(self, book_id: str, idempotency_key: str) -> dict[str, Any]:
        """Extract text/layout from the source PDF; return a parse summary."""
        ...

    async def generate_keyframes(self, book_id: str, idempotency_key: str) -> list[str]:
        """Generate keyframe images; return their object-storage keys."""
        ...

    async def delete_keyframes(self, book_id: str, keys: list[str]) -> None:
        """Compensation: delete keyframe objects written by a failed import."""
        ...

    async def lock_identity(self, book_id: str, idempotency_key: str) -> str:
        """Build the character/identity lock; return its id."""
        ...

    async def release_identity(self, book_id: str, identity_id: str) -> None:
        """Compensation: release/delete an identity lock."""
        ...

    async def build_canon(self, book_id: str, idempotency_key: str) -> str:
        """Build the versioned canon draft; return the canon version id."""
        ...

    async def drop_canon(self, book_id: str, canon_id: str) -> None:
        """Compensation: drop a canon draft."""
        ...

    async def mark_ready(self, book_id: str) -> None:
        """Flip the book to ``ready`` (terminal, no compensation needed)."""
        ...


def _book_id(ctx: StepContext) -> str:
    book_id = ctx.input.get("book_id") if isinstance(ctx.input, dict) else None
    if not book_id:
        from app.sagas.errors import PermanentStepError

        raise PermanentStepError("ingest workflow input must include a book_id")
    return str(book_id)


def build_ingest_workflow(port: IngestPort) -> Workflow:
    """Wire the ingest :class:`IngestPort` into a durable :class:`Workflow`."""

    async def parse(ctx: StepContext) -> dict[str, Any]:
        return await port.parse(_book_id(ctx), ctx.idempotency_key)

    async def keyframes(ctx: StepContext) -> list[str]:
        keys = await port.generate_keyframes(_book_id(ctx), ctx.idempotency_key)
        ctx.set("keyframe_keys", keys)
        return keys

    async def undo_keyframes(ctx: StepContext) -> None:
        keys = ctx.result_of("keyframes") or ctx.get("keyframe_keys") or []
        await port.delete_keyframes(_book_id(ctx), list(keys))

    async def identity(ctx: StepContext) -> str:
        ident = await port.lock_identity(_book_id(ctx), ctx.idempotency_key)
        ctx.set("identity_id", ident)
        return ident

    async def undo_identity(ctx: StepContext) -> None:
        ident = ctx.result_of("identity") or ctx.get("identity_id")
        if ident:
            await port.release_identity(_book_id(ctx), str(ident))

    async def canon(ctx: StepContext) -> str:
        canon_id = await port.build_canon(_book_id(ctx), ctx.idempotency_key)
        ctx.set("canon_id", canon_id)
        return canon_id

    async def undo_canon(ctx: StepContext) -> None:
        canon_id = ctx.result_of("canon") or ctx.get("canon_id")
        if canon_id:
            await port.drop_canon(_book_id(ctx), str(canon_id))

    async def finish(ctx: StepContext) -> None:
        await port.mark_ready(_book_id(ctx))

    return Workflow(
        name="ingest_pipeline",
        description="Phase-A ingest: parse → keyframes → identity → canon → ready",
        steps=(
            Step("parse", parse, retry=_PARSE_RETRY),
            Step("keyframes", keyframes, compensation=undo_keyframes, retry=_KEYFRAME_RETRY),
            Step("identity", identity, compensation=undo_identity, retry=_PARSE_RETRY),
            Step(
                "canon",
                canon,
                compensation=undo_canon,
                retry=_PARSE_RETRY,
                timeout=_CANON_TIMEOUT,
            ),
            Step("mark_ready", finish, retry=_PARSE_RETRY),
        ),
    )


__all__ = ["IngestPort", "build_ingest_workflow"]
