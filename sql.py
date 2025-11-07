import sqlite3
from datetime import datetime

# Connect to the database
db_path = "/Users/taranveersingh/application-Software/remote_server/Trading alert bot/penny_basing.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Query all alerts, excluding ratio and direction
cursor.execute("SELECT timestamp, symbol, total_bids, total_asks, heavy_venues FROM alerts ORDER BY timestamp")
alerts = cursor.fetchall()

# Print alerts in a readable format
for alert in alerts:
    ts, symbol, total_bids, total_asks, heavy_venues = alert
    ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts_str}] {symbol} | Bids: {total_bids:,} | Asks: {total_asks:,} | Heavy Venues: {heavy_venues}")

# Optional: Filter by symbol (e.g., SNAP), excluding ratio and direction
cursor.execute("SELECT timestamp, symbol, total_bids, total_asks, heavy_venues FROM alerts WHERE symbol = ? ORDER BY timestamp", ("SNAP",))
snap_alerts = cursor.fetchall()
print("\nSNAP Alerts:")
for alert in snap_alerts:
    ts, symbol, total_bids, total_asks, heavy_venues = alert
    ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts_str}] Bids: {total_bids:,} | Asks: {total_asks:,} | Heavy Venues: {heavy_venues}")

# Close connection
conn.close()