import requests
import json
import time
import os
from datetime import datetime, timezone


BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
OUTPUT_FILE = "rt_volatility_data.json"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1401633089641250986/1I8pP8oqRXcWo99ga1OMpGTfUvr5bwHUKq5h8Bjw5gshTWwX1_0hRQIt16ocnp4qwKDg"
ALERT_THRESHOLD = 10  # cents

def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def find_alerts(current_data, previous_data):
    alerts = []

    for event_title, event_data in current_data.items():
        prev_event = previous_data.get(event_title, {})
        prev_markets = {m['ticker']: m for m in prev_event.get("markets", [])}

        for market in event_data["markets"]:
            ticker = market["ticker"]
            bin_label = market["bin"]

            prev = prev_markets.get(ticker)
            if not prev:
                continue

            for key in ["yes_ask", "yes_bid", "last_price"]:
                old = prev.get(key)
                new = market.get(key)
                if old is None or new is None:
                    continue
                if abs(new - old) >= ALERT_THRESHOLD:
                    alerts.append({
                        "event": event_title,
                        "ticker": ticker,
                        "bin": bin_label,
                        "field": key,
                        "old": old,
                        "new": new,
                        "change": new - old
                    })

    return alerts

def send_discord_alert(alerts):
    if not alerts:
        return

    content = "**🔔 Kalshi Volatility Alert (≥10¢ change)**\n"
    for alert in alerts:
        content += (
            f"- `{alert['event']}`\n"
            f"  Bin **{alert['bin']}** ({alert['field']}): "
            f"{alert['old']}¢ → {alert['new']}¢ (Δ {alert['change']}¢)\n"
        )

    requests.post(DISCORD_WEBHOOK_URL, json={"content": content})


def log_alerts_to_file(alerts, path="alerts_history.json"):
    if not alerts:
        return

    # Ensure file is a dict, not a list
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                existing = json.load(f)
                if not isinstance(existing, dict):
                    existing = {}
            except json.JSONDecodeError:
                existing = {}
    else:
        existing = {}

    # Add new alerts with timestamp
    timestamp = datetime.now(datetime.UTC).isoformat()
    existing[timestamp] = alerts

    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


def save_alert_history(path, history):
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(history, f, indent=2)

    
def load_alert_history(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r') as f:
        return json.load(f)





def get_all_rotten_tomatoes_markets():
    url = f"{BASE_URL}/markets"
    params = {
        'category': 'entertainment',
        'status': 'open',
        'limit': 1000
    }

    all_markets = []
    cursor = None

    while True:
        if cursor:
            params['cursor'] = cursor

        response = requests.get(url, params=params)
        if response.status_code != 200:
            print(f"❌ Error fetching markets: {response.status_code}")
            break

        data = response.json()
        for market in data.get('markets', []):
            if "Rotten Tomatoes" in market.get("title", ""):
                all_markets.append(market)

        cursor = data.get('cursor')
        if not cursor:
            break

    return all_markets

def get_event_markets(event_ticker):
    url = f"{BASE_URL}/events/{event_ticker}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if 'markets' in data:
            return data['markets']
        elif 'event' in data and 'markets' in data['event']:
            return data['event']['markets']
    return []

def get_market_details(ticker):
    url = f"{BASE_URL}/markets/{ticker}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json().get('market', {})
    return {}

def scan_and_store():
    print("🔍 Scanning Rotten Tomatoes markets...")
    markets = get_all_rotten_tomatoes_markets()
    output = {}

    for m in markets:
        title = m.get("title")
        event = m.get("event_ticker")
        if not event:
            continue

        event_data = []
        sub_markets = get_event_markets(event)
        for sm in sub_markets:
            details = get_market_details(sm['ticker'])
            market_bin = sm['ticker'].split("-")[-1]
            data_point = {
                "bin": market_bin,
                "yes_ask": details.get("yes_ask"),
                "yes_bid": details.get("yes_bid"),
                "no_ask": details.get("no_ask"),
                "no_bid": details.get("no_bid"),
                "last_price": details.get("last_price"),
                "volume": sm.get("volume"),
                "ticker": sm['ticker']
            }
            event_data.append(data_point)

        output[title] = {
            "event_ticker": event,
            "markets": event_data
        }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"✅ Data saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    previous_data = load_json("previous_data.json")
    scan_and_store()
    current_data = load_json("rt_volatility_data.json")

    alerts = find_alerts(current_data, previous_data)

    if alerts:
        send_discord_alert(alerts)
        if alerts:
            history = load_alert_history("alerts_history.json")
            timestamp = datetime.now(timezone.utc).isoformat()
            for alert in alerts:
                alert["timestamp"] = timestamp
                history.append(alert)
            save_alert_history("alerts_history.json", history)
        log_alerts_to_file(alerts)

    save_json("previous_data.json", current_data)

