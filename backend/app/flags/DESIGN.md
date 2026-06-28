# Feature Flags & Experimentation Platform (`app.flags`)

A self-contained flag + A/B experimentation platform for Kinora. The design
goal is a **pure, deterministic evaluator usable with zero infrastructure**,
wrapped by optional persistence (Postgres), an evaluation cache with streaming
invalidation (Redis), an admin API, an SDK-style client, and a full audit
trail. Maps to kinora.md §13 (the metrics / eval harness): the same deterministic
bucketing lets the §13 crew-vs-baseline experiment, the watermark-tuning A/B
(§18 Q4), and any product rollout (live-video gate, render ladder thresholds)
all flow through one typed, auditable surface instead of ad-hoc `if` checks.

## Design principles

1. **Pure core, no I/O.** `hashing`, `models`, `targeting`, `rollout`,
   `evaluator`, `experiment`, `stats` import nothing from the network/DB. They
   are deterministic, side-effect-free, and exhaustively unit-tested. Anyone can
   `from app.flags import FlagEvaluator` and evaluate against an in-memory
   snapshot with no DASHSCOPE key, no Postgres, no Redis.
2. **Deterministic bucketing.** Every assignment is a stable hash of
   `salt + ":" + unit_id` mapped onto `[0, 10000)` basis points. The same unit
   always lands in the same bucket for a given salt, so rollouts are sticky and
   experiments are reproducible across processes and restarts.
3. **Additive on shared files.** New Settings fields (defaults preserve current
   behavior), a new router appended to `ROUTERS`, new models imported in
   `db/models/__init__.py`, a new Alembic revision on the current head. No edits
   to other agents' code.
4. **Off by default / fail safe.** A missing/disabled flag returns its default
   variation; evaluation never raises into the caller — it returns a reasoned
   `Evaluation` with a `reason` enum. The store/cache fail open.

## Module map

| Module | Responsibility | Infra |
|---|---|---|
| `hashing.py` | Deterministic bucketing (`bucket_bp`, `bucket_unit`, `variant_for`) | none |
| `models.py` | Frozen dataclasses: `Flag`, `Variation`, `Rule`, `Clause`, `Rollout`, `Prerequisite`, `Target`, `FlagSnapshot`, `Evaluation` | none |
| `context.py` | `EvalContext` attribute bag with typed accessors | none |
| `targeting.py` | Predicate clause evaluation (eq/in/gt/lt/contains/semver/regex/...) | none |
| `rollout.py` | Percentage / progressive rollout + weighted multivariate distribution | none |
| `evaluator.py` | `FlagEvaluator` — pure rule waterfall; emits `Evaluation` with reason | none |
| `experiment.py` | `ExperimentEngine` — assignment, exposure de-dup keys, metric specs | none |
| `stats.py` | Two-proportion z / Welch t + **sequential-safe** mSPRT / always-valid CIs | none |
| `errors.py` | Typed error hierarchy | none |
| `serialization.py` | JSON (de)serialization of flags/experiments/snapshots | none |
| `client.py` | `FlagsClient` SDK facade over a snapshot; `*_variation` accessors, exposure sink | none |
| `db_models.py` | SQLAlchemy ORM rows (`feature_flags`, `flag_experiments`, `flag_exposures`, `flag_audit`) | Postgres |
| `store.py` | Async Postgres-backed CRUD repos for flags/experiments | Postgres |
| `audit.py` | Append-only change log + structural diffing | Postgres |
| `cache.py` | `FlagCache` — versioned snapshot cache with Redis pub/sub streaming invalidation; in-memory fallback | Redis |
| `service.py` | `FlagService` — orchestrates store + cache + audit + evaluator | Postgres+Redis |
| `api.py` | Admin + evaluation FastAPI router | — |

## Determinism contract

`bucket_bp(unit, salt) -> int in [0, 10000)`:
- SHA-256 of `f"{salt}:{unit}"`, take first 8 hex → uint32 → scaled into
  `[0, 10000)` basis points (so 1bp resolution, 100.00% granularity).
