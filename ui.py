import sqlite3
from contextlib import closing
from pathlib import Path
import pandas as pd
import streamlit as st
import time as time_module

REFRESH_INTERVAL_SECONDS = 5
DB_PATH = Path("penny_basing.db").resolve()

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

def load_alerts() -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            df = pd.read_sql_query("SELECT * FROM alerts ORDER BY timestamp DESC", conn)
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


def load_paper_positions() -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            df = pd.read_sql_query("SELECT * FROM paper_positions", conn)
    except Exception:
        return pd.DataFrame()
    return df


# NEW — Load daily PnL written by paper_trader.py
def load_daily_pnl():
    try:
        with open("daily_pnl.txt", "r") as f:
            return float(f.read().strip())
    except:
        return 0.0

# ===================== LOGBOOK (SESSION) =====================

if "logbook" not in st.session_state:
    st.session_state["logbook"] = pd.DataFrame()

alerts_df = load_alerts()
st.session_state["logbook"] = pd.concat([alerts_df, st.session_state["logbook"]], ignore_index=True)
st.session_state["logbook"] = (
    st.session_state["logbook"]
    .drop_duplicates(subset=["timestamp", "symbol"])
    .sort_values("timestamp", ascending=False)
    .reset_index(drop=True)
)

# ===================== LATEST ALERTS HEADER =====================

st.markdown("### Latest Alerts")

if not alerts_df.empty:
    latest = alerts_df.drop_duplicates(subset=["symbol"], keep="first")
    cols = st.columns(min(len(latest), 5))

    for idx, (_, row) in enumerate(latest.iterrows()):
        with cols[idx % 5]:
            color = "BUY" if "bid" in row["direction"].lower() else "SELL"
            st.metric(
                label=f"{color} {row['symbol']}",
                value=f"${row['price']:.3f}",
                delta=f"{row['direction']}",
            )
else:
    st.info("Waiting for alerts...")

st.divider()

# ===================== LAYOUT COLUMNS =====================

positions_col, logbook_col = st.columns([1, 1.2], gap="large")

# ===================== OPEN POSITIONS PANEL =====================

with positions_col:
    st.markdown("<div class='panel alert-panel'>", unsafe_allow_html=True)
    st.markdown("### Open Positions")

    positions_df = load_paper_positions()

    if positions_df.empty:
        st.info("No positions.")
    else:
        display = positions_df.copy()
        display["entry_price"] = display["entry_price"].apply(lambda x: f"${x:.3f}")
        display["current_price"] = display["current_price"].apply(lambda x: f"${x:.3f}")
        display["pnl"] = display["pnl"].apply(lambda x: f"${x:+.2f}")
        display["pnl_percent"] = display["pnl_percent"].apply(lambda x: f"{x:+.1f}%")

        st.dataframe(
            display[["symbol", "qty", "entry_price", "current_price", "pnl", "pnl_percent"]],
            use_container_width=True,
            hide_index=True
        )

        # Unrealized PnL total
        total_pnl = positions_df["pnl"].sum()

        # NEW — Realized Daily PnL
        daily_pnl = load_daily_pnl()

        metric_cols = st.columns(2)
        with metric_cols[0]:
            st.metric("Open Position P&L (Unrealized)", f"${total_pnl:+.2f}")
        with metric_cols[1]:
            st.metric("Daily PnL (Realized)", f"${daily_pnl:+.2f}")

    st.markdown("</div>", unsafe_allow_html=True)

# ===================== ALERT LOG PANEL =====================

with logbook_col:
    st.markdown("<div class='panel log-panel'>", unsafe_allow_html=True)
    st.markdown("### Alert Log")

    logbook_df = st.session_state["logbook"]

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
