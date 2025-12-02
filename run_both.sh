#!/usr/bin/env bash
# run_both.sh – FULL SYSTEM:
# grok → paper_trader → live_trader → UI

set -eo pipefail
IFS=$'\n\t'

if [[ -d ".venv" ]]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python3"
fi
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GROK_LOG="$BASE_DIR/grok.log"
PAPER_LOG="$BASE_DIR/paper_trader.log"
LIVE_LOG="$BASE_DIR/live_trader.log"
UI_LOG="$BASE_DIR/ui.log"

# 0 = live mode, 1 = dry-run (safe)
LIVE_DRY_RUN="${LIVE_DRY_RUN:-0}"

cleanup() {
    echo -e "\n[STOP] Cleaning up..."
    for pid_var in GROK_PID PAPER_PID LIVE_PID UI_PID; do
        if [[ -n "${!pid_var:-}" ]] && kill -0 "${!pid_var}" 2>/dev/null; then
            echo " • Stopping $pid_var (${!pid_var})"
            kill "${!pid_var}" 2>/dev/null || true
        fi
        wait "${!pid_var:-}" 2>/dev/null || true
    done
    exit 0
}

trap 'cleanup' INT TERM EXIT

echo "[RESET] Clearing previous data..."
rm -f "$BASE_DIR"/*.log
rm -f "$BASE_DIR/penny_basing.db"
rm -f "$BASE_DIR/paper_trader_state.json"
rm -f "$BASE_DIR/live_trader_state.json"
rm -f "$BASE_DIR/daily_pnl.txt"
echo " • Deleted logs, DB, and state files."

echo "[1/4] Starting grok.py..."
$PYTHON_BIN grok.py --min-volume 100000 \
    > "$GROK_LOG" 2>&1 &
GROK_PID=$!
echo " • grok.py → $GROK_LOG"

echo "[2/4] Starting paper_trader.py..."
$PYTHON_BIN -u paper_trader.py > "$PAPER_LOG" 2>&1 &
PAPER_PID=$!
echo " • paper_trader.py → $PAPER_LOG"

RUN_STANDALONE_LIVE=${RUN_STANDALONE_LIVE:-0}
if [[ "$RUN_STANDALONE_LIVE" == "1" ]]; then
    echo "[3/4] Starting live_trader.py (standalone fallback)..."

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
    echo "[3/4] Skipping standalone live_trader; inline dispatch in grok.py handles orders"
fi

echo "[4/4] Starting UI..."
$PYTHON_BIN -m streamlit run ui.py \
    --server.port=8501 --server.headless=true --server.enableCORS=false \
    > "$UI_LOG" 2>&1 &
UI_PID=$!
echo " • UI running → http://localhost:8501"


echo -e "\nALL SERVICES RUNNING:"

echo " • grok              → $GROK_LOG"
echo " • paper_trader      → $PAPER_LOG"
echo " • live_trader       → $LIVE_LOG"
echo " • UI                → $UI_LOG"
echo -e "\nCTRL+C to shut everything down.\n"

wait "$GROK_PID"
