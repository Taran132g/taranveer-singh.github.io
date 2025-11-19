import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import pandas as pd
import streamlit as st
import time as time_module

REFRESH_INTERVAL_SECONDS = 5
DB_PATH = Path("penny_basing.db").resolve()
LIVE_STATE_PATH = Path(os.getenv("LIVE_STATE_FILE", "live_trader_state.json")).resolve()


def _read_live_state_positions() -> dict[str, int]:
    if not LIVE_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(LIVE_STATE_PATH.read_text())
    except Exception as exc:
        st.warning(f"Failed to read live state: {exc}")
        return {}

    positions = data.get("positions", {}) or {}
    cleaned: dict[str, int] = {}
    for symbol, qty in positions.items():
        try:
            cleaned[str(symbol)] = int(qty)
        except Exception:
            continue
    return cleaned

def init_db():
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'")
            if not cursor.fetchone():
                cursor.execute('''
                    CREATE TABLE alerts (
                        timestamp REAL,
                        symbol TEXT,
                        ratio REAL,
                        total_bids INTEGER,
                        total_asks INTEGER,
                        heavy_venues INTEGER,
                        direction TEXT,
                        price REAL
                    )
                ''')
                conn.commit()
    except Exception as e:
        st.error(f"DB init failed: {e}")

init_db()

st.set_page_config(page_title="Penny Basing", layout="wide")

# Track the UI start time so we can limit output to this session
if "start_timestamp" not in st.session_state:
    st.session_state["start_timestamp"] = time_module.time()

# Capture the live position snapshot present when the UI booted
if "live_position_baseline" not in st.session_state:
    st.session_state["live_position_baseline"] = _read_live_state_positions()

# ===================== STYLING =====================

