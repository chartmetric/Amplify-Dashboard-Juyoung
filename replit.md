# Workspace

## Overview

pnpm workspace monorepo using TypeScript and Python. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Python version**: 3.12
- **Package manager**: pnpm (JS/TS), uv/pip (Python)
- **TypeScript version**: 5.9
- **API framework (TS)**: Express 5
- **API framework (Python)**: Flask
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Structure

```text
artifacts-monorepo/
├── artifacts/              # Deployable applications
│   ├── api-server/         # Express API server (TypeScript) — routed to /api-server
│   └── amplify/            # Amplify - Product Marketing Autopilot (Python Flask)
│       ├── app.py          # Flask application entry point (waitress WSGI)
│       ├── config.py       # Module-level env var config
│       ├── templates/      # Jinja2 HTML templates
│       │   └── index.html  # Dashboard placeholder
│       ├── sources/        # Data source adapters
│       │   ├── __init__.py
│       │   ├── base.py     # FeatureContext dataclass + SourceAdapter ABC
│       │   ├── asana_source.py  # Asana project task ingestion
│       │   ├── slack_source.py  # Slack channel message ingestion
│       │   └── manual_source.py # Manual feature entry
│       ├── integrations/    # Publishing integrations
│       │   ├── __init__.py
│       │   ├── twitter_client.py  # Twitter/X publishing (API + fallback intent URL)
│       │   ├── sendgrid_client.py # Resend email sending (Resend-only, individual sends via Batch API)
│       │   └── inapp_client.py    # In-app announcements (in-memory store)
│       └── ai/             # AI integration module
│           ├── __init__.py
│           └── generator.py  # Empty placeholder for now
├── lib/                    # Shared libraries
│   ├── api-spec/           # OpenAPI spec + Orval codegen config
│   ├── api-client-react/   # Generated React Query hooks
│   ├── api-zod/            # Generated Zod schemas from OpenAPI
│   └── db/                 # Drizzle ORM schema + DB connection
├── scripts/                # Utility scripts (single workspace package)
│   └── src/                # Individual .ts scripts
├── pnpm-workspace.yaml     # pnpm workspace
├── tsconfig.base.json      # Shared TS options
├── tsconfig.json           # Root TS project references
└── package.json            # Root package with hoisted devDeps
```

## Amplify (Python Flask App)

Product marketing autopilot that ingests feature data from multiple sources and generates marketing content.

- **Entry**: `artifacts/amplify/app.py` (waitress WSGI server)
- **Config**: `artifacts/amplify/config.py` (module-level env vars: ANTHROPIC_API_KEY, ASANA_ACCESS_TOKEN, SLACK_BOT_TOKEN)
- **Port**: 5000
- **Paths**: `/` and `/api` (owns both route prefixes)
- **Python packages**: flask, anthropic, asana, slack-sdk, requests, python-dotenv, waitress, ffmpeg (system)
- **Video uploads**: Local video file upload to `.publish_videos/` via `/api/publish/video` (POST, base64 data URL); ffmpeg extracts first-frame thumbnail; served via `/api/videos/<id>` and `/api/videos/<id>/thumb`; `[video: filename]` markers in email content render as clickable thumbnail with play button overlay
- **Publishing channels**:
  - **API-backed**: twitter (X API + intent URL fallback), email_newsletter/email_standalone (Resend), inapp (in-memory announcements)
  - **Clipboard**: linkedin, notion_monthly, article_hmc (copy to clipboard with channel-specific paste instructions)
  - **Email standalone**: Single UI card with a length dropdown (Short ≤500 / Medium ≤1000 / Long ≤1500). Backend has three separate channel configs (email_short, email_medium, email_long) that define different generation prompts/limits. Frontend maps `email_standalone` to the selected variant key before API calls and remaps responses back. Changing the dropdown triggers regeneration with the new variant config. Legacy cached email_short/medium/long data is auto-migrated to email_standalone in the UI.
