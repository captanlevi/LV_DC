#!/bin/bash
# Per-session verify and frames (README §4): every 60 s, for each PASS session (check.md) without verify.json, LVQoE
# verify_session.py --ingest (QA-only caches, never cited); without frames.jsonl, extract_frames.py; both under
# $LVDC_COMPUTE_PIN (env.sh: nice 19 on the upper half of the CPUs). FAILs
# (_failed/) are skipped; suspicious (exit 2) moves nothing. Logs to _summaries/verify_events.txt. Restart if it dies.
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
D=$LVDC_DATA; E=$D/_summaries/verify_events.txt
while true; do
  for c in "$D"/youtube/*/check.md "$D"/twitch/*/check.md "$D"/tiktok/*/check.md; do
    [ -f "$c" ] || continue; s=$(dirname "$c")
    head -1 "$c" | grep -q "PASS" || continue
    if [ ! -f "$s/verify.json" ]; then
      t0=$(date +%s)
      (cd "$LVQOE_LOOP_DIR" && PYTHONDONTWRITEBYTECODE=1 $LVDC_COMPUTE_PIN "$LVQOE_PY" verify_session.py "$s" --ingest > "$s/verify.log" 2>&1)
      rc=$?; echo "$(date -Is) verify ${s#$D/} rc=$rc ($(( $(date +%s)-t0 )) s; 0 PASS / 2 suspicious / 1 error; lvqoe $(git -C "$LVQOE_LOOP_DIR" rev-parse --short HEAD))" >> "$E"
    fi
    if [ ! -f "$s/frames.jsonl" ]; then
      t0=$(date +%s)
      (cd "$LVQOE_LOOP_DIR" && PYTHONDONTWRITEBYTECODE=1 $LVDC_COMPUTE_PIN .venv/bin/python extract_frames.py "$s" > "$s/frames.log" 2>&1)
      rc=$?; echo "$(date -Is) frames ${s#$D/} rc=$rc ($(( $(date +%s)-t0 )) s; lvqoe $(git -C "$LVQOE_LOOP_DIR" rev-parse --short HEAD))" >> "$E"
    fi
  done
  sleep 60
done
