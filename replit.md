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
- **Python packages**: flask, anthropic, asana, slack-sdk, requests, python-dotenv, waitress
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
- **Architecture (Slack-first pipeline)**:
  - `sources/base.py` — FeatureContext dataclass + SourceAdapter ABC
  - `sources/slack_source.py` — SlackSource: extracts features from #product-updates release messages as bullets with stable IDs (slack-{ts}-{idx}), parses Slack link format `<URL|text>`, extracts prefixes (PE/Devin/FE/BE), release versions, reactions, thread URLs
  - `sources/asana_source.py` — AsanaSource: enrichment-only via `enrich_feature()` (3-tier: URL match → title search → no match); `list_unannounced_tasks()` for Asana-only features; workspace GID=1198197264916217
  - `sources/manual_source.py` — ManualSource (stateless, returns FeatureContext from kwargs)
  - `ai/generator.py` — Content generation across 7 channels (twitter, email_newsletter, email_standalone, inapp, linkedin, notion_monthly, article_hmc)
  - `ai/classifier.py` — Claude-powered feature classification with multi-category support
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
