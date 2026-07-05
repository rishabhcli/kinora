"""The runtime-config evaluation context — Kinora's four targeting dimensions.

Where the §13 experimentation platform uses a generic
:class:`~app.flags.context.EvalContext` attribute bag, the *runtime config plane*
targets a fixed, named set of dimensions that map onto Kinora's domain: the
**book** being adapted, the **user** reading it, the **cohort** they belong to
(e.g. ``beta`` / ``internal`` / ``partner``), and the **provider** a code path is
about to call (e.g. ``dashscope`` / ``minimax``). A flag rule constrains a subset
of these; a percentage rollout buckets on one of them.

The context is an immutable value with no infra ties, so a worker, a request
handler, or a test all build one the same way. :meth:`as_eval_context` bridges to
the §13 ``EvalContext`` when a caller wants to reuse that platform's generic
clause engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.flags.context import EvalContext


@dataclass(frozen=True, slots=True)
class FlagContext:
    """Identity for one runtime-config resolution along Kinora's dimensions.

    Every field is optional — an unconstrained dimension simply never matches a
    rule that constrains it, and a rollout that buckets on an absent dimension
    excludes the context (fail safe). ``extra`` carries any additional attributes
    (kept for forward-compat / bridging to the §13 evaluator) without widening
    the targeting surface.
    """

    book: str | None = None
    user: str | None = None
    cohort: str | None = None
    provider: str | None = None
    extra: dict[str, Any] | None = None

    def dimension(self, name: str) -> str | None:
        """Return the value of targeting dimension ``name`` (or ``None``)."""
        if name == "book":
            return self.book
        if name == "user":
            return self.user
        if name == "cohort":
            return self.cohort
        if name == "provider":
            return self.provider
        return None

    def unit_for(self, bucket_by: str) -> str:
        """Resolve the bucketing unit for a percentage rollout.

        ``"key"`` maps to the user (the canonical bucketing identity); otherwise
        the named dimension's value, or ``""`` when that dimension is absent (the
        rollout treats an empty unit as "not in the ramp").
        """
        if bucket_by == "key":
            return self.user or ""
        return self.dimension(bucket_by) or ""

    def as_eval_context(self) -> EvalContext:
        """Bridge to the §13 generic :class:`EvalContext` (user-keyed).

        Lets a runtime flag optionally reuse the experimentation platform's
        richer clause engine while keeping the plane's own typed dimensions as
        first-class attributes.
        """
        attrs: dict[str, Any] = {
            "book": self.book,
            "cohort": self.cohort,
            "provider": self.provider,
        }
        if self.extra:
            attrs.update(self.extra)
        units = {
            dim: val
            for dim, val in (
                ("book", self.book),
                ("cohort", self.cohort),
                ("provider", self.provider),
            )
            if val is not None
        }
        return EvalContext(
            key=self.user or "anonymous",
            kind="user",
            attributes={k: v for k, v in attrs.items() if v is not None},
            units=units,
            anonymous=self.user is None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "book": self.book,
            "user": self.user,
            "cohort": self.cohort,
            "provider": self.provider,
            "extra": dict(self.extra) if self.extra else None,
        }


#: A context that matches no rule and buckets nowhere — the implicit base context.
EMPTY_CONTEXT = FlagContext()


__all__ = ["EMPTY_CONTEXT", "FlagContext"]
