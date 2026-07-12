#!/usr/bin/env bash
# Live-demo operations helper, run on the Alibaba ECS host via Cloud Assistant:
#   cd /opt/kinora/source && git fetch origin main && git show FETCH_HEAD:deploy/live_ops.sh | MODE=diag bash
# MODE=diag (default) is read-only; it never mutates state. Other modes are gated
# explicitly. All output is mirrored to object storage at /media/debug/<key> so it
# can be read back over the public reverse proxy without SSH/log-export gymnastics.
set +e
MODE="${MODE:-diag}"
BID="pubdom11000000000000000000000000"
SRC=/opt/kinora/source
INFRA="$SRC/infra"
C="docker compose --env-file $SRC/backend/.env -f $INFRA/docker-compose.cloud.yml"
OUT=/tmp/kops_out.txt

publish() {  # publish <local-file> <s3-key>
  cd "$INFRA" || return 1
  $C exec -T api python -c "import boto3,os,sys; d=open('$1','rb').read(); boto3.client('s3',endpoint_url=os.environ['S3_ENDPOINT_URL'],aws_access_key_id=os.environ['S3_ACCESS_KEY'],aws_secret_access_key=os.environ['S3_SECRET_KEY'],region_name=os.environ.get('S3_REGION')).put_object(Bucket=os.environ['S3_BUCKET'],Key='$2',Body=d,ContentType='text/plain; charset=utf-8'); print('PUBLISHED $2', len(d))"
}

diag() {
  {
    echo "=== MODE=$MODE  $(date -u +%FT%TZ) ==="
    cd "$SRC" || exit 3
    echo "--- HEAD ---"; git rev-parse --short HEAD; git log --oneline -1
    echo "--- GIT STATUS (uncommitted working-tree changes) ---"; git status --porcelain
    echo "--- ENV: gate / models / budget ---"
    grep -E '^(KINORA_LIVE_VIDEO|VIDEO_BACKEND|VIDEO_MODEL|VIDEO_MODEL_I2V|VIDEO_MODEL_R2V|BUDGET_)' "$SRC/backend/.env"
    cd "$INFRA" || exit 3
    echo "--- COMPOSE PS ---"; $C ps --format '{{.Service}} {{.Status}} {{.Image}}'
    echo "--- SHOT STATUS (demo book) ---"
    $C exec -T postgres psql -U kinora -d kinora -Atc \
      "SELECT status, render_mode, count(*) FROM shots WHERE book_id='$BID' GROUP BY status, render_mode ORDER BY status, render_mode"
    echo "--- RENDER-WORKER LOG (last 30m, tail 90) ---"
    $C logs --since=30m --tail=90 render-worker 2>&1 | tail -90
  } > "$OUT" 2>&1
  publish "$OUT" "debug/kops.txt" >> "$OUT" 2>&1
  publish "$OUT" "debug/kops.txt"
}

case "$MODE" in
  diag) diag ;;
  *) echo "unknown MODE=$MODE" ;;
esac
