# Config management plane (`app.configmgmt`)

A production-grade layer that sits **beside** `app.core.config.Settings` — never
replacing it — and answers four operational questions about the live config:

1. **Is it coherent?** — cross-field invariant validation -> a typed readiness verdict.
2. **Is it safe to boot in prod?** — a hard safety gate that refuses an unsafe boot.
3. **What does each environment expect?** — named profiles, overlay precedence, diff.
4. **Where do secrets come from, safely?** — a pluggable secret-backend abstraction
   + secret-masked introspection.

It is strictly **additive**: `Settings` remains the single source of truth, loaded
from the environment / `backend/.env`. This plane *reads* `Settings`; it never
mutates the class and it never enables anything (notably `KINORA_LIVE_VIDEO`,
which is treated as a guarded spend gate — see below).

## Modules

| Module | Responsibility |
|---|---|
| `errors.py` | `Severity` (ordered `INFO<WARNING<ERROR<FATAL`), `ConfigFinding` (immutable observation: code, message, fields, hint), `ProdSafetyError`. |
| `validator.py` | The cross-field invariant suite (`INVARIANTS`), `ConfigValidator`, and the `ReadinessVerdict` roll-up. Pure functions `Settings -> list[ConfigFinding]`. Reports; never raises. |
| `safety.py` | `ProdSafetyGate` / `assert_safe_to_boot` — the only component that raises. Enforces prod non-negotiables and escalates validator `ERROR`s to `FATAL`. |
| `profiles.py` | `ProfileName` + built-in `PROFILES` presets, `overlay` (last-wins precedence + provenance), `diff_profiles` (added/removed/changed). Pure dict transforms. |
| `secrets.py` | `SecretBackend` protocol + `Env`/`File`/`Static` backends, `SecretResolver` (chain, TTL cache, rotation hooks), `SecretValue` (refuses to print itself). |
| `redaction.py` | `redacted_dump(settings)` + `is_secret_field` — secret-masked, structurally-faithful config introspection, reusing the `app.core.logging` secret vocabulary. |

## The validator (cross-field invariants)

`Settings` already self-validates single-field shapes and has two model
validators (the JWT-secret prod guard, the reasoning-provider toggle). The
validator adds invariants a type checker can't express, each emitting a finding
only when **violated**:

- `live_video.*` — if `KINORA_LIVE_VIDEO` is on, a positive USD cap + video-seconds
  cap + a usable provider key must exist (and we warn unconditionally that spend
  is possible). **We never advise turning it on.**
- `video_backend.*` — must be `dashscope`|`minimax`; minimax needs `MINIMAX_API_KEY`.
- `reasoning.*` — provider known; `openai` needs `OPENAI_API_KEY`.
- `s3.*` — bucket/endpoint present; public-base-url coherent outside local.
- `scheduler.*` — watermarks/horizons ordered.
- `budget.*` / `finops.*` — positive budgets; non-decreasing alert fractions in `(0,1]`.
- `embed.bad_dim`, `logging.bad_level`, `cors.wildcard`, `mcp.unauthenticated`,
  `secrets.default_*`.

The pass rolls into a `ReadinessVerdict` (`is_ready`, `max_severity`, counts,
`to_dict()` for a health surface). It is non-fatal by design — the *gate* decides
what blocks a boot.

## The prod-safety gate

`ProdSafetyGate.assert_safe(settings)` raises `ProdSafetyError` (carrying **every**
violation) when a `staging`/`prod` Settings is unsafe. Fatal rules:

- demo/placeholder credentials (JWT secret, API-key pepper, S3 `kinora`/`kinora-secret`,
  the local billing webhook secret),
- `KINORA_LIVE_VIDEO` armed **without** the explicit opt-in env
  `KINORA_PROD_LIVE_VIDEO_OK` (so spend can never be armed by a stray flag),
- debug posture (`LOG_LEVEL=DEBUG`, an armed `KINORA_CHAOS_ARMED`),
- any validator `ERROR`, escalated to `FATAL`.

Locally it's a near no-op (returns the verdict, raises nothing). The gate's
`environ` is injectable so tests never read or mutate the real environment.

## Profiles

`profile_for(env)` returns the posture preset for `local`/`test`/`staging`/`prod`
(alias-tolerant: `production`→`prod`, `ci`→`test`, …). Presets carry **posture
only, never secrets**. `overlay(profile_for("prod"), env_overrides)` merges layers
last-wins and records per-key provenance. `diff_profiles(staging, prod)` gives a
structured diff for drift review.

## Secrets

`SecretResolver` resolves a `SecretRef` across a chain (primary + fallbacks),
caches hits for a TTL, and fires rotation hooks when re-resolution yields changed
material. `SecretValue.reveal()` is the only path to plaintext; `repr`/`str`
render `[REDACTED]`. Backends: `EnvSecretBackend` (default, prod-friendly),
`FileSecretBackend` (Docker/K8s secret mounts, version-aware), `StaticSecretBackend`
(tests / pluggable). A Vault backend drops in by satisfying `SecretBackend`.

## Boot-path wiring (sketch — not wired here to stay additive)

```python
from app.configmgmt import assert_safe_to_boot, validate_settings
from app.core.config import get_settings

settings = get_settings()
report = assert_safe_to_boot(settings)   # raises ProdSafetyError in unsafe prod
for finding in report.verdict.warnings:  # log non-fatal advisories
    log.warning("config_warning", **finding.to_dict())
```

A `/config` debug surface can return `redacted_dump(settings)` +
`validate_settings(settings).to_dict()` safely.

## Testing

Every module has deterministic, infra-free tests (`tests/test_configmgmt_*.py`):
each invariant fires on a crafted Settings and passes on a clean one; prod-safety
refusals + the live-video opt-in; profile overlay precedence + diff; secret
resolution + caching + rotation + redaction; the readiness roll-up.
`KINORA_LIVE_VIDEO` is only ever *inspected*, never enabled.
```
