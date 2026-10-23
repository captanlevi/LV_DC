# ops/env.sh: the box-specific settings of the operator scripts, sourced by each of them. Defaults are chaos's
# (shagun's on-box layer, 2026-10-17/18). Override in the environment or in ops/env.local.sh (gitignored).
OPS=${OPS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
[ -f "$OPS/env.local.sh" ] && . "$OPS/env.local.sh"
: "${LVDC_ROOT:=$(dirname "$OPS")}"                 # the LV_DC checkout the batch runs from
: "${LVDC_STATE:=$LVDC_ROOT/current_data}"               # flags BATCH_GO / RESUME_HOLD / SCREEN_OFF, batch_spec, logs (gitignored)
: "${LVDC_MOUNT:=/media/$(id -un)/memories}"             # the data volume's mount point
: "${LVDC_DEVICE:=/dev/sda1}"                            # its block device (resume_after_login.sh remounts it there)
: "${LVDC_DATA:=$LVDC_MOUNT/LV_DC_v2_data}"              # where data/ resolves
# the data volume's filesystem UUID (findmnt -o UUID); preflight refuses a session whose data root is not on it, not rw
# or not writable (a USB disconnect, errors=remount-ro). Empty = not checked; set it in env.local.sh at the cutover
export LVDC_DATA_ROOT_UUID="${LVDC_DATA_ROOT_UUID:-}"
: "${LVDC_IFACE:=wlp0s20f3}"                             # the shaped interface
: "${LVDC_NOIPV6_SYSCTL:=/etc/sysctl.d/99-lvqoe-collection-noipv6.conf}"
: "${LVDC_BATCH_SPEC:=youtube=100,twitch=80,tiktok=80}"  # default floors; $LVDC_STATE/batch_spec overrides (apply_at_boundary.sh)
: "${LVDC_BATCH_OPTS:=--dpms-off}"
: "${LVDC_LANG:=en_AU.UTF-8}"                            # LANG of the ssh-identical relaunch environment
: "${LVDC_UID:=$(id -u)}"
: "${LVQOE_PY:=$HOME/Desktop/LV/.venv/bin/python}"       # LVQoE's interpreter (was verify_loop.sh's; archived t69 C9)
# the one LVQoE worktree the verify / frames loop runs (verify_session.py and extract_frames.py), at a pinned commit;
# moving the loop to a new LVQoE commit = a new worktree + this default (supervisor 2026-10-18: master 010c47c)
: "${LVQOE_LOOP_DIR:=$HOME/Desktop/LV/.worktrees/shagun-loop-010c47c}"
# verify / frames jobs run away from the collector (shagun 2026-10-18, compute-load covariate): nice 19, pinned to the
# upper half of the CPUs (chaos: 12-23, off the collector's P-cores); plain nice where taskset or cores are lacking
if [ -z "${LVDC_COMPUTE_PIN+x}" ]; then
  _n=$(nproc 2>/dev/null || echo 1)
  if command -v taskset >/dev/null && [ "$_n" -ge 4 ]; then LVDC_COMPUTE_PIN="nice -n 19 taskset -c $((_n / 2))-$((_n - 1))"
  else LVDC_COMPUTE_PIN="nice -n 19"; fi
fi
export DISPLAY=${DISPLAY:-:0} XAUTHORITY=${XAUTHORITY:-/run/user/$LVDC_UID/gdm/Xauthority}
EVENTS=$LVDC_DATA/_summaries/resume_events.txt           # every script logs here
ORCH_PATTERN='^../.venv/bin/python -u orchestrate.py --batch'
EXP_PATTERN="^$LVDC_ROOT/networkSim/../.venv/bin/python exp.py"
OS_TIMERS="unattended-upgrades apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer"
log() { mkdir -p "$(dirname "$EVENTS")"; echo "$(date -Is) $*" | tee -a "$EVENTS"; }
# the batch is (re)launched in an ssh-identical minimal environment: the templates were built over ssh, so Chrome keeps
# its "basic" cookie store and the cloned profiles (incl. the tt login) decrypt (README §1, launch environment)
launch_wrapper() {
  setsid nohup env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" SHELL=/bin/bash LANG="$LVDC_LANG" \
    PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin XDG_RUNTIME_DIR="/run/user/$LVDC_UID" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$LVDC_UID/bus" OPS="$OPS" "$OPS/run_batch.sh" >/dev/null 2>&1 < /dev/null &
}
start_guards() {   # the on-box watchdogs, each started once
  [ ! -f "$LVDC_STATE/SCREEN_OFF" ] || pgrep -f "[k]eep_display_off.sh" >/dev/null || setsid nohup "$OPS/keep_display_off.sh" >/dev/null 2>&1 < /dev/null &
  # t69 C9 (levi 2026-10-23 18:03, collector v3): no verifier loop; the window post is the only trio verifier. verify_loop.sh and
  # guard_stall_contradiction.sh (superseded by player_frozen / I10) are archived in ops/_archive
  pgrep -f "[i]pv6_watchdog.sh" >/dev/null || setsid -f "$OPS/ipv6_watchdog.sh" >/dev/null 2>&1 < /dev/null
}
