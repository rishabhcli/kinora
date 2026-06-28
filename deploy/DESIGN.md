# Deployment orchestration — design & roadmap (kinora.md §12, §12.6)

> Living roadmap for `deploy/`. The §12.6 *proof-of-deployment* worker shows
> Kinora **running** on Alibaba Cloud. This package is the layer that decides
> **how a new build gets there safely**: blue-green / canary rollout, SLO-gated
> automatic rollback, a deploy state machine + audit trail, artifact promotion,
> smoke gating, config/secret hydration, drain coordination with the §12.1
> render queue, and a deterministic, cloud-free simulator that proves the logic.

## Why this exists

`deploy/alibaba_render_worker.py` is honest about *running* on Alibaba (OSS +
DashScope + ECS/FC). But a hard submission requirement (§12.6) and the
engineering-depth rubric (§14) reward the *unglamorous 30%*: the machinery that
ships a new version without a stall, a double-spend, or an orphaned render job.
That machinery is here, and — critically — it is **provable offline**: the whole
rollout/rollback decision tree runs against in-memory fakes with a virtual clock,
spending **zero credits** and hitting **zero cloud endpoints**, the same
constraint the rest of the repo lives under (`KINORA_LIVE_VIDEO` OFF).

## Design principles

1. **Cloud-agnostic, pure core.** Every effect is a tiny typed Protocol
   (`seams.py`, `health.HealthProbe`, `slo.MetricSource`, `drain.DrainTarget`,
   `hydration.{ConfigSource,SecretSource}`, `smoke.SmokeCheck`). Production wires
   an Alibaba adapter; tests/simulator wire a fake. **No `oss2` / `dashscope` /
   `boto3` import anywhere in this package** (asserted by a test).
2. **No hidden clock.** Time is injected via `now: Callable[[], float]`. The
   orchestrator never `sleep`s — pacing belongs to the caller/simulator — so a
   run is deterministic and reproducible.
3. **The film never hard-stops (§12.4).** Any gate failure rolls back to the
   prior known-good digest; the running version keeps serving. A failure
   *before* anything is provisioned fails fast (nothing to roll back to).
4. **Honest safety.** Hydration refuses `KINORA_LIVE_VIDEO=on` unless explicitly
   allowed; promotion enforces the dev→staging→prod gap rule; secrets are
   redacted in every logged/audited view.
5. **Idempotency, one level up from §12.1.** Re-deploying the digest already live
   in a target is a no-op success — the deployment analogue of the `shot_hash`
   idempotency that stops the render queue double-spending.

## Module map

| Module | Responsibility |
|---|---|
| `models.py` | Frozen value types + the **deploy state machine** (`DeployState`, `LEGAL_TRANSITIONS`, `can_transition`), `Artifact` (content-addressed), `SLOTarget`/`SLOResult`, `ServiceRole` (the §0 process roles), `Environment`. |
| `audit.py` | Append-only, sequenced, monotonic-clocked `AuditTrail` over a pluggable `AuditSink`. The forensic record of every transition + decision. |
| `health.py` | `HealthProbe` client of the backend's `/ready` contract; `StabilityWindow` (N consecutive healthy samples); `HealthGate.wait_until_stable`. |
| `slo.py` | `SLOEvaluator` folds a metric stream against targets; consecutive-breach tolerance; `DEFAULT_RENDER_SLOS` tuned to the off-gate reality. |
| `strategies.py` | Pure rollout planners → `RolloutPlan`: `BlueGreenStrategy`, `CanaryStrategy` (5→25→50→100), `RecreateStrategy`. |
| `hydration.py` | Merge config + secrets, enforce required keys + the live-video gate, produce a `HydratedConfig` with a secret-free `redacted()` view + stable `fingerprint()`. |
| `smoke.py` | `SmokeGate` runs the Kinora smoke suite (health, ready, provider-preflight, degraded Ken-Burns render) before traffic shifts; required vs advisory checks. |
| `drain.py` | `DrainCoordinator` cordon → quiesce → terminate of the retiring render-worker; releases wedged leases at the deadline so a survivor re-claims them. |
| `seams.py` | `Provisioner` + `TrafficRouter` — the bring-up / traffic-shift effects. |
| `orchestrator.py` | `DeploymentOrchestrator` — the state-machine engine that ties it all together, auto-rolling-back on any gate failure. |
| `fakes.py` | In-memory doubles for every seam: `FakeProvisioner`, `FakeTrafficRouter`, `ScriptedHealthProbe`, `ScriptedMetricSource`, `FakeRenderWorker`, `VirtualClock`. |
| `simulator.py` | The deterministic, cloud-free rollout/rollback simulator + canonical scenarios + a `python -m deploy.orchestrator.simulator` CLI. |

