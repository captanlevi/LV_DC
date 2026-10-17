#!/bin/bash
# Merge a staged LV_DC ref while ANOTHER job holds the pause (e.g. an LVQoE run that needs the box): waits for <trigger>
# in resume_events, stops the paused orchestrate, ff-merges REF, runs the tests; the other job's resume then relaunches
# on the new code. A failed test removes BATCH_GO (no relaunch until fixed).
# Do NOT run alongside another chain that resumes or TERMs (the tw_15 race). Usage: apply_during_pause.sh "<trigger>" [ref]
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
TRIG=${1:?trigger text}; REF=${2:-shagun/incoming}
until grep -q "$TRIG" "$EVENTS"; do sleep 15; done
O=$(pgrep -f "$ORCH_PATTERN"); [ -n "$O" ] && { kill -TERM $O; kill -CONT $O; sleep 8; }
cd "$LVDC_ROOT" && git merge --ff-only -q "$REF" && log "APPLY lvdc $(git rev-parse --short HEAD) during a pause ($TRIG)"
cd networkSim || exit 1; T=""; for t in $(ls test_*.py | grep -v test_browser); do PYTHONDONTWRITEBYTECODE=1 nice -n 10 ../.venv/bin/python "$t" > "/tmp/apply_$t.log" 2>&1 && T="$T $t:ok" || T="$T $t:FAIL"; done
log "APPLY tests:$T"
echo "$T" | grep -q FAIL && rm -f "$LVDC_STATE/BATCH_GO" && log "APPLY: test FAILED -> BATCH_GO removed (no relaunch until fixed)"
