#!/bin/bash
# Screen-off mode (README §1 display; rushi: physical display off during the batch): re-assert DPMS off every 60 s; the
# X session and 1920x1080 stay alive, and x11 shots work with the monitor dark. Started only while $LVDC_STATE/SCREEN_OFF.
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
while true; do xset q 2>/dev/null | grep -q "Monitor is On" && xset dpms force off; sleep 60; done