- **Manual features**: POST /api/features/manual - adds feature, classifies via Claude, returns to list
- **Source registry**: SOURCE_REGISTRY dict mapping "asana", "slack", "manual" to adapter instances
- **Routes**:
  - `GET /` — HTML dashboard
  - `GET /api/health` — JSON health check with API key status
  - `GET /api/sources` — list of registered source types
  - `GET /api/sources/asana/features` — list Asana features
  - `GET /api/sources/asana/features/<feature_id>` — get full Asana feature context
  - `GET /api/sources/slack/features` — list Slack features
  - `GET /api/sources/slack/features/<feature_id>` — get full Slack message context
  - `POST /api/sources/manual/feature` — create manual FeatureContext from JSON
  - `GET /api/features/<source_type>` — unified list endpoint
  - `GET /api/features/<source_type>/<feature_id>` — unified detail endpoint
  - `GET /api/features/all` — Slack-first pipeline features with cached classifications (?days=30&limit=100&refresh=false)
  - `GET /api/features/all-raw` — all Asana features as unclassified cards (bypasses pre-filter)
  - `GET /api/features/classified` — auto-classified features sorted by importance (?limit=20&min_importance=N)
  - `POST /api/features/<feature_id>/classify` — classify single feature on demand
  - `POST /api/features/classify-batch-async` — async batch classification
  - `POST /api/generate` — generate marketing content for a feature
  - `POST /api/generate/batch` — batch content generation
  - `POST /api/generate/single` — single channel content generation
  - `POST /api/features/from-url` — extract feature from pasted URL (Slack/Asana/GitHub) or plain text; supports multi-URL input
  - `POST /api/classification/override` — save classification override
  - `GET /api/classification/overrides` — list override history
- **Architecture (Slack-first pipeline)**:
  - `sources/base.py` — FeatureContext dataclass + SourceAdapter ABC
  - `sources/slack_source.py` — SlackSource: extracts features from #product-updates release messages as bullets with stable IDs (slack-{ts}-{idx}), parses Slack link format `<URL|text>`, extracts prefixes (PE/Devin/FE/BE), release versions, reactions, thread URLs
  - `sources/asana_source.py` — AsanaSource: enrichment-only via `enrich_feature()` (3-tier: URL match → title search → no match); `list_unannounced_tasks()` for Asana-only features; workspace GID=1198197264916217
  - `sources/manual_source.py` — ManualSource (stateless, returns FeatureContext from kwargs)
  - `ai/generator.py` — Content generation across 9 channels (twitter, email_newsletter, email_short, email_medium, email_long, inapp, linkedin, notion_monthly, article_hmc)
  - `ai/classifier.py` — Tiered classification: `quick_classify()` (keyword-based, no API call) + Claude API for everything else; adaptive learning disables keywords after 3+ overrides; word-boundary matching to prevent false positives. Low-signal guardrail: `has_sufficient_signal()` (title+desc) and `is_obviously_junk_title()` (title-only, used for backfill) short-circuit garbage inputs (`,etc.`, `tbd`, `[Duplicate] ...`, `v16 -> v17`) to importance 0 / `skip_reason: "insufficient_input"` / `classification_method: "guardrail_low_signal"`. Slack parser also rejects low-quality bullets via `is_low_quality_title()` in `sources/slack_source.py`.
  - `POST /api/admin/backfill-low-signal-classifications` — one-shot endpoint that walks the cache and downgrades any historical row whose title is obviously junk (supports `dry_run`).
  - `ai/classification_overrides.py` — In-memory override store + learning context
- **Pipeline**: `_get_slack_first_features(days)` → Slack extraction → parallel Asana enrichment (ThreadPoolExecutor, 5 workers) → unannounced task scan → 120s TTL cache
- **Feature IDs**: `slack-{ts}-{bullet_idx}` for Slack features; Asana GID for asana-only
- **Feature sources**: `slack+asana` (teal), `slack_only` (yellow), `asana_only` (orange)
- **Run**: `python app.py` (from artifacts/amplify directory)
- **Classification overrides**: Inline editing on dashboard cards
  - Score badge: clickable, shows popover with 1-5 options
  - Category badges: clickable, multi-select toggle popover, supports 1-3 categories per feature
  - Override reason: inline text input appears after change, optional
  - Auto-save: pending override commits on card switch or navigation
  - Visual indicators: pencil icon on overridden scores/categories with tooltip
  - Learning: last 3 overrides injected into Claude classifier system prompt
  - `POST /api/classification/override` — save override + recalculate channels + teach AI
  - `GET /api/classification/overrides` — list all override history
  - `ai/classification_overrides.py` — in-memory override store + learning context builder
- **Tiered classification**: 
  - Tier 1 (quick_classify): 50 keyword patterns with word-boundary regex matching, auto-assigns importance 1, no Claude API call
  - Tier 2 (Claude): full classification for features that don't match any keyword
  - Adaptive learning: keyword override tracking; after 3+ marketer overrides on same keyword, that keyword is disabled from auto-skip
  - Dashboard shows tier breakdown: "X auto-skipped | Y AI-classified | Z pending"
  - Auto-classified cards show yellow "&#9889; Auto-classified" pill and "Reclassify with AI" button
  - `POST /api/features/reclassify` — force Claude reclassification for a specific feature
  - `GET /api/classifier/keywords` — list all keywords with match/override counts
  - `POST /api/classifier/keywords` — add/remove keywords
  - `GET /api/classifier/tier-stats` — get auto-skipped/AI-classified/total counts
