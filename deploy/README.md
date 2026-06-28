# Kinora render worker on Alibaba Cloud (§12.6 proof-of-deployment)

`alibaba_render_worker.py` is Kinora's **proof-of-deployment artifact**
(kinora.md §12.6): a real, runnable render worker that demonstrably uses
**Alibaba OSS** (object storage) and **DashScope / Model Studio** (hosted Wan
video synthesis + the Qwen crew), designed to run on **ECS or Function Compute** as a
queue worker.

It does **not** reimplement the pipeline — it reuses the application's real
modules so the proof is honest:

| Alibaba service | Used via | In this file |
|---|---|---|
| **OSS** (object storage) | `app.storage.object_store.ObjectStore` (boto3, S3v4, pointed at the OSS S3-compatible endpoint) | `render_shot_to_oss()` writes the clip; `build_worker()` persists all outputs |
| **DashScope** (Model Studio) | `app.providers.video.VideoProvider` → hosted Wan async video-synthesis; the rest of the Qwen crew | `render_shot_to_oss()` + the pipeline inside `build_worker()` |
| **ECS / Function Compute** | runs `main()` (`build_worker().run()`) as a long-lived Redis-queue consumer | `deploy/Dockerfile`, `infra/terraform` |

## Two entrypoints

- **`main()`** — the deployable worker. Drains the Redis priority queue and runs
  the full per-shot pipeline (DashScope render → Critic → OSS persist, or the
  ffmpeg degradation ladder when `KINORA_LIVE_VIDEO` is off). This is what the
  container `CMD` runs on ECS/FC.
- **`render_shot_to_oss(spec)`** — the minimal §12.6 demonstration: render one
  shot with hosted DashScope Wan and `put_object` the clip to OSS, returning an
  `oss://…` URL. Mirrors the spec's `render_shot(spec)` signature.

## Configuration (env)

The spec's `OSS_*` names are accepted as aliases for the app's `S3_*` settings
(OSS is reached through its S3-compatible endpoint with one boto3 client):

| Spec env | App setting | Example |
|---|---|---|
| `DASHSCOPE_API_KEY` | `dashscope_api_key` | `sk-…` |
| `DASHSCOPE_BASE_URL` | `dashscope_base_url` | `https://dashscope-intl.aliyuncs.com` (default) |
| `OSS_ENDPOINT` | `S3_ENDPOINT_URL` | `https://oss-ap-southeast-1.aliyuncs.com` |
| `OSS_AK` | `S3_ACCESS_KEY` | RAM user access key id |
| `OSS_SECRET` | `S3_SECRET_KEY` | RAM user access key secret |
| `OSS_BUCKET` | `S3_BUCKET` | `kinora-assets` |
| `REDIS_URL` | `redis_url` | `redis://:pass@<tair-host>:6379/0` |
| `DATABASE_URL` | `database_url` | `postgresql+asyncpg://kinora:pass@<rds-host>:5432/kinora` |
| `KINORA_LIVE_VIDEO` | `kinora_live_video` | `false` until you intend to spend Wan video-seconds |
| `VIDEO_MODEL` / `_I2V` / `_R2V` | `video_model*` | defaults: `wan2.1-t2v-turbo` / `wan2.1-i2v-turbo`; quality overrides: `wan2.5-t2v-preview`, `wan2.2-i2v-plus` |

## Run it

Locally against your Alibaba services (from the repo root):

```bash
DASHSCOPE_API_KEY=sk-... \
OSS_ENDPOINT=https://oss-ap-southeast-1.aliyuncs.com \
OSS_AK=... OSS_SECRET=... OSS_BUCKET=kinora-assets \
REDIS_URL=redis://:pass@<tair-host>:6379/0 \
DATABASE_URL=postgresql+asyncpg://kinora:pass@<rds-host>:5432/kinora \
python deploy/alibaba_render_worker.py
```

As a container (the form ECS/FC runs):

```bash
docker build -f infra/docker/backend.Dockerfile -t kinora-backend:local .
docker build -f deploy/Dockerfile --build-arg BACKEND_IMAGE=kinora-backend:local \
    -t kinora-render-worker:local .
docker run --rm --env-file backend/.env \
    -e OSS_ENDPOINT=https://oss-ap-southeast-1.aliyuncs.com \
    -e OSS_AK=... -e OSS_SECRET=... -e OSS_BUCKET=kinora-assets \
    -e REDIS_URL=redis://:pass@<tair-host>:6379/0 \
    -e DATABASE_URL=postgresql+asyncpg://kinora:pass@<rds-host>:5432/kinora \
    kinora-render-worker:local
```

## Deploying the whole backend

`infra/terraform` provisions the full footprint (VPC, OSS, RDS PostgreSQL with
pgvector, Tair/Redis, and ECS nodes for api + ingest-worker + render-worker +
mcp + the browser renderer). Each backend ECS node runs this image with its role
command via cloud-init. See
[`infra/terraform/README.md`](../infra/terraform/README.md).

### Function Compute alternative

The render-worker is stateless (its only shared state is Redis + OSS + RDS), so
it can run on **Function Compute** instead of always-on ECS: package this image
as an FC custom-container function and trigger it on a schedule or an MNS/queue
event, calling `render_shot_to_oss(spec)` per message (or run `main()` as an FC
custom-runtime long task). ECS is the default here because it maps 1:1 to the
local `docker-compose.yml` process model.

## Recording the proof (Devpost §17)

Run the provider preflight first, then flip `KINORA_LIVE_VIDEO=1`, enqueue one
shot, and record the worker rendering a real hosted Wan clip through DashScope
and writing it to OSS. The proof should show the safe model diagnostics, one
`clip_ready` event, the OSS object URL, and worker logs with the model id plus
object key — then link this file in the submission (kinora.md §12.6 / §17).
