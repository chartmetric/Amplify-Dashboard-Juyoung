// 경로: src/lib/upstreamClient.ts
import { logger } from "./logger";
import { getEnv } from "./env";

export interface UpstreamRequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  path: string;
  query?: Record<string, string | string[] | number | undefined>;
  body?: unknown;
  token?: string | null;
}

export interface UpstreamResponse<T> {
  status: number;
  headers: Headers;
  body: T;
}

export class UpstreamError extends Error {
  status: number;
  payload: unknown;
  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

let cachedServiceToken: string | null = null;
let inflightLogin: Promise<string> | null = null;

function buildQueryString(
  query: UpstreamRequestOptions["query"] | undefined,
): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        params.append(`${key}[]`, String(item));
      }
    } else {
      params.append(key, String(value));
    }
  }
  const search = params.toString();
  return search ? `?${search}` : "";
}

async function doFetch<T>(
  baseUrl: string,
  options: UpstreamRequestOptions,
): Promise<UpstreamResponse<T>> {
  const url = `${baseUrl}${options.path}${buildQueryString(options.query)}`;
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers: {
      Accept: "application/json",
    },
  };
  if (options.token) {
    init.headers = {
      ...init.headers,
      Cookie: options.token,
    };
  }
  if (options.body !== undefined) {
    init.headers = {
      ...init.headers,
      "Content-Type": "application/json",
    };
    init.body = JSON.stringify(options.body);
  }

  const res = await fetch(url, init);
  const contentType = res.headers.get("content-type") ?? "";
  let parsed: unknown = null;
  if (res.status !== 204) {
    if (contentType.includes("application/json")) {
      parsed = await res.json();
    } else {
      parsed = await res.text();
    }
  }

  return {
    status: res.status,
    headers: res.headers,
    body: parsed as T,
  };
}

export interface LoginPayload {
  username: string;
  password: string;
  code?: string;
}

export interface LoginResult {
  status: number;
  token: string | null;
  user: UpstreamUser | null;
  requiresCode: boolean;
  raw: unknown;
}

export interface UpstreamUser {
  id: number;
  email: string;
  name?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  is_paying?: boolean;
  plan_paid?: boolean | string;
  plan?: string | null;
  status?: string | null;
  [key: string]: unknown;
}

export async function upstreamLogin(
  payload: LoginPayload,
): Promise<LoginResult> {
  const env = getEnv();
  const response = await doFetch<{ token?: string; user?: UpstreamUser } | string>(
    env.authBaseUrl,
    {
      method: "POST",
      path: "/login",
      body: payload,
    },
  );

  if (response.status === 202) {
    return {
      status: 202,
      token: null,
      user: null,
      requiresCode: true,
      raw: response.body,
    };
  }

  if (response.status < 200 || response.status >= 300) {
    return {
      status: response.status,
      token: null,
      user: null,
      requiresCode: false,
      raw: response.body,
    };
  }

  // Chartmetric API uses cookie-based auth: session is in Set-Cookie, not body
  const setCookies = response.headers.getSetCookie?.() ?? [];
  const cookieHeader = setCookies
    .map((c) => c.split(";")[0].trim())
    .join("; ");

  const user =
    typeof response.body === "object" && response.body !== null
      ? (response.body as UpstreamUser)
      : null;

  return {
    status: response.status,
    token: cookieHeader || null,
    user,
    requiresCode: false,
    raw: response.body,
  };
}

async function getServiceAccountToken(force = false): Promise<string> {
  if (!force && cachedServiceToken) return cachedServiceToken;
  if (inflightLogin) return inflightLogin;

  const env = getEnv();
  if (!env.serviceAccountEmail || !env.serviceAccountPassword) {
    throw new UpstreamError(
      500,
      "Service-account credentials are not configured (CM_SERVICE_ACCOUNT_EMAIL / CM_SERVICE_ACCOUNT_PASSWORD).",
      null,
    );
  }

  inflightLogin = (async (): Promise<string> => {
    const result = await upstreamLogin({
      username: env.serviceAccountEmail!,
      password: env.serviceAccountPassword!,
    });
    if (!result.token) {
      throw new UpstreamError(
        result.status,
        "Failed to authenticate service account",
        result.raw,
      );
    }
    cachedServiceToken = result.token;
    return result.token;
  })().finally(() => {
    inflightLogin = null;
  });

  return inflightLogin;
}

function isAuthFailure(status: number): boolean {
  return status === 401 || status === 403;
}

export interface CallUpstreamOptions extends UpstreamRequestOptions {
  visitorToken?: string | null;
}

export async function callUpstream<T = unknown>(
  options: CallUpstreamOptions,
): Promise<UpstreamResponse<T>> {
  const env = getEnv();
  const token = options.visitorToken ?? (await getServiceAccountToken());

  const first = await doFetch<T>(env.apiBaseUrl, { ...options, token });

  if (!isAuthFailure(first.status)) return first;

  if (options.visitorToken) {
    return first;
  }

  logger.warn(
    { status: first.status, path: options.path },
    "Upstream auth failure; refreshing service-account token",
  );
  cachedServiceToken = null;
  const refreshed = await getServiceAccountToken(true);
  return doFetch<T>(env.apiBaseUrl, { ...options, token: refreshed });
}

export function clearServiceAccountTokenCache(): void {
  cachedServiceToken = null;
}
