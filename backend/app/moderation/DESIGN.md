# Content Moderation & Safety subsystem — DESIGN

Self-contained safety layer under `backend/app/moderation/`. Kinora turns
arbitrary reader-supplied books into generated film; this subsystem screens both
trust boundaries — the **source book at ingest** and **every generated
keyframe/clip at generation** — and runs the human-review, takedown/appeal,
repeat-offender, audit, and per-tenant-policy machinery behind them. Reference:
kinora.md §9 (generation pipeline), §10 (prompt contracts / guardrails). Works
**with** the §9.5 Critic, not instead of it: the Critic enforces *canon
fidelity*; this enforces *policy*.

## Architecture (layers, bottom-up)

| Module | Role | Purity |
|---|---|---|
| `taxonomy.py` | Categories, ordered severity tiers, per-category disposition rules, zero-tolerance floor | pure |
| `contracts.py` | Immutable value objects: `ContentLabel`, `ClassificationResult`, `ModerationVerdict`, `ModerationContext`, `Decision`, `ReviewState`, `Surface` | pure |
| `classifier.py` | **Injectable seam** (`ContentClassifier` Protocol: text + frame). `KeywordClassifier` deterministic fake; `ModelTextClassifier`/`ModelVisionClassifier` production (chat/VL providers, never called in tests) | seam |
| `tenant_policy.py` | Configurable per-tenant policy + builtin presets (default / children / mature); zero-tolerance floor cannot be relaxed | pure |
| `policy.py` | Deterministic policy **engine** — labels + policy → verdict; `merge_verdicts` for multi-modal | pure |
| `models.py` | 5 ORM tables (events, audit, review items, tenant policies, violation counters) | DB |
| `repositories.py` | Repos (flush-never-commit); hash-chain `compute_hash`; queries | DB |
| `audit.py` | Append-only, **hash-chained, tamper-evident** moderation audit log + replay/verify | DB |
| `review.py` | Human-review queue + takedown/appeal **state-machine** (pure `can_transition` graph + `ReviewWorkflow` driver) | mixed |
| `escalation.py` | Rate-of-violation rolling window + **repeat-offender ladder** (pure `compute_tier`/`window_expired` + `EscalationService`) | mixed |
| `gate.py` | The two product gates: `screen_book_text`/`screen_page` (ingest, fail-closed) and `screen_keyframe`/`screen_clip`/`screen_comment` (generation, fail-open) | mixed |
| `service.py` | `ModerationService` façade + `ModerationFactory` DI seam | mixed |
| `routes.py` | `/api/moderation/*` admin/ops surface | API |

### Key decisions
- **Every model-based judgment is behind `ContentClassifier`** — a Protocol with a
  deterministic keyword/regex fake. Zero live calls, zero credits in tests.
- **Fail posture is surface-aware**: ingest fails *closed* (an unscreenable source
  is held), generation fails *open* (a provider blip never silently drops a clip),
  and a tenant can tighten generation to fail-closed.
- **Zero-tolerance floor** (CSAM, extremism) cannot be relaxed by any tenant
  override — enforced in `tenant_policy.rule_for`.
- **Tamper-evident audit**: per-tenant hash chain; `replay` re-hashes and reports
  the first broken seq.

## Additive shared-file changes (documented per coordination rules)
- `app/db/models/__init__.py` — import the 5 moderation models so they register
  on `Base.metadata` (Alembic autogenerate + `create_all`). Single additive hook.
- `app/api/routes/__init__.py` — append `moderation_router` to `ROUTERS`.
- `app/composition.py` — additive `Container.moderation_factory` seam +
  `build_moderation(session)` / `_moderation_factory()` (lazy; keyword fake when
  no providers). No existing wiring touched.
- `migrations/versions/d4f7c2a9b1e3_moderation_safety_subsystem.py` — new
  Alembic revision on head `a1b2c3d4e5f6`. Verified: full chain upgrades, downgrade
  drops cleanly, autogenerate shows **no moderation drift**.

## Status
- **Milestone 1 (DONE)** — taxonomy, contracts, classifier seam + fake, policy engine,
  tenant policy. 31 pure tests.
- **Milestone 2 (DONE)** — DB models + migration + repositories.
- **Milestone 3 (DONE)** — audit log, review state machine, escalation ladder. 36 pure tests.
- **Milestone 4 (DONE)** — gate (ingest + generation), service façade. 10 gate integration tests.
- **Milestone 5 (DONE)** — review/escalation/audit persistence. 13 integration tests.
- **Milestone 6 (DONE)** — API routes + factory wiring. 10 API tests.

Totals: 100 moderation tests (67 pure+DB unit, 10 API); `make lint` (ruff+mypy)
green across 243 source files.

## Remaining roadmap (future phases)
- **Pipeline wiring**: call `screen_book_text` from `app.ingest` before canon build,
  and `screen_clip`/`screen_keyframe` from `app.render` alongside the Critic verdict
  (gate result → degradation/skip, emit a feed event). Seam + service are ready;
  this is the integration point.
- **PII redaction** (not just detection) on narration/source spans.
- **Perceptual-hash blocklist** for known-bad media (CSAM hash matching) as a
  pre-classifier fast path in the vision lane.
- **Reviewer SLA + queue-age metrics** surfaced through `app.observability`.
- **Per-tenant escalation policy** persisted (currently process-default
  `EscalationPolicy`; the tenant-policy table can carry it).
- **Appeal deadlines / auto-expiry** of stale review items.
