# Infra changelog

## Unreleased — production-grade IaC build-out

A decade-scale expansion of `infra/` from a single flat Alibaba Terraform root +
two Dockerfiles + a basic Prometheus scrape into a full, multi-target,
multi-environment IaC platform. **No application code touched; everything stays
under `infra/` plus the infrastructure workflows.** The Terraform topology has
not been applied to the live single-node deployment.

### Terraform
- **8 reusable Alibaba modules**: `network` (VPC, vswitches, optional NAT,
  fail-closed SGs incl. the intra-VPC-only MCP rule), `secrets` (db/redis/jwt/mcp
  resolution), `storage` (OSS — private, versioned, SSE, lifecycle), `database`
  (RDS pgvector + backup policy + HA toggle), `redis` (Tair), `compute` (the
  per-role ECS fleet via cloud-init), `observability` (CloudMonitor + SLS), and a
  composing `stack` module.
- **3 Alibaba environments** (`dev` / `staging` / `prod`) as thin roots over
  `stack`, differing only by sizing/scale; the legacy flat root is preserved for
  back-compat.
- **Portable AWS target**: 7 `aws-*` modules + an `aws-prod` env — VPC/subnets/SGs,
  S3, RDS pgvector, ElastiCache, Secrets Manager, and ECS-on-Fargate (one service
  per role, api+frontend behind an ALB, MCP internal via Cloud Map) with CPU
  autoscaling. Same fail-closed posture, same `KINORA_LIVE_VIDEO=off` default.

### Kubernetes
- **Helm chart** (`k8s/helm/kinora`): the six roles as one image / many commands,
  HPA (api + render-worker), PodDisruptionBudgets, HTTP/TCP/exec probes +
  startupProbe, resource requests/limits, hardened securityContext (non-root,
  read-only rootfs, dropped caps, RuntimeDefault seccomp), default-deny + per-role
  NetworkPolicies (the **MCP-intra-app** keystone rule), ConfigMap/Secret (or an
  External-Secrets `existingSecret`), Ingress (api+frontend only), ServiceMonitor,
  and the migrate Job as a pre-install/upgrade hook. `dev`/`prod` value overlays.
- **Kustomize** base + dev/prod overlays as a raw-manifest alternative.

### Docker
- Hardened `backend.Dockerfile` (BuildKit cache mounts, `tini` PID 1, system
  `ffmpeg` for the degradation ladder, non-root uid 10001) and `desktop.Dockerfile`
  (pnpm store cache mount, healthcheck). Per-Dockerfile `.dockerignore`s and a
  `.hadolint.yaml`.

### CI/CD (`.github/workflows/`)
- `infra-validate.yml` — terraform fmt/validate across every module + env,
  hadolint, yamllint, shellcheck, helm lint, kubeconform, kustomize, compose
  config, promtool/amtool/dashboard checks.
- `infra-security.yml` — Checkov + Trivy (config + fs) with SARIF upload.
- `infra-release.yml` — build + Trivy-scan both images, gated push (GHCR default /
  ACR/ECR via vars), and a manual-dispatch gated deploy hand-off.

### Observability
- Prometheus recording + alerting rules (22, all keyed to real
  `app/observability/metrics.py` series; budget §11.1 + buffer §12.5 tripwires
  incl. **spend-with-gate-off**), Alertmanager, Loki + Promtail, an OpenTelemetry
  collector, and two Grafana dashboards (buffer sawtooth; pipeline + budget) with
  provisioning. Layered via `docker-compose.observability.yml`.

### Tooling
- `infra/Makefile` validation harness mirroring CI; `scripts/gen-secrets.sh` +
  `scripts/stack-smoke.sh`; per-area READMEs.

### Verified locally
`terraform validate` (all envs + AWS), `helm lint` + `kubeconform` (default/dev/prod
+ kustomize), `hadolint`, a real `docker build` + boot of the backend image,
`promtool`/`amtool`, `yamllint`, `shellcheck`, and `docker compose config` (base +
observability layer) all pass.
