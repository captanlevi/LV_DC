#!/bin/bash
# ONE script per code change (README §4): pause at the boundary -> TERM + CONT the stopped orchestrate (it exits with no
# session running, so the old code leaves memory) -> git merge --ff-only REF -> every test_*.py except the live
# test_browser, as a script and under pytest (levi t58 T-1: both runners gate) -> optional new batch spec / options ($LVDC_STATE/batch_spec, batch_opts) -> resume. A failed test leaves the batch paused.
# Never run two boundary scripts at once: one CONTs while the other TERMs (tw_15 lost its Chrome that way).
# Get REF onto the box over ssh (git push <box>:<repo> <ref>:refs/heads/<name>), never a fetch during a session.
# Usage: apply_at_boundary.sh [ref] [batch-spec] [batch-opts]   (opts: "--dpms-off" = night, "none" = day / screen on)
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
REF=${1:-shagun/incoming}; BATCH=$2; OPTS=$3
"$OPS/pause_batch.sh" > /tmp/apply_pause.log 2>&1
until tail -1 "$EVENTS" | grep -q "box free"; do sleep 10; done
O=$(pgrep -f "$ORCH_PATTERN"); [ -n "$O" ] && { kill -TERM $O; kill -CONT $O; sleep 8; }
cd "$LVDC_ROOT" && git merge --ff-only -q "$REF" && log "APPLY lvdc $(git rev-parse --short HEAD) ($REF)" || log "APPLY ff-merge FAILED for $REF; resuming on $(git rev-parse --short HEAD)"
cd networkSim || exit 1; T=""; for t in $(ls test_*.py | grep -v test_browser); do PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python "$t" > "/tmp/apply_$t.log" 2>&1 && T="$T $t:ok" || T="$T $t:FAIL"; done
PT=$(PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider --ignore=test_browser.py . > /tmp/apply_pytest.log 2>&1 && echo ok || echo FAIL); T="$T pytest:$PT [$(tail -1 /tmp/apply_pytest.log)]"
log "APPLY tests:$T"
echo "$T" | grep -q FAIL && { log "APPLY: a test FAILED, batch left paused"; exit 1; }
[ -n "$BATCH" ] && { echo "$BATCH" > "$LVDC_STATE/batch_spec"; log "APPLY batch -> $BATCH"; }
[ -n "$OPTS" ] && { [ "$OPTS" = none ] && OPTS=""; echo "$OPTS" > "$LVDC_STATE/batch_opts"; log "APPLY batch opts -> '${OPTS}'"; }
"$OPS/resume_batch.sh" > /tmp/apply_resume.log 2>&1
