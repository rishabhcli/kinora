"""``FingerprintDiff`` — explain *why* two clips differ.

When a Director re-reads a passage and the footage looks different — or when a
surgical re-render produces a changed clip — the question is always "what
changed, and was it supposed to?" The diff answers that by comparing two
:class:`RenderFingerprint`\\ s field by field and attributing every difference to
a named cause, ranked by how strongly that field bears on the *output*.

Attribution is the intelligence here: not all fields are equal. A changed
``seed`` or ``prompt_digest`` *will* change pixels; a changed
``reference_identity_digest`` changes the character's appearance (the §8.7
surgical-re-render trigger); a changed ``model`` / ``version`` can change
everything; a changed set of ``concessions`` (e.g. one clip was budget-degraded
to Ken-Burns) is often the real reason a clip "looks worse." A changed
``provider_task_id`` alone is *expected* on any re-render and is explicitly
classified as **non-causal** so it never gets blamed.

The result also reports whether the two fingerprints would have *re-issued the
same provider request* (equal ``request_digest``) — which, combined with the
determinism label, tells you whether a difference is explainable by inputs or is
the model's own nondeterminism.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict

from .fingerprint import RenderFingerprint


class Causality(IntEnum):
    """How strongly a changed field bears on the rendered output (higher = more).

    An :class:`IntEnum` so findings sort by impact trivially.
    """

    #: Expected to differ on any re-render; never the cause of a visual change.
    NON_CAUSAL = 0
    #: Provenance/context change; does not itself change the provider request.
    CONTEXTUAL = 1
    #: Changes the bytes after generation (a post-op), not the generation itself.
    POST = 2
    #: Changes the provider request → will change output (seed/prompt/refs/params).
    REQUEST = 3
    #: Changes the model/provider/version itself → can change everything.
    MODEL = 4


class ChangeKind(StrEnum):
    """What kind of change a field underwent."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class FieldChange(BaseModel):
    """One attributed difference between two fingerprints."""

    model_config = ConfigDict(frozen=True)

    field: str
    kind: ChangeKind
    causality: Causality
    before: object | None = None
    after: object | None = None
    explanation: str = ""


class FingerprintDiff(BaseModel):
    """The attributed difference between two fingerprints (``a`` = before)."""

    model_config = ConfigDict(frozen=True)

    a_fingerprint_id: str
    b_fingerprint_id: str
    identical: bool
    #: Equal ``request_digest`` → the *provider request* is unchanged, so any clip
    #: difference is the model's own (non)determinism, not an input change.
    same_request: bool
    changes: tuple[FieldChange, ...] = ()

    @property
    def primary_cause(self) -> FieldChange | None:
        """The highest-impact change — the headline reason the clips differ."""
        if not self.changes:
            return None
        return max(self.changes, key=lambda c: (int(c.causality), c.field))

    def summary(self) -> str:
        """A one-line human explanation suitable for a log or a UI tooltip."""
        if self.identical:
            return "identical fingerprints (same clip provenance)"
        cause = self.primary_cause
        assert cause is not None  # not identical ⇒ at least one change
        head = f"clips differ: {cause.explanation or cause.field}"
        if self.same_request and cause.causality <= Causality.CONTEXTUAL:
            head += " (provider request unchanged — difference is provenance/context"
            head += " or model nondeterminism)"
        return head


# Field → (causality, human label) attribution table. Order here also defines a
# stable iteration order for deterministic diff output.
_ATTRIBUTION: tuple[tuple[str, Causality, str], ...] = (
    ("provider.provider", Causality.MODEL, "render provider changed"),
    ("provider.model", Causality.MODEL, "model id changed"),
    ("provider.version", Causality.MODEL, "model version changed"),
    ("provider.protocol", Causality.MODEL, "request protocol changed"),
    ("mode", Causality.REQUEST, "Wan render mode changed"),
    ("seed", Causality.REQUEST, "seed changed (a fresh variation)"),
    ("prompt_digest", Causality.REQUEST, "prompt / negative-prompt / dialect changed"),
    (
        "reference_identity_digest",
        Causality.REQUEST,
        "reference identity changed (a canon edit re-rendered this shot)",
    ),
    ("duration_s", Causality.REQUEST, "duration changed"),
    ("resolution", Causality.REQUEST, "resolution changed"),
    ("params", Causality.REQUEST, "generation parameters changed"),
    ("post_ops", Causality.POST, "post-generation pipeline changed"),
    ("concessions", Causality.CONTEXTUAL, "planner concessions changed (e.g. budget degrade)"),
    ("canon_version_at_render", Causality.CONTEXTUAL, "canon version at render changed"),
    ("scene_id", Causality.CONTEXTUAL, "scene context changed"),
    ("beat_id", Causality.CONTEXTUAL, "beat context changed"),
    ("shot_id", Causality.CONTEXTUAL, "shot id changed"),
    ("book_id", Causality.CONTEXTUAL, "book changed"),
    ("provider_task_id", Causality.NON_CAUSAL, "provider task id differs (expected on re-render)"),
)


def _get(fp: RenderFingerprint, dotted: str) -> object:
    """Read a possibly-nested field by dotted path; tuples → comparable lists."""
    obj: object = fp
    for part in dotted.split("."):
        obj = getattr(obj, part)
    # Normalise enums + tuples to comparable, JSON-safe values.
    if isinstance(obj, tuple):
        return [
            item.model_dump() if isinstance(item, BaseModel) else item for item in obj
        ]
    if hasattr(obj, "value") and not isinstance(obj, (str, int, float, bool)):
        return obj.value  # StrEnum / enum
    return obj


def diff_fingerprints(
    a: RenderFingerprint, b: RenderFingerprint
) -> FingerprintDiff:
    """Compute the attributed diff explaining why clip ``a`` and ``b`` differ.

    Pure and deterministic. Compares the keyed/provenance fields via the
    attribution table; the ``provider_task_id`` difference is recorded but always
    NON_CAUSAL so a re-render's new task id never masquerades as a cause.
    """
    changes: list[FieldChange] = []
    for dotted, causality, label in _ATTRIBUTION:
        before = _get(a, dotted)
        after = _get(b, dotted)
        if before == after:
            continue
        if before is None:
            kind = ChangeKind.ADDED
        elif after is None:
            kind = ChangeKind.REMOVED
        else:
            kind = ChangeKind.CHANGED
        changes.append(
            FieldChange(
                field=dotted,
                kind=kind,
                causality=causality,
                before=before,
                after=after,
                explanation=label,
            )
        )

    # Sort by impact (desc) then field name for a stable, readable ordering.
    changes.sort(key=lambda c: (-int(c.causality), c.field))

    return FingerprintDiff(
        a_fingerprint_id=a.fingerprint_id,
        b_fingerprint_id=b.fingerprint_id,
        identical=a.fingerprint_id == b.fingerprint_id,
        same_request=a.request_digest == b.request_digest,
        changes=tuple(changes),
    )


__all__ = [
    "Causality",
    "ChangeKind",
    "FieldChange",
    "FingerprintDiff",
    "diff_fingerprints",
]
