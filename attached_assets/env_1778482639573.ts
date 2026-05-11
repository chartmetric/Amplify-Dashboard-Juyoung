export interface AppEnv {
  apiBaseUrl: string;
  authBaseUrl: string;
  serviceAccountEmail: string | null;
  serviceAccountPassword: string | null;
  cookieDomain: string | null;
  cookieSecure: boolean;
  spaDir: string | null;
  isProduction: boolean;
}

function readEnv(name: string): string | null {
  const value = process.env[name];
  if (!value || value.trim().length === 0) return null;
  return value;
}

function required(name: string): string {
  const value = readEnv(name);
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

let cached: AppEnv | null = null;

export function getEnv(): AppEnv {
  if (cached) return cached;
  const apiBaseUrl = required("CM_API_BASE_URL").replace(/\/$/, "");
  const authBaseUrl =
    (readEnv("CM_AUTH_BASE_URL") ?? apiBaseUrl).replace(/\/$/, "");

  cached = {
    apiBaseUrl,
    authBaseUrl,
    serviceAccountEmail: readEnv("CM_SERVICE_ACCOUNT_EMAIL"),
    serviceAccountPassword: readEnv("CM_SERVICE_ACCOUNT_PASSWORD"),
    cookieDomain: readEnv("COOKIE_DOMAIN"),
    cookieSecure: (readEnv("COOKIE_SECURE") ?? "true") !== "false",
    spaDir: readEnv("SPA_DIR"),
    isProduction: (readEnv("NODE_ENV") ?? "production") === "production",
  };
  return cached;
}
