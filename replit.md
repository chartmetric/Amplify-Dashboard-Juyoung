# Product Marketing Autopilot

A marketing autopilot that ingests feature data and generates marketing content.

## Run & Operate

- **Run (Amplify)**: `python artifacts/amplify/app.py` (from `artifacts/amplify` directory, listens on port 5000)
- **Run (API Server dev)**: `pnpm --filter @workspace/api-server run dev`
- **Build**: `pnpm run build` (runs `typecheck` then `build` in all packages)
- **Typecheck**: `pnpm run typecheck` (runs `tsc --build --emitDeclarationOnly` from root)
- **Codegen (API client/schemas)**: `pnpm --filter @workspace/api-spec run codegen`
- **DB Push (dev)**: `pnpm --filter @workspace/db run push` (or `push-force`)

**Required Environment Variables**:
- `ANTHROPIC_API_KEY` (Amplify)
- `ASANA_ACCESS_TOKEN` (Amplify)
- `SLACK_BOT_TOKEN` (Amplify)
- `AMPLIFY_ADMIN_TOKEN` (Amplify, for admin endpoints)
- `FIREBASE_API_KEY`, `FIREBASE_PROJECT_ID`, `FIREBASE_APP_ID` (Amplify, Firebase Google Sign-In)
- `DATABASE_URL` (lib/db, for Drizzle Kit config)
- `PORT` (api-server)

## Stack

- **Monorepo**: pnpm workspaces
- **Node.js**: 24
- **Python**: 3.12
- **JS/TS Package Manager**: pnpm
- **Python Package Manager**: uv/pip
- **TypeScript**: 5.9
- **TS API Framework**: Express 5
- **Python API Framework**: Flask
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (v4), `drizzle-zod`
- **API Codegen**: Orval (from OpenAPI spec)
- **Build Tool**: esbuild (CJS bundle)

## Where things live

- `/artifacts/amplify`: Python Flask application (entry: `app.py`)
- `/artifacts/api-server`: TypeScript Express API server (entry: `src/index.ts`)
- `/lib/api-spec`: OpenAPI spec (`openapi.yaml`) and Orval config (`orval.config.ts`)
- `/lib/api-client-react`: Generated React Query hooks
- `/lib/api-zod`: Generated Zod schemas
- `/lib/db`: Drizzle ORM schema and DB connection (schema: `src/schema/index.ts`)
- `/scripts`: Utility scripts
- `pnpm-workspace.yaml`: pnpm workspace configuration
- `tsconfig.base.json`: Shared TypeScript configuration

## Architecture decisions

- **Amplify owns both `/` and `/api` prefixes**: simplifies routing for a hybrid app.
- **Email Drafts persistence includes robust wipe-proof rules**: specific per-row helpers, restricted `DELETE` operations, daily JSON snapshots, and snapshot restoration on startup prevent data loss due to transient DB issues or accidental schema changes.
- **`lib/db` has an empty Drizzle schema**: Drizzle ORM is used for type-safety and client generation, but the actual database schema is managed externally by the Python app and Express session middleware. This prevents Drizzle Kit from dropping tables on `drizzle-kit push`.
- **Slack-first pipeline with Asana enrichment**: Features are primarily extracted from Slack, then enriched with details from Asana if available.
- **Tiered Classification with Adaptive Learning**: Combines keyword-based `quick_classify` for speed with Claude API for complex cases. Adaptive learning tracks marketer overrides and disables keywords that lead to frequent corrections.

## Product

- **Feature Ingestion**: From Asana, Slack, and manual entries. Supports URL extraction.
- **Content Generation**: AI-powered marketing content for multiple channels (Twitter, Email, In-app, LinkedIn, Notion, Article).
- **Classification & Prioritization**: Auto-classifies features by importance and category, with user-definable overrides and adaptive learning.
- **Video Publishing**: Uploads, thumbnail generation, and serving of videos for marketing content.
- **Draft Management**: Save-as-draft functionality with server-side autosave and persistence for email drafts.
- **Admin Tools**: Endpoints for backfilling classifications and cleaning up orphan video files.

## User preferences

- _Populate as you build_

## Gotchas

- **Typechecking**: Always run `pnpm run typecheck` from the root to ensure correct cross-package type resolution. Running `tsc` within a single package may fail.
- **DB Migrations**: `lib/db` does not manage schema via Drizzle. Do NOT re-add `drizzle-kit push` to build scripts unless `lib/db/src/schema/index.ts` is populated to match all real tables, as it will drop existing tables.
- **Autosave Invariants**: Server-side autosave for drafts only triggers when `window._currentDraftId` is present and `prepBatchResults` is not empty. Manual saves cancel pending autosaves to prevent stale data overwrites.

## Pointers

- **Drizzle ORM**: [https://orm.drizzle.team/](https://orm.drizzle.team/)
- **Orval**: [https://orval.dev/](https://orval.dev/)
- **Zod**: [https://zod.dev/](https://zod.dev/)
- **pnpm workspaces**: [https://pnpm.io/workspaces](https://pnpm.io/workspaces)
- **TypeScript Project References**: [https://www.typescriptlang.org/docs/handbook/project-references.html](https://www.typescriptlang.org/docs/handbook/project-references.html)