# Content-Safety / Moderation GATEWAY — DESIGN

Self-contained gateway under `backend/app/safety/`. Kinora turns *arbitrary
reader-supplied books* into generated film, and the clips come from **many
providers** (DashScope Wan, MiniMax Hailuo, future hosted lanes) each with a
**different content policy**. This subsystem puts **one gateway** in front of the
whole generation loop so a render *prompt* and the generated *video* are checked
the same way regardless of which provider runs the render. Reference: kinora.md §9
(generation pipeline), §9.2 (multi-provider routing), §10 (prompt contracts /
guardrails).

It is deliberately **distinct from `app/moderation`**:

| | `app/moderation` | `app/safety` (this) |
|---|---|---|
| Concern | may content *exist / be shown* | what the *pipeline does with a render request* |
| Surfaces | source book at ingest, shown output | render **prompt** (pre-gen) + generated clip (post-gen) |
| Actions | ALLOW / FLAG / BLOCK → review queue | ALLOW / **TRANSFORM** / QUARANTINE / BLOCK |
| Signature feature | human review, takedown, escalation | **intent-preserving prompt auto-softening** + **per-provider routing avoidance** + **age-rating** |

The two share a vocabulary (categories, severity buckets line up name-for-name) so
a classifier can serve both, but neither imports the other — `app/safety` is fully
additive and self-contained.

## Architecture (layers, bottom-up)

| Module | Role | Purity |
|---|---|---|
| `taxonomy.py` | Categories, ordered severity, the 4 gateway actions (ALLOW/TRANSFORM/QUARANTINE/BLOCK), per-category policy + zero-tolerance floor | pure |
| `contracts.py` | Immutable value objects: `Finding`, `PromptAssessment`, `SofteningResult`, `PromptDecision`, `OutputAssessment`, `RoutingPlan`, `ContentAdvisory`, `SafetyContext`, `DecisionRecordView` | pure |
| `classifier.py` | **Injectable seam** (`SafetyClassifier` Protocol: text + frames). `KeywordSafetyClassifier` deterministic fake; `ModelText/FrameSafetyClassifier` production (chat/VL providers, never in tests) | seam |
| `rules.py` | Deterministic **rule engine** — findings + `PolicyTable` → strictest action + driving findings; overrides can never relax the floor | pure |
| `softener.py` | **Intent-preserving prompt auto-softener** — `RuleSoftener` (deterministic phrase substitutions + tasteful-framing clauses) + `ModelSoftener` (chat provider, rule fallback) | seam |
| `profiles.py` | **Per-provider POLICY PROFILES** — what each model refuses, per category/severity; `ProfileRegistry`; floor clamped | pure |
| `routing.py` | **Routing avoidance** — findings + profiles → `RoutingPlan` (viable providers best-first, avoided categories, explainable rankings) | pure |
| `advisory.py` | **Age-rating / content-advisory tagger** — findings → `ContentAdvisory` (MPAA-style band + descriptors); streaming `AdvisoryAccumulator` | pure |
| `decision_log.py` | **Immutable, hash-chained DECISION LOG** + appeal/override hooks; `InMemoryDecisionLog` (no-DB default), verify/replay | mixed |
| `prompt_gate.py` | Pre-generation gate: classify → rules → **soften before blocking** → re-evaluate → route → typed `PromptDecision` | mixed |
| `output_gate.py` | Post-generation gate: sampled-frame classification → allow/quarantine/block (`allow_transform=False`); surface-aware fail posture | mixed |
| `gateway.py` | `SafetyGateway` façade (the pipeline/router entry point) + `build_default_gateway` DI seam | mixed |
| `config.py` | Additive `SAFETY_*` env settings (`SafetySettings`) — never touches the global `Settings` schema | config |

### Key decisions
- **Soften, don't hard-block.** The core product promise: a faithful adaptation
  routinely describes violence/sexuality/gore a provider refuses *as phrased* but
  that is admissible *tastefully framed*. The prompt gate attempts an
  intent-preserving rewrite **before** accepting any non-ALLOW action whenever a
  softenable category drove it, then re-classifies the rewrite. Non-softenable
  categories (hate, CSAM, extremism, self-harm) are never rewritten — they are
  reported in `unsoftenable` and escalated.
- **Routing avoidance saves spends.** Sending a prompt to a provider that will
  reject it wastes a metered render. The router consults per-provider profiles and
  drops providers that would refuse the (softened) content; if *no* provider is
  viable an otherwise-allowable action is downgraded to QUARANTINE.
- **Every model judgment is behind a Protocol** with a deterministic
  keyword/regex/substitution fake — zero network, zero credits in the unit suite.
- **Zero-tolerance floor** (CSAM, violent extremism) cannot be relaxed by any
  policy override or provider profile — re-asserted in `PolicyTable.with_overrides`
  and clamped in `ProviderPolicyProfile.refusal_severity`.
- **Tamper-evident decision log**: per-tenant SHA-256 hash chain; `verify` re-hashes
  and reports the first broken seq. Overrides/appeals are *appended* records that
  reference the original — a decision is never mutated.
- **Surface-aware fail posture**: a degraded output-gate classifier **fails open**
  (the prompt was already pre-screened) unless `SAFETY_OUTPUT_FAIL_CLOSED=1`.

## Additive / coordination notes
- No shared-file edits. The package is import-only and self-contained; `app.main`
  / `create_app()` are unaffected (verified). Settings are a **separate**
  `SafetySettings` block (`SAFETY_*`), so the global `Settings` schema is untouched.
- Wiring into the pipeline/router is the next integration step (not done here, to
  keep this additive): the render path calls `SafetyGateway.screen_prompt` before a
  provider submit (using `routing.ordered_providers` to pick the lane) and
  `screen_output` on the returned frames; ingest calls `tag_book`. The
  composition root builds the gateway via `build_default_gateway(providers, ...)`.

## Status
- **Milestone 1 (DONE)** — taxonomy, contracts, classifier seam + fake.
- **Milestone 2 (DONE)** — rule engine, intent-preserving softener.
- **Milestone 3 (DONE)** — per-provider profiles + routing avoidance.
- **Milestone 4 (DONE)** — age-rating advisory tagger.
- **Milestone 5 (DONE)** — immutable hash-chained decision log + appeal/override.
- **Milestone 6 (DONE)** — prompt gate, output gate, gateway façade, config.

89 deterministic safety tests (pure + seam); ruff + mypy green on the subsystem,
no network, no spend.

## Remaining roadmap (future phases)
- **Pipeline wiring**: call `screen_prompt`/`screen_output` from `app.render` and
  `tag_book` from `app.ingest`; surface the routing plan to `providers.video_router`.
- **DB-backed `DecisionLog`** (persist the chain; the Protocol + in-memory impl are ready).
- **Per-tenant / per-book policy tables** (the `PolicyTable.with_overrides` seam exists).
- **Perceptual-hash fast path** in the frame lane (known-bad media) before the model call.
- **Softening feedback loop**: learn provider refusals back into the profiles.
- **Admin/ops API surface** (`/api/safety/*`) over the decision log + appeals.
