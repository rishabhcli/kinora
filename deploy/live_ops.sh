#!/usr/bin/env bash
# Live-demo operations helper, run on the Alibaba ECS host via Cloud Assistant:
#   cd /opt/kinora/source && git fetch origin main && git show FETCH_HEAD:deploy/live_ops.sh | MODE=diag bash
# MODE=diag (default) is read-only; it never mutates state. Other modes are gated
# explicitly. Output is both printed (captured by Cloud Assistant) and mirrored to
# object storage at /media/debug/<key> so it can be read back over the public proxy.
set +e
MODE="${MODE:-diag}"
BID="pubdom11000000000000000000000000"
SRC=/opt/kinora/source
INFRA="$SRC/infra"
C="docker compose --env-file $SRC/backend/.env -f $INFRA/docker-compose.cloud.yml"
OUT=/tmp/kops_out.txt

publish() {  # publish <local-file> <s3-key> ; pipes host file into the api container over stdin
  cd "$INFRA" || return 1
  cat "$1" | $C exec -T api python -c "import boto3,os,sys; d=sys.stdin.buffer.read(); boto3.client('s3',endpoint_url=os.environ['S3_ENDPOINT_URL'],aws_access_key_id=os.environ['S3_ACCESS_KEY'],aws_secret_access_key=os.environ['S3_SECRET_KEY'],region_name=os.environ.get('S3_REGION')).put_object(Bucket=os.environ['S3_BUCKET'],Key='$2',Body=d,ContentType='text/plain; charset=utf-8'); sys.stderr.write('PUBLISHED $2 '+str(len(d))+'\n')"
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
  } 2>&1 | tee "$OUT"
  publish "$OUT" "debug/kops.txt"
}

apply() {
  {
    echo "=== APPLY  $(date -u +%FT%TZ) ==="
    cd "$SRC" || exit 3
    echo "--- pull latest main (untracked deploy files are preserved) ---"
    git fetch origin main && git reset --hard origin/main
    echo "HEAD now: $(git rev-parse --short HEAD)  $(git log --oneline -1)"
    cd "$INFRA" || exit 3
    echo "--- build api + render-worker ---"; $C build api render-worker
    echo "--- recreate api + render-worker ---"; $C up -d --no-deps api render-worker
    sleep 10
    echo "--- reset demo book: text_to_video, planned, clear jobs/cache/ledger/outputs ---"
    $C exec -T postgres psql -v ON_ERROR_STOP=1 -U kinora -d kinora -c \
      "BEGIN; \
       DELETE FROM render_jobs r USING shots s WHERE r.shot_id=s.id AND s.book_id='$BID'; \
       DELETE FROM shot_cache WHERE book_id='$BID'; \
       DELETE FROM budget_ledger WHERE book_id='$BID'; \
       UPDATE shots SET status='planned', render_mode='text_to_video', duration_s=5.0, \
         output=NULL, qa=NULL, cost=NULL, accepted_at=NULL, clip_start_s=NULL, clip_end_s=NULL \
       WHERE book_id='$BID'; \
       COMMIT;"
    echo "--- restart render-worker ---"; $C restart render-worker
    sleep 6
    echo "--- health ---"; curl -fsS http://127.0.0.1/health; echo
    echo "--- shot status after apply ---"
    $C exec -T postgres psql -U kinora -d kinora -Atc \
      "SELECT status, render_mode, count(*) FROM shots WHERE book_id='$BID' GROUP BY status, render_mode ORDER BY status, render_mode"
    echo "APPLY_COMPLETE"
  } 2>&1 | tee "$OUT"
  publish "$OUT" "debug/kops.txt"
}

reset_demo() {
  # DB reset + worker restart only (no build/up) — fast enough for the console's
  # 60s command timeout, for when the code is already deployed.
  {
    echo "=== RESET  $(date -u +%FT%TZ) ==="
    cd "$INFRA" || exit 3
    $C exec -T postgres psql -v ON_ERROR_STOP=1 -U kinora -d kinora -c \
      "BEGIN; \
       DELETE FROM render_jobs r USING shots s WHERE r.shot_id=s.id AND s.book_id='$BID'; \
       DELETE FROM shot_cache WHERE book_id='$BID'; \
       DELETE FROM budget_ledger WHERE book_id='$BID'; \
       UPDATE shots SET status='planned', render_mode='text_to_video', duration_s=5.0, \
         output=NULL, qa=NULL, cost=NULL, accepted_at=NULL, clip_start_s=NULL, clip_end_s=NULL \
       WHERE book_id='$BID'; \
       COMMIT;"
    $C restart render-worker
    sleep 5
    curl -fsS http://127.0.0.1/health; echo
    $C exec -T postgres psql -U kinora -d kinora -Atc \
      "SELECT status, count(*) FROM shots WHERE book_id='$BID' GROUP BY status ORDER BY status"
    echo "RESET_COMPLETE"
  } 2>&1 | tee "$OUT"
  publish "$OUT" "debug/kops.txt"
}

case "$MODE" in
  diag) diag ;;
  apply) apply ;;
  reset) reset_demo ;;
  *) echo "unknown MODE=$MODE" ;;
esac
