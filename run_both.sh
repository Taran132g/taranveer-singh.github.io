#!/usr/bin/env bash
# run_both.sh – S/R → grok.py → BUY/SELL alerts
# Taranveer Singh @taranve63826864

set -euo pipefail          # safe bash
IFS=$'\n\t'

# ────────────────────── CONFIG ──────────────────────
PYTHON_BIN="python3"               # or full path to your venv python
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALERTS_FILE="${BASE_DIR}/alerts.txt"
SUP_LOG="${BASE_DIR}/sup_res.log"
GROK_LOG="${BASE_DIR}/grok.log"

# ────────────────────── CLEANUP ──────────────────────
cleanup() {
    local exit_code=${1:-0}

    # kill background jobs if they exist
    for pid_var in SUP_PID GROK_PID; do
        if [[ -n "${!pid_var:-}" ]] && kill -0 "${!pid_var}" 2>/dev/null; then
            kill "${!pid_var}" 2>/dev/null || true
        fi
        wait "${!pid_var:-}" 2>/dev/null || true
        unset "$pid_var"
    done

    exit "$exit_code"
}

trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup $?' EXIT

# ────────────────────── ONE-TIME SCAN ──────────────────────
echo "Updating support/resistance watchlist..."
if ! "$PYTHON_BIN" sup_res.py --once --output "$ALERTS_FILE" | tee "$SUP_LOG"; then
    echo "Initial sup_res scan failed. See $SUP_LOG"
    exit 1
fi

# ────────────────────── EXTRACT TICKERS ──────────────────────
SYMBOLS_LINE=$(awk -F ':' '/^TICKERS:/ {gsub(/ /,"",$2); print $2}' "$ALERTS_FILE")
if [[ -n "$SYMBOLS_LINE" ]]; then
    echo "Starting grok.py with symbols: $SYMBOLS_LINE"
    "$PYTHON_BIN" grok.py --symbols "$SYMBOLS_LINE" \
        --min-volume 100000 \
        > "$GROK_LOG" 2>&1 &
    GROK_PID=$!
    echo "grok.py log → $GROK_LOG"
else
    echo "No tickers yet – grok.py will start after first alert."
fi

# ────────────────────── CONTINUOUS MONITOR ──────────────────────
echo "Starting continuous sup_res monitor..."
"$PYTHON_BIN" sup_res.py --watch --output "$ALERTS_FILE" \
    >> "$SUP_LOG" 2>&1 &
SUP_PID=$!
echo "sup_res log → $SUP_LOG"

echo "Trading alert services are running. Press Ctrl+C to stop."

# Wait for the *watch* process (it never exits on its own)
wait "$SUP_PID"
