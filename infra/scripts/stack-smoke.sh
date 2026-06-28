#!/usr/bin/env bash
# Post-bring-up smoke check for the local stack. Confirms the API health endpoint,
# MinIO, and (optionally) the observability layer are reachable. Read-only — never
# touches the live-video gate, never spends model credits.
#
# Usage:
#   infra/scripts/stack-smoke.sh                 # base stack
#   OBS=1 infra/scripts/stack-smoke.sh           # also check Grafana/Prometheus
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
MINIO_URL="${MINIO_URL:-http://localhost:9000}"
PROM_URL="${PROM_URL:-http://localhost:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"

fail=0

check() {
  local name="$1" url="$2"
  if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
    printf '  ok    %-12s %s\n' "$name" "$url"
  else
    printf '  FAIL  %-12s %s\n' "$name" "$url"
    fail=1
  fi
}

echo "Kinora stack smoke check"
check "api"   "$API_URL/health"
check "minio" "$MINIO_URL/minio/health/live"

if [ "${OBS:-0}" = "1" ]; then
  check "prometheus" "$PROM_URL/-/healthy"
  check "grafana"    "$GRAFANA_URL/api/health"
fi

# Confirm the go-live gate is OFF (defends the KINORA_LIVE_VIDEO invariant).
if curl -fsS --max-time 5 "$API_URL/metrics" 2>/dev/null | grep -qE '^kinora_live_video 1(\.0)?$'; then
  echo "  WARN  kinora_live_video is ON — confirm this is intentional spend."
fi

if [ "$fail" -ne 0 ]; then
  echo "smoke check FAILED"
  exit 1
fi
echo "smoke check OK"
