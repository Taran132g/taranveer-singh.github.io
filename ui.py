import sqlite3
import time
from contextlib import closing

import pandas as pd
import streamlit as st

DB_PATH = "/Users/taranveersingh/application-Software/remote_server/Trading alert bot/penny_basing.db"
REFRESH_INTERVAL_SECONDS = 5

st.set_page_config(page_title="Arbitrage Tracker Dashboard", layout="wide")

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

st.title("Arbitrage Tracker Dashboard")
placeholder = st.empty()


def load_alerts() -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM alerts ORDER BY timestamp DESC",
            conn,
        )
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


if "logbook" not in st.session_state:
    st.session_state["logbook"] = pd.DataFrame()

while True:
    alerts_df = load_alerts()
    st.session_state["logbook"] = update_logbook(
        st.session_state["logbook"], alerts_df
    )

    with placeholder.container():
        alerts_col, log_col = st.columns(2, gap="large")

        with alerts_col:
            st.markdown("<div class='panel alert-panel'>", unsafe_allow_html=True)
            st.subheader("Live Alerts")

            if alerts_df.empty:
                st.info("No alerts have been generated yet. Monitoring continues.")
            else:
                for _, row in alerts_df.iterrows():
                    title_parts = [row.get("symbol", "Unknown")]
                    direction = row.get("direction")
                    price = row.get("price")
                    if direction:
                        title_parts.append(direction.upper())
                    if pd.notna(price):
                        try:
                            title_parts.append(f"@ {float(price):.2f}")
                        except (TypeError, ValueError):
                            title_parts.append(f"@ {price}")

                    header_text = " • ".join(title_parts)
                    timestamp = row.get("timestamp")
                    timestamp_str = (
                        timestamp.strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(timestamp, pd.Timestamp)
                        else str(timestamp)
                    )

                    with st.expander(header_text, expanded=False):
                        st.markdown(
                            f"<div class='alert-header'>{row.get('symbol', 'Unknown')}</div>",
                            unsafe_allow_html=True,
                        )
                        st.markdown(
                            f"<div class='alert-meta'>Captured {timestamp_str}</div>",
                            unsafe_allow_html=True,
                        )

                        detail_items = {
                            "Ticker": row.get("symbol"),
                            "Direction": row.get("direction"),
                            "Price": row.get("price"),
                            "Ratio": row.get("ratio"),
                            "Total Bids": row.get("total_bids"),
                            "Total Asks": row.get("total_asks"),
                            "Heavy Venues": row.get("heavy_venues"),
                        }
                        for label, value in detail_items.items():
                            if pd.notna(value):
                                st.write(f"**{label}:** {value}")

            st.markdown("</div>", unsafe_allow_html=True)

        with log_col:
            st.markdown("<div class='panel log-panel'>", unsafe_allow_html=True)
            st.subheader("Logbook")

            logbook_df = st.session_state["logbook"]
            if logbook_df.empty:
                st.info("No log entries yet. New activity will appear here automatically.")
            else:
                display_cols = [
                    col
                    for col in [
                        "timestamp",
                        "symbol",
                        "direction",
                        "price",
                        "ratio",
                        "total_bids",
                        "total_asks",
                        "heavy_venues",
                    ]
                    if col in logbook_df.columns
                ]
                st.dataframe(
                    logbook_df[display_cols],
                    use_container_width=True,
                    hide_index=True,
                )

            st.markdown("</div>", unsafe_allow_html=True)

    time.sleep(REFRESH_INTERVAL_SECONDS)
    st.experimental_rerun()
