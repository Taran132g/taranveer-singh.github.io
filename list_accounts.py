import os
import sys
import json
from dotenv import load_dotenv
from schwab.auth import easy_client

def main():
    load_dotenv()
    api_key = os.getenv("SCHWAB_CLIENT_ID")
    app_secret = os.getenv("SCHWAB_APP_SECRET")
    redirect_uri = os.getenv("SCHWAB_REDIRECT_URI")
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "./schwab_tokens.json")

    if not all([api_key, app_secret, redirect_uri]):
        print("Error: Missing environment variables. Please ensure SCHWAB_CLIENT_ID, SCHWAB_APP_SECRET, and SCHWAB_REDIRECT_URI are set in .env")
        return

    try:
        client = easy_client(
            api_key=api_key,
            app_secret=app_secret,
            callback_url=redirect_uri,
            token_path=token_path,
        )
    except Exception as e:
        print(f"Error initializing client: {e}")
        return

    try:
        # Fetch account numbers and hashes
        resp = client.get_account_numbers()
        if resp.status_code != 200:
            print(f"Error fetching accounts: {resp.status_code} {resp.text}")
            return

        accounts = resp.json()
        print("\n--- Schwab Accounts ---")
        for acc in accounts:
            print(f"Account Number: {acc.get('accountNumber')}")
            print(f"Account Hash:   {acc.get('hashValue')}")
            print("-" * 30)
        
        print("\nUse the 'Account Hash' corresponding to your Paper Money account in your .env file as SCHWAB_ACCOUNT_ID.")

    except Exception as e:
        print(f"Error fetching accounts: {e}")

if __name__ == "__main__":
    main()
