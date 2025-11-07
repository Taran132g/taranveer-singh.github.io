#!/bin/bash

echo "Starting Trading Alert Bot..."

# Ensure working directory is correct
cd /Users/taranveersingh/application-Software/remote_server/Trading\ alert\ bot

# Load .env file
if [ -f .env ]; then
    echo "Loading .env file..."
    source .env
else
    echo "❌ .env file not found at $(pwd)/.env"
    exit 1
fi

# Check environment variables
if [ -z "$SCHWAB_CLIENT_ID" ] || [ -z "$SCHWAB_APP_SECRET" ] || [ -z "$SCHWAB_REDIRECT_URI" ] || [ -z "$SCHWAB_ACCOUNT_ID" ]; then
    echo "❌ Missing required environment variables:"
    echo "SCHWAB_CLIENT_ID: $SCHWAB_CLIENT_ID"
    echo "SCHWAB_APP_SECRET: $SCHWAB_APP_SECRET"
    echo "SCHWAB_REDIRECT_URI: $SCHWAB_REDIRECT_URI"
    echo "SCHWAB_ACCOUNT_ID: $SCHWAB_ACCOUNT_ID"
    echo "Please check your .env file:"
    cat .env
    exit 1
fi

# Generate tokens if missing
if [ ! -f "$SCHWAB_TOKEN_PATH" ]; then
    echo "Generating Schwab tokens..."
    python -c "import os; from schwab.auth import easy_client; from dotenv import load_dotenv; load_dotenv(); easy_client(api_key=os.getenv('SCHWAB_CLIENT_ID'), app_secret=os.getenv('SCHWAB_APP_SECRET'), callback_url=os.getenv('SCHWAB_REDIRECT_URI'), token_path=os.getenv('SCHWAB_TOKEN_PATH', './schwab_tokens.json'))" || {
        echo "❌ Token generation failed"
        exit 1
    }
fi

# Start grok.py
echo "Starting grok.py..."
python grok.py --symbols SNAP --min-volume 100000 > grok.log 2>&1 &
GROK_PID=$!

# Wait for grok.py to initialize
sleep 5

# Check if grok.py is running
if ! kill -0 $GROK_PID 2>/dev/null; then
    echo "❌ grok.py failed to start. Check grok.log for errors."
    cat grok.log
    exit 1
fi

echo "✅ grok.py started (PID: $GROK_PID)"
echo "Starting Streamlit UI..."

# Start Streamlit
streamlit run ui.py &

echo "✅ Both services started. Access UI at http://localhost:8501"
echo "Press Ctrl+C to stop..."

wait $GROK_PID