- **Pagination**: Client-side pagination with `currentPage`/`perPage` (default 25); `renderAll()` slices filtered features; `renderPagination()` appends controls after feature list; per-page dropdown (25/50/100/All); page resets on filter/category changes; section dividers render within paginated view
- **Feature IDs (manual)**: UUID-based (`manual-{uuid4.hex[:12]}`) to prevent collision when multiple features are added in rapid succession
- **Multi-category**: classifier returns `categories` array (1-3) alongside primary `category`; category filter matches any; dashboard renders multiple category pills per card
- **JSON parsing**: classifier strips markdown code blocks from Claude responses before parsing

## TypeScript & Composite Projects

Every package extends `tsconfig.base.json` which sets `composite: true`. The root `tsconfig.json` lists all packages as project references. This means:

- **Always typecheck from the root** — run `pnpm run typecheck` (which runs `tsc --build --emitDeclarationOnly`). This builds the full dependency graph so that cross-package imports resolve correctly. Running `tsc` inside a single package will fail if its dependencies haven't been built yet.
- **`emitDeclarationOnly`** — we only emit `.d.ts` files during typecheck; actual JS bundling is handled by esbuild/tsx/vite...etc, not `tsc`.
- **Project references** — when package A depends on package B, A's `tsconfig.json` must list B in its `references` array. `tsc --build` uses this to determine build order and skip up-to-date packages.

## Root Scripts

- `pnpm run build` — runs `typecheck` first, then recursively runs `build` in all packages that define it
- `pnpm run typecheck` — runs `tsc --build --emitDeclarationOnly` using project references

## Packages

### `artifacts/api-server` (`@workspace/api-server`)

Express 5 API server. Routes live in `src/routes/` and use `@workspace/api-zod` for request and response validation and `@workspace/db` for persistence. Routed to `/api-server` path.

- Entry: `src/index.ts` — reads `PORT`, starts Express
- App setup: `src/app.ts` — mounts CORS, JSON/urlencoded parsing, routes at `/api`
- Routes: `src/routes/index.ts` mounts sub-routers; `src/routes/health.ts` exposes `GET /health` (full path: `/api/health`)
- Depends on: `@workspace/db`, `@workspace/api-zod`
- `pnpm --filter @workspace/api-server run dev` — run the dev server
- `pnpm --filter @workspace/api-server run build` — production esbuild bundle (`dist/index.cjs`)
- Build bundles an allowlist of deps (express, cors, pg, drizzle-orm, zod, etc.) and externalizes the rest

### `lib/db` (`@workspace/db`)

Database layer using Drizzle ORM with PostgreSQL. Exports a Drizzle client instance and schema models.

- `src/index.ts` — creates a `Pool` + Drizzle instance, exports schema
- `src/schema/index.ts` — barrel re-export of all models
- `src/schema/<modelname>.ts` — table definitions with `drizzle-zod` insert schemas (no models definitions exist right now)
- `drizzle.config.ts` — Drizzle Kit config (requires `DATABASE_URL`, automatically provided by Replit)
- Exports: `.` (pool, db, schema), `./schema` (schema only)

Production migrations are handled by Replit when publishing. In development, we just use `pnpm --filter @workspace/db run push`, and we fallback to `pnpm --filter @workspace/db run push-force`.

### `lib/api-spec` (`@workspace/api-spec`)

Owns the OpenAPI 3.1 spec (`openapi.yaml`) and the Orval config (`orval.config.ts`). Running codegen produces output into two sibling packages:

1. `lib/api-client-react/src/generated/` — React Query hooks + fetch client
2. `lib/api-zod/src/generated/` — Zod schemas

Run codegen: `pnpm --filter @workspace/api-spec run codegen`

### `lib/api-zod` (`@workspace/api-zod`)

Generated Zod schemas from the OpenAPI spec (e.g. `HealthCheckResponse`). Used by `api-server` for response validation.

### `lib/api-client-react` (`@workspace/api-client-react`)

Generated React Query hooks and fetch client from the OpenAPI spec (e.g. `useHealthCheck`, `healthCheck`).

### `scripts` (`@workspace/scripts`)

Utility scripts package. Each script is a `.ts` file in `src/` with a corresponding npm script in `package.json`. Run scripts via `pnpm --filter @workspace/scripts run <script>`. Scripts can import any workspace package (e.g., `@workspace/db`) by adding it as a dependency in `scripts/package.json`.
