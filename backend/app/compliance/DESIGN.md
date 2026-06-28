# Compliance & consent subsystem — `backend/app/compliance/`

> **Owner:** Agent (compliance domain). **Status:** living roadmap.
>
> Kinora ingests user-uploaded books/PDFs and generates derived media, so it is a
> data processor with real GDPR/CCPA obligations. The `dataportability` domain
> (built by a sibling agent) already covers **GDPR export + erasure** (Art. 15
> access in machine-readable form, Art. 17 right to be forgotten). This subsystem
> **complements** that — it owns the *governance* layer around it:
>
> * **Consent management** — versioned policies, purpose-based grant/withdraw,
>   immutable proof-of-consent records (GDPR Art. 7(1) "demonstrate consent").
> * **Retention policy engine** — per-data-class TTL + lawful-basis tagging
>   (Art. 6) + automated expiry-candidate computation (Art. 5(1)(e) storage
>   limitation). It *identifies* expired data; the dataportability eraser deletes.
> * **DSAR workflow** — a data-subject-access-request state machine (Art. 12
>   one-month deadline, extensions, the request lifecycle) that *orchestrates*
>   export/erasure rather than re-implementing them.
> * **Legal hold** — suspends retention/erasure for litigation/regulatory holds
>   (a hold blocks both expiry and DSAR-erasure of the held subject's data).
> * **Compliance audit ledger** — one consolidated, hash-chained, append-only,
>   tamper-evident ledger aggregating consent/retention/DSAR/legal-hold/security/
>   moderation/billing events (mirrors the `canon_audit` chain design in §8).
> * **Policy-as-code** — declarative rules evaluated against a subject's
>   consent + retention + hold state, producing a compliance report + decisions.

## Why this is not a duplicate of `dataportability`

| Concern | Owner |
|---|---|
| *Produce* the export bundle / *delete* the rows | `dataportability` (GDPR export/erasure) |
| *Whether we're allowed to* process / how long we keep / can we erase now | `compliance` (this domain) |
| Record that consent was given, at which policy version, for which purpose | `compliance` |
| Decide which rows are expiry candidates (TTL × lawful-basis × holds) | `compliance` |
| Orchestrate a DSAR through its legal deadline | `compliance` (calls into dataportability to fulfil) |

The DSAR machine treats export/erasure execution as an injected `Fulfiller` seam,
so it stays decoupled from whatever the dataportability domain exposes.

## Layout

```
compliance/
  __init__.py
  DESIGN.md                  ← this file
  errors.py                  ← typed domain errors (translated to APIError at the router)
  enums.py                   ← StrEnum value objects: purposes, data classes, lawful bases, states
  clock.py                   ← injectable UTC clock for deterministic deadline tests
  db/models.py               ← ORM models for the domain (registered in db/models/__init__.py additively)
  repositories/*.py          ← async repos over the compliance tables
  consent/
    policy.py                ← versioned consent-policy documents + purpose catalog
    service.py               ← grant / withdraw / current-state / proof records
  retention/
    classes.py               ← the data-class registry (TTL + lawful basis defaults)
    engine.py                ← retention evaluation: expiry candidates, hold-aware
  hold/
    service.py               ← legal-hold place / lift / scope checks
  ledger/
    chain.py                 ← the hash-chain primitive (canonical-JSON + sha256 link)
    service.py               ← the consolidated append-only compliance audit ledger
  dsar/
    machine.py               ← the DSAR state machine (states, transitions, deadlines)
    service.py               ← request lifecycle orchestration + Fulfiller seam
  policy/
    rules.py                 ← policy-as-code: rule predicates + built-in rule set
    engine.py                ← evaluate rules against a subject's compliance facts
    report.py                ← consolidated compliance report builder
  service.py                 ← ComplianceService facade wiring the above together
  api/routes.py              ← FastAPI router (additively appended to ROUTERS)
  api/schemas.py             ← request/response pydantic models for the router
```

## DB tables (all additive; one Alembic migration off head `a1b2c3d4e5f6`)

* `consent_policies` — versioned policy documents (purpose, body, version, effective window).
* `consent_records` — append-only proof-of-consent (grant/withdraw events, policy version, purpose, basis).
* `retention_rules` — per-data-class TTL + lawful basis (seedable defaults, overridable).
* `legal_holds` — a hold over a subject (+ optional data-class / matter id), with place/lift events.
* `dsar_requests` — the DSAR row carrying state, kind, deadlines, and the audit trail.
* `dsar_events` — append-only state-transition log for a DSAR.
* `compliance_ledger` — the consolidated hash-chained immutable audit ledger.

## Milestones

1. **M1 — foundations**: errors, enums, clock, DB models, migration, repos. ✅
2. **M2 — consent**: versioned policies + grant/withdraw + proof records + service. ✅
3. **M3 — retention**: data-class registry + retention engine (hold-aware expiry candidates). ✅
4. **M4 — legal hold**: place/lift + scope predicate consumed by retention + DSAR. ✅
5. **M5 — ledger**: consolidated hash-chained ledger + verification. ✅
6. **M6 — DSAR**: state machine + service + Fulfiller seam. ✅
7. **M7 — policy-as-code**: rule predicates + engine + report. ✅
8. **M8 — API**: router + schemas, additively mounted. ✅
9. **M9 — tests**: unit (no infra) + integration (isolated DB) across all of the above. ✅

## Additive shared-file changes

* `app/api/routes/__init__.py` — append `compliance` router to `ROUTERS` (additive import + list entry).
* `app/db/models/__init__.py` — import + export compliance models so Alembic autogenerate
  and `Base.metadata.create_all` see them (additive).
* New Alembic revision `c0mp11ance0001` (unique id) off head `a1b2c3d4e5f6`.

Isolated test DB: `kinora_compliance_test` on :5433 — integration tests skip cleanly when
`KINORA_TEST_DATABASE_URL` is unset (mirrors the existing `requires_infra` gate).