## The deploy state machine

```
PENDING → HYDRATING → PROVISIONING → ROLLING_OUT ⇄ VERIFYING → PROMOTING → SUCCEEDED
                                          │            │
                                          └────────────┴──→ ROLLING_BACK → ROLLED_BACK
              (pre-provision failure) ─────────────────────────────────→ FAILED
              (operator)              ABORTING ──→ ROLLED_BACK / FAILED
```

`ROLLING_OUT ⇄ VERIFYING` is the canary loop: shift weight → verify SLOs →
advance to the next weight, repeating until 100% or a breach.

## Rollout decision flow (per step)

1. Shift traffic to the step's weight.
2. **Health gate** — new fleet must reach a stable window or → rollback.
3. **Smoke gate** (staging / first step) — required checks pass or → rollback.
4. **SLO verification** — fold N metric samples; any target breaching beyond its
   tolerance → rollback (early-out on first breach to limit blast radius).
5. Advance. After the final 100% step: drain the old render-worker, mark the
   digest succeeded/live, → SUCCEEDED.

## Status — what's built (Phase 1–3, done)

- [x] **Phase 1 — core types + state machine.** `models.py` with full legal-edge
  table, reachability invariant (every state can reach a terminal), abortable
  set. 12 tests.
- [x] **Phase 2 — gates.** Health stability windows, SLO evaluator with
  consecutive-breach tolerance + worst-value tracking, smoke gate with
  required/advisory + short-circuit + throwing-check safety, hydration with
  required-key + live-video refusal + secret redaction + fingerprint. ~45 tests.
- [x] **Phase 3 — orchestration + rollback + drain + promotion + simulator.**
  `DeploymentOrchestrator` (canary + blue-green + recreate), auto-rollback on
  health/smoke/SLO failure, pre-provision fast-fail, idempotent no-op, operator
  abort, drain coordination (clean + timed-out-release), promotion gap/soak/
  idempotency rules, and the deterministic simulator with 7 canonical scenarios.
  ~57 tests.

**Total: 114 offline tests, clean `ruff` + strict `mypy`, zero cloud calls.**

## Roadmap — next phases

- [ ] **Phase 4 — Alibaba adapters (production wiring).** Real Protocol
  implementations behind the same seams: `EssProvisioner` (Auto Scaling group
  desired-capacity), `SlbTrafficRouter` (weighted listener / DNS weight),
  `ReadyHttpProbe` (`GET /ready` fanout + quorum), `PrometheusMetricSource`
  (scrape `/metrics`), `KmsSecretSource`, `RedisQueueDrainTarget` (set the real
  `RenderWorker` stop event via a control key + read in-flight from queue stats).
  Each gated behind an integration marker; **never** run against real endpoints
  in CI.
- [ ] **Phase 5 — multi-role orchestration.** A `ReleaseOrchestrator` that
  promotes one digest across the §0 roles in dependency order
  (migrate → mcp → api → workers → frontend), each role its own
  `DeploymentOrchestrator`, with a release-level audit trail and a single
  release-wide rollback.
- [ ] **Phase 6 — schema/migration gating.** Alembic forward/backward
  compatibility check before promote (expand/contract migration safety), so a
  rollback of the image never strands the DB on a newer schema.
- [ ] **Phase 7 — progressive-delivery analysis.** Statistical SLO comparison
  (canary vs baseline fleet, not just absolute thresholds) — a t-test / Mann-
  Whitney gate over the §13 metrics so a regression *relative to the incumbent*
  trips rollback even when both are inside absolute bounds.
- [ ] **Phase 8 — persistence + resume.** Persist the audit trail + state to
  RDS/OSS so an orchestrator restart resumes a mid-flight rollout instead of
  stranding a half-shifted fleet.
- [ ] **Phase 9 — CLI / FastAPI surface.** `deploy/cli.py` and an optional
  `/deploy` admin route to trigger + watch a rollout (auth-gated), feeding the
  §12.5 metrics panel a rollout timeline.

## Testing & verification

```bash
# from the repo root, with a venv that has ruff/mypy/pytest:
ruff check deploy/orchestrator deploy/tests deploy/conftest.py
mypy --python-version 3.12 --disallow-untyped-defs --ignore-missing-imports \
     deploy/orchestrator deploy/tests deploy/conftest.py
pytest deploy/tests -q

# watch the rollout/rollback logic prove itself, offline:
python -m deploy.orchestrator.simulator --scenario all
```

All tests inject fakes for every provider/cloud seam and run on a virtual clock,
so they never hit Alibaba/DashScope/OSS and never spend a credit
(`KINORA_LIVE_VIDEO` stays OFF, as required).
