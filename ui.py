# ui.py
import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import time

st.title("Penny Basing Alerts")

# Placeholder for the dataframe
placeholder = st.empty()

while True:
    # Connect to database and query alerts
    conn = sqlite3.connect("/Users/taranveersingh/application-Software/remote_server/Trading alert bot/penny_basing.db")
    df = pd.read_sql_query("SELECT * FROM alerts ORDER BY timestamp DESC", conn)
    conn.close()

    # Convert timestamp to datetime
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')

    # Display the dataframe
    with placeholder.container():
        st.write("Latest Alerts (updates every 5 seconds)")
        if not df.empty:
            st.dataframe(df[['timestamp', 'symbol', 'ratio', 'total_bids', 'total_asks', 'heavy_venues', 'direction', 'price']])
        else:
            st.write("No alerts yet.")

    # Wait before refreshing
    time.sleep(5)
    st.rerun()  # Correctly call st.rerun()
