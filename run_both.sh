#!/usr/bin/env bash
# run_both.sh – FULL SYSTEM:
# sup_res → grok → paper_trader → live_trader → UI
#
# This is a convenience launcher for non-engineers. It kicks off every piece
# in order, writes each component's output to its own log file, and cleans up
# all processes on exit.

set -eo pipefail
IFS=$'\n\t'

PYTHON_BIN="python3"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ALERTS_FILE="$BASE_DIR/alerts.txt"
SUP_LOG="$BASE_DIR/sup_res.log"
GROK_LOG="$BASE_DIR/grok.log"
PAPER_LOG="$BASE_DIR/paper_trader.log"
LIVE_LOG="$BASE_DIR/live_trader.log"
UI_LOG="$BASE_DIR/ui.log"

# 0 = live mode, 1 = dry-run (safe)
LIVE_DRY_RUN="${LIVE_DRY_RUN:-0}"

cleanup() {
    echo -e "\n[STOP] Cleaning up..."
    for pid_var in SUP_PID GROK_PID PAPER_PID LIVE_PID UI_PID; do
        if [[ -n "${!pid_var:-}" ]] && kill -0 "${!pid_var}" 2>/dev/null; then
            echo " • Stopping $pid_var (${!pid_var})"
            kill "${!pid_var}" 2>/dev/null || true
        fi
        wait "${!pid_var:-}" 2>/dev/null || true
    done
    exit 0
}

trap 'cleanup' INT TERM EXIT

echo "[1/5] Skipping support/resistance scan (manual symbol list for testing)..."
# $PYTHON_BIN sup_res.py --once --output "$ALERTS_FILE" | tee "$SUP_LOG"

# SYMBOLS_LINE=$(awk -F ':' '/^TICKERS:/ {gsub(/ /,"",$2); print $2}' "$ALERTS_FILE")
SYMBOLS_LINE="F"

echo "[2/5] Starting grok.py..."
$PYTHON_BIN grok.py --symbols "$SYMBOLS_LINE" --min-volume 100000 \
    > "$GROK_LOG" 2>&1 &
GROK_PID=$!
echo " • grok.py → $GROK_LOG"

echo "[3/5] Starting paper_trader.py..."
$PYTHON_BIN -u paper_trader.py > "$PAPER_LOG" 2>&1 &
PAPER_PID=$!
echo " • paper_trader.py → $PAPER_LOG"

RUN_STANDALONE_LIVE=${RUN_STANDALONE_LIVE:-0}
# Inline trading is handled inside grok.py; set RUN_STANDALONE_LIVE=1 only if
# you want the separate live_trader.py process as a fallback.
if [[ "$RUN_STANDALONE_LIVE" == "1" ]]; then
    echo "[4/5] Starting live_trader.py (standalone fallback)..."

    # -------- CRITICAL FIX --------
    # ALWAYS initialize before referencing
    LIVE_ARGS=()
    if [[ "$LIVE_DRY_RUN" == "1" ]]; then
        LIVE_ARGS+=( "--dry-run" )
        echo " • live_trader in DRY-RUN mode (NO REAL ORDERS)"
    fi
    # --------------------------------

    $PYTHON_BIN -u live_trader.py "${LIVE_ARGS[@]}" > "$LIVE_LOG" 2>&1 &
    LIVE_PID=$!
    echo " • live_trader.py → $LIVE_LOG"
else
    echo "[4/5] Skipping standalone live_trader; inline dispatch in grok.py handles orders"
fi

echo "[5/5] Starting UI..."
$PYTHON_BIN -m streamlit run ui.py \
    --server.port=8501 --server.headless=true --server.enableCORS=false \
    > "$UI_LOG" 2>&1 &
UI_PID=$!
echo " • UI running → http://localhost:8501"

echo -e "\n[WATCH] Support/resistance watch disabled for manual testing"
# $PYTHON_BIN sup_res.py --watch --output "$ALERTS_FILE" >> "$SUP_LOG" 2>&1 &
# SUP_PID=$!

echo -e "\nALL SERVICES RUNNING:"
# echo " • sup_res (watch)   → $SUP_LOG"
echo " • grok              → $GROK_LOG"
echo " • paper_trader      → $PAPER_LOG"
echo " • live_trader       → $LIVE_LOG"
echo " • UI                → $UI_LOG"
echo -e "\nCTRL+C to shut everything down.\n"

wait "${SUP_PID:-}"
