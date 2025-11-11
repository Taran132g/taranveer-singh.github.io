#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

PYTHON_BIN=${PYTHON_BIN:-python3}
ALERTS_FILE="$SCRIPT_DIR/alerts.txt"
SUP_LOG="$SCRIPT_DIR/sup_res.log"
GROK_LOG="$SCRIPT_DIR/grok.log"

cleanup() {
    local exit_code=$?
    trap - EXIT

    echo "\nStopping services..."

    if [[ -n "${SUP_PID:-}" ]]; then
        if kill -0 "$SUP_PID" 2>/dev/null; then
            kill "$SUP_PID" 2>/dev/null || true
        fi
        wait "$SUP_PID" 2>/dev/null || true
        unset SUP_PID
    fi

    if [[ -n "${GROK_PID:-}" ]]; then
        if kill -0 "$GROK_PID" 2>/dev/null; then
            kill "$GROK_PID" 2>/dev/null || true
        fi
        wait "$GROK_PID" 2>/dev/null || true
        unset GROK_PID
    fi

    exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Updating support/resistance watchlist..."
if ! "$PYTHON_BIN" sup_res.py --once --output "$ALERTS_FILE" | tee "$SUP_LOG"; then
    echo "❌ Initial sup_res scan failed. Check $SUP_LOG for details."
    exit 1
fi

SYMBOLS_LINE=$(awk -F ':' '/^TICKERS:/ {gsub(/ /, "", $2); print $2}' "$ALERTS_FILE")
if [[ -n "$SYMBOLS_LINE" ]]; then
    echo "Starting grok.py with symbols: $SYMBOLS_LINE"
    "$PYTHON_BIN" grok.py --symbols "$SYMBOLS_LINE" --min-volume 100000 --min-venues 4 > "$GROK_LOG" 2>&1 &
    GROK_PID=$!
    echo "grok.py log: $GROK_LOG"
else
    echo "⚠️ No qualifying tickers found in $ALERTS_FILE. grok.py will start after alerts are generated."
fi

echo "Starting continuous sup_res monitor..."
"$PYTHON_BIN" sup_res.py --watch --output "$ALERTS_FILE" >> "$SUP_LOG" 2>&1 &
SUP_PID=$!
echo "sup_res log: $SUP_LOG"

echo "✅ Trading alert services are running. Press Ctrl+C to stop."

if [[ -n "${SUP_PID:-}" ]]; then
    wait "$SUP_PID"
fi
