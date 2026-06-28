# Kinora Infrastructure — Design & Roadmap

> **Domain:** everything under `infra/`. Production-grade Infrastructure-as-Code for
> the Kinora backend (the §process model from `CLAUDE.md` / `README.md`) on
> **Alibaba Cloud** (primary submission target, kinora.md §12.6) and a portable
> **AWS** target, plus Kubernetes/Helm, hardened Dockerfiles, CI/CD, and an
> observability stack.
>
> This file is the living roadmap. Each phase records what was built, where it
> lives, and its validation status. **Nothing here is ever `terraform apply`-ed** —
> all Terraform is validated with a local backend and no real cloud credentials.

---

## Guiding constraints (from CLAUDE.md / kinora.md / the task brief)

- **`KINORA_LIVE_VIDEO` stays OFF** in every environment default. Flipping it is a
  deliberate per-env opt-in (`kinora_live_video = true`), never the default.
- **The process model is canonical** — every backend role is the *same image* with
  a different command: `api` (uvicorn; scheduler + ingest in-process),
  `ingest-worker` (`app.ingest.recovery`), `render-worker` (`app.queue.worker`),
  `mcp` (`app.mcp.run --http`), `frontend` (Nginx over the Vite build).
- **The MCP canon server (8765) is never internet-facing** — intra-VPC / intra-mesh
  only; the `MCP_AUTH_TOKEN` bearer is defense-in-depth on top.
- **Fail-closed networking** — no `0.0.0.0/0` on API/SSH; credentialed CORS so no
  wildcard origin; `JWT_SECRET` must be a real secret in non-local.
- **Local infra quirks preserved** — host Postgres on **5433** (5432 clashes),
  `S3_PUBLIC_BASE_URL` + `minio:9000`→`localhost:9000` rewrite, demo login.
- **Additive coordination** — nine other agents work in parallel. Stay strictly in
  `infra/` (+ `.github/workflows/` for CI). Reference docker-compose service names
  additively; never rename existing services or touch application code.

---

## Target topology (logical)

```
                         +---------------- Internet ----------------+
                         |                                          |
                    [ frontend :80 ]                           [ api :8000 ]
                    Nginx + Vite build                     uvicorn (REST/SSE/WS)
                         |   admin_cidr                      admin_cidr | scheduler+ingest in-proc
                         +---------------+--------------------------+
                                         |  (intra-VPC / intra-mesh)
        +--------------------------------+--------------------------------+
        |                 |              |              |                 |
 [ render-worker(s) ] [ ingest-worker ] [ mcp :8765 ]  |                 |
  app.queue.worker    app.ingest.recovery canon memory |                 |
        |                 |              |              |                 |
        +--------+--------+-------+------+------+-------+--------+--------+
                 |                |             |                |
          [ Postgres+pgvector ] [ Redis/Tair ] [ Object store ] [ DashScope ]
            canon/episodic        queue/locks     OSS / S3         (egress only)
```

---

## Phase plan & status

| Phase | Scope | Status | Primary locations |
|---|---|---|---|
| 0 | Survey existing infra; roadmap | done | this file |
| 1 | Terraform refactor -> reusable Alibaba modules + dev/staging/prod envs | done | `terraform/modules/*`, `terraform/environments/*` |
| 2 | Portable AWS target (mirror modules) | done | `terraform/modules/aws-*`, `terraform/environments/aws-*` |
| 3 | Kubernetes / Helm chart (6 roles, HPA, PDB, probes, limits, NetworkPolicy) | done | `k8s/helm/kinora/*`, `k8s/base/*` |
| 4 | Hardened multi-stage Dockerfiles + dockerignore + hadolint | done | `docker/*.Dockerfile`, `.hadolint.yaml`, `.dockerignore` |
| 5 | CI/CD pipeline (build/lint/test/scan/deploy) | done | `.github/workflows/infra-*.yml` |
| 6 | Observability / logging stack | done | `observability/*`, `prometheus/*` |
| 7 | Validation harness + Makefile + docs | done | `infra/Makefile`, `scripts/*`, READMEs |

> Status legend: done · in progress · planned.

---

## Phase 1 — Terraform modules + environments (Alibaba)

**Goal.** Turn the flat single-environment `terraform/*.tf` into composable modules
so dev/staging/prod differ only by `tfvars`, with no copy-paste drift.

**Modules** (`terraform/modules/`):
- `network` — VPC, per-zone vswitches, optional NAT gateway + EIP, security groups
  (app tier / data tier) with fail-closed rules + the intra-VPC MCP rule.
- `storage` — OSS bucket (private, versioned, SSE, lifecycle, referer/CORS).
- `database` — ApsaraDB RDS for PostgreSQL (pgvector), DB + account, backups.
- `redis` — Tair/Redis (AUTH, intra-VPC, backup window).
- `secrets` — `random_password` for db/redis/jwt/mcp + resolution logic.
- `compute` — the per-role ECS fleet via cloud-init, one image many commands.
- `observability` — CloudMonitor alarm group + SLS log project/store scaffolding.

**Environments** (`terraform/environments/`):
- `dev` — single zone, smallest classes, `kinora_live_video=false`, relaxed scale.
- `staging` — two zones, mid classes, live video off, 1 render-worker.
- `prod` — two zones, prod classes, live video off by default, >=2 render-workers,
  longer backups.

