# Kinora observability stack

Layers a full metrics + logs + tracing stack on top of the base compose stack.
The base stack already runs Prometheus (scraping the API `/metrics`, §12.5); this
adds the rest.

```bash
# Base stack + observability layer
docker compose -f infra/docker-compose.yml -f infra/docker-compose.observability.yml up -d
```

| Component | Port | Role |
|---|---|---|
| Prometheus (base) | 9090 | scrape API metrics, evaluate the recording + alerting rules |
| Alertmanager | 9093 | route alerts (default `null` receiver; wire Slack/webhook in `alertmanager.yml`) |
| Loki | 3100 | log aggregation (filesystem-backed) |
| Promtail | 9080 | tail Docker container logs → Loki, labelled by `role` (api/render-worker/…) |
| OpenTelemetry Collector | 4317/4318/8889 | OTLP funnel; re-exposes app metrics for Prometheus, ships logs to Loki |
| Grafana | 3000 | dashboards (`admin`/`kinora`; anonymous viewer enabled) |

## What's pre-built

- **Recording rules** (`prometheus/rules/kinora-recording.rules.yml`) — the §13
  headline metrics precomputed: accepted-footage efficiency, regeneration rate,
  cache-hit ratio, render p50/p95, video-seconds spent + burn rate, provider/HTTP
  error ratios, total queue depth, sessions-below-watermark.
- **Alerting rules** (`prometheus/rules/kinora-alerts.rules.yml`) — API down, high
  5xx, render latency, DLQ growth, provider throttling, queue backlog, low accepted
  footage, **video budget 80% / exhausted / spend-with-gate-off** (§11.1), and
  sessions below the 25s low watermark (§12.5).
- **Grafana dashboards** —
  - `kinora-buffer.json`: the §12.5 buffer-occupancy **sawtooth** (low=25s / high=75s
    thresholds), watermark crossings, seeks/idle/cancels, and a Loki log panel.
  - `kinora-pipeline.json`: budget gauge, accepted-footage efficiency, regen rate,
    cache-hit, render latency by mode, queue depth by lane, provider errors, DLQ,
    render-mode mix.

All metric names map 1:1 to `backend/app/observability/metrics.py`.

## Go-live note

The `KinoraVideoSpendWithGateOff` alert fires if the budget ledger records any
video-seconds while `kinora_live_video == 0` — a tripwire for the
`KINORA_LIVE_VIDEO`-off invariant (§11.1, AGENTS.md).

## Validation

`promtool check rules` + `promtool check config` pass (22 rules); `amtool
check-config` passes; the dashboard JSON is well-formed; the layered
`docker compose config` is valid. (All run in `infra-validate.yml` / locally.)
