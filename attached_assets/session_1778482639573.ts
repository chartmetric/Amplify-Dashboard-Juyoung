import type { Request, RequestHandler, Response } from "express";

import { getEnv } from "../lib/env";
import type { UpstreamUser } from "../lib/upstreamClient";

const COOKIE_NAME = "cm_session";
const USER_COOKIE = "cm_user";

export interface VisitorSession {
  token: string;
  user: UpstreamUser;
}

export function getVisitorToken(req: Request): string | null {
  const token = req.cookies?.[COOKIE_NAME];
  return typeof token === "string" && token.length > 0 ? token : null;
}

export function getVisitorUser(req: Request): UpstreamUser | null {
  const raw = req.cookies?.[USER_COOKIE];
  if (typeof raw !== "string" || raw.length === 0) return null;
  try {
    return JSON.parse(Buffer.from(raw, "base64url").toString("utf8")) as UpstreamUser;
  } catch {
    return null;
  }
}

export function setVisitorSession(
  res: Response,
  session: VisitorSession,
): void {
  const env = getEnv();
  const sevenDays = 7 * 24 * 60 * 60 * 1000;

  res.cookie(COOKIE_NAME, session.token, {
    httpOnly: true,
    secure: env.cookieSecure,
    sameSite: "lax",
    maxAge: sevenDays,
    ...(env.cookieDomain ? { domain: env.cookieDomain } : {}),
    path: "/",
  });

  const encoded = Buffer.from(JSON.stringify(session.user), "utf8").toString(
    "base64url",
  );
  res.cookie(USER_COOKIE, encoded, {
    httpOnly: false,
    secure: env.cookieSecure,
    sameSite: "lax",
    maxAge: sevenDays,
    ...(env.cookieDomain ? { domain: env.cookieDomain } : {}),
    path: "/",
  });
}

export function clearVisitorSession(res: Response): void {
  const env = getEnv();
  const opts = {
    httpOnly: true,
    secure: env.cookieSecure,
    sameSite: "lax" as const,
    ...(env.cookieDomain ? { domain: env.cookieDomain } : {}),
    path: "/",
  };
  res.clearCookie(COOKIE_NAME, opts);
  res.clearCookie(USER_COOKIE, { ...opts, httpOnly: false });
}

export const attachSession: RequestHandler = (_req, _res, next) => {
  next();
};
