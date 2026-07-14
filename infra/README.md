# Kinora — Infrastructure (`infra/`)

Production-grade Infrastructure-as-Code for the Kinora backend: the local
docker-compose stack, multi-environment Terraform for **Alibaba Cloud** (the
submission target, kinora.md §12.6) and a portable **AWS** mirror, a
**Kubernetes/Helm** chart (+ Kustomize), hardened **Dockerfiles**, **CI/CD**
workflows, and an **observability** stack.

The live hackathon instance uses the separate single-node bootstrap in
[`deploy/alibaba_single_node.sh`](../deploy/alibaba_single_node.sh). The larger
Terraform roots in this directory are validated infrastructure definitions; they
have not been applied to that instance. `KINORA_LIVE_VIDEO` stays **off** in every
default (kinora.md §11.1).

## Layout

```
docker-compose.yml                  the local stack (the §process model)
docker-compose.observability.yml    layer: Alertmanager/Loki/Promtail/OTel/Grafana
docker/                             hardened multi-stage Dockerfiles + dockerignores + hadolint
prometheus/                         scrape config + recording/alerting rules
observability/                      alertmanager, loki, promtail, otel, grafana provisioning + dashboards
terraform/
  *.tf                              legacy single-env Alibaba root (back-compat)
  modules/                          reusable building blocks (network/secrets/storage/database/
                                    redis/compute/observability/stack + aws-* mirrors)
  environments/                     dev · staging · prod (Alibaba) · aws-prod (portable AWS)
k8s/
  helm/kinora/                      the chart (6 roles, HPA, PDB, probes, NetworkPolicies, migrate hook)
  kustomize/                        base + dev/prod overlays (raw-manifest alternative)
scripts/                            gen-secrets.sh, stack-smoke.sh
Makefile                            local validation harness (mirrors the CI gate)
```

## The process model (one image, many commands)

Every backend role runs the **same image** with a different command — the local
compose, the Terraform `compute` module, and the Helm chart all express this
identically:

| Role | Command | Inbound |
|---|---|---|
| `api` | `uvicorn app.main:app` | 8000 (public) |
| `ingest-worker` | `python -m app.ingest.recovery` | — |
| `render-worker` | `python -m app.queue.worker` | — |
| `mcp` | `python -m app.mcp.run --http` | 8765 (**intra-VPC / intra-mesh only**) |
| `frontend` | Nginx over the Vite build | 80 (public) |
| `migrate` | `alembic upgrade head` | one-shot |

## Quick start

```bash
# Local stack (data plane + the six roles)
docker compose -f infra/docker-compose.yml up -d --build
# + observability
docker compose -f infra/docker-compose.yml -f infra/docker-compose.observability.yml up -d
infra/scripts/stack-smoke.sh            # OBS=1 to also check Grafana/Prometheus

# Validate ALL infra locally (mirrors CI)
cd infra && make validate
```

## Deploy targets

- **Alibaba (per env):** `cd terraform/environments/{dev,staging,prod}` →
  `cp terraform.tfvars.example terraform.tfvars` → `terraform init && validate && plan`.
  Fail-closed inputs (`admin_cidr`, `ssh_cidr`, `cors_origins`) have no defaults
  and reject wildcards; `jwt_secret`/`mcp_auth_token` auto-generate.
- **AWS (portable):** `cd terraform/environments/aws-prod` (ECS-on-Fargate + ALB +
  RDS pgvector + ElastiCache + S3 + Secrets Manager).
- **Kubernetes:** `helm install kinora k8s/helm/kinora -f .../values-prod.yaml`, or
  `kubectl apply -k k8s/kustomize/overlays/prod`.

## Validation

| Area | Tool | Where |
|---|---|---|
| Terraform | `terraform fmt -check` + `init -backend=false` + `validate` | `make tf-fmt tf-validate`, CI |
| Dockerfiles | `hadolint` + `docker build` | `make hadolint`, CI |
| Helm/k8s | `helm lint` + `kubeconform`, `kubectl kustomize` | `make helm-lint k8s-validate kustomize-validate`, CI |
| YAML/shell | `yamllint` + `shellcheck` | `make yamllint shellcheck`, CI |
| Prometheus/AM | `promtool` + `amtool` | `make prom-check am-check`, CI |
| Compose | `docker compose config` | `make compose-config`, CI |
| Security | Checkov + Trivy (SARIF → Security tab) | `infra-security.yml` |

The CI gate lives in `.github/workflows/infra-validate.yml`,
`infra-security.yml`, and `infra-release.yml`.
