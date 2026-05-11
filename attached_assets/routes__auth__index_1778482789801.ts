// 경로: src/routes/auth/index.ts
import { Router, type IRouter } from "express";

import { logger } from "../../lib/logger";
import { upstreamLogin, type UpstreamUser } from "../../lib/upstreamClient";
import {
  clearVisitorSession,
  getVisitorUser,
  setVisitorSession,
} from "../../middlewares/session";

const authRouter: IRouter = Router();

function toPublicUser(upstream: UpstreamUser): {
  id: number;
  email: string;
  name: string | null;
  isPaying: boolean;
  isChartmetricEmail: boolean;
} {
  const name =
    upstream.name ??
    [upstream.first_name, upstream.last_name]
      .filter((s): s is string => typeof s === "string" && s.length > 0)
      .join(" ") ??
    null;

  const planPaid = upstream.plan_paid;
  const isPaying =
    upstream.is_paying === true ||
    planPaid === true ||
    (typeof planPaid === "string" && planPaid.toLowerCase() === "true");

  const email = upstream.email ?? "";
  const isChartmetricEmail = email.toLowerCase().endsWith("@chartmetric.com");

  return {
    id: Number(upstream.id),
    email,
    name: name && name.length > 0 ? name : null,
    isPaying,
    isChartmetricEmail,
  };
}

authRouter.post("/login", async (req, res) => {
  const { email, password, code } = req.body as {
    email?: string;
    password?: string;
    code?: string;
  };
  if (!email || !password) {
    res.status(400).json({ message: "email and password are required" });
    return;
  }
  try {
    const result = await upstreamLogin({ username: email, password, code });
    if (result.requiresCode) {
      res.status(200).json({ user: null, requiresCode: true });
      return;
    }
    if (!result.token || !result.user) {
      res.status(result.status >= 400 ? result.status : 401).json({
        user: null,
        requiresCode: false,
        message: "Invalid credentials",
      });
      return;
    }
    setVisitorSession(res, { token: result.token, user: result.user });
    res.status(200).json({
      user: toPublicUser(result.user),
      requiresCode: false,
    });
  } catch (err) {
    logger.error({ err }, "Login request failed");
    res.status(500).json({ message: "Login failed" });
  }
});

authRouter.get("/me", (req, res) => {
  const user = getVisitorUser(req);
  if (!user) {
    res.status(401).json({ message: "Not authenticated" });
    return;
  }
  res.status(200).json(toPublicUser(user));
});

authRouter.post("/logout", (_req, res) => {
  clearVisitorSession(res);
  res.status(204).end();
});

export default authRouter;
