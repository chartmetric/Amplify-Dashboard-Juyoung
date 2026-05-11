# Chartmetric API Proxy — Implementation Guide

How to wrap the Chartmetric production API in an Express proxy server, handling
service-account token caching, visitor session cookies, and route forwarding.
The patterns here are extracted from `artifacts/api-server` in this repo.

---

## Overview

The proxy sits between your frontend and the upstream Chartmetric API
(`api.chartmetric.com`). It solves three problems:

1. **Anonymous reads** — a service account logs in once; its token is cached
   in memory and reused for every unauthenticated request.
2. **Visitor auth** — when a user signs in, their upstream session token is
   stored in an `HttpOnly` cookie on your domain, not exposed to the browser.
3. **CORS / credential safety** — API keys and credentials never reach the
   frontend.

```
Browser  ──→  your proxy (/api/*)  ──→  api.chartmetric.com
                   │
                   ├─ unauthenticated request? → use cached service-account token
                   └─ signed-in visitor?        → use token from HttpOnly cookie
```

---

## Directory structure

```
src/
  app.ts                 Express app setup (CORS, middleware, SPA fallback)
  index.ts               Entry point — reads PORT, calls app.listen()
  lib/
    env.ts               Typed env-var reader
    logger.ts            Pino logger (pretty in dev, JSON in prod)
    upstreamClient.ts    Core: doFetch, service-account token cache, callUpstream
  middlewares/
    session.ts           Cookie helpers: read/write/clear visitor session
  routes/
    index.ts             Mounts sub-routers under /api
    health.ts            GET /api/health
    auth/index.ts        POST /api/auth/login, GET /api/auth/me, POST /api/auth/logout
    your-feature/        Add one sub-router per feature domain
      index.ts
```

---

## Key files

### `lib/env.ts`

Read env vars once, cache the result. Only throw on vars that are truly
required at startup.

```ts
export interface AppEnv {
  apiBaseUrl: string;
  authBaseUrl: string;
  serviceAccountEmail: string | null;
  serviceAccountPassword: string | null;
  cookieDomain: string | null;
  cookieSecure: boolean;
  spaDir: string | null;
}

function readEnv(name: string): string | null {
  const value = process.env[name];
  if (!value || value.trim().length === 0) return null;
  return value;
}

function required(name: string): string {
  const value = readEnv(name);
  if (!value) throw new Error(`Missing required env var: ${name}`);
  return value;
}

let cached: AppEnv | null = null;

export function getEnv(): AppEnv {
  if (cached) return cached;
  const apiBaseUrl = required("CM_API_BASE_URL").replace(/\/$/, "");
  cached = {
    apiBaseUrl,
    authBaseUrl: (readEnv("CM_AUTH_BASE_URL") ?? apiBaseUrl).replace(/\/$/, ""),
    serviceAccountEmail: readEnv("CM_SERVICE_ACCOUNT_EMAIL"),
    serviceAccountPassword: readEnv("CM_SERVICE_ACCOUNT_PASSWORD"),
    cookieDomain: readEnv("COOKIE_DOMAIN"),
    cookieSecure: (readEnv("COOKIE_SECURE") ?? "true") !== "false",
    spaDir: readEnv("SPA_DIR"),
  };
  return cached;
}
```

**Rules:**
- Only `CM_API_BASE_URL` is `required()`. Everything else is nullable/defaulted.
- Strip trailing slashes from base URLs so path concatenation stays clean.
- The cache means `getEnv()` is safe to call anywhere without performance concern.

---

### `lib/upstreamClient.ts`

The heart of the proxy. Three layers:

1. **`doFetch`** — raw HTTP call, handles JSON vs text response, builds query string.
2. **`upstreamLogin`** — exchanges credentials for a session cookie token.
3. **`callUpstream`** — public API. Uses visitor token if present, otherwise
   falls back to cached service-account token with auto-refresh on 401/403.

