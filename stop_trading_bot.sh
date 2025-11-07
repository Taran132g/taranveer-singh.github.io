#!/bin/bash

# Identify PIDs for grok.py and ui.py
echo "Identifying running processes..."
PIDS=$(ps aux | grep -E "[g]rok.py|[s]treamlit.*ui.py" | awk '{print $2}')

if [ -z "$PIDS" ]; then
    echo "No grok.py or ui.py processes found."
    exit 0
fi

# Kill the identified PIDs
echo "Stopping processes with PIDs: $PIDS"
kill -9 $PIDS

# Verify
if ! ps -p $PIDS > /dev/null 2>&1; then
    echo "Processes stopped successfully."
else
    echo "Some processes may still be running. Check manually."
fi