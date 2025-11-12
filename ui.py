import sqlite3
from contextlib import closing
from pathlib import Path
import pandas as pd
import streamlit as st
import time as time_module

# === CONFIG ===
REFRESH_INTERVAL_SECONDS = 5

# === DATABASE PATH: Use same as your bot (current folder) ===
DB_PATH = Path("penny_basing.db").resolve()  # <-- Matches your bot

# === INIT DATABASE (Only creates if missing) ===
def init_db() -> None:
    """Ensure the alerts table exists with correct schema."""
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
        st.error(f"Failed to initialize DB: {e}")

def init_positions_table() -> None:
    """Create positions table if it doesn't exist."""
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='positions'")
            if not cursor.fetchone():
                cursor.execute('''
                    CREATE TABLE positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        current_price REAL NOT NULL,
                        quantity INTEGER NOT NULL,
                        entry_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'open',
                        pnl REAL,
                        pnl_percent REAL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.commit()
    except Exception:
        pass

# Initialize
init_db()
init_positions_table()

# === STREAMLIT UI ===
st.set_page_config(page_title="Penny Basing Alerts", layout="wide")
st.title("Penny Basing Alerts Dashboard")

# === CSS Styling ===
st.markdown(
    """
    <style>
    body { background-color: #0f172a; color: #f8fafc; }
    .panel {
        padding: 1.5rem;
        border-radius: 1.25rem;
        box-shadow: 0 25px 50px -12px rgba(15, 23, 42, 0.45);
    }
    .alert-panel {
        background: linear-gradient(160deg, rgba(30, 64, 175, 0.85), rgba(30, 64, 175, 0.55));
    }
    .log-panel {
        background: linear-gradient(160deg, rgba(15, 23, 42, 0.85), rgba(15, 23, 42, 0.55));
    }
    .alert-header { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.35rem; }
    .alert-meta { font-size: 0.85rem; opacity: 0.85; margin-bottom: 0.75rem; }
    .stExpander > div > div {
        border: 1px solid rgba(148, 163, 184, 0.25);
        border-radius: 0.85rem;
        background-color: rgba(15, 23, 42, 0.45);
    }
    .stDataFrame [data-testid="stDataFrame"] {
        background-color: rgba(15, 23, 42, 0.25);
        border-radius: 0.85rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# === DATA LOADING ===
def load_alerts() -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            df = pd.read_sql_query("SELECT * FROM alerts ORDER BY timestamp DESC", conn)
    except Exception as exc:
        st.warning(f"Database issue: {exc}")
        st.info(f"Looking for DB at: `{DB_PATH}`")
        st.write("Make sure your bot is running and saving to `penny_basing.db` in this folder.")
        return pd.DataFrame()

    if df.empty:
        return df

    # Convert Unix float timestamp → local time
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors='coerce').dt.tz_convert(None)
    return df

def update_logbook(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if incoming.empty:
        return existing
    combined = pd.concat([incoming, existing], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp", "symbol"]).sort_values("timestamp", ascending=False)
    return combined.reset_index(drop=True)

def load_positions() -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            df = pd.read_sql_query("SELECT * FROM positions WHERE status='open' ORDER BY entry_time DESC", conn)
    except Exception:
        return pd.DataFrame()
    return df

# === SESSION STATE ===
if "logbook" not in st.session_state:
    st.session_state["logbook"] = pd.DataFrame()

# Load data
alerts_df = load_alerts()
st.session_state["logbook"] = update_logbook(st.session_state["logbook"], alerts_df)

# === MAIN UI ===
st.markdown("### Latest Alerts (One Per Symbol)")

if not alerts_df.empty:
    latest_per_symbol = alerts_df.drop_duplicates(subset=["symbol"], keep="first")
    cols = st.columns(min(len(latest_per_symbol), 5))
    for idx, (_, row) in enumerate(latest_per_symbol.iterrows()):
        with cols[idx % 5]:
            symbol = row.get("symbol", "Unknown")
            direction = row.get("direction", "N/A")
            price = row.get("price", 0.0)
            ratio = row.get("ratio", 0.0)
            price_str = f"${float(price):.3f}" if pd.notna(price) else "N/A"
            ratio_str = f"{float(ratio):.2f}" if pd.notna(ratio) else "N/A"

            # Color logic
            if "bid" in direction.lower():
                color = "BUY"
            elif "ask" in direction.lower():
                color = "SELL"
            else:
                color = "NEUTRAL"

            st.metric(
                label=f"{color} {symbol}",
                value=price_str,
                delta=f"{direction} | Ratio: {ratio_str}",
            )
else:
    st.info("No alerts yet. Waiting for bot...")

st.divider()

# === POSITIONS + LOGBOOK ===
positions_col, logbook_col = st.columns([1, 1.2], gap="large")

with positions_col:
    st.markdown("<div class='panel alert-panel'>", unsafe_allow_html=True)
    st.subheader("Open Positions")
    positions_df = load_positions()
    if positions_df.empty:
        st.info("No open positions.")
    else:
        display = positions_df[["symbol", "quantity", "entry_price", "current_price", "pnl", "pnl_percent"]].copy()
        display["entry_price"] = display["entry_price"].apply(lambda x: f"${x:.3f}")
        display["current_price"] = display["current_price"].apply(lambda x: f"${x:.3f}")
        display["pnl"] = display["pnl"].apply(lambda x: f"${x:+.2f}")
        display["pnl_percent"] = display["pnl_percent"].apply(lambda x: f"{x:+.1f}%")
        display.columns = ["Symbol", "Qty", "Entry", "Current", "P&L $", "P&L %"]
        st.dataframe(display, use_container_width=True, hide_index=True)

        total_pnl = positions_df["pnl"].sum()
        gross_cost = (positions_df["entry_price"] * positions_df["quantity"]).sum()
        total_pnl_pct = (total_pnl / gross_cost * 100) if gross_cost > 0 else 0
        c1, c2 = st.columns(2)
        with c1: st.metric("Total P&L", f"${total_pnl:+.2f}", delta=f"{total_pnl_pct:+.1f}%")
        with c2: st.metric("Open Trades", len(positions_df))
    st.markdown("</div>", unsafe_allow_html=True)

with logbook_col:
    st.markdown("<div class='panel log-panel'>", unsafe_allow_html=True)
    st.subheader("Alert Logbook")
    logbook_df = st.session_state["logbook"]
    if logbook_df.empty:
        st.info("No alerts logged yet.")
    else:
        cols_to_show = [c for c in ["timestamp", "symbol", "direction", "price", "ratio", "total_bids", "total_asks", "heavy_venues"] if c in logbook_df.columns]
        logbook_df_display = logbook_df[cols_to_show].copy()
        logbook_df_display["price"] = logbook_df_display["price"].apply(lambda x: f"${x:.3f}" if pd.notna(x) else "N/A")
        logbook_df_display["ratio"] = logbook_df_display["ratio"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "N/A")
        logbook_df_display["timestamp"] = logbook_df_display["timestamp"].dt.strftime("%H:%M:%S")
        st.dataframe(logbook_df_display, use_container_width=True, hide_index=True, height=500)
    st.markdown("</div>", unsafe_allow_html=True)

# === AUTO REFRESH ===
refresh_col, info_col = st.columns([1, 3])
with refresh_col:
    if st.button("Refresh Now"):
        st.rerun()
with info_col:
    st.info(f"Auto-refresh every {REFRESH_INTERVAL_SECONDS} seconds")

# Auto-refresh
time_module.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
