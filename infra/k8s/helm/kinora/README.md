# Kinora Helm chart

Deploys the Kinora backend's six-role process model to Kubernetes. Every backend
role is the **same image with a different command** (mirroring
`infra/docker-compose.yml` and the Terraform compute module):

| Role | Command | Inbound | Scaling |
|---|---|---|---|
| `api` | `uvicorn app.main:app` | 8000 (Service + Ingress) | HPA on CPU |
| `mcp` | `python -m app.mcp.run --http` | 8765 (ClusterIP only) | fixed |
| `render-worker` | `python -m app.queue.worker` | — | HPA on CPU (+ optional queue depth) |
| `ingest-worker` | `python -m app.ingest.recovery` | — | fixed |
| `frontend` | Nginx over the Vite build | 80 (Service + Ingress) | fixed |
| `migrate` | `alembic upgrade head` | — | pre-install/upgrade hook |

What the chart ships:

- **HPA** for `api` + `render-worker` (CPU; `render-worker` can add a Redis
  queue-depth external metric once `prometheus-adapter` exposes it).
- **PodDisruptionBudgets** for `api`, `mcp`, `frontend`, `render-worker`.
- **Probes**: HTTP for `api`/`frontend`, TCP for `mcp`, exec-import for the workers,
  plus a `startupProbe` on `api` for cold starts/migrations.
- **Resource requests/limits** per role.
- **Hardened securityContext**: non-root, read-only rootfs, dropped caps,
  `RuntimeDefault` seccomp, `automountServiceAccountToken: false`, an `emptyDir`
  `/tmp` for the ffmpeg degradation-ladder work dir.
- **NetworkPolicies**: default-deny ingress + explicit allows; the keystone rule
  restricts **MCP (8765) to in-cluster Kinora pods only** — never the Ingress.
- **ConfigMap** (non-secret env) + **Secret** (chart-managed for dev, or an
  External-Secrets/Vault-synced `existingSecret` for prod).
- **Ingress** for `api` + `frontend` only (MCP is deliberately never exposed).
- **ServiceMonitor** scraping `api` `/metrics` (§12.5; needs the Prometheus Operator).

## Use

```bash
# Lint + render
helm lint ./kinora
helm template kinora ./kinora -f kinora/values.yaml -f kinora/values-prod.yaml

# Install (dev)
helm install kinora ./kinora -n kinora-dev --create-namespace -f values-dev.yaml

# Install (prod — secret managed externally)
kubectl -n kinora create secret generic kinora-runtime-secrets --from-env-file=prod.env
helm install kinora ./kinora -n kinora --create-namespace -f values-prod.yaml
```

## Go-live gate

`config.KINORA_LIVE_VIDEO` defaults to `"false"` in every values file. Real Wan
video spend is a deliberate per-environment opt-in (kinora.md §11.1) — set it to
`"true"` only when you intend to spend video-seconds.

## Validation status

`helm lint` + `helm template | kubeconform -strict -ignore-missing-schemas` pass
for the default, dev, and prod value sets. The only `Skipped` resource is the
`ServiceMonitor` (a Prometheus Operator CRD, not in the base K8s schema).
