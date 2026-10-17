#!/bin/bash
# Resume after a pause: re-applies IPv6-off, stops the OS timers, checks the mount, starts the on-box guards, touches
# BATCH_GO, then SIGCONTs orchestrate or relaunches the wrapper (after "pause_batch.sh now" a continued orchestrate
# exits 130, operator stop: the wrapper is relaunched and orchestrate resumes from the PASS sessions on disk).
# A no-op while $LVDC_STATE/RESUME_HOLD exists (a chain that needs the box after another job's pause creates it).
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
[ -f "$LVDC_STATE/RESUME_HOLD" ] && { log "RESUME held (RESUME_HOLD: $(cat "$LVDC_STATE/RESUME_HOLD"))"; exit 0; }
mountpoint -q "$LVDC_MOUNT" || { echo "ABORT: data volume not at $LVDC_MOUNT (run resume_after_login.sh or remount)"; exit 1; }
sudo -n sysctl -q -p "$LVDC_NOIPV6_SYSCTL"; G6=$(ip -6 addr show scope global | grep -c inet6); [ "$G6" = 0 ] || { echo "ABORT: global IPv6"; exit 1; }
# shellcheck disable=SC2086
sudo -n systemctl stop $OS_TIMERS
log "RESUME checks: mount ok, global_v6=0, timers $(systemctl is-active apt-daily.timer | head -1), chrome hold=$(apt-mark showhold | grep -c google-chrome), lvdc=$(git -C "$LVDC_ROOT" rev-parse --short HEAD) dirty=$(git -C "$LVDC_ROOT" status --short | wc -l)"
start_guards
touch "$LVDC_STATE/BATCH_GO"
O=$(pgrep -f "$ORCH_PATTERN")
if [ -n "$O" ]; then
  kill -CONT $O; log "RESUME: orchestrate $O continued"; sleep 20
  if ! pgrep -f "$ORCH_PATTERN" >/dev/null; then
    for _ in $(seq 30); do pgrep -f "[r]un_batch.sh" >/dev/null || break; sleep 2; done
    O=""; log "RESUME: orchestrate exited after continue (previous session aborted); relaunching"
  fi
fi
if [ -z "$O" ]; then
  launch_wrapper
  log "RESUME: batch wrapper relaunched (orchestrate resumes from disk)"
fi
