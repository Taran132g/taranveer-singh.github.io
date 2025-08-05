import requests

def get_all_markets():
    url = "https://trading-api.kalshi.com/trade-api/v2/markets"
    try:
        response = requests.get(url)
        return response.json().get("markets", [])
    except Exception as e:
        print(f"Error fetching Kalshi markets: {e}")
        return []
