#!/usr/bin/env bash
# Usage: run_session.sh <platform> <episodes> [initial_state]
# Runs one collection session and writes a one-line result to /tmp/session_result.txt
set -e
PLATFORM=${1:-youtube}
EPISODES=${2:-10}
INITIAL=${3:-}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../.venv"

CMD=()
if [ -d "$VENV" ]; then
    CMD=("$VENV/bin/python3" "-u")
else
    CMD=("python3" "-u")
fi

ARGS=("$SCRIPT_DIR/exp.py" "--platform" "$PLATFORM" "--episodes" "$EPISODES")
if [ -n "$INITIAL" ]; then
    ARGS+=("--initial-state" "$INITIAL")
fi

cd "$SCRIPT_DIR"
"${CMD[@]}" "${ARGS[@]}"
STATUS=$?

if [ $STATUS -eq 0 ]; then
    LATEST=$(ls -td "$SCRIPT_DIR/../data/$PLATFORM"/*/ 2>/dev/null | head -1)
    echo "OK $PLATFORM $(basename $LATEST) episodes=$EPISODES initial=${INITIAL:-random}" > /tmp/session_result.txt
else
    echo "FAILED $PLATFORM exit=$STATUS" > /tmp/session_result.txt
fi
exit $STATUS
