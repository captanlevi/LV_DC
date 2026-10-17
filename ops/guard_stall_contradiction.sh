#!/bin/bash
# Interim guard (supervisor 2026-10-17; superseded by check_session's player_frozen and I10 rules, kept while the batch
# runs): a session whose check.md PASSes with stall share > 0.5 while current_time advancing > 0.5 is re-archived to
# _failed/<platform>/ with ARCHIVE_REASON.txt (pcaps kept). Every 60 s; logs to resume_events.
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
M=$LVDC_DATA
while true; do
  for f in "$M"/{youtube,twitch,tiktok}/*/check.md; do
    [ -f "$f" ] || continue; grep -q "\*\*PASS\*\*" "$f" || continue
    st=$(grep -o "stall share | [0-9.]*" "$f" | grep -o "[0-9.]*$"); adv=$(grep -o "current_time advancing (playing, non-ad) | [0-9.]*" "$f" | grep -o "[0-9.]*$")
    if python3 -c "import sys; sys.exit(0 if float(\"${st:-0}\")>0.5 and float(\"${adv:-0}\")>0.5 else 1)"; then
      d=$(dirname "$f"); p=$(basename "$(dirname "$d")"); s=$(basename "$d"); mkdir -p "$M/_failed/$p"; t=$M/_failed/$p/$s; [ -e "$t" ] && t=${t}_stall
      mv "$d" "$t" && echo "stall_label_stuck: PASS check but stall share $st with current_time advancing $adv (stall-dominant PASS). Re-archived $(date -Is) by guard_stall_contradiction.sh (supervisor rule). pcaps intact." > "$t/ARCHIVE_REASON.txt" && echo "$(date -Is) GUARD re-archived $p/$s -> $t (stall $st, advancing $adv)" >> "$EVENTS"
    fi
  done; sleep 60
done
