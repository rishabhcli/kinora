# Runtime feature-flag / dynamic-config plane (`app.flags.plane`)

The unified, **read-through** runtime config plane. Where `app.flags` (the
sibling package) is a LaunchDarkly-style **A/B experimentation** platform (§13),
this is the **operational config** layer: a single typed place to ask "what is
the effective value of flag X for this context, right now?" and to flip behaviour
for a book / user / cohort / provider at runtime — instead of the scattered
`if settings.x` checks across the codebase.

## Principles

1. **Settings is the one base source.** Every flag binds to a `FlagSpec` whose
   default is stamped from a live `Settings` field (`registry.bind_settings`).
   The plane overlays runtime decisions on top — it never invents config.
2. **Layered resolution, most-specific wins.** `base (Settings) -> static
   override -> targeting rule -> rollout`. Of the matching targeting rules, the
   most *constrained* (then highest `priority`) wins. Rules target the four
   Kinora dimensions: **book / user / cohort / provider**.
3. **Deterministic, sticky rollouts.** Percentage ramps reuse the §13
   `app.flags.hashing.bucket_bp` bucketing, so a unit never flaps as a ramp
   grows (monotone), and assignments are reproducible across processes.
4. **Kill-switch safety — can only ever be forced *down*.** A `kill_switch=True`
   flag (`kinora.live_video`, `budget.ceiling_usd`) is guarded by
   `KillSwitchGuard`: a write that would *raise* it is rejected
   (`KillSwitchViolation` -> HTTP 409), and the resolver *clamps* on read as a
   backstop. **Live video can never be turned on through this surface.**
5. **Total read path.** `is_enabled` / `get_*` / `get` never raise; an unknown
   key or a malformed overlay degrades to the safe base.
6. **Validated, audited, notified writes.** Every override/rule value is coerced
   to the flag's type, run through the guard, diffed into an audit record (reuses
   `app.flags.audit`), and published to subscribers.
7. **Zero-infra core.** The default `InMemoryOverrideStore` means the whole plane
   runs and is fully unit-tested with no Postgres/Redis. A DB/Redis-backed store
   slots in behind the `OverrideStore` protocol without touching the plane.

## Module map

| Module | Responsibility | Infra |
|---|---|---|
| `spec.py` | `FlagSpec` + `FlagType` — typed defaults, coercion, choices, kill-switch flag | none |
| `context.py` | `FlagContext` — book/user/cohort/provider + bucketing-unit resolution | none |
| `overrides.py` | `OverrideLayer`, `StaticOverride`, `TargetingRule`, `PercentRollout` (sticky) | none |
| `safety.py` | `KillSwitchGuard` — a guarded flag can only be forced down | none |
| `resolution.py` | `LayeredResolver` — the total resolution waterfall + `Resolution`/reason | none |
| `subscriptions.py` | `SubscriptionHub` — synchronous, fault-isolated change fan-out (hot-reload) | none |
| `audit.py` | `PlaneAuditRecord` — structural change records (reuses `app.flags.audit`) | none |
| `registry.py` | `FlagRegistry` + `build_default_registry` (the Kinora flag catalog) + `bind_settings` | none |
| `store.py` | `OverrideStore` protocol + `InMemoryOverrideStore` (snapshot/export) | none |
| `plane.py` | `RuntimeConfigPlane` — the facade (typed read API + validated/audited writes) | none |
| `api.py` | `/runtime-config` admin router (read + write, kill-switch -> 409) | FastAPI |

## Wiring (additive)

* `Container.runtime_config_plane` (lazy; `RuntimeConfigPlane.from_settings`).
* `runtime_config_router` appended to `app.api.routes.ROUTERS` at
  `/api/runtime-config` — distinct from the `/api/flags` experimentation API.
* New `FlagSpec`s in `build_default_registry` mirror existing `Settings` fields;
  adding one changes no behaviour (the base value is still whatever Settings
  says) — it merely makes the knob addressable through the unified plane.