st.markdown(
    """
    <style>
    body { background-color: #0f172a; color: #f8fafc; }
    .panel {
        padding: 1rem 1.5rem 1.5rem;
        border-radius: 1.25rem;
        box-shadow: 0 25px 50px -12px rgba(15, 23, 42, 0.45);
    }
    .panel h3 { margin-top: 0; }
    .alert-panel { background: linear-gradient(160deg, rgba(30, 64, 175, 0.85), rgba(30, 64, 175, 0.55)); }
    .log-panel { background: linear-gradient(160deg, rgba(15, 23, 42, 0.85), rgba(15, 23, 42, 0.55)); }
    .stDataFrame [data-testid="stDataFrame"] { background-color: rgba(15, 23, 42, 0.25); border-radius: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ===================== DATA LOADERS =====================

def load_alerts(min_timestamp: float | None = None) -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            query = "SELECT * FROM alerts"
            params = ()
            if min_timestamp is not None:
                query += " WHERE timestamp >= ?"
                params = (min_timestamp,)
            query += " ORDER BY timestamp DESC"
            df = pd.read_sql_query(query, conn, params=params)
    except Exception as exc:
        st.warning(f"DB error: {exc}")
        return pd.DataFrame()
    if df.empty:
        return df

    df["timestamp"] = (
        pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")
          .dt.tz_convert("US/Eastern")
    )

    return df


def load_paper_positions(min_entry_time: float | None = None) -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            query = "SELECT * FROM paper_positions"
            params = ()
            if min_entry_time is not None:
                query += " WHERE entry_time >= ?"
                params = (min_entry_time,)
            df = pd.read_sql_query(query, conn, params=params)
    except Exception:
        return pd.DataFrame()
    return df


def _latest_prices(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            placeholders = ",".join(["?"] * len(symbols))
            query = f"""
                SELECT symbol, price
                FROM (
                    SELECT symbol, price, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
                    FROM alerts
                    WHERE symbol IN ({placeholders})
                )
                WHERE rn = 1
            """
            df = pd.read_sql_query(query, conn, params=symbols)
    except Exception as exc:
        st.warning(f"Price lookup failed: {exc}")
        return {}
    return dict(zip(df["symbol"], df["price"]))


def load_live_positions(baseline: dict[str, int] | None = None) -> pd.DataFrame:
    positions = _read_live_state_positions()
    if not positions:
        return pd.DataFrame()

    if baseline:
        adjusted: dict[str, int] = {}
        for symbol, qty in positions.items():
            delta = qty - baseline.get(symbol, 0)
            if delta != 0:
                adjusted[symbol] = delta
        positions = adjusted
        if not positions:
            return pd.DataFrame()

    df = pd.DataFrame(
        {
            "symbol": list(positions.keys()),
            "qty": [int(v) for v in positions.values()],
        }
    )
    prices = _latest_prices(df["symbol"].tolist())
    if prices:
        df["current_price"] = df["symbol"].map(prices)
    return df


# NEW — Load daily PnL written by paper_trader.py
def load_daily_pnl():
    try:
        with open("daily_pnl.txt", "r") as f:
            return float(f.read().strip())
    except:
        return 0.0

# ===================== LOGBOOK (SESSION) =====================

alerts_df = load_alerts(st.session_state["start_timestamp"])

# ===================== LATEST ALERTS HEADER =====================

st.markdown("### Latest Alerts")

if not alerts_df.empty:
    latest = alerts_df.drop_duplicates(subset=["symbol"], keep="first")
    cols = st.columns(min(len(latest), 5))

    for idx, (_, row) in enumerate(latest.iterrows()):
        with cols[idx % 5]:
            direction_text = row["direction"].lower()

            if "bid" in direction_text:
                # bid-heavy → UP arrow (green)
                label = f"BUY {row['symbol']}"
                delta = "bid-heavy"
                delta_color = "normal"     # UP arrow
            else:
                # ask-heavy → DOWN arrow (red)
                label = f"SELL {row['symbol']}"
                delta = "ask-heavy"
                delta_color = "inverse"    # DOWN arrow

            st.metric(
                label=label,
                value=f"${row['price']:.3f}",
                delta=delta,
                delta_color=delta_color,
            )

    st.divider()

# ===================== LAYOUT COLUMNS =====================

positions_col, logbook_col = st.columns([1, 1.2], gap="large")

# ===================== OPEN POSITIONS PANEL =====================

with positions_col:
    st.markdown("### Open Positions")

    position_source = st.radio(
        "Position source",
        ("Paper trader", "Live trader"),
        horizontal=True,
    )

    if position_source == "Paper trader":
        positions_df = load_paper_positions(st.session_state["start_timestamp"])
    else:
        positions_df = load_live_positions(st.session_state["live_position_baseline"])

    if positions_df.empty:
        st.info("No positions.")
    else:
        display = positions_df.copy()

        if {"entry_price", "current_price", "pnl", "pnl_percent"}.issubset(display.columns):
            display["entry_price"] = display["entry_price"].apply(lambda x: f"${x:.3f}")
            display["current_price"] = display["current_price"].apply(lambda x: f"${x:.3f}")
            display["pnl"] = display["pnl"].apply(lambda x: f"${x:+.2f}")
            display["pnl_percent"] = display["pnl_percent"].apply(lambda x: f"{x:+.1f}%")
            visible_cols = ["symbol", "qty", "entry_price", "current_price", "pnl", "pnl_percent"]
        else:
            if "current_price" in display.columns:
                display["current_price"] = display["current_price"].apply(lambda x: f"${x:.3f}")
                visible_cols = ["symbol", "qty", "current_price"]
            else:
                visible_cols = ["symbol", "qty"]

        st.dataframe(
            display[visible_cols],
            use_container_width=True,
            hide_index=True
        )

        if position_source == "Paper trader" and "pnl" in positions_df.columns:
            total_pnl = positions_df["pnl"].sum()
            daily_pnl = load_daily_pnl()

            metric_cols = st.columns(2)
            with metric_cols[0]:
                st.metric("Open Position P&L (Unrealized)", f"${total_pnl:+.2f}")
            with metric_cols[1]:
                st.metric("Daily PnL (Realized)", f"${daily_pnl:+.2f}")

    st.markdown("</div>", unsafe_allow_html=True)

# ===================== ALERT LOG PANEL =====================

with logbook_col:
    st.markdown("### Alert Log")

    logbook_df = alerts_df

    if not logbook_df.empty:
        show = logbook_df[["timestamp", "symbol", "direction", "price"]].copy()
        show["price"] = show["price"].apply(lambda x: f"${x:.3f}")
        show["timestamp"] = show["timestamp"].dt.strftime("%I:%M %p")

        st.dataframe(show, use_container_width=True, hide_index=True, height=500)

    st.markdown("</div>", unsafe_allow_html=True)

# ===================== REFRESH =====================

if st.button("Refresh"):
    st.rerun()

st.info(f"Auto-refresh in {REFRESH_INTERVAL_SECONDS}s")
time_module.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
