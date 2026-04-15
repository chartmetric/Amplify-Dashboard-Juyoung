#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push

if [ -f artifacts/amplify/requirements.txt ]; then
  echo "[post-merge] Installing Python dependencies..."
  pip install -q -r artifacts/amplify/requirements.txt 2>/dev/null || true
fi
