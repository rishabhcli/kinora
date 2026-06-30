"""Content-addressed identity for the exactly-once render path (kinora.md §9.7).

The whole durability subsystem hangs off one idea: a per-shot render is identified
not by the queue job id (which a re-delivery or a replay changes) but by **what is
being rendered** — the pair ``(shot_id, spec_digest)``. Two queue deliveries that
ask for the same shot at the same spec are *the same render*; the guard must run it
at most once and persist its accepted clip exactly once.

This module is the single source of truth for that identity:

* :func:`spec_digest` — a stable, cross-process digest of the render *intent*
  (render mode, prompt, seed, reference set, target duration). The same intent
  always hashes to the same digest; a redesign (new seed / new prompt) yields a
  new digest, so a genuine re-render is correctly treated as a *different* render.
* :class:`IdempotencyKey` — the ``(shot_id, spec_digest)`` pair, with a stable
  string form used as the store key for the idempotency + commit ledgers.

Pure: no DB, no provider, no ffmpeg — just hashing, so it is trivially testable and
identical whether computed in the worker, the pipeline, or the recovery loop.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "IdempotencyKey",
    "SpecLike",
    "spec_digest",
]


class SpecLike(Protocol):
    """The slice of an ``AgentShotSpec`` the digest reads (so tests pass a double)."""

    render_mode: Any
    prompt: str | None
    seed: int
    reference_image_ids: list[str]


def _enum_value(obj: Any) -> Any:
    """Flatten an enum (or any ``.value``-bearing object) to its stable value."""
    return getattr(obj, "value", obj)


def spec_digest(spec: SpecLike, *, target_duration_s: float | None = None) -> str:
    """A stable digest of a shot's render *intent* (§9.7).

    Deterministic across processes and restarts: the same render mode + prompt +
    seed + reference set (+ duration) always produces the same digest. A repair
    that re-rolls the seed or re-prompts produces a *different* digest, so the
    idempotency layer re-renders it rather than serving the stale attempt.

    The reference-image ids are sorted so an order-only difference does not split
    the digest (the locked reference *set* is what matters, not its list order).
    """
    refs = sorted(str(r) for r in (spec.reference_image_ids or []))
    parts: list[Any] = [
        "spec1",
        _enum_value(spec.render_mode),
        spec.prompt or "",
        int(spec.seed),
        refs,
    ]
    if target_duration_s is not None:
        # Round so a float-representation wobble can't split otherwise-equal specs.
        parts.append(round(float(target_duration_s), 3))
    raw = "|".join(repr(p) for p in parts)
    return "sd1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """The exactly-once identity of a render: ``(shot_id, spec_digest)``.

    Used as the store key for both the idempotency ledger (admission control for
    duplicate deliveries) and the commit log (exactly-once accepted-clip persist).
    Two deliveries with the same key are the same render and must not double-run.
    """

    shot_id: str
    spec_digest: str

    def as_str(self) -> str:
        """A stable flat string form (the ledger/commit-log storage key)."""
        return f"{self.shot_id}::{self.spec_digest}"

    @staticmethod
    def from_str(raw: str) -> IdempotencyKey:
        shot_id, _, digest = raw.partition("::")
        return IdempotencyKey(shot_id=shot_id, spec_digest=digest)

    @staticmethod
    def for_spec(
        shot_id: str, spec: SpecLike, *, target_duration_s: float | None = None
    ) -> IdempotencyKey:
        """Build the key for ``shot_id`` at ``spec`` (the common construction path)."""
        return IdempotencyKey(
            shot_id=shot_id, spec_digest=spec_digest(spec, target_duration_s=target_duration_s)
        )

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.as_str()
