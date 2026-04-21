#!/bin/bash
cd "$(dirname "$0")/artifacts/amplify" && exec python -u bootstrap.py