```ts
// --- Types ---

export interface UpstreamRequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  path: string;
  query?: Record<string, string | string[] | number | undefined>;
  body?: unknown;
  token?: string | null;
}

export class UpstreamError extends Error {
  constructor(
    public status: number,
    message: string,
    public payload: unknown,
  ) {
    super(message);
  }
}

// --- Service-account token cache ---
// Module-level; survives across requests. One in-flight login at a time.

let cachedServiceToken: string | null = null;
let inflightLogin: Promise<string> | null = null;

async function getServiceAccountToken(force = false): Promise<string> {
  if (!force && cachedServiceToken) return cachedServiceToken;
  if (inflightLogin) return inflightLogin;          // deduplicate concurrent callers

  const env = getEnv();
  if (!env.serviceAccountEmail || !env.serviceAccountPassword) {
    throw new UpstreamError(500, "Service-account credentials not configured", null);
  }

  inflightLogin = (async () => {
    const result = await upstreamLogin({
      username: env.serviceAccountEmail!,
      password: env.serviceAccountPassword!,
    });
    if (!result.token) {
      throw new UpstreamError(result.status, "Service-account login failed", result.raw);
    }
    cachedServiceToken = result.token;
    return result.token;
  })().finally(() => { inflightLogin = null; });

  return inflightLogin;
}

// --- Main entry point for route handlers ---

export interface CallUpstreamOptions extends UpstreamRequestOptions {
  visitorToken?: string | null;
}

export async function callUpstream<T = unknown>(
  options: CallUpstreamOptions,
): Promise<{ status: number; body: T }> {
  const env = getEnv();
  const token = options.visitorToken ?? (await getServiceAccountToken());

  const first = await doFetch<T>(env.apiBaseUrl, { ...options, token });

  // If the visitor's own token expired, return the failure directly.
  // Only auto-refresh for the service account.
  if (first.status !== 401 && first.status !== 403) return first;
  if (options.visitorToken) return first;

  logger.warn({ status: first.status, path: options.path }, "Refreshing service-account token");
  cachedServiceToken = null;
  const refreshed = await getServiceAccountToken(true);
  return doFetch<T>(env.apiBaseUrl, { ...options, token: refreshed });
}
```

**Key design decisions:**
- The service-account token is just the raw `Cookie` header string extracted
  from the upstream `Set-Cookie` response (e.g. `session=abc123`). It is
  forwarded as-is on subsequent requests.
- `inflightLogin` prevents a thundering herd: if 50 concurrent requests all
  find the cache empty, only one login call goes out.
- `force = true` bypasses the cache entirely for the refresh path.

---

### `middlewares/session.ts`

Two cookies per visitor session:

| Cookie | `HttpOnly` | Purpose |
|---|---|---|
| `cm_session` | yes | Upstream token — never readable by JS |
| `cm_user` | no | Base64url-encoded user JSON — read by the SPA to show username etc. |

```ts
const COOKIE_NAME = "cm_session";
const USER_COOKIE = "cm_user";

export function getVisitorToken(req: Request): string | null {
  const token = req.cookies?.[COOKIE_NAME];
  return typeof token === "string" && token.length > 0 ? token : null;
}

export function getVisitorUser(req: Request): UpstreamUser | null {
  const raw = req.cookies?.[USER_COOKIE];
  if (!raw) return null;
  try {
    return JSON.parse(Buffer.from(raw, "base64url").toString("utf8"));
  } catch {
    return null;
  }
}

export function setVisitorSession(res: Response, session: { token: string; user: UpstreamUser }): void {
  const env = getEnv();
  const sevenDays = 7 * 24 * 60 * 60 * 1000;
  const base = {
    secure: env.cookieSecure,
    sameSite: "lax" as const,
    maxAge: sevenDays,
    ...(env.cookieDomain ? { domain: env.cookieDomain } : {}),
    path: "/",
  };

  res.cookie(COOKIE_NAME, session.token, { ...base, httpOnly: true });
  res.cookie(
    USER_COOKIE,
    Buffer.from(JSON.stringify(session.user), "utf8").toString("base64url"),
    { ...base, httpOnly: false },
  );
}

export function clearVisitorSession(res: Response): void {
  const env = getEnv();
  const base = {
    secure: env.cookieSecure,
    sameSite: "lax" as const,
    ...(env.cookieDomain ? { domain: env.cookieDomain } : {}),
    path: "/",
  };
  res.clearCookie(COOKIE_NAME, { ...base, httpOnly: true });
  res.clearCookie(USER_COOKIE, { ...base, httpOnly: false });
}
```

**Important:** `sameSite: "lax"` is required for cross-subdomain setups (e.g.
browser on `app.chartmetric.com`, proxy on `update.chartmetric.com`). With
`COOKIE_DOMAIN=.chartmetric.com`, the cookie is shared across all subdomains.

---

### Route pattern — `forward()` helper

Every route handler follows the same shape. Extract it into a local helper to
avoid repetition:

```ts
import { Router, type Request, type Response } from "express";
import { callUpstream, UpstreamError } from "../../lib/upstreamClient";
import { getVisitorToken } from "../../middlewares/session";

const router = Router();

async function forward(
  req: Request,
  res: Response,
  options: Omit<CallUpstreamOptions, "visitorToken">,
): Promise<void> {
  try {
    const upstream = await callUpstream({ ...options, visitorToken: getVisitorToken(req) });
    if (upstream.status === 204) { res.status(204).end(); return; }
    res.status(upstream.status).json(upstream.body);
  } catch (err) {
    if (err instanceof UpstreamError) {
      res.status(err.status >= 400 ? err.status : 502).json({ message: err.message });
      return;
    }
    res.status(502).json({ message: "Upstream request failed" });
  }
}

// Example routes:
router.get("/list", (req, res) =>
  forward(req, res, { method: "GET", path: "/your-feature/list", query: { ... } })
);

router.post("/:id/action", (req, res) =>
  forward(req, res, { method: "POST", path: `/your-feature/${req.params.id}/action`, body: req.body })
);

export default router;
```