- Independent salts (one per flag rollout, one per experiment) keep a unit's
  rollout bucket uncorrelated with its experiment bucket (no carry-over bias).

## Evaluation waterfall (`FlagEvaluator.evaluate`)

```
1. flag missing            -> default,       reason=FLAG_NOT_FOUND
2. flag.archived           -> off_variation, reason=FLAG_ARCHIVED
3. flag disabled (off)     -> off_variation, reason=FLAG_OFF
4. prerequisite fails      -> off_variation, reason=PREREQUISITE_FAILED
5. individual target match -> targeted var,  reason=TARGET_MATCH
6. first matching rule     -> rule variation (or rule rollout), reason=RULE_MATCH
7. fallthrough rollout     -> bucketed var,  reason=FALLTHROUGH
8. else                    -> default var,   reason=DEFAULT
```

Every path returns an `Evaluation(value, variation_key, variation_index, reason,
rule_id, flag_version)`. Pure and total — never raises into the caller.

## Sequential-testing safety (`stats.py`)

A/B peeking inflates false positives. We provide:
- Fixed-horizon two-proportion z-test + Welch t-test (for completeness).
- **mSPRT** (mixture sequential probability ratio test) giving an always-valid
  p-value and confidence sequence that controls type-I error under continuous
  monitoring — you may peek every event and stop the moment it crosses α.
- Guardrail check: a one-sided always-valid test that flags a regression.

## Milestones (status)

- **M1 (pure core):** hashing, models, context, targeting, rollout, evaluator,
  errors. Fully tested, no infra. — DONE
- **M2 (SDK + serialization):** serialization, client, experiment assignment. — DONE
- **M3 (stats):** two-proportion z, Welch t, mSPRT always-valid, guardrails,
  power/sample-size. — DONE
- **M4 (persistence):** ORM models (4 tables), Alembic migration `f7a3b2c19e44`
  on head `a1b2c3d4e5f6`, async store, append-only audit log w/ diffing. — DONE
- **M5 (cache + streaming):** versioned snapshot cache + Redis pub/sub
  invalidation, fail-open. — DONE
- **M6 (service + API):** `FlagService` + `InMemoryFlagService`, admin + eval API
  (12 endpoints under `/api/flags`), additive Settings + DI (`Container.flag_service`)
  + ROUTERS wiring. — DONE
- **M7 (depth):** semver/regex/percentage operators, progressive rollout schedule,
  experiment decision report (always-valid ship/hold/rollback), canonical Kinora
  flag/experiment defaults, bucketing diagnostics. — DONE

### Test coverage

~165 tests: pure-core (hashing/models/targeting/evaluator/experiment/stats/
client/serialization/cache/report/defaults, no infra) + DB-backed store/service
integration (skips without `KINORA_FLAGS_TEST_DATABASE_URL`) + gateway API tests
(skip without throwaway Postgres+Redis+S3). `make lint` (ruff + mypy) green.

### Remaining roadmap (future)

- Continuous (mean-metric) experiment reports via Welch always-valid sequences.
- A background cache refresher task wired into the app lifespan (the stream
  consumer `FlagCache.listen` is built; an opt-in refresher loop would close it).
- A seed CLI (`python -m app.flags.seed`) persisting `default_flags()` /
  `default_experiments()` into a fresh deployment.
- Frontend admin surface (the API contract is stable and JSON-only).

## Additive shared-file changes

- `app/core/config.py`: `flags_enabled`, `flags_cache_ttl_s`, `flags_default_salt`,
  `flags_stream_channel` (all defaulted; behavior unchanged when untouched).
- `app/api/routes/__init__.py`: append `flags.router` to `ROUTERS`.
- `app/db/models/__init__.py`: import the four flag ORM rows.
- New Alembic revision (unique id) on head `a1b2c3d4e5f6`.

## Isolated test DB

Integration tests use `KINORA_FLAGS_TEST_DATABASE_URL` (e.g.
`postgresql+asyncpg://kinora:kinora@localhost:5433/kinora_flags_test`) and skip
cleanly when unset. The pure-core tests need no infra at all.
