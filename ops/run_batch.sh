#!/bin/bash
# The batch wrapper (shagun, t37): checks the mount and that no global IPv6 exists, logs lvdc / Chrome / screen, runs
# orchestrate.py --batch <spec> from networkSim/, and on exit undoes the shaping and kills the collection Chrome.
# Spec and options: $LVDC_STATE/batch_spec and batch_opts if present (apply_at_boundary.sh writes them), else
# LVDC_BATCH_SPEC / LVDC_BATCH_OPTS. Started by
# resume_batch.sh / resume_after_login.sh through launch_wrapper (ssh-identical env).
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
cd "$LVDC_ROOT/networkSim" || exit 1
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$LVDC_STATE"
SPEC=$(cat "$LVDC_STATE/batch_spec" 2>/dev/null || echo "$LVDC_BATCH_SPEC")
OPTS=$(cat "$LVDC_STATE/batch_opts" 2>/dev/null || echo "$LVDC_BATCH_OPTS")   # screen mode: --dpms-off (night) or empty (day)
L=$LVDC_STATE/batch_v2_$(date +%Y%m%d_%H%M).log
cleanup() { make disable >/dev/null 2>&1; make kill_chrome >/dev/null 2>&1; echo "$(date -Is) CLEANUP tc=$(tc qdisc show dev "$LVDC_IFACE" | head -1)" >> "$L"; echo ALLDONE >> "$L"; }
trap cleanup EXIT
mountpoint -q "$LVDC_MOUNT" || { echo "$(date -Is) ABORT $LVDC_MOUNT not mounted" >> "$L"; exit 1; }
[ "$(ip -6 addr show scope global | grep -c inet6)" = 0 ] || { echo "$(date -Is) ABORT global IPv6 present" >> "$L"; exit 1; }
echo "$(date -Is) START lvdc=$(git -C .. rev-parse --short HEAD) dirty=$(git -C .. status --short | wc -l) chrome=$(google-chrome --version) screen=$(xdpyinfo | grep -o "[0-9]*x[0-9]* pixels") spec=$SPEC opts=$OPTS" >> "$L"
# shellcheck disable=SC2086
../.venv/bin/python -u orchestrate.py --batch "$SPEC" $OPTS < /dev/null >> "$L" 2>&1
echo "$(date -Is) orchestrate rc=$?" >> "$L"