**Rules:**
- Always `encodeURIComponent(req.params.id)` when interpolating params into the
  upstream path — never trust user-supplied strings in URLs.
- Pass `visitorToken: getVisitorToken(req)` on every call. `callUpstream` falls
  back to the service account automatically when it's null.
- Return 502 (not 500) for upstream failures — it signals that *your* server is
  fine but the upstream is the problem.

---

### `app.ts`

```ts
import express from "express";
import cors from "cors";
import cookieParser from "cookie-parser";
import pinoHttp from "pino-http";
import router from "./routes";

const app = express();

app.use(pinoHttp({ logger, serializers: { /* strip cookies from logs */ } }));
app.use(cors({ origin: (origin, cb) => cb(null, origin ?? true), credentials: true }));
app.use(cookieParser());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

// Optional: serve the SPA from the same process in production
const spaDir = process.env.SPA_DIR;
if (spaDir && fs.existsSync(spaDir)) {
  app.use(express.static(spaDir, { index: false }));
  app.get(/^\/(?!api\/).*/, (_req, res) => res.sendFile(path.join(spaDir, "index.html")));
}

export default app;
```

**CORS note:** `origin: (origin, cb) => cb(null, origin ?? true)` mirrors back
whatever origin the browser sends. This is fine when `credentials: true` is
required — you cannot use `"*"` with credentials. Tighten this to an allowlist
in production if needed.

---

### `lib/logger.ts`

```ts
import pino from "pino";

export const logger = pino({
  level: process.env.LOG_LEVEL ?? "info",
  redact: [
    "req.headers.authorization",
    "req.headers.cookie",         // never log visitor tokens
    "res.headers['set-cookie']",
  ],
  ...(process.env.NODE_ENV !== "production"
    ? { transport: { target: "pino-pretty", options: { colorize: true } } }
    : {}),
});
```

The `redact` list is important — without it, pino-http will log the full
`Cookie` header on every request, leaking visitor session tokens.

---

## Environment variables

| Name | Required | Default | Notes |
|---|---|---|---|
| `CM_API_BASE_URL` | **yes** | — | e.g. `https://api.chartmetric.com` |
| `PORT` | **yes** | — | Required by `index.ts` at startup |
| `CM_SERVICE_ACCOUNT_EMAIL` | no | null | Anonymous reads disabled if unset |
| `CM_SERVICE_ACCOUNT_PASSWORD` | no | null | |
| `CM_AUTH_BASE_URL` | no | `CM_API_BASE_URL` | Override if auth lives on a different host |
| `COOKIE_DOMAIN` | no | null | Set to `.chartmetric.com` for cross-subdomain sharing |
| `COOKIE_SECURE` | no | `"true"` | Set to `"false"` for local http dev |
| `SPA_DIR` | no | null | Absolute path to built SPA; enables SPA serving |
| `NODE_ENV` | no | `"production"` | Set to `"development"` for pino-pretty logs |

---

## Auth flow

```
Visitor login:
  POST /api/auth/login { email, password }
    → proxy calls upstream POST /login
    → upstream responds with Set-Cookie (session token)
    → proxy extracts cookie string, stores it in HttpOnly cm_session cookie
    → proxy returns { user: { id, email, name, isPaying } } to browser

Subsequent requests:
  Browser sends cm_session cookie automatically
    → proxy reads it with getVisitorToken(req)
    → forwards it to upstream as Cookie header

Logout:
  POST /api/auth/logout
    → proxy clears cm_session and cm_user cookies
```

The browser never sees the raw upstream session token. All auth state lives in
cookies, not localStorage — this prevents XSS token theft.

---

## Checklist for a new app

- [ ] Copy `lib/env.ts`, `lib/upstreamClient.ts`, `lib/logger.ts`, `middlewares/session.ts`
- [ ] Add your feature sub-router under `routes/your-feature/index.ts`
- [ ] Register it in `routes/index.ts`
- [ ] Use the `forward()` helper for all route handlers
- [ ] Set `CM_API_BASE_URL` and `PORT` before starting
- [ ] Set `COOKIE_SECURE=false` for local dev if running on http
- [ ] Verify `redact` in logger covers all sensitive headers
