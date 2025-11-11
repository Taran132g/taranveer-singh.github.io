import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st

REFRESH_INTERVAL_SECONDS = 5

# Setup database path - use local AppData to avoid OneDrive sync conflicts
# Fallback: use project data folder if local path fails
LOCAL_DB_DIR = Path.home() / "AppData" / "Local" / "taranveer_app"
try:
    LOCAL_DB_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = LOCAL_DB_DIR / "penny_basing.db"
except Exception:
    BASE_DIR = Path(__file__).resolve().parent
    DB_DIR = BASE_DIR / "data"
    DB_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DB_DIR / "penny_basing.db"


def init_db() -> None:
    """Ensure the alerts table exists."""
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
            )
            if not cursor.fetchone():
                cursor.execute(
                    """
                    CREATE TABLE alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        direction TEXT,
                        price REAL,
                        ratio REAL,
                        total_bids INTEGER,
                        total_asks INTEGER,
                        heavy_venues TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
    except Exception:
        # Silently fail, app will handle empty results
        pass


def init_positions_table() -> None:
    """Create positions table if it doesn't exist."""
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            )
            if not cursor.fetchone():
                cursor.execute(
                    """
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
                    """
                )
                conn.commit()
    except Exception:
        pass


init_db()
init_positions_table()

st.set_page_config(page_title="Arbitrage Tracker Dashboard", layout="wide")

st.title("Penny Basing Alerts")
st.markdown(
    """
    <style>
    body {
        background-color: #0f172a;
        color: #f8fafc;
    }
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
    .alert-header {
        font-size: 1.05rem;
        font-weight: 600;
        margin-bottom: 0.35rem;
    }
    .alert-meta {
        font-size: 0.85rem;
        opacity: 0.85;
        margin-bottom: 0.75rem;
    }
    .stExpander > div > div {
        border: 1px solid rgba(148, 163, 184, 0.25);
        border-radius: 0.85rem;
        background-color: rgba(15, 23, 42, 0.45);
    }
    .stDataFrame div[data-testid="stDataFrame"] {
        background-color: rgba(15, 23, 42, 0.25);
        border-radius: 0.85rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_alerts() -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            df = pd.read_sql_query(
                "SELECT * FROM alerts ORDER BY timestamp DESC",
                conn,
            )
    except Exception as exc:
        st.warning(f"⚠️ Database issue: {exc}")
        st.info(f"📍 Looking for database at: `{DB_PATH}`")
        st.write("Make sure the trading bot is running and creating alerts...")
        return pd.DataFrame()

    if df.empty:
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(
        None
    )
    return df


def update_logbook(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if incoming.empty:
        return existing

    combined = pd.concat([incoming, existing], ignore_index=True)
    combined = combined.drop_duplicates().sort_values("timestamp", ascending=False)
    return combined.reset_index(drop=True)


def load_positions() -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as conn:
            df = pd.read_sql_query(
                "SELECT * FROM positions WHERE status='open' ORDER BY entry_time DESC",
                conn,
            )
    except Exception:
        return pd.DataFrame()

    return df


if "logbook" not in st.session_state:
    st.session_state["logbook"] = pd.DataFrame()

alerts_df = load_alerts()
st.session_state["logbook"] = update_logbook(st.session_state["logbook"], alerts_df)

st.markdown("### 📊 Recent Ticker Alerts (Latest per Stock)")

if not alerts_df.empty:
    latest_per_symbol = alerts_df.drop_duplicates(subset=["symbol"], keep="first")
    cols = st.columns(min(len(latest_per_symbol), 5))

    for idx, (_, row) in enumerate(latest_per_symbol.iterrows()):
        with cols[idx % 5]:
            symbol = row.get("symbol", "Unknown")
            direction = row.get("direction", "N/A")
            price = row.get("price", "N/A")
            ratio = row.get("ratio", "N/A")

            price_str = f"${float(price):.2f}" if pd.notna(price) else "N/A"

            if direction == "BUY":
                color = "🟢"
            elif direction == "SELL":
                color = "🔴"
            else:
                color = "⚪"

            st.metric(
                label=f"{color} {symbol}",
                value=price_str,
                delta=f"{direction} | Ratio: {ratio}",
            )
else:
    st.info("No alerts available yet")

st.divider()

positions_col, logbook_col = st.columns([1, 1.2], gap="large")

with positions_col:
    st.markdown("<div class='panel alert-panel'>", unsafe_allow_html=True)
    st.subheader("💰 Open Positions")

    positions_df = load_positions()

    if positions_df.empty:
        st.info("No open positions. Ready to trade!")
    else:
        display_positions = positions_df[
            ["symbol", "quantity", "entry_price", "current_price", "pnl", "pnl_percent"]
        ].copy()

        display_positions["entry_price"] = display_positions["entry_price"].apply(
            lambda value: f"${value:.2f}"
        )
        display_positions["current_price"] = display_positions["current_price"].apply(
            lambda value: f"${value:.2f}"
        )
        display_positions["pnl"] = display_positions["pnl"].apply(
            lambda value: f"${value:+.2f}"
        )
        display_positions["pnl_percent"] = display_positions["pnl_percent"].apply(
            lambda value: f"{value:+.1f}%"
        )

        display_positions.columns = [
            "Symbol",
            "Qty",
            "Entry",
            "Current",
            "P&L $",
            "P&L %",
        ]

        st.dataframe(display_positions, use_container_width=True, hide_index=True)

        total_pnl = positions_df["pnl"].sum()
        gross_cost = (positions_df["entry_price"] * positions_df["quantity"]).sum()
        total_pnl_percent = (total_pnl / gross_cost * 100) if gross_cost > 0 else 0

        pnl_col, count_col = st.columns(2)
        with pnl_col:
            st.metric("Total P&L", f"${total_pnl:+.2f}", delta=f"{total_pnl_percent:+.1f}%")
        with count_col:
            st.metric("Open Positions", len(positions_df))

    st.markdown("</div>", unsafe_allow_html=True)

with logbook_col:
    st.markdown("<div class='panel log-panel'>", unsafe_allow_html=True)
    st.subheader("📋 Alert Logbook")

    logbook_df = st.session_state["logbook"]

    if logbook_df.empty:
        st.info("No log entries yet. New activity will appear here automatically.")
    else:
        display_cols = [
            column
            for column in [
                "timestamp",
                "symbol",
                "direction",
                "price",
                "ratio",
                "total_bids",
                "total_asks",
                "heavy_venues",
            ]
            if column in logbook_df.columns
        ]

        st.markdown(
            """
            <style>
            .scrollable-logbook {
                max-height: 500px;
                overflow-y: auto;
                border: 1px solid rgba(148, 163, 184, 0.25);
                border-radius: 0.5rem;
                padding: 1rem;
                background-color: rgba(15, 23, 42, 0.25);
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        st.dataframe(
            logbook_df[display_cols],
            use_container_width=True,
            hide_index=True,
            height=500,
        )

    st.markdown("</div>", unsafe_allow_html=True)

import time as time_module

refresh_col, info_col = st.columns([1, 3])
with refresh_col:
    if st.button("🔄 Refresh Now"):
        st.rerun()

with info_col:
    st.info(
        f"Auto-refresh every {REFRESH_INTERVAL_SECONDS} seconds (set lower for real-time tracking)"
    )

time_module.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
