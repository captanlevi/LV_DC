#!/usr/bin/env bash
# calibrate_all.sh — run calibration for all platforms sequentially.
# Kills and restarts Chrome between platforms to get a clean state.
# Results land in data/calibration_{platform}.json
# Logs: /tmp/calibrate_{platform}.log

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../.venv"
PY=$( [ -d "$VENV" ] && echo "$VENV/bin/python3" || echo "python3" )

run_platform() {
    local PLATFORM=$1
    local SPEEDS=$2
    local LOG="/tmp/calibrate_${PLATFORM}.log"

    echo ""
    echo "════════════════════════════════════════"
    echo "  CALIBRATING: $PLATFORM  ($SPEEDS kbit/s)"
    echo "════════════════════════════════════════"

    # Kill any leftover Chrome, restart fresh
    sudo make -C "$SCRIPT_DIR" kill_chrome 2>/dev/null || true
    sleep 2
    sudo make -C "$SCRIPT_DIR" enable 2>/dev/null
    sudo make -C "$SCRIPT_DIR" start_chrome 2>/dev/null
    sleep 6  # let Chrome settle

    cd "$SCRIPT_DIR"
    $PY -u calibrate.py --platform "$PLATFORM" --speeds "$SPEEDS" --runs 2 \
        > "$LOG" 2>&1
    STATUS=$?

    echo "  → $PLATFORM done (exit $STATUS), log: $LOG"
    tail -30 "$LOG"
    return $STATUS
}

# ── YouTube: only the high speeds (low speeds already done, see calibration_youtube.json)
run_platform youtube "1800,2500,3500,5500,8000"

# ── Twitch
run_platform twitch "250,450,650,900,1200,1800,2500,3500,5500,8000"

# ── TikTok
run_platform tiktok "250,450,650,900,1200,1800,2500,3500,5500,8000"

# ── Bilibili
run_platform bilibili "250,450,650,900,1200,1800,2500,3500,5500,8000"

echo ""
echo "✅ All platforms calibrated."
echo "Results in: $(ls "$SCRIPT_DIR"/../data/calibration_*.json 2>/dev/null | tr '\n' ' ')"
