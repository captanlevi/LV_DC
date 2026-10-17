#!/bin/bash
# Install (or with --remove, remove) the login autostart entry that runs resume_after_login.sh (README §4, reboots).
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
DST=$HOME/.config/autostart/lvqoe-batch-resume.desktop
if [ "$1" = --remove ]; then rm -f "$DST" && echo "removed $DST"; exit 0; fi
mkdir -p "$(dirname "$DST")"
sed "s|@OPS@|$OPS|" "$OPS/autostart/lvqoe-batch-resume.desktop.in" > "$DST" && echo "installed $DST -> $OPS/resume_after_login.sh"
