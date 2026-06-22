# Kinora — Alibaba Cloud infrastructure (Terraform)

Ready-to-apply Infrastructure-as-Code for the Kinora backend on Alibaba Cloud
(kinora.md §6 / §12.6). It provisions the full managed footprint:

| Resource | Service | Purpose |
|---|---|---|
| `alicloud_vpc` + `alicloud_vswitch`×2 | VPC | Private network across two zones |
| `alicloud_security_group` (app / data) + rules | ECS | API (8000) → `admin_cidr`, SSH (22) → `ssh_cidr`, MCP (8765) → intra-VPC only; data tier intra-VPC only |
| `alicloud_oss_bucket` (+ acl/versioning/SSE) | OSS | Clips, keyframes, audio, locked refs, canon vault |
| `alicloud_db_instance` + database + account | ApsaraDB RDS for PostgreSQL | Canon graph, episodic **pgvector** store, jobs, budget ledger |
| `alicloud_kvstore_instance` | Tair (Redis) | Render queue, scheduler state, pub/sub, locks |
| `alicloud_instance` × (api + mcp + N render-workers) | ECS | The real process model, one container per role |

Each ECS node runs the **same backend image** as `infra/docker-compose.yml`, with
a different command (cloud-init in `cloud-init.sh.tftpl`):

- **api** → `uvicorn app.main:app` (also runs the Scheduler in-process + ingest)
- **render-worker** → `python -m app.queue.worker`
- **mcp** → `python -m app.mcp.run --http`

All model calls go to **DashScope / Model Studio** (`dashscope-intl`); object I/O
goes to **OSS** via its S3-compatible endpoint (the same boto3 `ObjectStore` used
locally against MinIO). This is the cloud form of the §12.6 proof artifact in
`deploy/alibaba_render_worker.py`.

## Prerequisites

- Terraform >= 1.5
- An Alibaba Cloud account + an access key (ideally a scoped RAM user)
- A backend image pushed to a registry the ECS nodes can pull (e.g. ACR);
  set `container_image`
- A DashScope intl API key (`dashscope_api_key`)
- **Required, no-default network/auth inputs** (fail-closed — `apply` won't run
  without them): `admin_cidr` (who can reach the API), `ssh_cidr` (who can SSH),
  and `cors_origins` (the deployed frontend origin[s]). `jwt_secret` and
  `mcp_auth_token` auto-generate when left empty (recommended).

## Usage

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in creds + secrets (gitignored)

terraform init
terraform fmt -check
terraform validate
terraform plan
terraform apply
```

After `apply`:

```bash
# migrations (also enables the pgvector extension on RDS)
docker exec kinora-api alembic -c alembic.ini upgrade head
# seed the bundled public-domain demo book through the real API
python backend/scripts/seed_demo.py --via api --api-url "http://$(terraform output -raw api_public_ip):8000"
```

`terraform output next_steps` prints this checklist after apply.

## Notes

- **Nothing is applied without your credentials.** The HCL is validated
  (`terraform validate`) and formatted (`terraform fmt`), but `apply` needs your
  Alibaba Cloud keys, which are never committed.
- **Secrets** come from `terraform.tfvars` (gitignored) or `ALICLOUD_*` env vars.
  DB/Redis passwords **and the app `jwt_secret` / `mcp_auth_token`** auto-generate
  (URL-safe) when left empty. The generated values are injected into the node env
  via cloud-init and can be read back with `terraform output -raw jwt_secret` /
  `terraform output -raw mcp_auth_token` (both marked `sensitive`).
- **pgvector**: RDS PostgreSQL >= 14 supports `CREATE EXTENSION vector`, which the
  app's first Alembic migration runs automatically.
- **Instance classes** (`rds_instance_type`, `redis_instance_class`,
  `ecs_instance_type`) are region-specific — adjust to what's available in your
  region before `apply`.
- **Network is fail-closed.** `admin_cidr` (API 8000) and `ssh_cidr` (SSH 22) have
  **no defaults** and reject `0.0.0.0/0` — you must set specific CIDRs (your LB /
  office egress, and ideally a bastion/VPN /32 for SSH). The **MCP port (8765) is
  intra-VPC only** (its ingress source is the app security group, like the
  RDS/Tair data tier), so the canon-memory server is never internet-facing; the
  `MCP_AUTH_TOKEN` bearer is defense-in-depth on top of that.
- **App auth/CORS are injected, not defaulted.** `JWT_SECRET` (so prod never boots
  on the insecure built-in default), `MCP_AUTH_TOKEN`, and `CORS_ORIGINS` (your
  `cors_origins`, rendered as a comma list; **no credentialed wildcard**) are
  written into each node's `/etc/kinora/kinora.env` by cloud-init.
- **No secret tracing in cloud-init.** `cloud-init.sh.tftpl` runs `set -euo pipefail`
  (no `-x`), so rendered secrets are never echoed into
  `/var/log/cloud-init-output.log`. user_data is still metadata-readable — for
  real prod prefer KMS-encrypted user_data / Secrets Manager / OOS injection.
- **Go-live gate**: `kinora_live_video` defaults to `false` so deploying never
  silently spends Wan video-seconds. Flip it to `true` deliberately.
- **Function Compute alternative**: the stateless render-workers can run on FC
  (queue/event triggered) instead of always-on ECS; see `deploy/README.md`.
- **State**: this uses local state by default. For teams, configure an OSS
  backend (`backend "oss" { ... }`) — omitted here so `init -backend=false`
  validates cleanly with no remote dependency.
