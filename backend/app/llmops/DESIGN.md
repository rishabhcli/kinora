# LLM-ops / prompt-registry platform (`backend/app/llmops/`)

A self-contained LLM-ops platform layered **over** the six-agent crew without
touching the agents or `agents/prompts.py`. The agents own their *current*
`VersionedPrompt`s; this package **manages prompts externally**: it ingests those
prompts as the seed of a versioned registry, then adds semver + changelog,
A/B + eval, regression detection, a prompt-injection + jailbreak defense filter,
a model registry + cost routing config, run tracing with a query API, response
caching, and a safety/guardrail policy layer.

Design constraints (per the worktree brief):
- **Additive-only** on shared files (`core/config.py`, `composition.py`,
  `api/routes/__init__.py`, `db/models/__init__.py`).
- Every model call is behind a **fake judge / fake responder** — zero live calls.
- New DB tables ship as **one Alembic migration** (`9f3c7a1e2b4d`) on the head at
  branch time (`a1b2c3d4e5f6`), UNIQUE revision id.
- `KINORA_LIVE_VIDEO` stays OFF; no credits spent.

Cites: kinora.md §10 (prompt contracts) and §13 (metrics / eval harness).

## Module map (all under `app/llmops/`)

| Module | Responsibility | Status |
|---|---|---|
| `errors.py` | Package exception hierarchy (`LLMOpsError` + subclasses). | ✅ |
| `semver.py` | Pure SemVer parse/compare/bump + `key@vN` ↔ semver tag parsing. | ✅ |
| `diff.py` | Text + token + labelled-section prompt diffing; bump suggestion. | ✅ |
| `registry.py` | In-memory prompt registry: register/diff/rollback/promote; semver + append-only changelog; seeded from `agents.prompts.PROMPTS`. | ✅ |
| `injection.py` | Prompt-injection + jailbreak **input** scanner (signatures + heuristics + scoring) and sanitizer (fence + delimiter-neutralize + redact). | ✅ |
| `output_policy.py` | **Output** policy checks (secret/PII/system-prompt leak, unsafe content, JSON-format) with severities. | ✅ |
| `guardrails.py` | Safety layer composing injection + output policy → `allow/sanitize/block`. | ✅ |
| `rubric.py` | Weighted, threshold-gated rubric scoring + 5 built-in rubrics. | ✅ |
| `datasets.py` | Golden datasets (typed case/dataset) + bundled fixtures incl. adversarial injection probes. | ✅ |
| `judge.py` | `Judge` protocol; deterministic `HeuristicJudge` (the **fake judge**); `ModelBackedJudge` (explicit runner only). | ✅ |
| `harness.py` | Eval harness: run a prompt's system text over a dataset, scored by a judge, mean ± spread over N runs. Fake + naive responders. | ✅ |
| `ab.py` | A/B runner: two prompt versions over one dataset, case-paired, Cohen's d + winner. | ✅ |
| `regression.py` | Regression detection (overall / pass-rate / per-criterion / per-case) vs a baseline report. | ✅ |
| `models_registry.py` | Model registry + capability/cost routing config; default Kinora catalog. | ✅ |
| `tracing.py` | Structured run tracing (prompt + inputs + outputs + tokens + cost + latency + guardrail), in-memory ring-buffer store, query + aggregate API. | ✅ |
| `cache.py` | Response cache keyed by `(prompt_version, normalized_inputs)`; TTL + LRU + hit/miss stats; pluggable backend. | ✅ |
| `store.py` | DB-backed persistence (`PromptVersionStore` / `RunTraceStore` / `EvalReportStore`) over the 4 tables. | ✅ |
| `service.py` | `LLMOpsService` — the façade the API + composition wire to. | ✅ |

## DB tables (one migration, `9f3c7a1e2b4d`, down_revision `a1b2c3d4e5f6`)
- `llmops_prompt_versions` — registry rows, unique `(prompt_key, version)`.
- `llmops_changelog` — append-only registry-mutation audit.
- `llmops_runs` — run-trace rows (loose `book_id`/`session_id`, no FK).
- `llmops_eval_reports` — cached eval/A-B/regression JSONB bodies.

All additive; no FK into existing tables. Upgrade + downgrade verified to round-trip.

## Shared-file additive changes (additive-only, documented here)
- `core/config.py`: added an `# --- LLM-ops / prompt registry ---` block of
  settings (all defaulted; `llmops_enabled=False` keeps the API unchanged).
- `composition.py`: added `Any` to the typing import, a private `_llmops` field,
  and a lazy `Container.llmops` property building `LLMOpsService` (pure + offline;
  no change to existing wiring).
- `api/routes/__init__.py`: imported `llmops` and appended `llmops.router` to `ROUTERS`.
- `db/models/__init__.py`: imported + exported the 4 new models.

## API surface (`/api/llmops`, gated on `llmops_enabled`; 404 when off)
prompts list/get/diff/register/rollback · guardrails check-input/check-output ·
models list/route · datasets · rubrics · eval/ab/regression · traces + traces/rollup ·
cache/stats. All require an authenticated user; eval runs use the offline
fake responder + judge (zero credits).

## Tests (`tests/test_llmops_*.py` + `tests/test_api_llmops.py`)
136 pure/offline unit tests (semver, diff, registry, injection, output policy,
guardrails, rubric, judge, harness/AB/regression, model registry, tracing, cache,
service, API routes) + 5 DB-backed store tests that skip cleanly when
`KINORA_TEST_DATABASE_URL` (isolated `kinora_llmops_test` :5433) is unset.

## Roadmap / phases
1. Foundations — errors, semver, diff. ✅
2. Prompt registry + changelog (seeded from agents). ✅
3. Injection + output policy + guardrails. ✅
4. Judge + rubric + golden datasets. ✅
5. Eval harness + A/B + regression detection. ✅
6. Model registry + capability/cost routing. ✅
7. Run tracing + query/aggregate API. ✅
8. Response cache. ✅
9. Service façade + DB store + Alembic migration + routes + config/composition. ✅

### Future breadth (not yet built)
- A `python -m app.llmops.run` CLI (eval a version, persist a report) mirroring `app.eval.run`.
- Wire the real crew (`BaseAgent`) as a live responder behind the harness (opt-in, off by default).
- Redis-backed `CacheBackend` + DB-backed `TraceStore` swap in the composition root.
- More golden datasets (continuity, showrunner, segment) + more injection signatures.
- Prompt-version *promotion gates*: block a `promote()` when `check_regression` flags a drop.
