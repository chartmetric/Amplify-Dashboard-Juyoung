#!/bin/bash
set -e
pnpm install --frozen-lockfile

# DO NOT add `pnpm --filter @workspace/db run push-force` here.
#
# `lib/db/src/schema/index.ts` is intentionally empty (`export {}`) — this
# project doesn't manage its tables through Drizzle. The only consumers
# of Postgres are the Python app (`artifacts/amplify/app.py`) which
# creates `email_drafts` / `email_hosted_images` via `CREATE TABLE IF
# NOT EXISTS`, and Express session middleware which creates `session`.
#
# `drizzle-kit push --force` against an empty schema treats every
# existing table as "undeclared" and drops them with CASCADE. This was
# the root cause of multiple production wipes of My Content (drafts +
# sent artifacts) on every task merge. Verified locally: running
# `npx drizzle-kit push --verbose` from `lib/db/` against the empty
# schema generates exactly:
#   DROP TABLE "email_hosted_images" CASCADE;
#   DROP TABLE "email_drafts" CASCADE;
#   DROP TABLE "session" CASCADE;
# The Python app then recreates the tables empty on the next request,
# which looks like the data simply vanished. If a future change
# actually wants to manage Postgres tables through Drizzle, declare
# the schema first and only re-enable the push after confirming the
# diff against production is non-destructive.

if [ -f artifacts/amplify/requirements.txt ]; then
  echo "[post-merge] Installing Python dependencies..."
  pip install -q -r artifacts/amplify/requirements.txt 2>/dev/null || true
fi
