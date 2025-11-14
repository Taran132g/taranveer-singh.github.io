#!/usr/bin/env bash
# run_all.sh – Starts EVERYTHING: sup_res → grok → paper_trader → UI
# Taranveer Singh @taranve63826864

set -euo pipefail
IFS=$'\n\t'

# ────────────────────── CONFIG ──────────────────────
PYTHON_BIN="python3"               # use your venv python if needed
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALERTS_FILE="${BASE_DIR}/alerts.txt"
SUP_LOG="${BASE_DIR}/sup_res.log"
GROK_LOG="${BASE_DIR}/grok.log"
PAPER_LOG="${BASE_DIR}/paper_trader.log"
UI_LOG="${BASE_DIR}/ui.log"

# ────────────────────── CLEANUP ──────────────────────
cleanup() {
    local exit_code=${1:-0}
    echo -e "\n[STOP] Shutting down all services..."

    for pid_var in SUP_PID GROK_PID PAPER_PID UI_PID; do
        if [[ -n "${!pid_var:-}" ]] && kill -0 "${!pid_var}" 2>/dev/null; then
            echo "   • Stopping $pid_var (PID: ${!pid_var})"
            kill "${!pid_var}" 2>/dev/null || true
        fi
        wait "${!pid_var:-}" 2>/dev/null || true
        unset "$pid_var"
    done

    echo "[STOP] All services stopped."
    exit "$exit_code"
}

trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup $?' EXIT

# ────────────────────── ONE-TIME SCAN ──────────────────────
echo "[1/4] Running initial support/resistance scan..."
if ! "$PYTHON_BIN" sup_res.py --once --output "$ALERTS_FILE" | tee "$SUP_LOG"; then
    echo "[ERROR] Initial scan failed. See $SUP_LOG"
    exit 1
fi

# ────────────────────── EXTRACT SYMBOLS ──────────────────────
SYMBOLS_LINE=$(awk -F ':' '/^TICKERS:/ {gsub(/ /,"",$2); print $2}' "$ALERTS_FILE")
if [[ -z "$SYMBOLS_LINE" ]]; then
    echo "[WARN] No tickers found yet. grok.py will start when symbols appear."
else
    echo "[2/4] Starting grok.py with symbols: $SYMBOLS_LINE"
    "$PYTHON_BIN" grok.py --symbols "$SYMBOLS_LINE" \
        --min-volume 100000 \
        > "$GROK_LOG" 2>&1 &
    GROK_PID=$!
    echo "   • grok.py log → $GROK_LOG"
fi

# ────────────────────── PAPER TRADER ──────────────────────
echo "[3/4] Starting paper_trader.py (paper money simulation)..."
"$PYTHON_BIN" -u paper_trader.py > "$PAPER_LOG" 2>&1 &
PAPER_PID=$!
echo "   • paper_trader.py log → $PAPER_LOG"

# ────────────────────── STREAMLIT UI ──────────────────────
echo "[4/4] Starting Streamlit UI (ui.py)..."
"$PYTHON_BIN" -m streamlit run ui.py \
    --server.port=8501 \
    --server.headless=true \
    --server.enableCORS=false \
    > "$UI_LOG" 2>&1 &
UI_PID=$!
echo "   • UI log → $UI_LOG"
echo "   • Open in browser: http://localhost:8501"

# ────────────────────── CONTINUOUS MONITOR ──────────────────────
echo "[WATCH] Starting continuous sup_res monitor..."
"$PYTHON_BIN" sup_res.py --watch --output "$ALERTS_FILE" \
    >> "$SUP_LOG" 2>&1 &
SUP_PID=$!
echo "   • sup_res watch log → $SUP_LOG"

# ────────────────────── SUMMARY ──────────────────────
echo -e "\nALL SERVICES ARE RUNNING:"
echo "   • sup_res.py (watch) → $SUP_LOG"
echo "   • grok.py (alerts)   → $GROK_LOG"
echo "   • paper_trader.py    → $PAPER_LOG"
echo "   • UI (Streamlit)     → http://localhost:8501 | log: $UI_LOG"
echo -e "\nPress Ctrl+C to stop everything.\n"

# Wait for the watch process (it runs forever)
wait "$SUP_PID"
