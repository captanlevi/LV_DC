#!/bin/bash
# Per-session verify and frames (README §4): every 60 s, for each PASS session (check.md) without verify.json, LVQoE
# verify_session.py --ingest (QA-only caches, never cited); without frames.jsonl, extract_frames.py; both under
# $LVDC_COMPUTE_PIN (env.sh: nice 19 on the upper half of the CPUs). FAILs
# (_failed/) are skipped; suspicious (exit 2) moves nothing. Logs to _summaries/verify_events.txt. Restart if it dies.
# Censored candidates (collection prereg Addendum Q am. 11; supervisor t37, 2026-10-18; replaces shagun's
# current_data/verify_censored_loop.sh): a _failed session whose meta.json abort_reason is player_frozen / player_dead
# (else check.json's session_class) is verified on its pre-cut part only, verify_session.py --ingest --until <its final
# run's onset> (LVQoE checkpoint_session_list.py --verify-until: the censored list's own rule), never whole-session;
# its verify.json records censored, until and abort_reason. It stays in _failed; nothing is moved or marked. One
# attempt per session per loop start (no retry until a restart). A whole-session verify.json from before is kept as
# verify_whole.json. Events: "verify-censored".
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
# the verdict verify_session wrote (rc: 0 PASS, 2 suspicious, 1 error); "none" when it wrote no verify.json
verdict_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict", "?"))' "$1/verify.json" 2>/dev/null || echo none; }
D=$LVDC_DATA; E=$D/_summaries/verify_events.txt
TRIED=$(mktemp); trap 'rm -f "$TRIED"' EXIT; trap 'exit 143' INT TERM HUP  # censored sessions tried since this start
CENSORED=1
grep -q -- "--verify-until" "$LVQOE_LOOP_DIR/scripts/checkpoint_session_list.py" 2>/dev/null || { CENSORED=0
  echo "$(date -Is) verify-censored off: lvqoe $(git -C "$LVQOE_LOOP_DIR" rev-parse --short HEAD) has no checkpoint_session_list.py --verify-until" >> "$E"; }
while true; do
  for c in "$D"/youtube/*/check.md "$D"/twitch/*/check.md "$D"/tiktok/*/check.md; do
    [ -f "$c" ] || continue; s=$(dirname "$c")
    head -1 "$c" | grep -q "PASS" || continue
    if [ ! -f "$s/verify.json" ]; then
      t0=$(date +%s)
      (cd "$LVQOE_LOOP_DIR" && PYTHONDONTWRITEBYTECODE=1 $LVDC_COMPUTE_PIN "$LVQOE_PY" verify_session.py "$s" --ingest > "$s/verify.log" 2>&1)
      rc=$?; echo "$(date -Is) verify ${s#$D/} verdict=$(verdict_of "$s") rc=$rc ($(( $(date +%s)-t0 )) s; lvqoe $(git -C "$LVQOE_LOOP_DIR" rev-parse --short HEAD))" >> "$E"
    fi
    if [ ! -f "$s/frames.jsonl" ]; then
      t0=$(date +%s)
      (cd "$LVQOE_LOOP_DIR" && PYTHONDONTWRITEBYTECODE=1 $LVDC_COMPUTE_PIN .venv/bin/python extract_frames.py "$s" > "$s/frames.log" 2>&1)
      rc=$?; echo "$(date -Is) frames ${s#$D/} rc=$rc ($(( $(date +%s)-t0 )) s; lvqoe $(git -C "$LVQOE_LOOP_DIR" rev-parse --short HEAD))" >> "$E"
    fi
  done
  for m in "$D"/_failed/youtube/*/meta.json "$D"/_failed/twitch/*/meta.json "$D"/_failed/tiktok/*/meta.json; do
    [ "$CENSORED" = 1 ] && [ -f "$m" ] || continue; s=$(dirname "$m")
    grep -qxF "$s" "$TRIED" && continue; echo "$s" >> "$TRIED"
    grep -q '"until": [0-9]' "$s/verify.json" 2>/dev/null && continue     # verified on its pre-cut part already
    u=$(cd "$LVQOE_LOOP_DIR" && PYTHONDONTWRITEBYTECODE=1 $LVDC_COMPUTE_PIN "$LVQOE_PY" scripts/checkpoint_session_list.py --verify-until "$s" 2>&1)
    rc=$?
    if [ "$rc" != 0 ]; then                                                  # 3: not a candidate, or no run onset
      [ "$u" = "skip: not a censored candidate" ] || echo "$(date -Is) verify-censored ${s#$D/} not run: rc=$rc $(tail -1 <<< "$u")" >> "$E"
      continue
    fi
    if [ -f "$s/verify.json" ]; then w=$s/verify_whole.json; [ -e "$w" ] && w=$s/verify_whole_$(date +%s).json; mv "$s/verify.json" "$w"; fi
    t0=$(date +%s)
    (cd "$LVQOE_LOOP_DIR" && PYTHONDONTWRITEBYTECODE=1 $LVDC_COMPUTE_PIN "$LVQOE_PY" verify_session.py "$s" --ingest --until "$u" > "$s/verify.log" 2>&1)
    rc=$?; echo "$(date -Is) verify-censored ${s#$D/} verdict=$(verdict_of "$s") rc=$rc ($(( $(date +%s)-t0 )) s; --until $u; lvqoe $(git -C "$LVQOE_LOOP_DIR" rev-parse --short HEAD))" >> "$E"
  done
  sleep 60
done
