# auth_login.py
import os, sys, argparse
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import urlparse
from schwab import auth


def _normalize_and_validate_callback(url: str) -> str:
    """
    Ensure callback URL has a scheme and netloc, and ends with a trailing slash.
    Returns the normalized URL. Raises ValueError on invalid input.
    """
    if not url:
        raise ValueError("SCHWAB_REDIRECT_URI is empty")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid SCHWAB_REDIRECT_URI '{url}'. Expected full URL like 'https://127.0.0.1:8182/'.")
    normalized = url if url.endswith("/") else url + "/"
    return normalized

def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize/refresh Schwab OAuth tokens.")
    parser.add_argument("--force-login", action="store_true", help="Force interactive login even if token file exists.")
    parser.add_argument("--non-interactive", action="store_true", help="Attempt non-interactive auth (if supported).")
    parser.add_argument("--timeout", type=int, default=300, help="Callback wait timeout (seconds) for login flow.")
    args = parser.parse_args()

    load_dotenv()
    api_key    = os.getenv("SCHWAB_CLIENT_ID")      # App Key (no @AMER.OAUTHAP for schwab-py)
    app_secret = os.getenv("SCHWAB_APP_SECRET")     # App Secret
    callback   = os.getenv("SCHWAB_REDIRECT_URI")   # e.g., https://127.0.0.1:8182/
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "./schwab_tokens.json")

    missing = [k for k,v in {
        "SCHWAB_CLIENT_ID": api_key,
        "SCHWAB_APP_SECRET": app_secret,
        "SCHWAB_REDIRECT_URI": callback,
    }.items() if not v]
    if missing:
        print(f"❌ Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    try:
        callback = _normalize_and_validate_callback(callback)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)

    token_file = Path(token_path)
    # Ensure token directory exists
    token_file.parent.mkdir(parents=True, exist_ok=True)

    def do_login_flow() -> None:
        print(f"🔐 Starting login flow… (token path: {token_file})")
        try:
            auth.client_from_login_flow(
                api_key=api_key,
                app_secret=app_secret,
                callback_url=callback,
                token_path=str(token_file),
                callback_timeout=args.timeout,
                interactive=not args.non_interactive,
            )
            print(f"✅ Token saved to {token_file.resolve()}")
        except Exception as e:
            print(f"❌ Login flow failed: {e}", file=sys.stderr)
            sys.exit(3)

    # Run login if forced or token missing
    if args.force_login or not token_file.exists():
        do_login_flow()

    # Try to refresh/validate tokens; if it fails, fall back to login once.
    try:
        _ = auth.client_from_token_file(
            token_path=str(token_file),
            api_key=api_key,
            app_secret=app_secret,
        )
        print("✅ Auth OK. Token file loaded/refreshed.")
    except Exception as e:
        print(f"⚠️  Token refresh failed ({e}). Trying login flow once…", file=sys.stderr)
        do_login_flow()
        try:
            _ = auth.client_from_token_file(
                token_path=str(token_file),
                api_key=api_key,
                app_secret=app_secret,
            )
            print("✅ Auth OK after re-login. Token file loaded/refreshed.")
        except Exception as e2:
            print(f"❌ Auth failed even after re-login: {e2}", file=sys.stderr)
            sys.exit(4)

if __name__ == "__main__":
    main()
