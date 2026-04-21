#!/bin/bash
while true; do
    echo "[run.sh] Starting Amplify server..."
    python -u bootstrap.py
    EXIT_CODE=$?
    echo "[run.sh] Server exited with code $EXIT_CODE, restarting in 2 seconds..."
    sleep 2
done
