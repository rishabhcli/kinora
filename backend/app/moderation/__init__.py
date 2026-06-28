"""Content moderation & safety subsystem (kinora.md §9, §10).

Kinora turns arbitrary reader-supplied books into generated film. Two trust
boundaries need a safety layer:

1. **Ingest-time** — the *source* book/PDF is screened before the canon is built,
   so a book that is wholesale disallowed (e.g. CSAM, terrorism instruction) is
   rejected at import rather than after spending tokens/credits on it.
2. **Generation-time** — every keyframe (image-gen) and clip (Wan video) is
   screened *before* it reaches the reader. This works **with** the §9.5 Critic,
   not instead of it: the Critic enforces *canon fidelity* (identity / style /
   timeline / motion); the safety gate enforces *policy* (is this content allowed
   to exist / be shown at all). A clip can be a perfect canon match and still be
   blocked for policy reasons, and vice-versa.

The subsystem is deliberately self-contained under :mod:`app.moderation` so it
can be wired into the existing pipeline additively:

* :mod:`.contracts` — typed, immutable value objects (labels, verdicts, ...).
* :mod:`.taxonomy` — the policy taxonomy: categories, severity tiers, default
  per-category dispositions.
* :mod:`.classifier` — the **pluggable classifier seam** (text + image/frame),
  an injectable Protocol with a deterministic in-repo fake.
* :mod:`.policy` — the deterministic policy engine (pure, exhaustively tested).
* :mod:`.gate` — the two product gates (ingest screening, generation safety).
* :mod:`.review` — the human-review queue + takedown/appeal state machine.
* :mod:`.escalation` — rate-of-violation tracking + repeat-offender escalation.
* :mod:`.audit` — the immutable, hash-chained moderation audit log.
* :mod:`.tenant_policy` — per-tenant configurable policy overrides.
* :mod:`.service` — the façade the API + pipeline call.

Everything model-based lives behind :class:`.classifier.ContentClassifier`, an
injectable Protocol with a deterministic in-repo fake — so the whole subsystem is
testable without any network or spend.
"""

from __future__ import annotations

from app.moderation.contracts import (
    ClassificationResult,
    ContentLabel,
    Decision,
    Disposition,
    ModerationCategory,
    ModerationContext,
    ModerationVerdict,
    ReviewState,
    Severity,
    Surface,
)

__all__ = [
    "ClassificationResult",
    "ContentLabel",
    "Decision",
    "Disposition",
    "ModerationCategory",
    "ModerationContext",
    "ModerationVerdict",
    "ReviewState",
    "Severity",
    "Surface",
]
