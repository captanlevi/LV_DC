#!/bin/bash
# Reboot hardening (shagun, t37), run at graphical login (XDG autostart, ops/install_autostart.sh): waits for the data
# disk and fixes the mount-path trap (README §1), re-applies IPv6-off, stops the OS timers, mutes audio, starts the
# on-box guards, then relaunches the batch iff BATCH_GO and no RESUME_HOLD (in the ssh-identical env, so the cookie
# store matches). levi 02:38 (a named defect: data-volume writes before NTP, a launch that ignored RESUME_HOLD): the
# guards and the launch wait for NTP sync (timedatectl NTPSynchronized = yes, LVDC_NTP_WAIT_S, default 600 s); on a
# timeout both are withheld (fail closed, logged), and nothing is written to the data volume before the sync. The mount,
# IPv6-off, the timers and the mute run regardless (they stamp nothing).
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
mkdir -p "$LVDC_STATE"
L=$LVDC_STATE/resume_$(date +%Y%m%d_%H%M%S).log; llog() { echo "$(date -Is) $*" >> "$L"; }
MOPTS=rw,nosuid,nodev,uid=$LVDC_UID,gid=$(id -g),fmask=0022,dmask=0022,iocharset=utf8,errors=remount-ro
: "${LVDC_NTP_WAIT_S:=600}" "${LVDC_NTP_POLL_S:=5}"
llog "login hook start (clock $(date -Is), NTPSynchronized=$(timedatectl show -p NTPSynchronized --value 2>/dev/null))"
for _ in $(seq 60); do T=$(findmnt -n -o TARGET -S "$LVDC_DEVICE" 2>/dev/null); [ -n "$T" ] && break; sleep 10; done
llog "$LVDC_DEVICE mounted at: ${T:-NOT MOUNTED}"
if [ -z "$T" ]; then sudo -n mkdir -p "$LVDC_MOUNT" && sudo -n mount -t exfat -o "$MOPTS" "$LVDC_DEVICE" "$LVDC_MOUNT" && llog "mounted $LVDC_DEVICE at $LVDC_MOUNT"
elif [ "$T" != "$LVDC_MOUNT" ]; then sudo -n umount "$T" && sudo -n rmdir "$T" 2>/dev/null; sudo -n rmdir "$LVDC_MOUNT" 2>/dev/null; sudo -n mkdir -p "$LVDC_MOUNT" && sudo -n mount -t exfat -o "$MOPTS" "$LVDC_DEVICE" "$LVDC_MOUNT" && llog "remounted $T -> $LVDC_MOUNT"; fi
mountpoint -q "$LVDC_MOUNT" || { llog "ABORT: data volume not at $LVDC_MOUNT"; exit 1; }
for _ in $(seq 30); do sudo -n sysctl -q -p "$LVDC_NOIPV6_SYSCTL"; G6=$(ip -6 addr show scope global | grep -c inet6); [ "$G6" = 0 ] && break; sleep 10; done
llog "ipv6 all=$(sysctl -n net.ipv6.conf.all.disable_ipv6) iface=$(sysctl -n "net.ipv6.conf.$LVDC_IFACE.disable_ipv6") global_v6=$G6"
[ "$G6" = 0 ] || { llog "ABORT: global IPv6 present"; exit 1; }
# shellcheck disable=SC2086
sudo -n systemctl stop $OS_TIMERS; llog "timers: $(systemctl is-active $OS_TIMERS | tr "\n" " ")"
XDG_RUNTIME_DIR=/run/user/$LVDC_UID pactl set-sink-mute @DEFAULT_SINK@ 1 2>/dev/null; XDG_RUNTIME_DIR=/run/user/$LVDC_UID pactl set-sink-volume @DEFAULT_SINK@ 0% 2>/dev/null
# NTP before anything that writes to the data volume (the guards log to resume_events there; the stall guard moves dirs)
ntp=no; t0=$SECONDS
while [ $((SECONDS - t0)) -le "$LVDC_NTP_WAIT_S" ]; do
  [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && { ntp=yes; break; }
  sleep "$LVDC_NTP_POLL_S"
done
llog "NTP synchronized=$ntp after $((SECONDS - t0)) s (clock now $(date -Is))"
if [ "$ntp" != yes ]; then
  llog "ABORT: NTP not synced within $LVDC_NTP_WAIT_S s: guards (guard_stall_contradiction, ipv6_watchdog, keep_display_off) and the launch withheld"
  exit 1
fi
start_guards; llog "guards started (display keeper only with SCREEN_OFF)"
if [ -f "$LVDC_STATE/RESUME_HOLD" ]; then
  llog "no resume (RESUME_HOLD: $(head -c 200 "$LVDC_STATE/RESUME_HOLD"))"
elif [ -e "$LVDC_STATE/BATCH_GO" ] && ! pgrep -f "[r]un_batch.sh" >/dev/null; then
  launch_wrapper
  llog "RESUME: batch wrapper relaunched (orchestrate --batch resumes from disk)"; log "batch resumed after reboot/login (resume_after_login.sh)"
else llog "no resume (BATCH_GO=$([ -e "$LVDC_STATE/BATCH_GO" ] && echo yes || echo no), wrapper running=$(pgrep -fc "[r]un_batch.sh"))"; fi
llog done
