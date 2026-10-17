#!/bin/bash
# Boundary pause (README §4). Usage: pause_batch.sh [now]
#   default: SIGSTOP orchestrate and let the running session finish (check included);
#   now:     also SIGTERM the running exp.py (finalised, archived to _failed as user_pause).
# Removes BATCH_GO (no relaunch after a reboot while paused), waits for the session to end (zombie-aware: a finished
# child of a stopped orchestrate stays a zombie), then undoes the shaping, kills the collection Chrome, logs "box free".
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
rm -f "$LVDC_STATE/BATCH_GO"; O=$(pgrep -f "$ORCH_PATTERN"); X=$(pgrep -f "$EXP_PATTERN")
[ -n "$O" ] && kill -STOP $O; log "PAUSE requested (${1:-finish}); BATCH_GO removed; orchestrate=$O frozen; session pid=${X:-none}"
if [ "$1" = now ] && [ -n "$X" ]; then kill -TERM $X; log "current session aborted (user_pause; archived to _failed by check_session)"; fi
while [ -n "$X" ] && kill -0 $X 2>/dev/null && [ "$(ps -o stat= -p $X | cut -c1)" != Z ]; do sleep 10; done
cd "$LVDC_ROOT/networkSim" && { make disable >/dev/null 2>&1; make kill_chrome >/dev/null 2>&1; }
log "box free: tc=$(tc qdisc show dev "$LVDC_IFACE" | head -1 | cut -c1-30) tcpdump=$(pgrep -fc "[t]cpdump -i ifb") chrome=$(pgrep -fc "[r]emote-debugging-port=9222")"