The legacy top-level `terraform/*.tf` is preserved as the single-env entrypoint
(back-compat for the README's `cd infra/terraform` flow) and the modules are the
forward path used by the environment stacks.

## Phase 2 — Portable AWS target

A faithful AWS mirror so the architecture isn't Alibaba-locked: VPC + subnets +
SGs, S3 (the OSS analogue, same boto3 `ObjectStore`), RDS PostgreSQL (pgvector),
ElastiCache Redis, Secrets Manager, and an ECS-on-Fargate service set (one task def
per role) behind an ALB. Same fail-closed network posture, same secret resolution,
same `KINORA_LIVE_VIDEO=off` default. An EKS variant hands off to the Helm chart.

## Phase 3 — Kubernetes / Helm

A single chart deploying all six roles as the same image with different commands,
plus: HPA on api + render-worker (CPU + custom queue-depth metric), PodDisruption
Budgets, liveness/readiness/startup probes, resource requests/limits,
securityContext (non-root, read-only rootfs, dropped caps), NetworkPolicies (MCP
ingress only from app pods, default-deny), an external-secrets-friendly Secret, a
ConfigMap for the non-secret env, Services, an Ingress for api+frontend, a
ServiceMonitor, and the migrate Job as a Helm hook. A `kustomize` base + dev/prod
overlays mirror the chart for GitOps shops that prefer raw manifests.

## Phase 4 — Hardened Dockerfiles

BuildKit cache mounts, non-root, dropped setuid, tini PID-1, multi-stage,
`.dockerignore`, a `hadolint` config, and a worker-runtime variant note (ffmpeg
present for the degradation ladder). Pinned base tags; digests left as a CI
follow-up so local builds stay reproducible without a network round-trip.

## Phase 5 — CI/CD

Workflow files (separate from the app `ci.yml`, additive): `infra-validate.yml`
(terraform fmt/validate across every env + module, hadolint, yamllint, shellcheck,
helm lint, kubeconform), `infra-security.yml` (checkov/tfsec/trivy), and
`infra-release.yml` (build + scan + push the backend/frontend images, then a gated
deploy job that no-ops unless deploy secrets exist).

## Phase 6 — Observability

Prometheus rules (recording + alerting on buffer health, queue depth, render
latency, video-seconds budget, error rates), Alertmanager routing, Grafana
dashboards + provisioning (buffer sawtooth §12.5, budget burn §11.1, queue health
§12.1/§12.2), a Loki + Promtail logging stack, and an OpenTelemetry collector — all
as a layerable compose file (`docker-compose.observability.yml`) and as Helm
values for the cluster path.

## Phase 7 — Validation harness + docs

`infra/Makefile` with `validate`, `fmt`, `lint`, `security`, `compose-config`,
`helm-lint`, `k8s-validate` targets; `infra/scripts/*` helpers; per-area READMEs;
and this DESIGN.md kept current.

---

## Validation status (all run + PASSING locally unless noted)

| Tool | Present | Result |
|---|---|---|
| `terraform` 1.9.8 | yes | `fmt -check -recursive` clean; `init -backend=false` + `validate` PASS for the legacy root, all 4 envs (dev/staging/prod/aws-prod), and every module (transitively + standalone) |
| `docker` 29.5 | yes | `compose config -q` PASS (base + observability layer merge verified); backend image **builds + boots** (uid 10001, ffmpeg present, `create_app()` OK with `DASHSCOPE_API_KEY=test`) |
| `hadolint` 2.14 | yes (installed) | both Dockerfiles PASS with `.hadolint.yaml` |
| `helm` v3.16 | yes (installed) | `helm lint` PASS for default/dev/prod |
| `kubeconform` 0.8 | yes (installed) | `helm template \| kubeconform -strict` PASS (default 22 / dev 16 / prod 22+1 CRD-skipped); kustomize dev 12 / prod 16 PASS |
| `kubectl` (kustomize) | yes | overlays build + validate clean (no deprecation warnings) |
| `yamllint` 1.38 | yes (installed) | full `infra/` tree clean (Helm Go-templates ignored) |
| `shellcheck` 0.11 | yes | helper scripts PASS |
| `promtool` / `amtool` | via docker | rules (22) + config + alertmanager config PASS |
| `checkov`/`trivy` | no | IaC + image security scan — configs shipped (`.checkov.yaml`); runs in `infra-security.yml` / `infra-release.yml` |

Anything absent locally is wired into CI (Phase 5) and noted here so the gap is
explicit rather than silent.

---

## Open follow-ups / future work

- Wire a real remote Terraform state backend (OSS / S3 + DynamoDB lock) per env —
  scaffolded as commented `backend` blocks so `init -backend=false` stays clean.
- Replace cloud-init secret injection with KMS / Secrets Manager / OOS (noted in
  `cloud-init.sh.tftpl`); the env-file path is the honest MVP.
- A `terraform-docs`-generated module reference once the tool is in CI.
- Cluster autoscaler / Karpenter values for the EKS path.
- Pin Docker base images by digest in CI after a trusted resolve step.
</content>
